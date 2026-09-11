#!/usr/bin/env bash
set -euo pipefail

interval="${CRASHPILOT_SNAPSHOT_INTERVAL_SECONDS:-60}"

background_agent_loop() {
  # A stop ends the loop between commands. One that arrives during a
  # heartbeat waits for it (bash runs the trap once the command returns), so
  # no heartbeat is still in flight when the node signs off.
  trap 'kill "${nap:-}" 2>/dev/null; exit 0' TERM
  while true; do
    crashpilot snapshot --quiet || true
    crashpilot heartbeat --quiet || true
    # In the background, because a foreground sleep would hold off the stop.
    sleep "$interval" &
    nap=$!
    wait "$nap" || true
  done
}

if [[ "${CRASHPILOT_ANALYZE_ON_START:-0}" == "1" ]]; then
  crashpilot analyze --force || true
fi

background_agent_loop &
loop_pid=$!

# Not exec'd: this shell stays PID 1 so a stop (docker stop, pod termination)
# is handled in order: heartbeat loop stopped, then sign-off, then the main
# process. exec used to drop the trap, and a heartbeat could land after the
# sign-off and mark the node online again.
"$@" &
main_pid=$!

stopping=0
stop() {
  stopping=1
  trap - TERM INT
  kill -TERM "$loop_pid" 2>/dev/null || true
  wait "$loop_pid" 2>/dev/null || true
  crashpilot sign-off --quiet || true
  kill -TERM "$main_pid" 2>/dev/null || true
}
trap stop TERM INT

status=0
wait "$main_pid" || status=$?
if [[ $stopping -eq 1 ]]; then
  # The signal cut the first wait short; this one returns the main process's
  # own exit status.
  status=0
  wait "$main_pid" || status=$?
else
  # The main process ended by itself: not a clean shutdown, so no sign-off.
  kill -TERM "$loop_pid" 2>/dev/null || true
fi
exit "$status"
