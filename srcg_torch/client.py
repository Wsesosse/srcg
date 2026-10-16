"""Small synchronous client for SRCG Reserved Memory IPC."""

from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
import os
from pathlib import Path
import socket
import time
import uuid
from typing import Iterator


@dataclass(frozen=True)
class ReservationToken:
    value: str
    # Workload-side estimate sent to the manager.
    requested_bytes: int
    # Capacity actually added to memory.max and charged to Reserved Memory.
    borrowed_bytes: int
    effective_limit_bytes: int
    wait_ms: float
    wait_count: int
    admission_ms: float


@dataclass(frozen=True)
class ReleaseResult:
    decision: str
    effective_limit_bytes: int
    reserved_memory_available_bytes: int
    reason: str


@dataclass(frozen=True)
class ShrinkResult:
    decision: str
    previous_borrowed_bytes: int
    borrowed_bytes: int
    returned_bytes: int
    effective_limit_bytes: int
    reserved_memory_available_bytes: int
    reason: str


class ReservationTimeout(RuntimeError):
    pass


@dataclass(frozen=True)
class CheckpointToken:
    value: str
    wait_ms: float



class ReservationClient:
    """Reserve before load, shrink after preprocessing, and release after H2D.

    This client never interprets WAIT as permission to load.  It retries until
    the timeout expires, then raises before ``Dataset.__getitem__`` is called.
    """

    def __init__(
        self,
        socket_path: Path | str,
        job_id: str,
        *,
        timeout_ms: int = 0,
        retry_ms: int = 50,
        release_safety_bytes: int = 64 * 1024 * 1024,
        admission_safety_bytes: int | None = None,
    ) -> None:
        if admission_safety_bytes is None:
            admission_safety_bytes = release_safety_bytes
        if (
            timeout_ms < 0
            or retry_ms <= 0
            or release_safety_bytes < 0
            or admission_safety_bytes < 0
        ):
            raise ValueError("invalid reservation timing or safety configuration")
        self.socket_path = Path(socket_path)
        self.job_id = job_id
        self.timeout_ms = timeout_ms
        self.retry_ms = retry_ms
        self.release_safety_bytes = release_safety_bytes
        self.admission_safety_bytes = admission_safety_bytes

    @classmethod
    def from_environment(
        cls,
        *,
        timeout_ms: int = 0,
        retry_ms: int = 50,
        release_safety_bytes: int = 64 * 1024 * 1024,
        admission_safety_bytes: int | None = None,
    ) -> "ReservationClient | None":
        socket_path = os.environ.get("SRCG_MANAGER_SOCKET")
        job_id = os.environ.get("SRCG_JOB_ID")
        if not socket_path and not job_id:
            return None
        if not socket_path or not job_id:
            raise RuntimeError("incomplete SRCG_MANAGER_SOCKET/SRCG_JOB_ID environment")
        return cls(
            socket_path,
            job_id,
            timeout_ms=timeout_ms,
            retry_ms=retry_ms,
            release_safety_bytes=release_safety_bytes,
            admission_safety_bytes=admission_safety_bytes,
        )

    def reserve(self, additional_bytes: int) -> ReservationToken:
        if additional_bytes <= 0:
            raise ValueError("reservation size must be positive")
        request_id = uuid.uuid4().hex
        started = time.perf_counter()
        deadline = (
            None if self.timeout_ms == 0 else started + self.timeout_ms / 1000.0
        )
        wait_count = 0
        wait_seconds = 0.0
        while True:
            fields = self._request(
                f"RESERVE job={self.job_id} request={request_id} "
                f"bytes={additional_bytes} safety={self.admission_safety_bytes}"
            )
            decision = fields.pop("_decision")
            if decision == "GRANTED":
                return ReservationToken(
                    value=fields["token"],
                    requested_bytes=additional_bytes,
                    borrowed_bytes=int(fields["borrowed_bytes"]),
                    effective_limit_bytes=int(fields["effective_limit"]),
                    wait_ms=wait_seconds * 1000.0,
                    wait_count=wait_count,
                    admission_ms=(time.perf_counter() - started) * 1000.0,
                )
            if decision == "DENIED":
                raise RuntimeError(f"SRCG reservation denied: {fields.get('reason', 'unknown')}")
            if decision != "WAIT":
                raise RuntimeError(f"unexpected SRCG reservation response: {decision}")
            wait_count += 1
            if deadline is not None and time.perf_counter() >= deadline:
                self._cancel_pending(request_id)
                raise ReservationTimeout(
                    f"SRCG reservation timed out before load: {fields.get('reason', 'unknown')}"
                )
            sleep_seconds = self.retry_ms / 1000.0
            if deadline is not None:
                sleep_seconds = min(
                    sleep_seconds,
                    max(0.0, deadline - time.perf_counter()),
                )
            slept_at = time.perf_counter()
            time.sleep(sleep_seconds)
            wait_seconds += time.perf_counter() - slept_at

    def begin_checkpoint(self) -> CheckpointToken:
        request_id = uuid.uuid4().hex
        started = time.perf_counter()
        deadline = (
            None if self.timeout_ms == 0 else started + self.timeout_ms / 1000.0
        )
        while True:
            fields = self._request(
                f"CHECKPOINT_BEGIN job={self.job_id} request={request_id} "
                f"safety={self.admission_safety_bytes}"
            )
            decision = fields.pop("_decision")
            if decision == "GRANTED":
                return CheckpointToken(
                    value=fields["token"],
                    wait_ms=(time.perf_counter() - started) * 1000.0,
                )
            if decision == "DENIED":
                raise RuntimeError(
                    "SRCG checkpoint admission denied: "
                    + fields.get("reason", "unknown")
                )
            if decision != "WAIT":
                raise RuntimeError(f"unexpected checkpoint response: {decision}")
            if deadline is not None and time.perf_counter() >= deadline:
                raise ReservationTimeout(
                    "SRCG checkpoint admission timed out: "
                    + fields.get("reason", "unknown")
                )
            time.sleep(self.retry_ms / 1000.0)

    def end_checkpoint(self, token: CheckpointToken) -> str:
        fields = self._request(
            f"CHECKPOINT_END job={self.job_id} token={token.value} "
            f"safety={self.release_safety_bytes}"
        )
        decision = fields.pop("_decision")
        if decision not in {"RELEASED", "PENDING", "ALREADY_RELEASED"}:
            raise RuntimeError(
                "SRCG checkpoint release failed: " + fields.get("reason", "unknown")
            )
        return decision

    @contextmanager
    def checkpoint(self) -> Iterator[CheckpointToken]:
        token = self.begin_checkpoint()
        try:
            yield token
        finally:
            self.end_checkpoint(token)

    def _cancel_pending(self, request_id: str) -> None:
        """Best-effort cleanup; the original timeout remains the user error."""
        try:
            self._request(f"CANCEL job={self.job_id} request={request_id}")
        except (OSError, RuntimeError):
            pass

    def release(self, token: ReservationToken) -> ReleaseResult:
        fields = self._request(
            f"RELEASE job={self.job_id} token={token.value} safety={self.release_safety_bytes}"
        )
        return ReleaseResult(
            decision=fields.pop("_decision"),
            effective_limit_bytes=int(fields.get("effective_limit", 0)),
            reserved_memory_available_bytes=int(fields.get("reserved_memory_available", 0)),
            reason=fields.get("reason", "unknown"),
        )

    def shrink(self, token: ReservationToken, remaining_bytes: int) -> ShrinkResult:
        if remaining_bytes < 0:
            raise ValueError("remaining reservation size cannot be negative")
        fields = self._request(
            f"SHRINK job={self.job_id} token={token.value} "
            f"remaining={remaining_bytes} safety={self.release_safety_bytes}"
        )
        return ShrinkResult(
            decision=fields.pop("_decision"),
            previous_borrowed_bytes=int(fields.get("previous_borrowed_bytes", 0)),
            borrowed_bytes=int(fields.get("borrowed_bytes", 0)),
            returned_bytes=int(fields.get("returned_bytes", 0)),
            effective_limit_bytes=int(fields.get("effective_limit", 0)),
            reserved_memory_available_bytes=int(
                fields.get("reserved_memory_available", 0)
            ),
            reason=fields.get("reason", "unknown"),
        )

    def retry_pending(self) -> tuple[int, int]:
        fields = self._request(
            f"RETRY job={self.job_id} safety={self.release_safety_bytes}"
        )
        if fields.pop("_decision") != "OK":
            raise RuntimeError("SRCG pending-release retry failed")
        return int(fields["released"]), int(fields["pending"])

    def _request(self, message: str) -> dict[str, str]:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(max(1.0, self.timeout_ms / 1000.0 + 1.0))
            connection.connect(str(self.socket_path))
            connection.sendall(message.encode("utf-8") + b"\n")
            chunks: list[bytes] = []
            size = 0
            while True:
                block = connection.recv(512)
                if not block:
                    break
                chunks.append(block)
                size += len(block)
                if b"\n" in block:
                    break
                if size > 8192:
                    raise RuntimeError("SRCG response exceeds 8192 bytes")
        line = b"".join(chunks).split(b"\n", 1)[0].decode("utf-8")
        parts = line.split()
        if not parts:
            raise RuntimeError("empty SRCG response")
        fields = {"_decision": parts[0]}
        for item in parts[1:]:
            if "=" in item:
                key, value = item.split("=", 1)
                fields[key] = value
        if parts[0] == "ERROR":
            raise RuntimeError(f"SRCG protocol error: {fields.get('reason', 'unknown')}")
        return fields
