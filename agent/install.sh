#!/usr/bin/env bash
# CrashPilot Universal Installer
# Supports: Ubuntu Linux and Ubuntu on WSL1/WSL2.
set -uo pipefail   # no -e: we handle errors explicitly so one bad package can't abort

# ── Locate the repo ───────────────────────────────────────────────────────────
# When run as `bash script.sh` from inside the repo, BASH_SOURCE[0] is the
# script file itself and REPO_DIR is its parent directory.
# When piped in via `bash -c "$(curl ...)"` or `curl | bash`, BASH_SOURCE[0]
# is unbound or set to "bash", so we clone the repo to a temp directory instead.
_src="${BASH_SOURCE[0]:-}"
if [[ -n "$_src" && "$_src" != "bash" && -f "$_src" ]]; then
  REPO_DIR="$(cd "$(dirname "$_src")/.." && pwd)"
else
  # curl-pipe install: clone the repo into a temp dir
  CLONE_DIR="$(mktemp -d)/CrashPilot"
  echo "[info]  curl-pipe install detected — cloning repository..."
  if command -v git &>/dev/null; then
    git clone --depth=1 https://github.com/kdigitalsystems/CrashPilot.git "$CLONE_DIR" \
      || { echo "[err ]  git clone failed"; exit 1; }
  else
    echo "[err ]  git is required for curl-pipe installs. Install it with:"
    echo "        sudo apt-get install git"
    exit 1
  fi
  REPO_DIR="$CLONE_DIR"
fi

INSTALL_SYSTEMD="${INSTALL_SYSTEMD:-auto}"  # auto | yes | no

# When running as root (sudo), install to system-wide paths so any user can
# invoke `crashpilot`.  When running as a normal user, install to $HOME.
if [[ $EUID -eq 0 ]]; then
  CONFIG_DIR="${CRASHPILOT_CONFIG_DIR:-/etc/crashpilot}"
  DATA_DIR="${CRASHPILOT_DATA_DIR:-/opt/crashpilot}"
else
  CONFIG_DIR="${CRASHPILOT_CONFIG_DIR:-$HOME/.config/crashpilot}"
  DATA_DIR="${CRASHPILOT_DATA_DIR:-$HOME/.local/share/crashpilot}"
fi
VENV_DIR="$DATA_DIR/venv"

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

# ── Parse arguments ─────────────────────────────────────────────────────────
# --connect <cpilot_…>  : after installing, configure push mode and bring the
#                         system online in one shot (the dashboard one-liner).
# A bare cpilot_… positional argument is also accepted.
# Can also be supplied via the CRASHPILOT_CONNECT environment variable.
CONNECT_STRING="${CRASHPILOT_CONNECT:-}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --connect)      CONNECT_STRING="${2:-}"; shift 2 ;;
    --connect=*)    CONNECT_STRING="${1#*=}"; shift ;;
    cpilot_*)       CONNECT_STRING="$1"; shift ;;
    -h|--help)
      echo "Usage: install.sh [--connect cpilot_<connection-string>]"
      echo "  --connect <string>   Install, then connect to the dashboard (push mode)."
      echo "  (a bare cpilot_… argument or \$CRASHPILOT_CONNECT also works)"
      exit 0 ;;
    *)              warn "Ignoring unknown argument: $1"; shift ;;
  esac
done

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

PKG_MGR=""
DISTRO=""
DISTRO_VER=""
INIT_SYS=""
IS_WSL=0
IS_CONTAINER=0

# OS release
if [[ -f /etc/os-release ]]; then
  . /etc/os-release
  DISTRO="${ID:-unknown}"
  DISTRO_VER="${VERSION_ID:-}"
fi

# Package manager
if   command -v apt-get  &>/dev/null; then PKG_MGR="apt"
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

# Unsupported container environment detection
if [[ -f /.dockerenv ]] || grep -q docker /proc/1/cgroup 2>/dev/null; then
  IS_CONTAINER=1
fi
if [[ -n "${KUBERNETES_SERVICE_HOST:-}" ]] || [[ -d /var/run/secrets/kubernetes.io ]]; then
  IS_CONTAINER=1
fi

info "Distro: ${BOLD}${DISTRO} ${DISTRO_VER}${RESET} | Package manager: ${BOLD}${PKG_MGR}${RESET}"
info "Init system: ${BOLD}${INIT_SYS}${RESET}"
[[ $IS_WSL -eq 1 ]] && info "WSL version: ${BOLD}${WSL_VER}${RESET}"
if [[ "$DISTRO" != "ubuntu" ]]; then
  err "Unsupported distro: ${DISTRO:-unknown}. CrashPilot currently supports Ubuntu only."
  exit 1
