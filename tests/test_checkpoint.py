from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
import random
import signal
import tempfile
import unittest

import torch

from srcg_torch import AtomicCheckpointStore, CheckpointSession
from srcg_torch.session import capture_rng_state, restore_rng_state


class AtomicCheckpointTests(unittest.TestCase):
    def test_round_trip_and_manifest_checksum(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = AtomicCheckpointStore(directory)
            record = store.save({"global_step": 4, "value": torch.tensor([1, 2])}, 4)
            payload, loaded = store.load()

            self.assertTrue(store.has_complete_checkpoint())
            self.assertEqual(payload["global_step"], 4)
            self.assertTrue(torch.equal(payload["value"], torch.tensor([1, 2])))
            self.assertEqual(record.sha256, loaded.sha256)

    def test_failed_serialization_keeps_previous_latest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = AtomicCheckpointStore(directory)
            first = store.save({"global_step": 1}, 1)
            old_manifest = store.manifest_path.read_bytes()

            def fail(_payload: object, stream: object) -> None:
                stream.write(b"partial")  # type: ignore[attr-defined]
                raise RuntimeError("injected serialization failure")

            with self.assertRaisesRegex(RuntimeError, "injected"):
                store.save({"global_step": 2}, 2, serializer=fail)

            self.assertEqual(store.manifest_path.read_bytes(), old_manifest)
            payload, loaded = store.load()
            self.assertEqual(payload["global_step"], 1)
            self.assertEqual(loaded.path, first.path)
            self.assertFalse(list(Path(directory).glob("*.tmp")))

    def test_corruption_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = AtomicCheckpointStore(directory)
            record = store.save({"global_step": 1}, 1)
            with record.path.open("ab") as stream:
                stream.write(b"damage")
            with self.assertRaisesRegex(RuntimeError, "size mismatch"):
                store.load()

    def test_retention_keeps_latest_two(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = AtomicCheckpointStore(directory, keep=2)
            for step in range(1, 4):
                store.save({"global_step": step}, step)
            names = sorted(path.name for path in Path(directory).glob("checkpoint-step-*.pt"))
            self.assertEqual(len(names), 2)
            self.assertTrue(names[0].startswith("checkpoint-step-000000000002-"))
            self.assertTrue(names[1].startswith("checkpoint-step-000000000003-"))

    def test_signal_only_sets_flag_until_safe_point(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = CheckpointSession(AtomicCheckpointStore(directory))
            session.request_stop_for_test(signal.SIGTERM)
            self.assertFalse(Path(directory, "latest.json").exists())
            record = session.save_at_safe_point(3, lambda: {"value": 1})
            self.assertIsNotNone(record)
            self.assertTrue(Path(directory, "latest.json").exists())

    def test_periodic_checkpoint_uses_admission_only_every_x_steps(self) -> None:
        class Gate:
            def __init__(self) -> None:
                self.active = False
                self.entries = 0

            @contextmanager
            def checkpoint(self):
                self.entries += 1
                self.active = True
                try:
                    yield
                finally:
                    self.active = False

        with tempfile.TemporaryDirectory() as directory:
            gate = Gate()
            session = CheckpointSession(
                AtomicCheckpointStore(directory),
                every_steps=3,
                checkpoint_client=gate,  # type: ignore[arg-type]
            )

            def payload():
                self.assertTrue(gate.active)
                return {"value": 7}

            self.assertIsNone(session.save_at_safe_point(1, payload))
            self.assertIsNone(session.save_at_safe_point(2, payload))
            self.assertIsNotNone(session.save_at_safe_point(3, payload))
            self.assertFalse(gate.active)
            self.assertEqual(gate.entries, 1)
            saved, record = session.store.load()  # type: ignore[union-attr]
            self.assertEqual(record.step, 3)
            self.assertEqual(saved["global_step"], 3)

            def fail():
                self.assertTrue(gate.active)
                raise RuntimeError("payload failure")

            with self.assertRaisesRegex(RuntimeError, "payload failure"):
                session.save_at_safe_point(6, fail)
            self.assertFalse(gate.active)
            self.assertEqual(gate.entries, 2)
            self.assertEqual(session.store.load()[1].step, 3)  # type: ignore[union-attr]

    def test_rng_state_round_trip(self) -> None:
        random.seed(9)
        torch.manual_seed(9)
        state = capture_rng_state()
        expected = (random.random(), torch.rand(3))
        random.random()
        torch.rand(3)
        restore_rng_state(state)
        actual = (random.random(), torch.rand(3))
        self.assertEqual(expected[0], actual[0])
        self.assertTrue(torch.equal(expected[1], actual[1]))

    @unittest.skipUnless(torch.cuda.is_available(), "CUDA is required")
    def test_rng_state_restores_after_cuda_map_location(self) -> None:
        random.seed(13)
        torch.manual_seed(13)
        state = capture_rng_state()
        with tempfile.TemporaryDirectory() as directory:
            store = AtomicCheckpointStore(directory)
            store.save({"rng": state}, 1)
            payload, _record = store.load(map_location="cuda")

        self.assertEqual(payload["rng"]["torch_cpu"].device.type, "cuda")
        restore_rng_state(payload["rng"])
        self.assertEqual(torch.get_rng_state().device.type, "cpu")


if __name__ == "__main__":
    unittest.main()
