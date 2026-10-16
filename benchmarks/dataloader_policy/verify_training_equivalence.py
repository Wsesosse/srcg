#!/usr/bin/env python3
"""Fail unless candidate training results are semantically equal to a baseline."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import sys
from typing import Sequence


TRAINING_CONFIG_FIELDS = (
    "workload",
    "data_root",
    "model",
    "batch_size",
    "epochs",
    "optimizer",
    "learning_rate",
    "weight_decay",
    "momentum",
    "seed",
    "shuffle",
    "limit",
    "max_steps",
    "unet_base_channels",
    "volume_size",
)


@dataclass(frozen=True)
class TrainingResult:
    path: Path
    config: dict[str, object]
    losses: tuple[float, ...]
    processed_samples: int
    processed_steps: int
    final_model_sha256: str
    completed: bool


def read_result(path: Path) -> TrainingResult:
    config: dict[str, object] | None = None
    losses: list[float] = []
    summary: dict[str, object] | None = None
    completed = False
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"{path}:{line_number}: invalid JSON: {error}") from error
            event = record.get("event")
            if event == "run_config":
                config = dict(record["config"])
            elif event == "step_end":
                losses.append(float(record["loss"]))
            elif event == "training_end":
                completed = record.get("reason") == "epochs_complete"
            elif event == "performance_summary":
                summary = record
    if config is None or summary is None:
        raise ValueError(f"{path}: missing run_config or performance_summary")
    return TrainingResult(
        path=path,
        config=config,
        losses=tuple(losses),
        processed_samples=int(summary["processed_samples"]),
        processed_steps=int(summary["processed_steps"]),
        final_model_sha256=str(summary["final_model_sha256"]),
        completed=completed,
    )


def selected_config(result: TrainingResult) -> dict[str, object]:
    return {field: result.config.get(field) for field in TRAINING_CONFIG_FIELDS}


def compare_results(
    baseline: TrainingResult,
    candidate: TrainingResult,
    *,
    loss_atol: float = 0.0,
) -> dict[str, object]:
    config_equal = selected_config(baseline) == selected_config(candidate)
    step_count_equal = baseline.processed_steps == candidate.processed_steps
    sample_count_equal = baseline.processed_samples == candidate.processed_samples
    loss_count_equal = len(baseline.losses) == len(candidate.losses)
    max_loss_delta = (
        max(
            (abs(left - right) for left, right in zip(baseline.losses, candidate.losses)),
            default=0.0,
        )
        if loss_count_equal
        else None
    )
    loss_series_equal = bool(
        loss_count_equal
        and max_loss_delta is not None
        and max_loss_delta <= loss_atol
    )
    model_hash_equal = baseline.final_model_sha256 == candidate.final_model_sha256
    equivalent = all(
        (
            baseline.completed,
            candidate.completed,
            config_equal,
            step_count_equal,
            sample_count_equal,
            loss_series_equal,
            model_hash_equal,
        )
    )
    return {
        "event": "training_equivalence",
        "baseline": str(baseline.path),
        "candidate": str(candidate.path),
        "baseline_completed": baseline.completed,
        "candidate_completed": candidate.completed,
        "config_equal": config_equal,
        "step_count_equal": step_count_equal,
        "sample_count_equal": sample_count_equal,
        "loss_count": len(candidate.losses),
        "loss_atol": loss_atol,
        "max_loss_delta": max_loss_delta,
        "loss_series_equal": loss_series_equal,
        "model_hash_equal": model_hash_equal,
        "final_model_sha256": candidate.final_model_sha256,
        "equivalent": equivalent,
    }


def positive_or_zero_float(value: str) -> float:
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Compare completed steps, loss series, and final model bytes. "
            "Exit 1 when any candidate differs from the baseline."
        )
    )
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, action="append", required=True)
    parser.add_argument(
        "--loss-atol",
        type=positive_or_zero_float,
        default=0.0,
        help="absolute tolerance for each logged loss; default requires exact equality",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        baseline = read_result(args.baseline)
        comparisons = [
            compare_results(baseline, read_result(path), loss_atol=args.loss_atol)
            for path in args.candidate
        ]
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"training equivalence check failed: {error}", file=sys.stderr)
        return 2
    for comparison in comparisons:
        print(json.dumps(comparison, sort_keys=True))
    return 0 if all(item["equivalent"] for item in comparisons) else 1


if __name__ == "__main__":
    raise SystemExit(main())