fi
if [[ "$PKG_MGR" != "apt" ]]; then
  err "Unsupported package manager: ${PKG_MGR}. CrashPilot currently supports Ubuntu apt installs only."
  exit 1
fi
if [[ $IS_CONTAINER -eq 1 ]]; then
  err "Containerized installs are not supported right now."
  exit 1
fi

# ── Python check ──────────────────────────────────────────────────────────────
section "Checking Python"

_sudo() { [[ $EUID -eq 0 ]] && "$@" || sudo "$@"; }

install_python() {
  case "$PKG_MGR" in
    apt)     _sudo apt-get install -y python3 python3-pip python3-venv ;;
    dnf|yum) _sudo "$PKG_MGR" install -y python3 python3-pip ;;
    pacman)  _sudo pacman -Sy --noconfirm python python-pip ;;
    zypper)  _sudo zypper install -y python3 python3-pip ;;
    apk)     _sudo apk add --no-cache python3 py3-pip ;;
    xbps)    _sudo xbps-install -Sy python3 python3-pip ;;
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

# mcelog was removed from Ubuntu 20.04+ (kernel 5.x+).
# rasdaemon is the modern replacement on those distros.
_mce_package() {
  local kernel_major
  kernel_major=$(uname -r | cut -d. -f1)
  if [[ "$PKG_MGR" == "apt" && "${kernel_major:-0}" -ge 5 ]]; then
    echo "rasdaemon"
  elif [[ "$PKG_MGR" =~ ^(dnf|yum)$ ]]; then
    echo "rasdaemon"
  else
    echo "mcelog"
  fi
}

# Try to install a single package; never exits — returns 0/1.
_try_install_pkg() {
  local pkg="$1"
  case "$PKG_MGR" in
    apt)     _sudo apt-get install -y "$pkg" &>/dev/null && return 0 ;;
    dnf|yum) _sudo "$PKG_MGR" install -y "$pkg" &>/dev/null && return 0 ;;
    pacman)  _sudo pacman -Sy --noconfirm "$pkg" &>/dev/null && return 0 ;;
    zypper)  _sudo zypper install -y "$pkg" &>/dev/null && return 0 ;;
    apk)     _sudo apk add --no-cache "$pkg" &>/dev/null && return 0 ;;
    xbps)    _sudo xbps-install -Sy "$pkg" &>/dev/null && return 0 ;;
  esac
  return 1
}

