#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
usage: run_safety_experiment.sh SCENARIO [OUTPUT_DIRECTORY]

SCENARIO:
  capacity-normal  Two normal full Lung jobs share only 4 GiB.
  capacity-sequential  Run those normal Lung jobs one after the other.
  capacity-srcg    The same jobs use SRCG admission inside 4 GiB.
  late-normal      Two normal jobs plus a 512 MiB sibling arriving after 5 s.
  late-srcg        SRCG has a 4 GiB parent; the sibling uses the 1 GiB outside it.
  pressure-normal  Normal concurrent timing with enough RAM (8 GiB parent).
  pressure-srcg    SRCG concurrent timing with the same 8 GiB parent.
  high-sequential  High-pressure baseline: two normal jobs run sequentially.
  high-srcg        High-pressure candidate: two jobs run concurrently via SRCG.

The normal scenarios are expected to return a non-zero status when Linux OOM-kills
the training jobs. Their logs are still complete experiment results.
EOF
  exit 2
}

scenario=${1:-}
script_path=$(readlink -f "$0")
script_dir=$(dirname "$script_path")
project_root=$(cd "$script_dir/../.." && pwd)
runner=$script_dir/run_full_experiment.sh
timestamp=$(date +%Y%m%d-%H%M%S)
scope_memory=5G
job_base_memory=1400MiB
reserved_memory=1296MiB
num_workers=0
prefetch_factor=2

case "$scenario" in
  capacity-normal)
    mode=normal
    default_output="artifacts/milestone3/safety/capacity-normal-$timestamp"
    parent_memory=4GiB
    host_headroom=1GiB
    probe_memory=
    ;;
  capacity-srcg)
    mode=srcg
    default_output="artifacts/milestone3/safety/capacity-srcg-$timestamp"
    parent_memory=4GiB
    host_headroom=1GiB
    probe_memory=
    ;;
  capacity-sequential)
    mode=sequential
    default_output="artifacts/milestone3/safety/capacity-sequential-$timestamp"
    parent_memory=4GiB
    host_headroom=1GiB
    probe_memory=
    ;;
  late-normal)
    mode=normal
    default_output="artifacts/milestone3/safety/late-normal-$timestamp"
    parent_memory=5GiB
    host_headroom=0
    probe_memory=512MiB
    ;;
  late-srcg)
    mode=srcg
    default_output="artifacts/milestone3/safety/late-srcg-$timestamp"
    parent_memory=4GiB
    host_headroom=1GiB
    probe_memory=512MiB
    ;;
  pressure-normal)
    mode=normal
    default_output="artifacts/milestone3/timing/pressure-normal-$timestamp"
    scope_memory=9G
    parent_memory=8GiB
    job_base_memory=3000MiB
    reserved_memory=2192MiB
    host_headroom=1GiB
    num_workers=2
    probe_memory=
    ;;
  pressure-srcg)
    mode=srcg
    default_output="artifacts/milestone3/timing/pressure-srcg-$timestamp"
    scope_memory=9G
    parent_memory=8GiB
    job_base_memory=3000MiB
    reserved_memory=2192MiB
    host_headroom=1GiB
    num_workers=2
    probe_memory=
    ;;
  high-sequential)
    mode=sequential
    default_output="artifacts/milestone3/timing/high-sequential-$timestamp"
    # parent (5 GiB) + protected headroom (1 GiB) + space for the manager,
    # controllers, and runner processes that live in the enclosing scope.
    scope_memory=7G
    parent_memory=5GiB
    job_base_memory=1660MiB
    reserved_memory=1800MiB
    host_headroom=1GiB
    num_workers=2
    probe_memory=
    ;;
  high-srcg)
    mode=srcg
    default_output="artifacts/milestone3/timing/high-srcg-$timestamp"
    scope_memory=7G
    parent_memory=5GiB
    job_base_memory=1660MiB
    reserved_memory=1800MiB
    host_headroom=1GiB
    num_workers=2
    probe_memory=
    ;;
  *) usage ;;
esac

output_dir=${2:-$default_output}

exec env \
  SRCG_SCOPE_MEMORY="$scope_memory" \
  SRCG_PARENT_MEMORY="$parent_memory" \
  SRCG_JOB_BASE_MEMORY="$job_base_memory" \
  SRCG_RESERVED_MEMORY="$reserved_memory" \
  SRCG_HOST_HEADROOM="$host_headroom" \
  SRCG_NUM_WORKERS="$num_workers" \
  SRCG_PREFETCH_FACTOR="$prefetch_factor" \
  SRCG_PROBE_MEMORY="$probe_memory" \
  SRCG_PROBE_DELAY_SECONDS=5 \
  SRCG_PROBE_HOLD_MS=30000 \
  bash "$runner" "$mode" "$output_dir"
