#!/bin/sh
# Run before the package is removed. POSIX sh (deb compatible).
# $1: deb -> "remove"/"upgrade".
set -e

# Only tear down services on a real removal, not during an upgrade.
case "${1:-}" in
    upgrade|1)
        exit 0
        ;;
esac

if command -v systemctl >/dev/null 2>&1; then
    systemctl disable --now crashpilot-heartbeat.timer >/dev/null 2>&1 || true
    systemctl disable --now crashpilot-snapshot.timer >/dev/null 2>&1 || true
    systemctl disable --now crashpilot-update.timer >/dev/null 2>&1 || true
    systemctl disable crashpilot.service >/dev/null 2>&1 || true
fi

exit 0
