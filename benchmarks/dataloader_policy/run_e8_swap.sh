#!/usr/bin/env bash
set -euo pipefail

if [[ $# != 3 || ( $1 != normal && $1 != srcg ) || ! $2 =~ ^[0-9]+$ || ! $3 =~ ^[1-9][0-9]*$ ]]; then
  echo "usage: $0 normal|srcg SWAP_MiB REPETITION" >&2
  exit 2
fi
mode=$1
swap_mib=$2
repetition=$3
if ((swap_mib > 2048 || swap_mib % 64 != 0)); then
  echo "SWAP_MiB must be multiple of 64 between 0 and 2048" >&2
  exit 2
fi

project_root=$(cd "$(dirname "$(readlink -f "$0")")/../.." && pwd)
output_dir=$project_root/artifacts/e8-swap-20261020/$mode-${swap_mib}m-r$repetition
[[ ! -e $output_dir ]] || { echo "output exists: $output_dir" >&2; exit 2; }
[[ -d $project_root/ML-Test-Dataset/Task06_Lung ]] || { echo "Task06_Lung missing" >&2; exit 2; }

swap_bytes=$((swap_mib * 1024 * 1024))
host_swap_total_kib=$(awk '$1=="SwapTotal:" {print $2}' /proc/meminfo)
host_swap_free_kib=$(awk '$1=="SwapFree:" {print $2}' /proc/meminfo)
if ((host_swap_total_kib * 1024 < swap_bytes || host_swap_free_kib * 1024 < swap_bytes + 256 * 1024 * 1024)); then
  echo "insufficient host swap: requested=$swap_bytes free_kib=$host_swap_free_kib" >&2
  exit 2
fi
mkdir -p "$output_dir"

{
  echo "mode=$mode swap_mib=$swap_mib repetition=$repetition outer_ram=8GiB parent_ram=6GiB outer_swap_bytes=$swap_bytes parent_swap_bytes=$swap_bytes job_swap_bytes=$swap_bytes"
  echo "host_swap_total_kib=$host_swap_total_kib host_swap_free_kib=$host_swap_free_kib"
  echo "dataset=Task06_Lung volumes=63 model=3D_UNet_base32 batch=1 workers_per_job=6 prefetch=2 pin_memory=0 seed=42 epochs=1 optimizer=AdamW lr=0.0001 checkpoint=off"
  echo "srcg_base=2GiB srcg_reserved_memory=2GiB host_headroom=1GiB"
  echo "boot_id=$(< /proc/sys/kernel/random/boot_id) kernel=$(uname -r) started_utc=$(date -u +%FT%TZ)"
  echo "zswap_enabled=$(< /sys/module/zswap/parameters/enabled)"
  echo "swappiness=$(< /proc/sys/vm/swappiness)"
  echo "swapon_devices:"
  swapon --show --bytes --output NAME,TYPE,SIZE,USED,PRIO
  for zram in /sys/block/zram*; do
    [[ -d $zram ]] || continue
    echo "zram_device=$zram disksize=$(< "$zram/disksize") comp_algorithm=$(< "$zram/comp_algorithm")"
    echo "zram_mm_stat=$(< "$zram/mm_stat")"
  done
  echo "gpu=$(nvidia-smi --query-gpu=name,memory.total,memory.used --format=csv,noheader)"
  echo "git_commit=$(git -C "$project_root" rev-parse HEAD)"
} > "$output_dir/environment.txt"
cp /proc/vmstat "$output_dir/host-vmstat-before.txt"

set +e
timeout --signal=TERM --kill-after=15s 300s env \
  SRCG_SCOPE_MEMORY=8589934592 SRCG_PARENT_MEMORY=6GiB \
  SRCG_SCOPE_SWAP="$swap_bytes" SRCG_PARENT_SWAP="$swap_bytes" SRCG_JOB_SWAP="$swap_bytes" \
  SRCG_JOB_BASE_MEMORY=2GiB SRCG_RESERVED_MEMORY=2GiB SRCG_HOST_HEADROOM=1GiB \
  SRCG_JOB_COUNT=2 SRCG_BATCH_SIZE=1 SRCG_NUM_WORKERS=6 \
  SRCG_PREFETCH_FACTOR=2 SRCG_PIN_MEMORY=0 \
  bash "$project_root/benchmarks/dataloader_policy/run_full_experiment.sh" \
  "$mode" "$output_dir" > "$output_dir/console.log" 2>&1 &
runner_pid=$!
"$project_root/.venv/bin/python" "$project_root/benchmarks/dataloader_policy/monitor_swap_cgroups.py" \
  --console "$output_dir/console.log" --output "$output_dir/cgroup-trace.jsonl" \
  --mode "$mode" > "$output_dir/monitor.log" 2>&1 &
monitor_pid=$!
set -e

last_steps=-1
last_progress_seconds=$SECONDS
stall_detected=0
while kill -0 "$runner_pid" 2>/dev/null; do
  sleep 5
  steps=0
  for suffix in a b; do
    if [[ -f $output_dir/lung-$suffix.jsonl ]]; then
      count=$(rg -c '"event": "step_end"' "$output_dir/lung-$suffix.jsonl" || true)
      steps=$((steps + ${count:-0}))
    fi
  done
  if ((steps != last_steps)); then
    last_steps=$steps
    last_progress_seconds=$SECONDS
  fi
  if [[ $mode == srcg && -f $output_dir/reservations.log ]] && \
     ((SECONDS - last_progress_seconds >= 45 && SECONDS >= 30)); then
    waits=$(rg -c 'decision=WAIT reason=reserved_memory_exhausted' "$output_dir/reservations.log" || true)
    if ((${waits:-0} >= 10)); then
      stall_detected=1
      kill -TERM "$runner_pid" 2>/dev/null || true
      break
    fi
  fi
done

set +e
wait "$runner_pid"
runner_status=$?
kill -TERM "$monitor_pid" 2>/dev/null || true
wait "$monitor_pid"
monitor_status=$?
set -e
cp /proc/vmstat "$output_dir/host-vmstat-after.txt"
{
  echo "host_swap_free_kib_after=$(awk '$1=="SwapFree:" {print $2}' /proc/meminfo)"
  swapon --show --bytes --output NAME,TYPE,SIZE,USED,PRIO
} > "$output_dir/host-swap-after.txt"
printf 'runner_status=%s monitor_status=%s watchdog_stall=%s last_total_steps=%s\n' \
  "$runner_status" "$monitor_status" "$stall_detected" "$last_steps" \
  > "$output_dir/e8-runner.txt"
"$project_root/.venv/bin/python" "$project_root/benchmarks/dataloader_policy/summarize_e8_swap.py" \
  --run "$output_dir" --expected-swap-bytes "$swap_bytes" \
  > "$output_dir/result.json"
rg '"(run|outcome|completed_jobs|limits_verified|parent_swap_peak_bytes|cgroup_oom_kills|makespan_ms)"' "$output_dir/result.json"
"$project_root/.venv/bin/python" -c 'import json,sys; r=json.load(open(sys.argv[1])); sys.exit(0 if r["limits_verified"] and r["monitor_status"]=="0" else 2)' "$output_dir/result.json"
