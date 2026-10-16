from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from benchmarks.dataloader_policy.verify_training_equivalence import (
    compare_results,
    read_result,
)


class TrainingEquivalenceTests(unittest.TestCase):
    def write_result(
        self,
        path: Path,
        *,
        losses: tuple[float, ...] = (0.5, 0.25),
        model_hash: str = "abc",
        reason: str = "epochs_complete",
    ) -> None:
        config = {
            "workload": "lung",
            "data_root": "/dataset",
            "model": "unet3d",
            "batch_size": 1,
            "epochs": 1,
            "optimizer": "adamw",
            "learning_rate": 0.0001,
            "weight_decay": 0.0001,
            "momentum": 0.9,
            "seed": 42,
            "shuffle": True,
            "limit": None,
            "max_steps": 0,
            "unet_base_channels": 32,
            "volume_size": [32, 128, 128],
        }
        records = [{"event": "run_config", "config": config}]
        records.extend(
            {"event": "step_end", "step": step, "loss": loss}
            for step, loss in enumerate(losses, start=1)
        )
        records.extend(
            (
                {"event": "training_end", "reason": reason, "steps": len(losses)},
                {
                    "event": "performance_summary",
                    "processed_samples": len(losses),
                    "processed_steps": len(losses),
                    "final_model_sha256": model_hash,
                },
            )
        )
        path.write_text(
            "".join(json.dumps(record) + "\n" for record in records),
            encoding="utf-8",
        )

    def test_exact_result_is_equivalent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            baseline_path = Path(directory) / "baseline.jsonl"
            candidate_path = Path(directory) / "candidate.jsonl"
            self.write_result(baseline_path)
            self.write_result(candidate_path)
            result = compare_results(
                read_result(baseline_path), read_result(candidate_path)
            )
            self.assertTrue(result["equivalent"])
            self.assertEqual(result["max_loss_delta"], 0.0)

    def test_model_or_loss_change_fails_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            baseline_path = Path(directory) / "baseline.jsonl"
            candidate_path = Path(directory) / "candidate.jsonl"
            self.write_result(baseline_path)
            self.write_result(candidate_path, losses=(0.5, 0.3), model_hash="def")
            result = compare_results(
                read_result(baseline_path), read_result(candidate_path)
            )
            self.assertFalse(result["equivalent"])
            self.assertFalse(result["loss_series_equal"])
            self.assertFalse(result["model_hash_equal"])


if __name__ == "__main__":
    unittest.main()