install_optional_tools() {
  # tool → (apt-pkg  dnf-pkg  pacman-pkg  apk-pkg)
  # "?"  = not available / skip for this distro
  declare -A PKG_APT=(
    [smartctl]=smartmontools
    [sensors]=lm-sensors
    [mcelog]="$(_mce_package)"
  )
  declare -A PKG_DNF=(
    [smartctl]=smartmontools
    [sensors]=lm_sensors
    [mcelog]=rasdaemon
  )
  declare -A PKG_PACMAN=(
    [smartctl]=smartmontools
    [sensors]=lm_sensors
  )
  declare -A PKG_APK=(
    [smartctl]=smartmontools
    [sensors]=lm-sensors
  )

  local missing=()
  for tool in smartctl sensors journalctl dmesg nvidia-smi; do
    if command -v "$tool" &>/dev/null; then
      ok "  $tool"
    else
      warn "  $tool — not found"
      local pkg=""
      case "$PKG_MGR" in
        apt)        pkg="${PKG_APT[$tool]:-}" ;;
        dnf|yum)    pkg="${PKG_DNF[$tool]:-}" ;;
        pacman)     pkg="${PKG_PACMAN[$tool]:-}" ;;
        apk)        pkg="${PKG_APK[$tool]:-}" ;;
      esac
      [[ -n "$pkg" ]] && missing+=("$pkg")
    fi
  done

  # MCE tool (separate because the package name varies by kernel version)
  local mce_pkg
  mce_pkg="$(_mce_package)"
  if ! command -v mcelog &>/dev/null && ! command -v rasdaemon &>/dev/null; then
    warn "  mcelog/rasdaemon — not found"
    missing+=("$mce_pkg")
  else
    ok "  mcelog/rasdaemon"
  fi

  if [[ ${#missing[@]} -eq 0 ]]; then
    return
  fi

  # Deduplicate
  local -A seen=()
  local unique=()
  for p in "${missing[@]}"; do
    [[ -z "${seen[$p]:-}" ]] && unique+=("$p") && seen[$p]=1
  done

  read -rp "$(echo -e "${YELLOW}Install missing packages (${unique[*]})?${RESET} [y/N] ")" ans
  if [[ ! "$ans" =~ ^[Yy]$ ]]; then
    warn "Skipping optional packages — some collectors will be limited"
    return
  fi

  # Install ONE AT A TIME so a missing package doesn't block others
  for pkg in "${unique[@]}"; do
    printf "  Installing %-20s ... " "$pkg"
    if _try_install_pkg "$pkg"; then
      echo -e "${GREEN}ok${RESET}"
    else
      echo -e "${YELLOW}not available (skipped)${RESET}"
    fi
  done
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
# Root installs keep the directory traversable for packaged tooling, but the
# .env file itself contains secrets once push mode is configured.
[[ $EUID -eq 0 ]] && chmod 755 "$CONFIG_DIR" || chmod 700 "$CONFIG_DIR"

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
  # Private because push-mode credentials are stored here after configure.
  # User installs: 600 (private — only the owning user needs it)
  chmod 600 "$CONFIG_DIR/.env"
  ok "Created config: $CONFIG_DIR/.env"
  echo -e "\n  ${YELLOW}ACTION REQUIRED:${RESET} Add your Anthropic API key:"
  echo -e "  ${CYAN}  nano $CONFIG_DIR/.env${RESET}"
else
  ok "Config exists: $CONFIG_DIR/.env"
fi

# Ensure correct permissions regardless of whether config was just created or existed.
# Push mode stores CRASHPILOT_SUPABASE_TOKEN here, so never leave it world-readable.
# User install: 600 (private)
chmod 600 "$CONFIG_DIR/.env"

# ── Install Python package ────────────────────────────────────────────────────
section "Installing CrashPilot"

# Ensure python3-venv is present (Ubuntu splits it into a separate package)
if ! python3 -m venv --help &>/dev/null; then
  info "Installing python3-venv..."
  _try_install_pkg python3-venv || _try_install_pkg python3-full || true
fi

info "Creating virtual environment at $VENV_DIR..."
mkdir -p "$DATA_DIR"
if ! python3 -m venv "$VENV_DIR"; then
  err "python3 -m venv failed. Installing python3-venv / python3-full..."
  _try_install_pkg python3-venv || true
  _try_install_pkg python3-full || true
  python3 -m venv "$VENV_DIR" || { err "Cannot create venv — install python3-venv manually"; exit 1; }
fi

# Bootstrap pip — Ubuntu 24.04 venvs sometimes ship without it
if [[ ! -x "$VENV_DIR/bin/pip" ]]; then
  info "pip missing from venv — bootstrapping with ensurepip..."
  "$VENV_DIR/bin/python3" -m ensurepip --upgrade 2>/dev/null || \
  curl -sSf https://bootstrap.pypa.io/get-pip.py | "$VENV_DIR/bin/python3" || \
  { err "Cannot bootstrap pip — install python3-pip manually"; exit 1; }
fi

info "Upgrading pip..."
"$VENV_DIR/bin/pip" install --quiet --upgrade pip || true

info "Installing CrashPilot package..."
# Use a regular (non-editable) install so the package is fully copied into the
# venv's site-packages. An editable install (-e) would leave a .pth pointer
# back to the source directory, which breaks when that directory is deleted
# (e.g. after a curl-pipe install where we cloned to a temp dir).
if ! "$VENV_DIR/bin/pip" install --quiet "$REPO_DIR/agent"; then
  err "pip install failed — check output above"
  exit 1
fi

# Verify the binary exists before declaring success
if [[ ! -x "$VENV_DIR/bin/crashpilot" ]]; then
  err "crashpilot binary not found in venv after install — something went wrong"
  exit 1
fi
ok "CrashPilot installed in venv: $VENV_DIR"

# Create a wrapper script in a system PATH location
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
  # System-wide install: /usr/local/bin is readable by all users
  create_wrapper /usr/local/bin/crashpilot
  # Make the venv readable by all users (it lives in /opt/crashpilot)
  chmod -R a+rX "$DATA_DIR"
  ok "Installed wrapper: /usr/local/bin/crashpilot"
else
  LOCAL_BIN="$HOME/.local/bin"
  mkdir -p "$LOCAL_BIN"
  create_wrapper "$LOCAL_BIN/crashpilot"
  ok "Installed wrapper: $LOCAL_BIN/crashpilot"
  if [[ ":$PATH:" != *":$LOCAL_BIN:"* ]]; then
    warn "Add to PATH: export PATH=\"\$HOME/.local/bin:\$PATH\""
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
  sudo systemctl enable --now "crashpilot-api@root" 2>/dev/null || \
    sudo systemctl start "crashpilot-api@root" 2>/dev/null || true

  # Install heartbeat timer (starts automatically when push mode is configured)
  if [[ -f "$service_src/crashpilot-heartbeat.service" && -f "$service_src/crashpilot-heartbeat.timer" ]]; then
    sed "s|__CRASHPILOT_BIN__|$VENV_DIR/bin/crashpilot|g" \
      "$service_src/crashpilot-heartbeat.service" > /tmp/crashpilot-heartbeat.service
    sudo cp /tmp/crashpilot-heartbeat.service "$svc_dir/crashpilot-heartbeat.service"
    sudo cp "$service_src/crashpilot-heartbeat.timer" "$svc_dir/crashpilot-heartbeat.timer"
    sudo systemctl daemon-reload
    sudo systemctl enable --now crashpilot-heartbeat.timer 2>/dev/null || true
  fi

  ok "systemd services installed and API server started"
  echo -e "  Boot analysis enabled: will run once per boot"
  echo -e "  Heartbeat timer: enabled (will ping dashboard every 60 s once configured)"
}

install_openrc_services() {
  # OpenRC is unreachable while support is Ubuntu-only.
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
  # runit is unreachable while support is Ubuntu-only.
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

# ── Auto-connect to the dashboard (push mode) ──────────────────────────────────
# Triggered by the one-liner the dashboard shows:
#   curl -fsSL .../install.sh | sudo bash -s -- --connect cpilot_<string>
CONNECTED=0
if [[ -n "$CONNECT_STRING" ]]; then
  section "Connecting to dashboard"
  if [[ -z "$CRASHPILOT_BIN" ]]; then
    err "Cannot connect — the CrashPilot CLI is not available."
  elif "$CRASHPILOT_BIN" configure "$CONNECT_STRING"; then
    # `configure` enables the heartbeat timer and sends the first heartbeat itself,
    # so the system is online as soon as this returns.
    CONNECTED=1
  else
    err "Could not connect with that connection string."
    err "Get a fresh one from the dashboard → Systems → Add system."
  fi
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}${BOLD}✓ Installation complete!${RESET}"
echo ""
echo -e "  Platform: ${BOLD}${DISTRO} ${DISTRO_VER}${RESET} | Init: ${BOLD}${INIT_SYS}${RESET}"
[[ $IS_WSL -eq 1 ]] && echo -e "  Mode: ${YELLOW}WSL ${WSL_VER}${RESET}"
echo ""
echo -e "  ${BOLD}Next steps:${RESET}"
echo ""
if [[ $CONNECTED -eq 1 ]]; then
  echo -e "  ${GREEN}✓ Connected to the dashboard${RESET} — view it at:"
  echo -e "     ${CYAN}https://kdigitalsystems.github.io/CrashPilot/${RESET}"
  echo ""
  echo -e "  ${BOLD}1.${RESET} ${DIM}(Optional)${RESET} Add an Anthropic API key for AI root-cause analysis:"
  echo -e "     ${CYAN}sudo nano $CONFIG_DIR/.env${RESET}   ${DIM}# set CRASHPILOT_ANTHROPIC_API_KEY${RESET}"
  echo ""
  echo -e "  ${BOLD}2.${RESET} Run your first analysis:"
  echo -e "     ${CYAN}sudo crashpilot analyze${RESET}"
else
  echo -e "  ${BOLD}1.${RESET} Connect to the dashboard ${DIM}— one command, no open ports needed:${RESET}"
  echo -e "     a. Sign in at ${CYAN}https://kdigitalsystems.github.io/CrashPilot/${RESET}"
  echo -e "        Go to ${BOLD}Systems → Add system${RESET}, enter a name, choose ${BOLD}Push mode${RESET}."
  echo -e "     b. Copy the one-line command it shows and run it here. It looks like:"
  echo -e "        ${CYAN}curl -fsSL .../install.sh | sudo bash -s -- --connect cpilot_<string>${RESET}"
  echo -e "        ${DIM}(or, since it's already installed: ${RESET}${CYAN}sudo crashpilot configure cpilot_<string>${RESET}${DIM})${RESET}"
  echo ""
  echo -e "  ${BOLD}2.${RESET} ${DIM}(Optional)${RESET} Add an Anthropic API key for AI root-cause analysis:"
  echo -e "     ${CYAN}sudo nano $CONFIG_DIR/.env${RESET}   ${DIM}# set CRASHPILOT_ANTHROPIC_API_KEY${RESET}"
  echo ""
  echo -e "  ${BOLD}3.${RESET} Run your first analysis:"
  echo -e "     ${CYAN}sudo crashpilot analyze${RESET}"
fi
echo ""
echo -e "  ${DIM}Something not working? Run ${RESET}${CYAN}sudo crashpilot doctor${RESET}${DIM} — it diagnoses config, connection, and the timer.${RESET}"
echo ""

# Clean up the temp clone directory if we created one during curl-pipe install
if [[ -n "${CLONE_DIR:-}" && -d "${CLONE_DIR:-}" ]]; then
  rm -rf "$CLONE_DIR"
fi
