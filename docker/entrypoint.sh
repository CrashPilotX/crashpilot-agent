#!/bin/bash
# CrashPilot Docker entrypoint
set -euo pipefail

# Allow reading host journald if mounted
export CRASHPILOT_DATA_DIR="${CRASHPILOT_DATA_DIR:-/data}"
mkdir -p "$CRASHPILOT_DATA_DIR"

# Write connection config into the persistent data dir so `crashpilot configure`
# (push-mode connect) survives container restarts. Pydantic also reads
# CRASHPILOT_SUPABASE_* env vars directly if you prefer the declarative route.
export CRASHPILOT_CONFIG_DIR="${CRASHPILOT_CONFIG_DIR:-$CRASHPILOT_DATA_DIR}"

# Containers have no systemd heartbeat timer, so for long-running modes we run a
# lightweight heartbeat loop in the background to keep the system "online" in the
# dashboard. `heartbeat --quiet` is a no-op (exit 0) when push mode isn't
# configured, so this is safe to start unconditionally.
start_heartbeat_loop() {
  (
    while true; do
      crashpilot heartbeat --quiet || true
      sleep 60
    done
  ) &
}

case "${1:-serve}" in
  analyze)
    echo "[CrashPilot] Running crash analysis..."
    exec crashpilot analyze "${@:2}"
    ;;
  serve)
    echo "[CrashPilot] Starting API server on ${CRASHPILOT_API_HOST:-0.0.0.0}:${CRASHPILOT_API_PORT:-7878}..."
    # Run analysis first (pushes a report if push mode is configured), then serve.
    crashpilot analyze --force 2>&1 || true
    start_heartbeat_loop
    exec crashpilot serve \
      --host "${CRASHPILOT_API_HOST:-0.0.0.0}" \
      --port "${CRASHPILOT_API_PORT:-7878}"
    ;;
  daemon)
    echo "[CrashPilot] Running in daemon mode (analyze + serve)..."
    crashpilot analyze 2>&1 || true
    start_heartbeat_loop
    exec crashpilot serve \
      --host "${CRASHPILOT_API_HOST:-0.0.0.0}" \
      --port "${CRASHPILOT_API_PORT:-7878}"
    ;;
  *)
    exec crashpilot "$@"
    ;;
esac
