"""Export E8 per-run kernel and training outcomes to an auditable CSV."""

import argparse
import csv
import json
from pathlib import Path


FIELDS = (
    "run", "method", "swap_cap_mib", "repetition", "outcome", "completed_jobs",
    "limits_verified", "ram_limits_verified", "swap_limits_verified",
    "parent_peak_within_swap_cap", "cgroup_oom_kills",
    "parent_memory_peak_mib", "parent_swap_peak_mib", "swap_event_max",
    "swap_event_fail", "swap_pswpin_pages", "swap_pswpout_pages",
    "host_pswpin_pages_global", "host_pswpout_pages_global",
    "memory_psi_some_ms", "memory_psi_full_ms", "makespan_s",
    "observed_duration_s", "admission_wait_events", "watchdog_stall",
    "steps_a", "steps_b", "digest_a", "digest_b", "raw_result",
)


def record(path: Path, result: dict) -> dict:
    def mib(value):
        return round(value / 1_048_576, 3) if value is not None else ""

    def seconds(value):
        return round(value / 1000, 3) if value is not None else ""

    return {
        "run": result["run"],
        "method": result["mode"],
        "swap_cap_mib": result["swap_budget_mib"],
        "repetition": int(result["run"].rsplit("-r", 1)[1]),
        "outcome": result["outcome"],
        "completed_jobs": result["completed_jobs"],
        "limits_verified": result["limits_verified"],
        "ram_limits_verified": result["ram_limits_verified"],
        "swap_limits_verified": result["swap_limits_verified"],
        "parent_peak_within_swap_cap": result["parent_swap_peak_within_budget"],
        "cgroup_oom_kills": result["cgroup_oom_kills"],
        "parent_memory_peak_mib": mib(result["parent_memory_peak_bytes"]),
        "parent_swap_peak_mib": mib(result["parent_swap_peak_bytes"]),
        "swap_event_max": result["swap_limit_events"].get("max", 0),
        "swap_event_fail": result["swap_limit_events"].get("fail", 0),
        "swap_pswpin_pages": result["parent_swap_io_pages"].get("pswpin", 0),
        "swap_pswpout_pages": result["parent_swap_io_pages"].get("pswpout", 0),
        "host_pswpin_pages_global": result["host_swap_io_pages_global"].get("pswpin", 0),
        "host_pswpout_pages_global": result["host_swap_io_pages_global"].get("pswpout", 0),
        "memory_psi_some_ms": round(result["parent_pressure_delta_us"].get("some", 0) / 1000, 3),
        "memory_psi_full_ms": round(result["parent_pressure_delta_us"].get("full", 0) / 1000, 3),
        "makespan_s": seconds(result["makespan_ms"]) if result["outcome"] == "completed" else "",
        "observed_duration_s": seconds(result["observed_run_duration_ms"]),
        "admission_wait_events": result["admission_wait_events"],
        "watchdog_stall": result["watchdog_stall"],
        "steps_a": result["steps"]["a"],
        "steps_b": result["steps"]["b"],
        "digest_a": result["model_digests"]["a"] or "",
        "digest_b": result["model_digests"]["b"] or "",
        "raw_result": str(path),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    records = []
    for path in args.raw_dir.glob("*/result.json"):
        result = json.loads(path.read_text())
        if not result["limits_verified"]:
            continue  # Exclude setup failures; the report identifies them separately.
        records.append(record(path, result))
    records.sort(key=lambda row: (row["method"], row["swap_cap_mib"], row["repetition"]))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(records)


if __name__ == "__main__":
    main()
