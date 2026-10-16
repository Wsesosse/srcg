#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "usage: $0 normal|sequential|srcg [OUTPUT_DIRECTORY]" >&2
  exit 2
}

mode=${1:-}
case "$mode" in
  normal|sequential|srcg) ;;
  *) usage ;;
esac

script_path=$(readlink -f "$0")
project_root=$(cd "$(dirname "$script_path")/../.." && pwd)
output_arg=${2:-"artifacts/milestone3/real-workloads/manual-${mode}-$(date +%Y%m%d-%H%M%S)"}
scope_memory=${SRCG_SCOPE_MEMORY:-6G}
parent_memory=${SRCG_PARENT_MEMORY:-5GiB}
job_base_memory=${SRCG_JOB_BASE_MEMORY:-1660MiB}
reserved_memory=${SRCG_RESERVED_MEMORY:-1800MiB}
host_headroom=${SRCG_HOST_HEADROOM:-1GiB}
probe_memory=${SRCG_PROBE_MEMORY:-}
probe_delay_seconds=${SRCG_PROBE_DELAY_SECONDS:-5}
probe_hold_ms=${SRCG_PROBE_HOLD_MS:-30000}
num_workers=${SRCG_NUM_WORKERS:-0}
prefetch_factor=${SRCG_PREFETCH_FACTOR:-2}
pin_memory=${SRCG_PIN_MEMORY:-0}
if [[ "$output_arg" = /* ]]; then
  output_dir=$output_arg
else
  output_dir=$project_root/$output_arg
fi

if [[ ${SRCG_EXPERIMENT_INNER:-0} != 1 ]]; then
  outer_unit="srcg-experiment-$BASHPID"
  cleanup_outer_unit() {
    systemctl --user stop "$outer_unit.service" >/dev/null 2>&1 || true
  }
  # systemd-run may exit on Ctrl-C while the transient service keeps running.
  # Stop the complete delegated scope so training workers cannot become
  # orphans that retain RAM or VRAM after an interrupted experiment.
  trap cleanup_outer_unit EXIT INT TERM HUP
  set +e
  systemd-run \
    --user \
    --pipe \
    --wait \
    --collect \
    --unit="$outer_unit" \
    --working-directory="$project_root" \
    --property=Delegate=yes \
    --property=MemoryMax="$scope_memory" \
    --property=MemorySwapMax=0 \
    --setenv=SRCG_EXPERIMENT_INNER=1 \
    --setenv=SRCG_MEMORY_TRACE="${SRCG_MEMORY_TRACE:-0}" \
    --setenv=SRCG_RESERVATION_PHASE_MODE="${SRCG_RESERVATION_PHASE_MODE:-auto}" \
    --setenv=SRCG_SCOPE_MEMORY="$scope_memory" \
    --setenv=SRCG_PARENT_MEMORY="$parent_memory" \
    --setenv=SRCG_JOB_BASE_MEMORY="$job_base_memory" \
    --setenv=SRCG_RESERVED_MEMORY="$reserved_memory" \
    --setenv=SRCG_HOST_HEADROOM="$host_headroom" \
    --setenv=SRCG_PROBE_MEMORY="$probe_memory" \
    --setenv=SRCG_PROBE_DELAY_SECONDS="$probe_delay_seconds" \
    --setenv=SRCG_PROBE_HOLD_MS="$probe_hold_ms" \
    --setenv=SRCG_NUM_WORKERS="$num_workers" \
    --setenv=SRCG_PREFETCH_FACTOR="$prefetch_factor" \
    --setenv=SRCG_PIN_MEMORY="$pin_memory" \
    /bin/bash "$script_path" "$mode" "$output_dir"
  outer_status=$?
  set -e
  cleanup_outer_unit
  trap - EXIT INT TERM HUP
  exit "$outer_status"
fi

mkdir -p "$output_dir"

scope_relative=$(sed -n 's/^0:://p' /proc/self/cgroup)
scope=/sys/fs/cgroup$scope_relative
runner_leaf=$scope/runner
mkdir "$runner_leaf"
echo "$BASHPID" > "$runner_leaf/cgroup.procs"

parent=$scope/srcg
srcg=$project_root/build-srcg/srcg
python=$project_root/.venv/bin/python
training=$project_root/benchmarks/dataloader_policy/train.py

"$srcg" init \
  --parent "$parent" \
  --parent-memory "$parent_memory" \
  --parent-swap 0 \
  --host-headroom "$host_headroom" \
  > "$output_dir/init.log"

manager_pid=
if [[ "$mode" == srcg ]]; then
  socket=/tmp/srcg-full-experiment-$BASHPID.sock
  "$srcg" serve \
    --parent "$parent" \
    --socket "$socket" \
    --reserved-memory "$reserved_memory" \
    --host-headroom "$host_headroom" \
    --log "$output_dir/reservations.log" &
  manager_pid=$!
  cleanup_manager() {
    kill "$manager_pid" 2>/dev/null || true
    wait "$manager_pid" 2>/dev/null || true
  }
  trap cleanup_manager EXIT
  for _attempt in $(seq 1 100); do
    [[ -S "$socket" ]] && break
    sleep 0.02
  done
  [[ -S "$socket" ]] || {
    echo "reservation manager socket did not appear" >&2
    exit 1
  }
fi

run_lung() {
  local suffix=$1
  local job="lung-${mode}-${suffix}"
  local -a controller=(
    "$srcg" run
    --parent "$parent"
    --job "$job"
    --swap 0
    --interval-ms 50
    --log "$output_dir/lung-${suffix}-srcg.log"
  )
  if [[ "$mode" == srcg ]]; then
    controller+=(--manager-socket "$socket" --memory "$job_base_memory")
  else
    # Equal to the shared parent: this observes the normal process without a
    # tighter per-job ceiling. The parent is the common experiment boundary.
    controller+=(--memory "$parent_memory")
  fi
  controller+=(
    --
    "$python" "$training" lung
    --batch-size 1
    --epochs 1
    --optimizer adamw
    --lr 0.0001
    --num-workers "$num_workers"
    --device cuda
  )
  if ((num_workers > 0)); then
    controller+=(--prefetch-factor "$prefetch_factor")
  fi
  if [[ "$pin_memory" == 1 ]]; then
    controller+=(--pin-memory)
  fi
  if [[ ${SRCG_MEMORY_TRACE:-0} == 1 ]]; then
    if [[ "$mode" != srcg ]]; then
      echo "SRCG_MEMORY_TRACE=1 requires srcg mode" >&2
      return 2
    fi
    controller+=(--memory-trace)
  fi
  "${controller[@]}" > "$output_dir/lung-${suffix}.jsonl" 2>&1
}

started_ns=$(date +%s%N)
probe_pid=
probe_status=not_requested
if [[ "$mode" == sequential ]]; then
  if [[ -n "$probe_memory" ]]; then
    echo "late-arrival probe is not supported in sequential mode" >&2
    exit 2
  fi
  set +e
  run_lung a
  first_status=$?
  run_lung b
  second_status=$?
  set -e
else
  run_lung a &
  first_pid=$!
  run_lung b &
  second_pid=$!

  if [[ -n "$probe_memory" ]]; then
    sleep "$probe_delay_seconds"
    "$project_root/build-srcg/srcg_memory_probe" \
      "$probe_memory" "$probe_hold_ms" \
      > "$output_dir/sibling-probe.jsonl" 2>&1 &
    probe_pid=$!
  fi

  set +e
  wait "$first_pid"
  first_status=$?
  wait "$second_pid"
  second_status=$?
  if [[ -n "$probe_pid" ]]; then
    wait "$probe_pid"
    probe_status=$?
  fi
  set -e
fi

ended_ns=$(date +%s%N)
makespan_ms=$(((ended_ns - started_ns) / 1000000))
parent_peak_bytes=$(<"$parent/memory.peak")
cp "$parent/memory.events" "$output_dir/parent-memory.events"
cp "$parent/memory.stat" "$output_dir/parent-memory.stat"
cp "$scope/memory.events" "$output_dir/scope-memory.events"
cp "$scope/memory.stat" "$output_dir/scope-memory.stat"
scope_peak_bytes=$(<"$scope/memory.peak")

printf 'mode=%s first_status=%s second_status=%s probe_status=%s makespan_ms=%s parent_peak_bytes=%s scope_peak_bytes=%s scope_memory=%s parent_memory=%s job_base_memory=%s reserved_memory=%s host_headroom=%s probe_memory=%s num_workers=%s prefetch_factor=%s pin_memory=%s reservation_phase_mode=%s\n' \
  "$mode" "$first_status" "$second_status" "$probe_status" "$makespan_ms" \
  "$parent_peak_bytes" "$scope_peak_bytes" "$scope_memory" "$parent_memory" \
  "$job_base_memory" "$reserved_memory" "$host_headroom" "${probe_memory:-none}" \
  "$num_workers" "$prefetch_factor" "$pin_memory" \
  "${SRCG_RESERVATION_PHASE_MODE:-auto}" \
  > "$output_dir/summary.txt"

if [[ -n "$manager_pid" ]]; then
  cleanup_manager
  trap - EXIT
fi

if [[ ${SRCG_MEMORY_TRACE:-0} == 1 && "$mode" == srcg ]]; then
  analyzer=$project_root/benchmarks/dataloader_policy/analyze_memory_trace.py
  for suffix in a b; do
    "$python" "$analyzer" \
      --training-log "$output_dir/lung-${suffix}.jsonl" \
      --controller-log "$output_dir/lung-${suffix}-srcg.log" \
      > "$output_dir/lung-${suffix}-memory-analysis.jsonl"
  done
fi

cat "$output_dir/summary.txt"
echo "artifacts=$output_dir"

if ((first_status != 0 || second_status != 0)) || \
   [[ "$probe_status" != not_requested && "$probe_status" != 0 ]]; then
  exit 1
fi
