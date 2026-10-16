#!/usr/bin/env python3

from __future__ import annotations

import gzip
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest

import torch
from torch.utils.data import DataLoader, Dataset

BENCHMARK_ROOT = Path(__file__).resolve().parent
if str(BENCHMARK_ROOT) not in sys.path:
    sys.path.insert(0, str(BENCHMARK_ROOT))

from train import (
    PlanningBatchSampler,
    ReservedBatch,
    ReservedBatchDataset,
    SamplePlan,
    estimate_batch_bytes,
    jpeg_dimensions,
    nifti_shape,
    unwrap_reserved_batch,
)
from srcg_torch import ReservationToken
from analyze_memory_trace import analyze_trace_record, make_summary


class PlanningTests(unittest.TestCase):
    def test_jpeg_dimensions_reads_header_without_pixel_decode(self) -> None:
        jpeg = b"\xff\xd8\xff\xc0\x00\x08\x08\x00\x10\x00\x20\x03"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.jpg"
            path.write_bytes(jpeg)
            self.assertEqual(jpeg_dimensions(path), (16, 32, 3))

    def test_nifti_shape_reads_gzip_header_without_voxel_load(self) -> None:
        header = bytearray(348)
        struct.pack_into("<i", header, 0, 348)
        struct.pack_into("<8h", header, 40, 3, 512, 512, 321, 1, 1, 1, 1)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.nii.gz"
            with gzip.open(path, "wb") as stream:
                stream.write(header)
            self.assertEqual(nifti_shape(path), (512, 512, 321))

    def test_seeded_shuffle_is_reproducible(self) -> None:
        plans = [SamplePlan(i, str(i), (1,), 100, 10) for i in range(10)]
        first = PlanningBatchSampler(plans, 2, True, 42, 1.25)
        second = PlanningBatchSampler(plans, 2, True, 42, 1.25)
        first_epoch = list(first)
        self.assertEqual(first_epoch, list(second))
        self.assertCountEqual([index for batch in first_epoch for index in batch], range(10))
        for _ in first_epoch:
            first.mark_batch_consumed()
        self.assertNotEqual(first_epoch, list(first))

    def test_checkpoint_cursor_tracks_consumed_not_prefetched_batches(self) -> None:
        plans = [SamplePlan(i, str(i), (1,), 100, 10) for i in range(8)]
        sampler = PlanningBatchSampler(plans, 2, True, 7, 1.25)
        iterator = iter(sampler)
        first = next(iterator)
        _prefetched = next(iterator)
        sampler.mark_batch_consumed()

        restored = PlanningBatchSampler(plans, 2, True, 7, 1.25)
        restored.load_state_dict(sampler.state_dict())
        remaining = list(restored)

        complete = PlanningBatchSampler(plans, 2, True, 7, 1.25)
        expected = list(complete)
        self.assertEqual(first, expected[0])
        self.assertEqual(remaining, expected[1:])

    def test_batch_estimate_accounts_for_collate_and_safety_factor(self) -> None:
        plans = [
            SamplePlan(0, "a", (1,), load_peak_bytes=100, resident_bytes=40),
            SamplePlan(1, "b", (1,), load_peak_bytes=100, resident_bytes=40),
        ]
        self.assertEqual(estimate_batch_bytes(plans, 1.25), 200)

    def test_hook_runs_before_dataset_load_with_zero_workers(self) -> None:
        events: list[str] = []

        class RecordingDataset(Dataset):
            def __len__(self) -> int:
                return 1

            def __getitem__(self, index: int) -> torch.Tensor:
                events.append("load")
                return torch.tensor(index)

        plans = [SamplePlan(0, "sample", (1,), 100, 10)]
        sampler = PlanningBatchSampler(
            plans,
            batch_size=1,
            shuffle=False,
            seed=42,
            safety_factor=1.25,
            before_load=lambda _: events.append("plan"),
        )
        list(DataLoader(RecordingDataset(), batch_sampler=sampler, num_workers=0))
        self.assertEqual(events, ["plan", "load"])

    def test_reserved_dataset_grants_before_batched_load(self) -> None:
        events: list[str] = []

        class RecordingDataset(Dataset):
            def __len__(self) -> int:
                return 2

            def __getitem__(self, index: int) -> tuple[torch.Tensor, torch.Tensor]:
                events.append(f"load-{index}")
                return torch.tensor([index]), torch.tensor(index)

        class RecordingClient:
            def retry_pending(self) -> tuple[int, int]:
                events.append("retry")
                return 0, 0

            def reserve(self, additional_bytes: int) -> ReservationToken:
                events.append(f"reserve-{additional_bytes}")
                return ReservationToken(
                    "test-token", additional_bytes, additional_bytes, 1024, 0.0, 0, 0.1
                )

        plans = [
            SamplePlan(0, "zero", (1,), 100, 10),
            SamplePlan(1, "one", (1,), 100, 10),
        ]
        dataset = ReservedBatchDataset(
            RecordingDataset(),
            plans,
            safety_factor=1.25,
            timeout_ms=1000,
            retry_ms=10,
            release_safety_bytes=0,
        )
        dataset._client = RecordingClient()  # type: ignore[assignment]
        dataset._client_pid = os.getpid()
        batches = list(
            DataLoader(
                dataset,
                batch_size=2,
                shuffle=False,
                num_workers=0,
                collate_fn=unwrap_reserved_batch,
            )
        )

        self.assertEqual(events, ["retry", "reserve-138", "load-0", "load-1"])
        self.assertEqual(len(batches), 1)
        self.assertIsInstance(batches[0], ReservedBatch)
        self.assertEqual(batches[0].token.value, "test-token")
        self.assertIsNone(batches[0].memory_trace)

    def test_reserved_dataset_records_opt_in_memory_boundaries(self) -> None:
        class OneSampleDataset(Dataset):
            def __len__(self) -> int:
                return 1

            def __getitem__(self, index: int) -> tuple[torch.Tensor, torch.Tensor]:
                return torch.tensor([index]), torch.tensor(index)

        class GrantingClient:
            def retry_pending(self) -> tuple[int, int]:
                return 0, 0

            def reserve(self, additional_bytes: int) -> ReservationToken:
                return ReservationToken(
                    "trace-token", additional_bytes, additional_bytes, 1024, 0.0, 0, 0.1
                )

        dataset = ReservedBatchDataset(
            OneSampleDataset(),
            [SamplePlan(0, "sample", (1,), 100, 10)],
            safety_factor=1.25,
            timeout_ms=1000,
            retry_ms=10,
            release_safety_bytes=0,
            memory_trace=True,
        )
        dataset._client = GrantingClient()  # type: ignore[assignment]
        dataset._client_pid = os.getpid()

        batch = next(
            iter(
                DataLoader(
                    dataset,
                    batch_size=1,
                    num_workers=0,
                    collate_fn=unwrap_reserved_batch,
                )
            )
        )

        self.assertEqual(batch.estimated_resident_bytes, 10)
        self.assertEqual(batch.estimated_resident_with_safety_bytes, 13)
        self.assertIsNotNone(batch.memory_trace)
        assert batch.memory_trace is not None
        self.assertIn("before_reserve", batch.memory_trace)
        self.assertIn("after_grant", batch.memory_trace)
        self.assertIn("load_started_monotonic_ms", batch.memory_trace)
        self.assertIn("after_collate_before_return", batch.memory_trace)

    def test_memory_trace_analysis_uses_controller_samples(self) -> None:
        def point(
            timestamp: int,
            current: int,
            limit: int = 1000,
            *,
            anon: int = 0,
            file: int = 0,
            inactive_file: int = 0,
        ) -> dict[str, object]:
            return {
                "monotonic_time_ms": timestamp,
                "current_bytes": current,
                "peak_bytes": current,
                "limit_bytes": limit,
                "stat_bytes": {
                    "anon": anon,
                    "file": file,
                    "inactive_file": inactive_file,
                },
            }

        record: dict[str, object] = {
            "event": "batch_memory_trace",
            "step": 1,
            "batch_id": 1,
            "sample_ids": ["sample"],
            "estimated_batch_allocation_bytes": 500,
            "estimated_resident_bytes": 20,
            "phase_points": {
                "before_reserve": point(
                    10, 400, anon=250, file=100, inactive_file=50
                ),
                "load_started_monotonic_ms": 20,
                "after_collate_before_return": point(40, 650, anon=350, file=250),
                "after_h2d_call": point(50, 500),
                "after_optimizer": point(60, 480, anon=375, file=240),
                "before_release": point(61, 470),
                "after_release": point(62, 460),
            },
        }
        analysis = analyze_trace_record(record, [(25, 800), (35, 900), (45, 999)])

        self.assertEqual(analysis["sampled_load_peak_bytes"], 900)
        self.assertEqual(analysis["sampled_load_increase_bytes"], 500)
        self.assertEqual(analysis["total_current_headroom_bytes"], 600)
        self.assertEqual(analysis["protected_current_bytes"], 350)
        self.assertEqual(analysis["protected_current_headroom_bytes"], 650)
        self.assertEqual(analysis["expected_borrowed_bytes"], 0)
        self.assertEqual(analysis["controller_samples_during_load"], 2)
        self.assertEqual(analysis["load_anon_change_bytes"], 100)
        self.assertEqual(analysis["load_file_change_bytes"], 150)
        self.assertEqual(analysis["collate_to_optimizer_anon_change_bytes"], 25)
        summary = make_summary([analysis])
        self.assertEqual(summary["batches"], 1)
        self.assertEqual(summary["sampled_load_increase_mean_bytes"], 500)


if __name__ == "__main__":
    unittest.main()
