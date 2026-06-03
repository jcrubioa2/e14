#!/usr/bin/env bash
# Re-pin all running e14detector batch jobs (cropping `process` + `publish-loop`) to a
# subset of cores so the rest stay free for interactive use, and persist the choice so
# the supervisor's respawns inherit it too.
#
#   scripts/cpu_throttle.sh 0-9     # batch on cores 0-9, free 10-11 (low priority)
#   scripts/cpu_throttle.sh 0-6     # batch on cores 0-6, free 7-11
#   scripts/cpu_throttle.sh all     # give everything back to all 12 cores, normal priority
#
# The core set is written to .cpu_set (read by scripts/publish_supervisor.sh) so a
# publish-loop respawn keeps the same confinement until you change it.
set -u
cd "$(dirname "$0")/.."

ARG="${1:-}"
if [[ -z "$ARG" ]]; then
  echo "usage: $0 <cpuset|all>   e.g. 0-9 | 0-6 | all"; exit 2
fi

if [[ "$ARG" == "all" ]]; then
  CPU_SET="0-11"; NICE=0
else
  CPU_SET="$ARG"; NICE=15
fi

echo "$CPU_SET" > .cpu_set   # persisted for supervisor respawns

n=0
for p in $(pgrep -f "e14detector"); do
  taskset -a -cp "$CPU_SET" "$p" >/dev/null 2>&1 && renice -n "$NICE" -p "$p" >/dev/null 2>&1 && n=$((n+1))
done
echo "re-pinned $n e14detector process(es) to cores $CPU_SET (nice $NICE); saved to .cpu_set"
