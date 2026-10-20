"""Sample E8's live cgroups before transient jobs/services disappear."""

import argparse
import json
from pathlib import Path
import re
import signal
import subprocess
import time


stop = False


def request_stop(_signal, _frame):
    global stop
    stop = True


def numbers(path: Path) -> dict[str, int]:
    return {key: int(value) for key, value in (line.split()[:2] for line in path.read_text().splitlines() if len(line.split()) >= 2)}


def pressure(path: Path) -> dict[str, int]:
    result = {}
    for line in path.read_text().splitlines():
        parts = line.split()
        if parts:
            for field in parts[1:]:
                if field.startswith("total="):
                    result[parts[0]] = int(field.split("=", 1)[1])
    return result


def cgroup_sample(path: Path) -> dict | None:
    try:
        swap_limit = (path / "memory.swap.max").read_text().strip()
        swap_current = int((path / "memory.swap.current").read_text().strip())
        swap_peak = int((path / "memory.swap.peak").read_text().strip())
        memory_peak = int((path / "memory.peak").read_text().strip())
        return {
            "swap_max": swap_limit,
            "swap_current": swap_current,
            "swap_peak": swap_peak,
            "memory_peak": memory_peak,
            "swap_events": numbers(path / "memory.swap.events"),
            "memory_events": numbers(path / "memory.events"),
            "memory_stat": {key: value for key, value in numbers(path / "memory.stat").items() if key in {"pswpin", "pswpout", "zswpin", "zswpout", "zswpwb", "pgscan", "pgsteal"}},
            "pressure": pressure(path / "memory.pressure"),
        }
    except (FileNotFoundError, ValueError, PermissionError):
        return None


def find_scope(console: Path) -> Path | None:
    try:
        match = re.search(r"Running as unit: (srcg-experiment-[A-Za-z0-9-]+\.service)", console.read_text())
    except FileNotFoundError:
        return None
    if not match:
        return None
    show = subprocess.run(
        ["systemctl", "--user", "show", match.group(1), "-p", "ControlGroup", "--value"],
        text=True,
        capture_output=True,
        check=False,
    )
    group = show.stdout.strip()
    if show.returncode != 0 or not group.startswith("/"):
        return None
    return Path("/sys/fs/cgroup") / group.lstrip("/")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--console", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mode", choices=("normal", "srcg"), required=True)
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    scope = None
    with args.output.open("w") as output:
        while not stop:
            if scope is None:
                scope = find_scope(args.console)
            if scope is not None:
                parent = scope / "srcg"
                row = {
                    "monotonic_ns": time.monotonic_ns(),
                    "scope": cgroup_sample(scope),
                    "parent": cgroup_sample(parent),
                    "job_a": cgroup_sample(parent / f"lung-{args.mode}-a"),
                    "job_b": cgroup_sample(parent / f"lung-{args.mode}-b"),
                }
                if any(row[key] is not None for key in ("scope", "parent", "job_a", "job_b")):
                    output.write(json.dumps(row, separators=(",", ":")) + "\n")
                    output.flush()
            time.sleep(0.05)


if __name__ == "__main__":
    main()
