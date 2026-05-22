#!/bin/bash
# CrashPilot Docker entrypoint
set -euo pipefail

# Allow reading host journald if mounted
export CRASHPILOT_DATA_DIR="${CRASHPILOT_DATA_DIR:-/data}"
mkdir -p "$CRASHPILOT_DATA_DIR"

case "${1:-serve}" in
  analyze)
    echo "[CrashPilot] Running crash analysis..."
    exec crashpilot analyze "${@:2}"
    ;;
  serve)
    echo "[CrashPilot] Starting API server on ${CRASHPILOT_API_HOST:-0.0.0.0}:${CRASHPILOT_API_PORT:-7878}..."
    # Run analysis first, then keep the API server up
    crashpilot analyze --force 2>&1 || true
    exec crashpilot serve \
      --host "${CRASHPILOT_API_HOST:-0.0.0.0}" \
      --port "${CRASHPILOT_API_PORT:-7878}"
    ;;
  daemon)
    echo "[CrashPilot] Running in daemon mode (analyze + serve)..."
    crashpilot analyze 2>&1 || true
    exec crashpilot serve \
      --host "${CRASHPILOT_API_HOST:-0.0.0.0}" \
      --port "${CRASHPILOT_API_PORT:-7878}"
    ;;
  *)
    exec crashpilot "$@"
    ;;
esac
