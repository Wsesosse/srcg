#!/usr/bin/env python3
"""Summarize the one-job, cross-boot recovery experiment.

This intentionally reports process-active time separately from end-to-end
time. End-to-end time includes reboot and the delay before the user launches
the next attempt; active time does not.
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Attempt:
    directory: Path
    fields: dict[str, str]
    events: tuple[dict[str, Any], ...]

    @property
    def duration_seconds(self) -> float:
        started = int(self.fields["started_unix_ns"])
        ended = int(self.fields["ended_unix_ns"])
        return (ended - started) / 1_000_000_000


def parse_summary(path: Path) -> dict[str, str]:
    fields: dict[str, str] = {}
    for item in shlex.split(path.read_text(encoding="utf-8")):
        key, separator, value = item.partition("=")
        if not separator:
            raise ValueError(f"{path}: malformed field {item!r}")
        fields[key] = value
    required = {
        "attempt",
        "mode",
        "result",
        "exit_status",
        "boot_id",
        "started_unix_ns",
        "ended_unix_ns",
        "checkpoint_before_step",
        "checkpoint_after_step",
        "last_completed_step",
        "parent_peak_bytes",
        "oom_kill",
    }
    missing = required - fields.keys()
    if missing:
        raise ValueError(f"{path}: missing fields: {', '.join(sorted(missing))}")
    return fields


def read_events(path: Path) -> tuple[dict[str, Any], ...]:
    events: list[dict[str, Any]] = []
    if not path.exists():
        return ()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            # The controller may append a diagnostic after an OOM-kill. It is
            # not a training event and should not invalidate earlier JSONL.
            continue
        if isinstance(value, dict):
            events.append(value)
        else:
            raise ValueError(f"{path}:{line_number}: JSON event is not an object")
    return tuple(events)


def read_attempts(run_directory: Path) -> list[Attempt]:
    attempts_directory = run_directory / "attempts"
    if not attempts_directory.is_dir():
        raise ValueError(f"{run_directory}: attempts directory does not exist")
    attempts: list[Attempt] = []
    for directory in sorted(path for path in attempts_directory.iterdir() if path.is_dir()):
        if (directory / "INVALID.txt").exists():
            continue
        summary = directory / "summary.txt"
        if not summary.exists():
            continue
        attempts.append(
            Attempt(
                directory=directory,
                fields=parse_summary(summary),
                events=read_events(directory / "training.jsonl"),
            )
        )
    if not attempts:
        raise ValueError(f"{run_directory}: no valid completed attempts found")
    return attempts


def optional_step(value: str) -> int | None:
    return None if value == "none" else int(value)


def last_event(attempts: list[Attempt], event_name: str) -> dict[str, Any] | None:
    found: dict[str, Any] | None = None
    for attempt in attempts:
        for event in attempt.events:
            if event.get("event") == event_name:
                found = event
    return found


def summarize(run_directory: Path) -> dict[str, Any]:
    attempts = read_attempts(run_directory)
    modes = {attempt.fields["mode"] for attempt in attempts}
    if len(modes) != 1:
        raise ValueError(f"{run_directory}: attempts contain more than one mode")

    completed_attempts = [
        attempt for attempt in attempts if attempt.fields["result"] == "completed"
    ]
    completed = bool(completed_attempts)
    active_seconds = sum(attempt.duration_seconds for attempt in attempts)
    first_start = min(int(attempt.fields["started_unix_ns"]) for attempt in attempts)
    last_end = max(int(attempt.fields["ended_unix_ns"]) for attempt in attempts)
    end_to_end_seconds = (last_end - first_start) / 1_000_000_000

    lost_steps = 0
    executed_steps = 0
    checkpoint_write_ms = 0.0
    checkpoint_load_ms = 0.0
    for attempt in attempts:
        before = optional_step(attempt.fields["checkpoint_before_step"]) or 0
        after = optional_step(attempt.fields["checkpoint_after_step"]) or before
        last_step = int(attempt.fields["last_completed_step"])
        executed_steps += max(0, last_step - before)
        if attempt.fields["result"] != "completed":
            lost_steps += max(0, last_step - after)
        for event in attempt.events:
            if event.get("event") == "checkpoint_committed":
                checkpoint_write_ms += float(event.get("save_ms", 0.0))
            elif event.get("event") == "checkpoint_restored":
                checkpoint_load_ms += float(event.get("load_ms", 0.0))

    performance = last_event(attempts, "performance_summary")
    training_end = last_event(attempts, "training_end")
    return {
        "run_directory": str(run_directory.resolve()),
        "mode": next(iter(modes)),
        "completed": completed,
        "attempts": len(attempts),
        "failed_attempts": sum(
            attempt.fields["result"] != "completed" for attempt in attempts
        ),
        "boot_count": len({attempt.fields["boot_id"] for attempt in attempts}),
        "active_seconds": round(active_seconds, 6),
        "end_to_end_seconds": round(end_to_end_seconds, 6),
        "between_attempt_seconds": round(max(0.0, end_to_end_seconds - active_seconds), 6),
        "oom_kills": sum(int(attempt.fields["oom_kill"]) for attempt in attempts),
        "oom_attempts": sum(
            attempt.fields.get("classification") == "cgroup_oom"
            for attempt in attempts
        ),
        "parent_peak_bytes": max(
            int(attempt.fields["parent_peak_bytes"]) for attempt in attempts
        ),
        "executed_steps_including_lost_work": executed_steps,
        "lost_steps_after_latest_checkpoint": lost_steps,
        "checkpoint_write_ms": round(checkpoint_write_ms, 3),
        "checkpoint_load_ms": round(checkpoint_load_ms, 3),
        "final_step": None if training_end is None else int(training_end["steps"]),
        "final_model_sha256": (
            None if performance is None else performance.get("final_model_sha256")
        ),
    }


def print_human(baseline: dict[str, Any], srcg: dict[str, Any]) -> None:
    columns = (baseline, srcg)
    print("metric\tbaseline\tsrcg")
    for key in (
        "completed",
        "attempts",
        "failed_attempts",
        "boot_count",
        "active_seconds",
        "end_to_end_seconds",
        "between_attempt_seconds",
        "oom_kills",
        "oom_attempts",
        "parent_peak_bytes",
        "executed_steps_including_lost_work",
        "lost_steps_after_latest_checkpoint",
        "checkpoint_write_ms",
        "checkpoint_load_ms",
        "final_step",
        "final_model_sha256",
    ):
        print(f"{key}\t{columns[0][key]}\t{columns[1][key]}")
    hashes = [column["final_model_sha256"] for column in columns]
    quality_equal = all(column["completed"] for column in columns) and hashes[0] == hashes[1]
    print(f"exact_final_model_match\t{quality_equal}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("baseline_run", type=Path)
    parser.add_argument("srcg_run", type=Path)
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()
    try:
        baseline = summarize(args.baseline_run)
        srcg = summarize(args.srcg_run)
        if baseline["mode"] != "baseline":
            raise ValueError(f"{args.baseline_run}: expected baseline mode")
        if srcg["mode"] != "srcg":
            raise ValueError(f"{args.srcg_run}: expected srcg mode")
    except (OSError, ValueError) as error:
        parser.error(str(error))

    if args.as_json:
        print(json.dumps({"baseline": baseline, "srcg": srcg}, indent=2, sort_keys=True))
    else:
        print_human(baseline, srcg)
    return 0 if baseline["completed"] and srcg["completed"] else 2


if __name__ == "__main__":
    sys.exit(main())
