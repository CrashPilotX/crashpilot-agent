#!/bin/sh
# Run after the package is installed/upgraded. POSIX sh (deb compatible).
set -e

if [ -f /etc/crashpilot/.env ]; then
    chmod 600 /etc/crashpilot/.env || true
fi

if command -v systemctl >/dev/null 2>&1; then
    systemctl daemon-reload || true
    # Boot-time crash analysis (runs once per boot)
    systemctl enable crashpilot.service >/dev/null 2>&1 || true
    # Heartbeat timer keeps the system "online" once push mode is configured;
    # it's a no-op until then, so it's safe to enable now.
    systemctl enable --now crashpilot-heartbeat.timer >/dev/null 2>&1 || true
fi

echo ""
echo "CrashPilot installed. Next:"
echo "  1. Connect to the dashboard:  sudo crashpilot configure cpilot_<string>"
echo "     (create a system at https://kdigitalsystems.github.io/CrashPilot/)"
echo "  2. Check everything:          sudo crashpilot doctor"
echo "  3. Analyze the last boot:     sudo crashpilot analyze"
echo "  AI is optional — heuristic analysis works with no API key."
echo ""

exit 0
