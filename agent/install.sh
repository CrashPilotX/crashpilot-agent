#!/usr/bin/env bash
# CrashPilot Universal Installer
# Supports: Ubuntu/Debian, RHEL/CentOS/Rocky/Alma, Fedora,
#           Arch Linux, openSUSE, Alpine, Void Linux, WSL1/2
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_DIR="${CRASHPILOT_CONFIG_DIR:-$HOME/.config/crashpilot}"
DATA_DIR="${CRASHPILOT_DATA_DIR:-$HOME/.local/share/crashpilot}"
VENV_DIR="$DATA_DIR/venv"
INSTALL_SYSTEMD="${INSTALL_SYSTEMD:-auto}"  # auto | yes | no

# ── Colors ────────────────────────────────────────────────────────────────────
if [[ -t 1 ]]; then
  RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
  CYAN='\033[0;36m'; BOLD='\033[1m'; DIM='\033[2m'; RESET='\033[0m'
else
  RED=''; GREEN=''; YELLOW=''; CYAN=''; BOLD=''; DIM=''; RESET=''
fi

info()    { echo -e "${CYAN}[info]${RESET}  $*"; }
ok()      { echo -e "${GREEN}[ ok ]${RESET}  $*"; }
warn()    { echo -e "${YELLOW}[warn]${RESET}  $*"; }
err()     { echo -e "${RED}[err ]${RESET}  $*" >&2; }
section() { echo -e "\n${BOLD}── $* ──────────────────────────────────────────${RESET}"; }

banner() {
cat << 'EOF'
   ____               _    ____  _ _       _
  / ___|_ __ __ _ ___| |__|  _ \(_) | ___ | |_
 | |   | '__/ _` / __| '_ \ |_) | | |/ _ \| __|
 | |___| | | (_| \__ \ | | |  __/| | | (_) | |_
  \____|_|  \__,_|___/_| |_|_|  |_|_|_\___/ \__|
  AI-powered Linux crash forensics  v0.1
EOF
}
banner

# ── Platform detection ────────────────────────────────────────────────────────
section "Detecting platform"

OS=""
PKG_MGR=""
DISTRO=""
DISTRO_VER=""
INIT_SYS=""
IS_WSL=0
IS_DOCKER=0
IS_CONTAINER=0

# OS release
if [[ -f /etc/os-release ]]; then
  . /etc/os-release
  DISTRO="${ID:-unknown}"
  DISTRO_VER="${VERSION_ID:-}"
fi

# Package manager
if   command -v apt-get  &>/dev/null; then PKG_MGR="apt"
elif command -v dnf      &>/dev/null; then PKG_MGR="dnf"
elif command -v yum      &>/dev/null; then PKG_MGR="yum"
elif command -v pacman   &>/dev/null; then PKG_MGR="pacman"
elif command -v zypper   &>/dev/null; then PKG_MGR="zypper"
elif command -v apk      &>/dev/null; then PKG_MGR="apk"
elif command -v xbps-install &>/dev/null; then PKG_MGR="xbps"
else PKG_MGR="unknown"; fi

# Init system
if [[ -f /proc/1/comm ]]; then
  INIT_COMM=$(cat /proc/1/comm)
  case "$INIT_COMM" in
    systemd) INIT_SYS="systemd" ;;
    openrc|openrc-init) INIT_SYS="openrc" ;;
    runit|runsvdir) INIT_SYS="runit" ;;
    *) INIT_SYS="other" ;;
  esac
else
  INIT_SYS="unknown"
fi

# WSL detection
if grep -qi "microsoft\|wsl" /proc/sys/kernel/osrelease 2>/dev/null; then
  IS_WSL=1
  if grep -qi "wsl2" /proc/sys/kernel/osrelease 2>/dev/null; then
    WSL_VER=2
  else
    WSL_VER=1
  fi
fi

# Docker/container detection
if [[ -f /.dockerenv ]] || grep -q docker /proc/1/cgroup 2>/dev/null; then
  IS_DOCKER=1; IS_CONTAINER=1
fi
if [[ -n "${KUBERNETES_SERVICE_HOST:-}" ]] || [[ -d /var/run/secrets/kubernetes.io ]]; then
  IS_CONTAINER=1
fi

info "Distro: ${BOLD}${DISTRO} ${DISTRO_VER}${RESET} | Package manager: ${BOLD}${PKG_MGR}${RESET}"
info "Init system: ${BOLD}${INIT_SYS}${RESET}"
[[ $IS_WSL -eq 1 ]] && info "WSL version: ${BOLD}${WSL_VER}${RESET}"
[[ $IS_CONTAINER -eq 1 ]] && warn "Running inside a container — some telemetry collectors require privileged access"

