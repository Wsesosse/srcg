"""Crash-safe local checkpoint storage.

The training process owns PyTorch serialization.  SRCG only decides when a
safe point may save and whether a failed process may be restarted.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any, Callable
import uuid

import torch


@dataclass(frozen=True)
class CheckpointRecord:
    path: Path
    step: int
    size_bytes: int
    sha256: str
    elapsed_ms: float


class AtomicCheckpointStore:
    """Write a checkpoint and its latest-manifest using atomic renames."""

    manifest_version = 1

    def __init__(self, directory: Path | str, keep: int = 2) -> None:
        if keep < 1:
            raise ValueError("checkpoint retention must be at least one")
        self.directory = Path(directory)
        self.keep = keep

    @property
    def manifest_path(self) -> Path:
        return self.directory / "latest.json"

    def save(
        self,
        payload: dict[str, Any],
        step: int,
        *,
        serializer: Callable[[dict[str, Any], Any], None] = torch.save,
    ) -> CheckpointRecord:
        if step < 0:
            raise ValueError("checkpoint step cannot be negative")
        self.directory.mkdir(parents=True, exist_ok=True)
        suffix = f"{os.getpid()}-{uuid.uuid4().hex}"
        # A unique final name keeps the checkpoint referenced by the old
        # manifest intact until the new manifest is committed.
        final_path = self.directory / f"checkpoint-step-{step:012d}-{uuid.uuid4().hex}.pt"
        temporary_path = self.directory / f".{final_path.name}.{suffix}.tmp"
        manifest_temporary = self.directory / f".latest.{suffix}.tmp"
        started = time.perf_counter()
        checkpoint_renamed = False
        manifest_committed = False
        try:
            with temporary_path.open("xb") as stream:
                serializer(payload, stream)
                stream.flush()
                os.fsync(stream.fileno())

            digest = self._sha256(temporary_path)
            size_bytes = temporary_path.stat().st_size
            os.replace(temporary_path, final_path)
            checkpoint_renamed = True
            self._fsync_directory()

            manifest = {
                "version": self.manifest_version,
                "checkpoint": final_path.name,
                "step": step,
                "size_bytes": size_bytes,
                "sha256": digest,
                "created_unix_ns": time.time_ns(),
            }
            with manifest_temporary.open("x", encoding="utf-8") as stream:
                json.dump(manifest, stream, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(manifest_temporary, self.manifest_path)
            manifest_committed = True
            self._fsync_directory()
            self._remove_expired(final_path)
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            manifest_temporary.unlink(missing_ok=True)
            if checkpoint_renamed and not manifest_committed:
                final_path.unlink(missing_ok=True)
            raise

        return CheckpointRecord(
            path=final_path,
            step=step,
            size_bytes=size_bytes,
            sha256=digest,
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
        )

    def load(self, source: str | Path = "auto", *, map_location: Any = "cpu") -> tuple[dict[str, Any], CheckpointRecord]:
        started = time.perf_counter()
        if str(source) == "auto":
            manifest = self._read_manifest()
            path = self.directory / manifest["checkpoint"]
            expected_digest = manifest["sha256"]
            expected_size = int(manifest["size_bytes"])
            step = int(manifest["step"])
        else:
            path = Path(source)
            if not path.is_absolute():
                path = self.directory / path
            expected_digest = None
            expected_size = path.stat().st_size
            step = -1

        actual_size = path.stat().st_size
        if actual_size != expected_size:
            raise RuntimeError(
                f"checkpoint size mismatch for {path}: expected {expected_size}, got {actual_size}"
            )
        digest = self._sha256(path)
        if expected_digest is not None and digest != expected_digest:
            raise RuntimeError(f"checkpoint checksum mismatch for {path}")
        with path.open("rb") as stream:
            payload = torch.load(stream, map_location=map_location, weights_only=False)
        if not isinstance(payload, dict):
            raise RuntimeError(f"invalid checkpoint payload in {path}")
        if step < 0:
            step = int(payload.get("global_step", -1))
        return payload, CheckpointRecord(
            path=path,
            step=step,
            size_bytes=actual_size,
            sha256=digest,
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
        )

    def has_complete_checkpoint(self) -> bool:
        try:
            manifest = self._read_manifest()
            path = self.directory / manifest["checkpoint"]
            return (
                path.is_file()
                and path.stat().st_size == int(manifest["size_bytes"])
                and self._sha256(path) == manifest["sha256"]
            )
        except (OSError, RuntimeError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            return False

    def _read_manifest(self) -> dict[str, Any]:
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        if manifest.get("version") != self.manifest_version:
            raise RuntimeError("unsupported checkpoint manifest version")
        checkpoint = manifest.get("checkpoint")
        if not isinstance(checkpoint, str) or Path(checkpoint).name != checkpoint:
            raise RuntimeError("invalid checkpoint path in manifest")
        return manifest

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def _fsync_directory(self) -> None:
        descriptor = os.open(self.directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _remove_expired(self, current: Path) -> None:
        checkpoints = sorted(
            self.directory.glob("checkpoint-step-*.pt"),
            key=lambda path: (path.stat().st_mtime_ns, path.name),
            reverse=True,
        )
        retained = {current, *checkpoints[: self.keep]}
        for path in checkpoints:
            if path not in retained:
                path.unlink(missing_ok=True)
