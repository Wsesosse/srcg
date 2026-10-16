#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
usage: run_cold_boot_comparison.sh capacity-sequential|capacity-srcg|high-sequential|high-srcg [OUTPUT_DIRECTORY]

Run this immediately after reboot. The guard permits only one recorded comparison
run per Linux boot and rejects a boot older than SRCG_MAX_BOOT_AGE_SECONDS (default
600 seconds). Sequential and SRCG must be collected on separate reboots.
EOF
  exit 2
}

scenario=${1:-}
case "$scenario" in
  capacity-sequential|capacity-srcg|high-sequential|high-srcg) ;;
  *) usage ;;
esac

script_path=$(readlink -f "$0")
script_dir=$(dirname "$script_path")
project_root=$(cd "$script_dir/../.." && pwd)
boot_id=$(tr -d '[:space:]' < /proc/sys/kernel/random/boot_id)
boot_age_seconds=${SECONDS:-0}
if [[ -r /proc/uptime ]]; then
  read -r uptime_value _ < /proc/uptime
  boot_age_seconds=${uptime_value%%.*}
fi
max_boot_age_seconds=${SRCG_MAX_BOOT_AGE_SECONDS:-600}
if ((boot_age_seconds > max_boot_age_seconds)); then
  echo "cold-boot comparison refused: boot age ${boot_age_seconds}s exceeds ${max_boot_age_seconds}s" >&2
  echo "reboot, then run this command before accessing the dataset" >&2
  exit 3
fi

# Check the systemd transport before consuming this boot. In restricted shells
# systemd-run can fail before the experiment process exists; that failure has
# not touched the dataset and must not falsely mark a cold boot as used.
if ! systemctl --user show-environment >/dev/null 2>&1; then
  echo "cold-boot comparison refused: cannot connect to the user systemd bus" >&2
  echo "run the command from a session with access to the user systemd manager" >&2
  exit 4
fi

state_dir=$project_root/artifacts/milestone3/cold-boot
mkdir -p "$state_dir"
last_boot_file=$state_dir/last-recorded-boot-id.txt
if [[ -f "$last_boot_file" ]] && [[ $(<"$last_boot_file") == "$boot_id" ]]; then
  echo "cold-boot comparison refused: boot $boot_id already has a recorded run" >&2
  echo "reboot before collecting the next mode" >&2
  exit 3
fi

output_arg=${2:-"artifacts/milestone3/cold-boot/${scenario}-${boot_id}"}
if [[ "$output_arg" = /* ]]; then
  output_dir=$output_arg
else
  output_dir=$project_root/$output_arg
fi
mkdir -p "$output_dir"

# Consume the boot before starting. A failed or interrupted run must not be
# silently retried on a now-warm cache and presented as a cold-boot result.
printf '%s\n' "$boot_id" > "$last_boot_file"
{
  printf 'scenario=%s\n' "$scenario"
  printf 'boot_id=%s\n' "$boot_id"
  printf 'boot_age_seconds_at_start=%s\n' "$boot_age_seconds"
  printf 'max_boot_age_seconds=%s\n' "$max_boot_age_seconds"
  printf 'started_utc=%s\n' "$(date --utc +%Y-%m-%dT%H:%M:%SZ)"
  printf 'kernel=%s\n' "$(uname -r)"
  printf 'srcg_sha256=%s\n' "$(sha256sum "$project_root/build-srcg/srcg" | cut -d ' ' -f 1)"
  printf 'training_sha256=%s\n' "$(sha256sum "$script_dir/train.py" | cut -d ' ' -f 1)"
  printf 'runner_sha256=%s\n' "$(sha256sum "$script_dir/run_full_experiment.sh" | cut -d ' ' -f 1)"
  printf 'scenario_runner_sha256=%s\n' "$(sha256sum "$script_dir/run_safety_experiment.sh" | cut -d ' ' -f 1)"
} > "$output_dir/cold-boot-metadata.txt"

set +e
"$script_dir/run_safety_experiment.sh" "$scenario" "$output_dir"
status=$?
set -e
printf 'exit_status=%s\n' "$status" >> "$output_dir/cold-boot-metadata.txt"
printf 'finished_utc=%s\n' "$(date --utc +%Y-%m-%dT%H:%M:%SZ)" \
  >> "$output_dir/cold-boot-metadata.txt"
exit "$status"
