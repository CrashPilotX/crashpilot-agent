#!/usr/bin/env bash
set -euo pipefail

interval="${CRASHPILOT_SNAPSHOT_INTERVAL_SECONDS:-60}"

background_agent_loop() {
  while true; do
    crashpilot snapshot --quiet || true
    crashpilot heartbeat --quiet || true
    sleep "$interval"
  done
}

if [[ "${CRASHPILOT_ANALYZE_ON_START:-0}" == "1" ]]; then
  crashpilot analyze --force || true
fi

background_agent_loop &
loop_pid=$!
trap 'kill "$loop_pid" 2>/dev/null || true' EXIT INT TERM

exec "$@"
