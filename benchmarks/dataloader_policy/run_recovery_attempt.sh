#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
usage: run_recovery_attempt.sh baseline|srcg RUN_DIRECTORY

Run one complete-or-OOM attempt of the single-job Task06_Lung recovery
experiment. RUN_DIRECTORY persists checkpoints and attempt metadata across
boots. Invoke the script once after each reboot until result=completed.

Environment overrides used for calibration:
  SRCG_RECOVERY_SCOPE_MEMORY       enclosing scope (default 6G)
  SRCG_RECOVERY_PARENT_MEMORY      one-job training boundary (default 3840MiB)
  SRCG_RECOVERY_JOB_BASE_MEMORY    SRCG base quota (default 1660MiB)
  SRCG_RECOVERY_RESERVED_MEMORY    batch borrowing pool (default 1924MiB)
  SRCG_RECOVERY_CHECKPOINT_BUDGET  protected checkpoint capacity (default 256MiB)
  SRCG_RECOVERY_HOST_HEADROOM      protected ancestor capacity (default 1GiB)
  SRCG_RECOVERY_CHECKPOINT_STEPS   periodic interval (default 5)
  SRCG_RECOVERY_NUM_WORKERS        DataLoader workers (default 4)
  SRCG_RECOVERY_PREFETCH_FACTOR    batches per worker (default 2)
  SRCG_RECOVERY_VOLUME_DEPTH       resized Lung depth (default 32)
  SRCG_RECOVERY_MAX_BOOT_AGE       0 disables boot-age guard (default 0)
  SRCG_RECOVERY_FAIL_AFTER_STEP    deterministic validation fault (default 0)
EOF
  exit 2
}

mode=${1:-}
case "$mode" in
  baseline|srcg) ;;
  *) usage ;;
