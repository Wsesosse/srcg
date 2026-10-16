from __future__ import annotations

from pathlib import Path
import socket
import tempfile
import threading
import unittest

from srcg_torch.client import ReservationClient, ReservationTimeout


class ScriptedServer:
    def __init__(self, path: Path, responses: list[str]) -> None:
        self.path = path
        self.responses = responses
        self.requests: list[str] = []
        self.ready = threading.Event()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run)

    def __enter__(self) -> "ScriptedServer":
        self.thread.start()
        self.ready.wait(timeout=2)
        return self

    def __exit__(self, *_args: object) -> None:
        self.stop.set()
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.connect(str(self.path))
                connection.sendall(b"STOP\n")
        except OSError:
            pass
        self.thread.join(timeout=2)

    def _run(self) -> None:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(self.path))
            listener.listen()
            self.ready.set()
            index = 0
            while not self.stop.is_set():
                connection, _ = listener.accept()
                with connection:
                    request = b""
                    while b"\n" not in request:
                        request += connection.recv(512)
                    if self.stop.is_set():
                        break
                    self.requests.append(request.split(b"\n", 1)[0].decode())
                    response = self.responses[min(index, len(self.responses) - 1)]
                    index += 1
                    connection.sendall(response.encode() + b"\n")


class ReservationClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "probe.sock"
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                    probe.bind(str(path))
            except PermissionError as exc:
                raise unittest.SkipTest("sandbox denies AF_UNIX bind") from exc

    def test_wait_reuses_request_id_then_grants(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manager.sock"
            with ScriptedServer(
                path,
                [
                    "WAIT effective_limit=100 reserved_memory_available=0 reason=reserved_memory_exhausted",
                    "GRANTED effective_limit=150 borrowed_bytes=25 reserved_memory_available=50 reason=granted token=rsv-1",
                ],
            ) as server:
                client = ReservationClient(path, "job", timeout_ms=100, retry_ms=1)
                token = client.reserve(50)
            self.assertEqual(token.value, "rsv-1")
            self.assertEqual(token.requested_bytes, 50)
            self.assertEqual(token.borrowed_bytes, 25)
            self.assertEqual(token.wait_count, 1)
            first_id = server.requests[0].split("request=", 1)[1].split()[0]
            second_id = server.requests[1].split("request=", 1)[1].split()[0]
            self.assertEqual(first_id, second_id)
            self.assertTrue(
                all("bytes=50 safety=67108864" in request for request in server.requests)
            )

    def test_timeout_never_turns_wait_into_permission(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manager.sock"
            responses = [
                "WAIT effective_limit=100 reserved_memory_available=0 reason=reserved_memory_exhausted"
            ]
            with ScriptedServer(path, responses) as server:
                client = ReservationClient(path, "job", timeout_ms=3, retry_ms=1)
                with self.assertRaisesRegex(ReservationTimeout, "before load"):
                    client.reserve(50)
            self.assertGreaterEqual(len(server.requests), 2)
            self.assertTrue(server.requests[-1].startswith("CANCEL job=job request="))

    def test_shrink_reports_capacity_returned_by_manager(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manager.sock"
            with ScriptedServer(
                path,
                [
                    "GRANTED effective_limit=300 borrowed_bytes=100 "
                    "reserved_memory_available=0 reason=granted token=rsv-7",
                    "SHRUNK previous_borrowed_bytes=100 borrowed_bytes=25 "
                    "returned_bytes=75 effective_limit=225 "
                    "reserved_memory_available=75 reason=shrunk_to_remaining_estimate",
                ],
            ) as server:
                client = ReservationClient(path, "job")
                token = client.reserve(100)
                result = client.shrink(token, 20)
            self.assertEqual(result.decision, "SHRUNK")
            self.assertEqual(result.previous_borrowed_bytes, 100)
            self.assertEqual(result.borrowed_bytes, 25)
            self.assertEqual(result.returned_bytes, 75)
            self.assertEqual(
                server.requests[-1],
                "SHRINK job=job token=rsv-7 remaining=20 safety=67108864",
            )

    def test_admission_and_release_margins_can_differ(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manager.sock"
            with ScriptedServer(
                path,
                [
                    "GRANTED effective_limit=300 borrowed_bytes=100 "
                    "reserved_memory_available=0 reason=granted token=rsv-8",
                    "RELEASED effective_limit=200 reserved_memory_available=100 "
                    "reason=released",
                ],
            ) as server:
                client = ReservationClient(
                    path,
                    "job",
                    release_safety_bytes=64,
                    admission_safety_bytes=128,
                )
                token = client.reserve(100)
                client.release(token)
            self.assertIn("bytes=100 safety=128", server.requests[0])
            self.assertEqual(
                server.requests[1],
                "RELEASE job=job token=rsv-8 safety=64",
            )

    def test_checkpoint_wait_then_release(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manager.sock"
            with ScriptedServer(
                path,
                [
                    "WAIT effective_limit=100 checkpoint_available=0 reason=checkpoint_budget_busy",
                    "GRANTED effective_limit=200 checkpoint_available=0 reason=granted token=ckpt-1",
                    "RELEASED effective_limit=100 checkpoint_available=100 reason=released",
                ],
            ) as server:
                client = ReservationClient(path, "job", timeout_ms=100, retry_ms=1)
                with client.checkpoint() as token:
                    self.assertEqual(token.value, "ckpt-1")
            self.assertEqual(len(server.requests), 3)
            first_id = server.requests[0].split("request=", 1)[1].split()[0]
            second_id = server.requests[1].split("request=", 1)[1].split()[0]
            self.assertEqual(first_id, second_id)
            self.assertEqual(
                server.requests[2],
                "CHECKPOINT_END job=job token=ckpt-1 safety=67108864",
            )


if __name__ == "__main__":
    unittest.main()