# ── Python check ──────────────────────────────────────────────────────────────
section "Checking Python"

install_python() {
  case "$PKG_MGR" in
    apt)     sudo apt-get install -y python3 python3-pip python3-venv ;;
    dnf|yum) sudo "$PKG_MGR" install -y python3 python3-pip ;;
    pacman)  sudo pacman -Sy --noconfirm python python-pip ;;
    zypper)  sudo zypper install -y python3 python3-pip ;;
    apk)     sudo apk add --no-cache python3 py3-pip ;;
    xbps)    sudo xbps-install -Sy python3 python3-pip ;;
    *)       err "Unknown package manager — install Python 3.10+ manually"; exit 1 ;;
  esac
}

if ! command -v python3 &>/dev/null; then
  warn "Python 3 not found — installing..."
  install_python
fi

PYTHON_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
if python3 -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)"; then
  ok "Python $PYTHON_VER"
else
  warn "Python $PYTHON_VER found but 3.10+ recommended. Attempting upgrade..."
  install_python
  PYTHON_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
  ok "Python $PYTHON_VER"
fi

# ── Optional system tools ─────────────────────────────────────────────────────
section "Checking optional tools"

install_optional_tools() {
  local missing_apt=() missing_dnf=() missing_pacman=() missing_apk=()
  declare -A TOOL_PKG_APT=( [smartctl]=smartmontools [sensors]=lm-sensors [mcelog]=mcelog )
  declare -A TOOL_PKG_DNF=( [smartctl]=smartmontools [sensors]=lm_sensors  [mcelog]=mcelog )
  declare -A TOOL_PKG_PACMAN=( [smartctl]=smartmontools [sensors]=lm_sensors )
  declare -A TOOL_PKG_APK=( [smartctl]=smartmontools [sensors]=lm-sensors )

  for tool in smartctl sensors journalctl dmesg docker nvidia-smi kubectl mcelog; do
    if command -v "$tool" &>/dev/null; then
      ok "  $tool"
    else
      warn "  $tool — not found"
      case "$PKG_MGR" in
        apt) [[ -n "${TOOL_PKG_APT[$tool]:-}" ]] && missing_apt+=("${TOOL_PKG_APT[$tool]}") ;;
        dnf|yum) [[ -n "${TOOL_PKG_DNF[$tool]:-}" ]] && missing_dnf+=("${TOOL_PKG_DNF[$tool]}") ;;
        pacman) [[ -n "${TOOL_PKG_PACMAN[$tool]:-}" ]] && missing_pacman+=("${TOOL_PKG_PACMAN[$tool]}") ;;
        apk) [[ -n "${TOOL_PKG_APK[$tool]:-}" ]] && missing_apk+=("${TOOL_PKG_APK[$tool]}") ;;
      esac
    fi
  done

  if [[ ${#missing_apt[@]} -gt 0 && "$PKG_MGR" == "apt" ]]; then
    read -rp "Install missing packages (${missing_apt[*]})? [y/N] " ans
    if [[ "$ans" =~ ^[Yy]$ ]]; then
      sudo apt-get install -y "${missing_apt[@]}"
    fi
  fi
  if [[ ${#missing_dnf[@]} -gt 0 && "$PKG_MGR" =~ ^(dnf|yum)$ ]]; then
    read -rp "Install missing packages (${missing_dnf[*]})? [y/N] " ans
    if [[ "$ans" =~ ^[Yy]$ ]]; then
      sudo "$PKG_MGR" install -y "${missing_dnf[@]}"
    fi
  fi
}

install_optional_tools

# ── journalctl permission check ───────────────────────────────────────────────
section "Checking log access"

if command -v journalctl &>/dev/null; then
  if journalctl --lines=1 &>/dev/null; then
    ok "journalctl readable"
  elif [[ $EUID -eq 0 ]]; then
    ok "journalctl readable (root)"
  else
    warn "journalctl restricted — adding $USER to systemd-journal group"
    if getent group systemd-journal &>/dev/null; then
      sudo usermod -aG systemd-journal "$USER" && ok "Added to systemd-journal (re-login required)"
    fi
  fi
fi

if [[ $IS_WSL -eq 1 && ${WSL_VER:-1} -eq 1 ]]; then
  warn "WSL1 detected — journalctl and dmesg are not available"
  warn "Analysis will use Windows Event Log via PowerShell interop"
fi

# ── Create directories and config ─────────────────────────────────────────────
section "Setting up configuration"

mkdir -p "$CONFIG_DIR" "$DATA_DIR"
chmod 700 "$CONFIG_DIR"

if [[ ! -f "$CONFIG_DIR/.env" ]]; then
  cat > "$CONFIG_DIR/.env" << 'ENVEOF'
# CrashPilot Configuration
# ─────────────────────────────────────────────────
# Anthropic API key (get one at https://console.anthropic.com/)
CRASHPILOT_ANTHROPIC_API_KEY=

# Claude model (claude-opus-4-7 is the most capable)
CRASHPILOT_CLAUDE_MODEL=claude-opus-4-7

# Local API server (used by the web dashboard)
CRASHPILOT_API_HOST=127.0.0.1
CRASHPILOT_API_PORT=7878

# Telemetry limits
CRASHPILOT_JOURNAL_LINES=5000
CRASHPILOT_DMESG_LINES=2000
CRASHPILOT_ANALYSIS_TIMEOUT=120

# Data storage
# CRASHPILOT_DATA_DIR=/var/lib/crashpilot  # uncomment for system-wide install
ENVEOF
  chmod 600 "$CONFIG_DIR/.env"
  ok "Created config: $CONFIG_DIR/.env"
  echo -e "\n  ${YELLOW}ACTION REQUIRED:${RESET} Add your Anthropic API key:"
  echo -e "  ${CYAN}  nano $CONFIG_DIR/.env${RESET}"
else
  ok "Config exists: $CONFIG_DIR/.env"
fi

# ── Install Python package ────────────────────────────────────────────────────
section "Installing CrashPilot"

# Always use a venv — respects PEP 668 (externally managed environments)
info "Creating virtual environment at $VENV_DIR..."
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet -e "$REPO_DIR/agent"
ok "CrashPilot installed in venv"

# Create wrapper in /usr/local/bin (requires root) or ~/.local/bin
WRAPPER_INSTALLED=0
create_wrapper() {
  local target="$1"
  mkdir -p "$(dirname "$target")"
  cat > "$target" << WRAPPER
#!/bin/bash
# CrashPilot wrapper — generated by install.sh
exec "$VENV_DIR/bin/crashpilot" "\$@"
WRAPPER
  chmod +x "$target"
}

if [[ $EUID -eq 0 ]]; then
  create_wrapper /usr/local/bin/crashpilot
  ok "Installed wrapper: /usr/local/bin/crashpilot"
  WRAPPER_INSTALLED=1
else
  LOCAL_BIN="$HOME/.local/bin"
  mkdir -p "$LOCAL_BIN"
  create_wrapper "$LOCAL_BIN/crashpilot"
  ok "Installed wrapper: $LOCAL_BIN/crashpilot"
  WRAPPER_INSTALLED=1
  # Ensure ~/.local/bin is in PATH
  if [[ ":$PATH:" != *":$LOCAL_BIN:"* ]]; then
    warn "Add $LOCAL_BIN to PATH: export PATH=\"\$HOME/.local/bin:\$PATH\""
  fi
fi

# ── Systemd service installation ──────────────────────────────────────────────
section "Setting up service"

install_systemd_services() {
  local service_src="$REPO_DIR/systemd"
  local svc_dir="/etc/systemd/system"

  # Replace venv path placeholder in service files
  sed "s|__CRASHPILOT_BIN__|$VENV_DIR/bin/crashpilot|g" \
    "$service_src/crashpilot.service" > /tmp/crashpilot.service
  sed "s|__CRASHPILOT_BIN__|$VENV_DIR/bin/crashpilot|g" \
    "$service_src/crashpilot-api.service" > /tmp/crashpilot-api.service

  sudo cp /tmp/crashpilot.service "$svc_dir/crashpilot.service"
  sudo cp /tmp/crashpilot-api.service "$svc_dir/crashpilot-api@.service"
  sudo systemctl daemon-reload
  sudo systemctl enable crashpilot.service
  ok "systemd services installed"
  echo -e "  Boot analysis enabled: will run once per boot"
  echo -e "  Start API server: ${CYAN}sudo systemctl start crashpilot-api@$USER${RESET}"
}

install_openrc_services() {
  # OpenRC (Alpine, Gentoo, Void)
  cat > /tmp/crashpilot-rc << RCEOF
#!/sbin/openrc-run
description="CrashPilot crash analysis"
command="$VENV_DIR/bin/crashpilot"
command_args="analyze"
command_user="${SUDO_USER:-$USER}"
depend() { need localmount logger; }
RCEOF
  sudo cp /tmp/crashpilot-rc /etc/init.d/crashpilot
  sudo chmod +x /etc/init.d/crashpilot
  sudo rc-update add crashpilot default
  ok "OpenRC service installed"
}

install_runit_services() {
  # runit (Void Linux)
  local sv_dir="/etc/sv/crashpilot"
  sudo mkdir -p "$sv_dir"
  sudo tee "$sv_dir/run" > /dev/null << RUNIT
#!/bin/sh
exec "$VENV_DIR/bin/crashpilot" analyze 2>&1
RUNIT
  sudo chmod +x "$sv_dir/run"
  ok "runit service installed at $sv_dir"
}

if [[ $IS_WSL -eq 1 ]]; then
  info "WSL detected — skipping system service installation"
  echo -e "  ${DIM}In WSL, run manually: crashpilot analyze${RESET}"
  echo -e "  ${DIM}Or add to ~/.bashrc / ~/.profile for auto-run on WSL start${RESET}"

elif [[ $IS_CONTAINER -eq 1 ]]; then
  info "Container detected — skipping service installation"
  echo -e "  ${DIM}In containers, run: crashpilot analyze${RESET}"

elif [[ "$INSTALL_SYSTEMD" == "no" ]]; then
  info "Systemd install skipped (INSTALL_SYSTEMD=no)"

elif [[ "$INIT_SYS" == "systemd" ]]; then
  if [[ $EUID -eq 0 ]]; then
    install_systemd_services
  else
    read -rp "Install systemd services (requires sudo)? [Y/n] " ans
    if [[ ! "$ans" =~ ^[Nn]$ ]]; then
      install_systemd_services
    else
      info "Skipping systemd services"
    fi
  fi

elif [[ "$INIT_SYS" == "openrc" ]]; then
  if [[ $EUID -eq 0 ]]; then
    install_openrc_services
  else
    warn "Run as root to install OpenRC service"
  fi

elif [[ "$INIT_SYS" == "runit" ]]; then
  if [[ $EUID -eq 0 ]]; then
    install_runit_services
  else
    warn "Run as root to install runit service"
  fi

else
  warn "Unknown init system '$INIT_SYS' — skipping service installation"
  info "Run manually: crashpilot analyze"
fi

# ── Docker & Kubernetes install info ─────────────────────────────────────────
if [[ $IS_DOCKER -eq 0 ]] && command -v docker &>/dev/null; then
  echo ""
  info "Docker detected — to monitor Docker containers, CrashPilot needs access to /var/run/docker.sock"
  if ! groups | grep -q docker; then
    warn "Add yourself to the docker group: sudo usermod -aG docker $USER"
  fi
fi

if command -v kubectl &>/dev/null; then
  echo ""
  info "kubectl detected — CrashPilot can also analyze Kubernetes pod crashes"
  info "Deploy as DaemonSet: kubectl apply -k $REPO_DIR/k8s/"
fi

# ── Test installation ─────────────────────────────────────────────────────────
section "Testing installation"

CRASHPILOT_BIN=""
if command -v crashpilot &>/dev/null; then
  CRASHPILOT_BIN="crashpilot"
elif [[ -f "$HOME/.local/bin/crashpilot" ]]; then
  CRASHPILOT_BIN="$HOME/.local/bin/crashpilot"
elif [[ -f "$VENV_DIR/bin/crashpilot" ]]; then
  CRASHPILOT_BIN="$VENV_DIR/bin/crashpilot"
fi

if [[ -n "$CRASHPILOT_BIN" ]] && "$CRASHPILOT_BIN" --help &>/dev/null; then
  ok "CrashPilot CLI working"
else
  err "CLI not found in PATH"
  info "Use: $VENV_DIR/bin/crashpilot"
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}${BOLD}✓ Installation complete!${RESET}"
echo ""
echo "  Platform: ${BOLD}${DISTRO} ${DISTRO_VER}${RESET} | Init: ${BOLD}${INIT_SYS}${RESET}"
[[ $IS_WSL -eq 1 ]] && echo "  Mode: ${YELLOW}WSL ${WSL_VER}${RESET}"
[[ $IS_CONTAINER -eq 1 ]] && echo "  Mode: ${YELLOW}Container${RESET}"
echo ""
echo "  Next steps:"
echo -e "  1. Set API key:  ${CYAN}nano $CONFIG_DIR/.env${RESET}"
echo -e "  2. Analyze:      ${CYAN}crashpilot analyze${RESET}"
echo -e "  3. Serve:        ${CYAN}crashpilot serve${RESET}"
echo -e "  4. Dashboard:    ${CYAN}https://kdigitalsystems.github.io/CrashPilot${RESET}"
echo ""

# Docker install tip
if [[ $IS_DOCKER -eq 0 ]]; then
  echo -e "  ${DIM}Docker deploy:      docker run --privileged -v /var/log:/var/log ghcr.io/kdigitalsystems/crashpilot${RESET}"
  echo -e "  ${DIM}Kubernetes deploy:  kubectl apply -k $REPO_DIR/k8s/${RESET}"
  echo ""
fi