esac
[[ $# -eq 2 ]] || usage

script_path=$(readlink -f "$0")
script_dir=$(dirname "$script_path")
project_root=$(cd "$script_dir/../.." && pwd)
run_arg=$2
if [[ "$run_arg" = /* ]]; then
  run_dir=$run_arg
else
  run_dir=$project_root/$run_arg
fi

scope_memory=${SRCG_RECOVERY_SCOPE_MEMORY:-6G}
parent_memory=${SRCG_RECOVERY_PARENT_MEMORY:-3840MiB}
job_base_memory=${SRCG_RECOVERY_JOB_BASE_MEMORY:-1660MiB}
reserved_memory=${SRCG_RECOVERY_RESERVED_MEMORY:-1924MiB}
checkpoint_budget=${SRCG_RECOVERY_CHECKPOINT_BUDGET:-256MiB}
host_headroom=${SRCG_RECOVERY_HOST_HEADROOM:-1GiB}
checkpoint_steps=${SRCG_RECOVERY_CHECKPOINT_STEPS:-5}
num_workers=${SRCG_RECOVERY_NUM_WORKERS:-4}
prefetch_factor=${SRCG_RECOVERY_PREFETCH_FACTOR:-2}
volume_depth=${SRCG_RECOVERY_VOLUME_DEPTH:-32}
max_boot_age=${SRCG_RECOVERY_MAX_BOOT_AGE:-0}
fail_after_step=${SRCG_RECOVERY_FAIL_AFTER_STEP:-0}

if [[ ${SRCG_RECOVERY_INNER:-0} != 1 ]]; then
  if ! systemctl --user show-environment >/dev/null 2>&1; then
    echo "recovery attempt refused: cannot connect to the user systemd bus" >&2
    exit 4
  fi
  if ((max_boot_age > 0)); then
    read -r uptime_value _ < /proc/uptime
    boot_age=${uptime_value%%.*}
    if ((boot_age > max_boot_age)); then
      echo "recovery attempt refused: boot age ${boot_age}s exceeds ${max_boot_age}s" >&2
      exit 3
    fi
  fi
  outer_unit="srcg-recovery-$BASHPID"
  cleanup_outer_unit() {
    systemctl --user stop "$outer_unit.service" >/dev/null 2>&1 || true
  }
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
    --setenv=SRCG_RECOVERY_INNER=1 \
    --setenv=SRCG_RECOVERY_SCOPE_MEMORY="$scope_memory" \
    --setenv=SRCG_RECOVERY_PARENT_MEMORY="$parent_memory" \
    --setenv=SRCG_RECOVERY_JOB_BASE_MEMORY="$job_base_memory" \
    --setenv=SRCG_RECOVERY_RESERVED_MEMORY="$reserved_memory" \
    --setenv=SRCG_RECOVERY_CHECKPOINT_BUDGET="$checkpoint_budget" \
    --setenv=SRCG_RECOVERY_HOST_HEADROOM="$host_headroom" \
    --setenv=SRCG_RECOVERY_CHECKPOINT_STEPS="$checkpoint_steps" \
    --setenv=SRCG_RECOVERY_NUM_WORKERS="$num_workers" \
    --setenv=SRCG_RECOVERY_PREFETCH_FACTOR="$prefetch_factor" \
    --setenv=SRCG_RECOVERY_VOLUME_DEPTH="$volume_depth" \
    --setenv=SRCG_RECOVERY_MAX_BOOT_AGE="$max_boot_age" \
    --setenv=SRCG_RECOVERY_FAIL_AFTER_STEP="$fail_after_step" \
    /bin/bash "$script_path" "$mode" "$run_dir"
  outer_status=$?
  set -e
  cleanup_outer_unit
  trap - EXIT INT TERM HUP
  exit "$outer_status"
fi

mkdir -p "$run_dir/attempts" "$run_dir/checkpoints"
config_line="mode=$mode scope_memory=$scope_memory parent_memory=$parent_memory job_base_memory=$job_base_memory reserved_memory=$reserved_memory checkpoint_budget=$checkpoint_budget host_headroom=$host_headroom checkpoint_steps=$checkpoint_steps num_workers=$num_workers prefetch_factor=$prefetch_factor volume_depth=$volume_depth"
if [[ -f "$run_dir/config.txt" ]]; then
  if [[ $(<"$run_dir/config.txt") != "$config_line" ]]; then
    echo "recovery attempt refused: configuration differs from the first attempt" >&2
    echo "recorded: $(<"$run_dir/config.txt")" >&2
    echo "current:  $config_line" >&2
    exit 3
  fi
else
  printf '%s\n' "$config_line" > "$run_dir/config.txt"
fi

boot_id=$(tr -d '[:space:]' < /proc/sys/kernel/random/boot_id)
if find "$run_dir/attempts" -mindepth 1 -maxdepth 1 -type d \
    -name "*-$boot_id" -print -quit | grep -q .; then
  echo "recovery attempt refused: run directory already records boot $boot_id" >&2
  exit 3
fi

attempt_count=$(find "$run_dir/attempts" -mindepth 1 -maxdepth 1 -type d | wc -l)
attempt_number=$((attempt_count + 1))
attempt_name=$(printf 'attempt-%04d-%s' "$attempt_number" "$boot_id")
attempt_dir=$run_dir/attempts/$attempt_name
mkdir "$attempt_dir"

if [[ ! -f "$run_dir/first-start-unix-ns.txt" ]]; then
  date +%s%N > "$run_dir/first-start-unix-ns.txt"
fi
started_unix_ns=$(date +%s%N)
read -r uptime_value _ < /proc/uptime
boot_age_seconds=${uptime_value%%.*}
checkpoint_before=none
if [[ -f "$run_dir/checkpoints/latest.json" ]]; then
  checkpoint_before=$(jq -r '.step' "$run_dir/checkpoints/latest.json")
fi

scope_relative=$(sed -n 's/^0:://p' /proc/self/cgroup)
scope=/sys/fs/cgroup$scope_relative
runner_leaf=$scope/runner
mkdir "$runner_leaf"
echo "$BASHPID" > "$runner_leaf/cgroup.procs"

parent=$scope/srcg
srcg=$project_root/build-srcg/srcg
python=$project_root/.venv/bin/python
training=$script_dir/train.py

"$srcg" init \
  --parent "$parent" \
  --parent-memory "$parent_memory" \
  --parent-swap 0 \
  --host-headroom "$host_headroom" \
  > "$attempt_dir/init.log"

manager_pid=
socket=
if [[ "$mode" == srcg ]]; then
  socket=/tmp/srcg-recovery-$BASHPID.sock
  "$srcg" serve \
    --parent "$parent" \
    --socket "$socket" \
    --reserved-memory "$reserved_memory" \
    --checkpoint-budget "$checkpoint_budget" \
    --host-headroom "$host_headroom" \
    --log "$attempt_dir/reservations.log" &
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

job_memory=$parent_memory
if [[ "$mode" == srcg ]]; then
  job_memory=$job_base_memory
fi
controller=(
  "$srcg" run
  --parent "$parent"
  --job "lung-recovery-$mode"
  --memory "$job_memory"
  --swap 0
  --interval-ms 50
  --log "$attempt_dir/controller.log"
  --checkpoint-dir "$run_dir/checkpoints"
)
if [[ "$mode" == srcg ]]; then
  controller+=(--manager-socket "$socket")
fi
controller+=(
  --
  "$python" "$training" lung
  --batch-size 1
  --epochs 1
  --optimizer adamw
  --lr 0.0001
  --num-workers "$num_workers"
  --prefetch-factor "$prefetch_factor"
  --volume-size "$volume_depth" 128 128
  --device cuda
  --checkpoint-every-steps "$checkpoint_steps"
)
if ((fail_after_step > 0)); then
  controller+=(--fail-after-checkpoint-step "$fail_after_step")
fi

set +e
"${controller[@]}" > "$attempt_dir/training.jsonl" 2>&1
status=$?
set -e

ended_unix_ns=$(date +%s%N)
cp "$parent/memory.events" "$attempt_dir/parent-memory.events"
cp "$parent/memory.stat" "$attempt_dir/parent-memory.stat"
parent_peak_bytes=$(<"$parent/memory.peak")
cp "$scope/memory.events" "$attempt_dir/scope-memory.events"
scope_peak_bytes=$(<"$scope/memory.peak")
checkpoint_after=none
if [[ -f "$run_dir/checkpoints/latest.json" ]]; then
  checkpoint_after=$(jq -r '.step' "$run_dir/checkpoints/latest.json")
fi
last_completed_step=$(jq -Rr 'fromjson? | select(.event == "step_end") | .step' \
  "$attempt_dir/training.jsonl" | tail -n 1)
last_completed_step=${last_completed_step:-0}
classification=$(sed -n 's/.*classification=\([^ ]*\).*/\1/p' \
  "$attempt_dir/controller.log" | tail -n 1)
classification=${classification:-unknown}
oom=$(awk '$1 == "oom" {print $2}' "$attempt_dir/parent-memory.events")
oom_kill=$(awk '$1 == "oom_kill" {print $2}' "$attempt_dir/parent-memory.events")
result=failed
if ((status == 0)); then
  result=completed
fi

summary="attempt=$attempt_number mode=$mode result=$result exit_status=$status classification=$classification boot_id=$boot_id boot_age_seconds=$boot_age_seconds started_unix_ns=$started_unix_ns ended_unix_ns=$ended_unix_ns checkpoint_before_step=$checkpoint_before checkpoint_after_step=$checkpoint_after last_completed_step=$last_completed_step parent_peak_bytes=$parent_peak_bytes scope_peak_bytes=$scope_peak_bytes oom=$oom oom_kill=$oom_kill"
printf '%s\n' "$summary" > "$attempt_dir/summary.txt"
printf '%s\n' "$summary" >> "$run_dir/attempts.log"
if [[ "$result" == completed ]]; then
  printf '%s\n' "$ended_unix_ns" > "$run_dir/completed-unix-ns.txt"
fi

if [[ -n "$manager_pid" ]]; then
  cleanup_manager
  trap - EXIT
fi

printf '%s\n' "$summary"
printf 'artifacts=%s\n' "$attempt_dir"
exit "$status"
