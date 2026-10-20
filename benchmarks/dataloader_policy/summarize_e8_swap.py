"""Summarize one E8 run; keep failures distinct from admission WAIT and swap events."""

import argparse
import json
from pathlib import Path
import re


EXPECTED_MODEL = "3502fe809eb9d75055dab6c4bc5fa0a032bc850a09d711deb3e427c76882767e"


def kv_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    return dict(part.split("=", 1) for part in path.read_text().split() if "=" in part)


def counter_file(path: Path) -> dict[str, int]:
    if not path.exists():
        return {}
    result = {}
    for line in path.read_text().splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].isdigit():
            result[parts[0]] = int(parts[1])
    return result


def training_log(path: Path) -> dict:
    result = {"steps": 0, "training_complete": False, "model_digest": None, "reserve_wait_events": 0}
    if not path.exists():
        return result
    for line in path.read_text().splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if item.get("event") == "step_end":
            result["steps"] += 1
        elif item.get("event") == "training_end" and item.get("reason") == "epochs_complete":
            result["training_complete"] = item.get("steps") == 63
        elif item.get("event") == "performance_summary":
            result["model_digest"] = item.get("final_model_sha256")
            result["reserve_wait_events"] = item.get("reserve_wait_events", 0)
    return result


def trace_stats(rows: list[dict], level: str) -> dict:
    samples = [row[level] for row in rows if row.get(level) is not None]
    if not samples:
        return {"seen": False}
    first, last = samples[0], samples[-1]
    return {
        "seen": True,
        "swap_max_values": sorted(set(sample["swap_max"] for sample in samples)),
        "swap_peak_bytes": max(sample["swap_peak"] for sample in samples),
        "swap_current_peak_sampled_bytes": max(sample["swap_current"] for sample in samples),
        "memory_peak_bytes": max(sample["memory_peak"] for sample in samples),
        "swap_events": {key: max(sample["swap_events"].get(key, 0) for sample in samples) for key in ("high", "max", "fail")},
        "memory_events": {key: max(sample["memory_events"].get(key, 0) for sample in samples) for key in ("oom", "oom_kill")},
        "swap_io_pages": {key: max(sample["memory_stat"].get(key, 0) for sample in samples) for key in ("pswpin", "pswpout")},
        "pressure_delta_us": {key: max(0, last["pressure"].get(key, 0) - first["pressure"].get(key, 0)) for key in ("some", "full")},
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--expected-swap-bytes", type=int, required=True)
    args = parser.parse_args()
    run = args.run
    environment = kv_file(run / "environment.txt")
    summary = kv_file(run / "summary.txt")
    runner = kv_file(run / "e8-runner.txt")
    init_log = (run / "init.log").read_text() if (run / "init.log").exists() else ""
    parent_ram_match = re.search(r"\bmemory_max=(\d+)", init_log)
    outer_ram_match = re.search(r"\bheadroom_boundary_bytes=(\d+)", init_log)
    ram_limits_verified = (
        parent_ram_match is not None and int(parent_ram_match.group(1)) == 6 * 1024**3
        and outer_ram_match is not None and int(outer_ram_match.group(1)) == 8 * 1024**3
    )
    mode = environment.get("mode", "unknown")
    rows = []
    if (run / "cgroup-trace.jsonl").exists():
        for line in (run / "cgroup-trace.jsonl").read_text().splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    trace_duration_ms = (
        (rows[-1]["monotonic_ns"] - rows[0]["monotonic_ns"]) // 1_000_000
        if len(rows) >= 2 else None
    )
    levels = {name: trace_stats(rows, name) for name in ("scope", "parent", "job_a", "job_b")}
    expected = str(args.expected_swap_bytes)
    swap_limits_verified = all(levels[name].get("swap_max_values") == [expected] for name in levels)
    limits_verified = swap_limits_verified and ram_limits_verified
    sibling_swap_current_sum_peak = max(
        (
            row["job_a"]["swap_current"] + row["job_b"]["swap_current"]
            for row in rows
            if row.get("job_a") is not None and row.get("job_b") is not None
        ),
        default=None,
    )
    parent_swap = levels["parent"].get("swap_peak_bytes", 0)
    parent_swap_events = levels["parent"].get("swap_events", {})
    parent_memory_events = levels["parent"].get("memory_events", {})
    if (run / "parent-memory.swap.events").exists():
        parent_swap_events = counter_file(run / "parent-memory.swap.events")
        parent_memory_events = counter_file(run / "parent-memory.events")
        parent_swap = max(parent_swap, int((run / "parent-memory.swap.peak").read_text().strip()))
    scope_memory_events = levels["scope"].get("memory_events", {})
    if (run / "scope-memory.events").exists():
        scope_memory_events = counter_file(run / "scope-memory.events")
    jobs = {suffix: training_log(run / f"lung-{suffix}.jsonl") for suffix in ("a", "b")}
    complete = (
        summary.get("first_status") == "0"
        and summary.get("second_status") == "0"
        and all(job["training_complete"] and job["steps"] == 63 and job["model_digest"] == EXPECTED_MODEL for job in jobs.values())
    )
    oom_kills = max(parent_memory_events.get("oom_kill", 0), scope_memory_events.get("oom_kill", 0))
    wait_count = 0
    if (run / "reservations.log").exists():
        wait_count = sum("event=reserve " in line and "decision=WAIT" in line for line in (run / "reservations.log").read_text().splitlines())
    if not limits_verified:
        outcome = "invalid_limits_or_missing_trace"
    elif complete:
        outcome = "completed"
    elif runner.get("watchdog_stall") == "1" and oom_kills == 0:
        outcome = "admission_stall"
    elif oom_kills > 0:
        outcome = "cgroup_oom"
    elif runner.get("runner_status") == "124":
        outcome = "timeout_no_oom"
    else:
        outcome = "failed_other"
    host_before = counter_file(run / "host-vmstat-before.txt")
    host_after = counter_file(run / "host-vmstat-after.txt")
    host_io = {key: max(0, host_after.get(key, 0) - host_before.get(key, 0)) for key in ("pswpin", "pswpout")}
    result = {
        "run": run.name,
        "mode": mode,
        "swap_budget_mib": args.expected_swap_bytes // (1024 * 1024),
        "outcome": outcome,
        "limits_verified": limits_verified,
        "ram_limits_verified": ram_limits_verified,
        "swap_limits_verified": swap_limits_verified,
        "hierarchical_parent_swap_budget_bytes": args.expected_swap_bytes if limits_verified else None,
        "sibling_swap_current_sum_peak_sampled_bytes": sibling_swap_current_sum_peak,
        "sibling_swap_sum_sample_is_atomic": False,
        "completed_jobs": sum(job["training_complete"] and job["model_digest"] == EXPECTED_MODEL for job in jobs.values()),
        "steps": {suffix: jobs[suffix]["steps"] for suffix in jobs},
        "model_digests": {suffix: jobs[suffix]["model_digest"] for suffix in jobs},
        "cgroup_oom_kills": oom_kills,
        "swap_limit_events": parent_swap_events,
        "parent_memory_peak_bytes": int(summary["parent_peak_bytes"]) if summary.get("parent_peak_bytes") else levels["parent"].get("memory_peak_bytes"),
        "parent_swap_peak_bytes": parent_swap,
        "parent_swap_peak_within_budget": parent_swap <= args.expected_swap_bytes if limits_verified else None,
        "parent_swap_io_pages": levels["parent"].get("swap_io_pages", {}),
        "parent_pressure_delta_us": levels["parent"].get("pressure_delta_us", {}),
        "host_swap_io_pages_global": host_io,
        "makespan_ms": int(summary["makespan_ms"]) if summary.get("makespan_ms") else None,
        "observed_run_duration_ms": int(summary["makespan_ms"]) if summary.get("makespan_ms") else trace_duration_ms,
        "admission_wait_events": wait_count,
        "watchdog_stall": runner.get("watchdog_stall") == "1",
        "runner_status": runner.get("runner_status"),
        "monitor_status": runner.get("monitor_status"),
        "levels": levels,
    }
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
