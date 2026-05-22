#!/usr/bin/env bash
# CrashPilot installer — sets up the agent, systemd services, and config
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_DIR="$HOME/.config/crashpilot"
DATA_DIR="$HOME/.local/share/crashpilot"

# ── Colors ────────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

info()  { echo -e "${CYAN}[INFO]${RESET} $*"; }
ok()    { echo -e "${GREEN}[OK]${RESET}   $*"; }
warn()  { echo -e "${YELLOW}[WARN]${RESET} $*"; }
err()   { echo -e "${RED}[ERR]${RESET}  $*" >&2; }

echo -e "${BOLD}${CYAN}"
echo "  ╔═══════════════════════════════╗"
echo "  ║   CrashPilot Installer v0.1   ║"
echo "  ║  AI Linux Crash Forensics     ║"
echo "  ╚═══════════════════════════════╝"
echo -e "${RESET}"

# ── Check prerequisites ───────────────────────────────────────────────────────
info "Checking prerequisites..."

if ! command -v python3 &>/dev/null; then
    err "Python 3.10+ is required. Install it first."
    exit 1
fi

PYTHON_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
if python3 -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)"; then
    ok "Python $PYTHON_VER found"
else
    err "Python 3.10+ required, found $PYTHON_VER"
    exit 1
fi

if ! command -v pip3 &>/dev/null && ! command -v pip &>/dev/null; then
    warn "pip not found — installing via ensurepip"
    python3 -m ensurepip --upgrade
fi

# ── Optional tools check ──────────────────────────────────────────────────────
info "Checking optional tools..."
for tool in smartctl sensors journalctl dmesg docker nvidia-smi; do
    if command -v "$tool" &>/dev/null; then
        ok "  $tool found"
    else
        warn "  $tool not found (some collectors will be skipped)"
    fi
done

# ── Create config directory ───────────────────────────────────────────────────
info "Creating config directory: $CONFIG_DIR"
mkdir -p "$CONFIG_DIR" "$DATA_DIR"

if [[ ! -f "$CONFIG_DIR/.env" ]]; then
    cat > "$CONFIG_DIR/.env" << 'EOF'
# CrashPilot Configuration
# Get your API key at https://console.anthropic.com/
CRASHPILOT_ANTHROPIC_API_KEY=

# Claude model to use for analysis
CRASHPILOT_CLAUDE_MODEL=claude-opus-4-7

# Local API server settings
CRASHPILOT_API_HOST=127.0.0.1
CRASHPILOT_API_PORT=7878

# Analysis settings
CRASHPILOT_ANALYSIS_TIMEOUT=120
EOF
    ok "Created config template at $CONFIG_DIR/.env"
    echo ""
    echo -e "  ${YELLOW}ACTION REQUIRED:${RESET} Add your Anthropic API key:"
    echo -e "  ${CYAN}  nano $CONFIG_DIR/.env${RESET}"
    echo ""
else
    ok "Config file already exists: $CONFIG_DIR/.env"
fi

# ── Install Python package ────────────────────────────────────────────────────
info "Installing CrashPilot Python package..."
if pip3 install -e "$REPO_DIR/agent" --quiet 2>&1; then
    ok "CrashPilot installed"
else
    # Try with --break-system-packages for newer distros
    pip3 install -e "$REPO_DIR/agent" --break-system-packages --quiet || {
        # Last resort: install in venv
        warn "System pip install failed — using virtual environment"
        python3 -m venv "$DATA_DIR/venv"
        "$DATA_DIR/venv/bin/pip" install -e "$REPO_DIR/agent" --quiet
        # Create wrapper script
        sudo tee /usr/local/bin/crashpilot > /dev/null << WRAPPER
#!/bin/bash
exec "$DATA_DIR/venv/bin/crashpilot" "\$@"
WRAPPER
        sudo chmod +x /usr/local/bin/crashpilot
        ok "CrashPilot installed in venv"
    }
fi

# ── Install systemd services ──────────────────────────────────────────────────
if command -v systemctl &>/dev/null && [[ -d /etc/systemd/system ]]; then
    info "Installing systemd services..."

    if [[ $EUID -eq 0 ]]; then
        # Running as root
        cp "$REPO_DIR/systemd/crashpilot.service" /etc/systemd/system/
        cp "$REPO_DIR/systemd/crashpilot-api.service" /etc/systemd/system/
        systemctl daemon-reload
        systemctl enable crashpilot.service
        ok "Systemd services installed and enabled"
        echo ""
        echo -e "  To start the API server:"
        echo -e "  ${CYAN}  sudo systemctl start crashpilot-api@$USER${RESET}"
    else
        warn "Not running as root — skipping systemd installation"
        echo -e "  To install services, run: ${CYAN}sudo $0${RESET}"
    fi
else
    warn "systemd not available — you can run crashpilot manually"
fi

# ── Test installation ─────────────────────────────────────────────────────────
echo ""
info "Testing installation..."
if crashpilot --help &>/dev/null; then
    ok "CrashPilot CLI is working"
else
    err "CrashPilot CLI not found in PATH"
    exit 1
fi

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}${BOLD}✓ Installation complete!${RESET}"
echo ""
echo "  Next steps:"
echo -e "  1. Add your API key:  ${CYAN}nano $CONFIG_DIR/.env${RESET}"
echo -e "  2. Run analysis:      ${CYAN}crashpilot analyze${RESET}"
echo -e "  3. Start API server:  ${CYAN}crashpilot serve${RESET}"
echo -e "  4. Open dashboard:    ${CYAN}https://kdigitalsystems.github.io/CrashPilot${RESET}"
echo ""
