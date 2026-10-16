#!/usr/bin/env python3
"""Join opt-in batch phase markers with SRCG cgroup memory samples.

The analyzer does not change admission. It reports sampled, not guaranteed
exact, per-batch RAM changes because the controller observes memory.current at
its configured interval.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Iterable


def read_training_traces(path: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("event") == "batch_memory_trace":
            records.append(record)
    return records


def read_controller_samples(path: Path) -> list[tuple[int, int]]:
    samples: list[tuple[int, int]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        fields: dict[str, str] = {}
        for item in line.split():
            if "=" in item:
                key, value = item.split("=", 1)
                fields[key] = value
        if fields.get("event") != "memory_sample":
            continue
        try:
            samples.append(
                (int(fields["monotonic_time_ms"]), int(fields["memory_current_bytes"]))
            )
        except (KeyError, ValueError):
            continue
    return samples


def point_value(phases: dict[str, object], phase: str, field: str) -> int | None:
    point = phases.get(phase)
    if not isinstance(point, dict):
        return None
    value = point.get(field)
    return value if isinstance(value, int) else None


def point_stat_value(
    phases: dict[str, object], phase: str, stat_name: str
) -> int | None:
    point = phases.get(phase)
    if not isinstance(point, dict):
        return None
    stat = point.get("stat_bytes")
    if not isinstance(stat, dict):
        return None
    value = stat.get(stat_name)
    return value if isinstance(value, int) else None


def difference(end: int | None, start: int | None) -> int | None:
    return end - start if end is not None and start is not None else None


def sampled_peak(
    samples: Iterable[tuple[int, int]],
    start_ms: int,
    end_ms: int,
    endpoints: Iterable[int | None],
) -> tuple[int | None, int]:
    values = [value for timestamp, value in samples if start_ms <= timestamp <= end_ms]
    count = len(values)
    values.extend(value for value in endpoints if value is not None)
    return (max(values), count) if values else (None, count)


def analyze_trace_record(
    record: dict[str, object], samples: list[tuple[int, int]]
) -> dict[str, object]:
    phases = record.get("phase_points")
    if not isinstance(phases, dict):
        raise ValueError("batch_memory_trace has no phase_points object")

    start_ms = phases.get("load_started_monotonic_ms")
    collate_phase = (
        "after_collate_before_return"
        if "after_collate_before_return" in phases
        else "after_collate"
    )
    end_ms = point_value(phases, collate_phase, "monotonic_time_ms")
    before_current = point_value(phases, "before_reserve", "current_bytes")
    before_limit = point_value(phases, "before_reserve", "limit_bytes")
    after_collate = point_value(phases, collate_phase, "current_bytes")
    after_collate_with_refs = point_value(
        phases, "after_collate_with_sample_refs", "current_bytes"
    )
    requested = record.get("estimated_batch_allocation_bytes")
    if not isinstance(requested, int):
        raise ValueError("batch_memory_trace has no integer estimate")

    load_peak: int | None = None
    load_sample_count = 0
    if isinstance(start_ms, int) and isinstance(end_ms, int):
        load_peak, load_sample_count = sampled_peak(
            samples,
            start_ms,
            end_ms,
            (before_current, after_collate_with_refs, after_collate),
        )

    observed_increase = (
        max(0, load_peak - before_current)
        if load_peak is not None and before_current is not None
        else None
    )
    total_current_headroom = (
        max(0, before_limit - before_current)
        if before_limit is not None and before_current is not None
        else None
    )
    inactive_file = point_stat_value(phases, "before_reserve", "inactive_file")
    protected_current = (
        max(0, before_current - inactive_file)
        if before_current is not None and inactive_file is not None
        else before_current
    )
    protected_current_headroom = (
        max(0, before_limit - protected_current)
        if before_limit is not None and protected_current is not None
        else None
    )
    expected_borrowed = (
        max(0, requested - protected_current_headroom)
        if protected_current_headroom is not None
        else None
    )
    actual_borrowed = record.get("token_borrowed_bytes")
    if not isinstance(actual_borrowed, int):
        actual_borrowed = None
    before_anon = point_stat_value(phases, "before_reserve", "anon")
    before_file = point_stat_value(phases, "before_reserve", "file")
    collate_anon = point_stat_value(phases, collate_phase, "anon")
    collate_file = point_stat_value(phases, collate_phase, "file")
    optimizer_anon = point_stat_value(phases, "after_optimizer", "anon")
    optimizer_file = point_stat_value(phases, "after_optimizer", "file")

    return {
        "event": "batch_memory_analysis",
        "step": record.get("step"),
        "batch_id": record.get("batch_id"),
        "sample_ids": record.get("sample_ids"),
        "estimated_batch_allocation_bytes": requested,
        "estimated_resident_bytes": record.get("estimated_resident_bytes"),
        "before_reserve_current_bytes": before_current,
        "before_reserve_limit_bytes": before_limit,
        "total_current_headroom_bytes": total_current_headroom,
        "inactive_file_bytes": inactive_file,
        "protected_current_bytes": protected_current,
        "protected_current_headroom_bytes": protected_current_headroom,
        "sampled_load_peak_bytes": load_peak,
        "sampled_load_increase_bytes": observed_increase,
        "estimate_minus_sampled_increase_bytes": (
            requested - observed_increase if observed_increase is not None else None
        ),
        "expected_borrowed_bytes": expected_borrowed,
        "actual_borrowed_bytes": actual_borrowed,
        "borrowed_difference_bytes": difference(actual_borrowed, expected_borrowed),
        "after_collate_current_bytes": after_collate,
        "before_reserve_anon_bytes": before_anon,
        "before_reserve_file_bytes": before_file,
        "after_collate_anon_bytes": collate_anon,
        "after_collate_file_bytes": collate_file,
        "load_anon_change_bytes": difference(collate_anon, before_anon),
        "load_file_change_bytes": difference(collate_file, before_file),
        "after_h2d_call_current_bytes": point_value(
            phases, "after_h2d_call", "current_bytes"
        ),
        "after_optimizer_current_bytes": point_value(
            phases, "after_optimizer", "current_bytes"
        ),
        "after_optimizer_anon_bytes": optimizer_anon,
        "after_optimizer_file_bytes": optimizer_file,
        "collate_to_optimizer_anon_change_bytes": difference(
            optimizer_anon, collate_anon
        ),
        "collate_to_optimizer_file_change_bytes": difference(
            optimizer_file, collate_file
        ),
        "before_release_current_bytes": point_value(
            phases, "before_release", "current_bytes"
        ),
        "after_release_current_bytes": point_value(
            phases, "after_release", "current_bytes"
        ),
        "load_interval_ms": (
            end_ms - start_ms
            if isinstance(start_ms, int) and isinstance(end_ms, int)
            else None
        ),
        "controller_samples_during_load": load_sample_count,
    }


def make_summary(records: list[dict[str, object]]) -> dict[str, object]:
    def integer_values(field: str) -> list[int]:
        return [value for record in records if isinstance((value := record.get(field)), int)]

    estimates = integer_values("estimated_batch_allocation_bytes")
    observed = integer_values("sampled_load_increase_bytes")
    expected_borrows = integer_values("expected_borrowed_bytes")
    actual_borrows = integer_values("actual_borrowed_bytes")
    anon_changes = integer_values("load_anon_change_bytes")
    file_changes = integer_values("load_file_change_bytes")
    covered = sum(
        isinstance(record.get("controller_samples_during_load"), int)
        and int(record["controller_samples_during_load"]) > 0
        for record in records
    )
    return {
        "event": "memory_trace_summary",
        "batches": len(records),
        "batches_with_controller_samples": covered,
        "estimated_batch_allocation_total_bytes": sum(estimates),
        "sampled_load_increase_total_bytes": sum(observed),
        "expected_borrowed_total_bytes": sum(expected_borrows),
        "actual_borrowed_total_bytes": sum(actual_borrows),
        "estimated_batch_allocation_mean_bytes": (
            round(sum(estimates) / len(estimates)) if estimates else None
        ),
        "sampled_load_increase_mean_bytes": (
            round(sum(observed) / len(observed)) if observed else None
        ),
        "expected_borrowed_mean_bytes": (
            round(sum(expected_borrows) / len(expected_borrows))
            if expected_borrows
            else None
        ),
        "actual_borrowed_mean_bytes": (
            round(sum(actual_borrows) / len(actual_borrows))
            if actual_borrows
            else None
        ),
        "sampled_load_increase_max_bytes": max(observed) if observed else None,
        "load_anon_change_mean_bytes": (
            round(sum(anon_changes) / len(anon_changes)) if anon_changes else None
        ),
        "load_file_change_mean_bytes": (
            round(sum(file_changes) / len(file_changes)) if file_changes else None
        ),
        "note": (
            "sampled values can miss short peaks between controller samples; "
            "expected borrow uses the same current/inactive_file formula as Phase 2"
        ),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--training-log", required=True, type=Path)
    parser.add_argument("--controller-log", required=True, type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    traces = read_training_traces(args.training_log)
    if not traces:
        raise SystemExit("no batch_memory_trace events found; run training with --memory-trace")
    samples = read_controller_samples(args.controller_log)
    analyzed = [analyze_trace_record(record, samples) for record in traces]
    for record in analyzed:
        print(json.dumps(record, sort_keys=True))
    print(json.dumps(make_summary(analyzed), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
