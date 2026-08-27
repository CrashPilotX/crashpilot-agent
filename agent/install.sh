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
fi

if [[ -z "${REPO_DIR:-}" || ! -f "$REPO_DIR/agent/pyproject.toml" ]]; then
  # curl-pipe install: fetch the public agent bundle. The GitHub repository may
  # stay private; the website publishes just the installable agent files.
  CLONE_DIR="$(mktemp -d)/CrashPilot"
  BUNDLE_URL="${CRASHPILOT_BUNDLE_URL:-https://crashpilotx.com/crashpilot-agent.tar.gz}"
  echo "[info]  Standalone installer detected: downloading agent bundle..."
  if command -v curl &>/dev/null && command -v tar &>/dev/null; then
    mkdir -p "$(dirname "$CLONE_DIR")"
    curl -fsSL "$BUNDLE_URL" | tar -xz -C "$(dirname "$CLONE_DIR")" \
      || { echo "[err ]  agent bundle download failed: $BUNDLE_URL"; exit 1; }
  else
    echo "[err ]  curl and tar are required for curl-pipe installs. Install them with:"
    echo "        sudo apt-get install curl tar"
    exit 1
  fi
  REPO_DIR="$CLONE_DIR"
  if [[ ! -f "$REPO_DIR/agent/pyproject.toml" ]]; then
    echo "[err ]  downloaded bundle did not contain the CrashPilot agent"
    exit 1
  fi
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
    --connect)
      if [[ $# -lt 2 || -z "${2:-}" || "${2:-}" == --* ]]; then
        err "--connect requires a cpilot_<connection-string> value."
        echo "Usage: install.sh --connect cpilot_<connection-string>"
        exit 2
      fi
      CONNECT_STRING="$2"; shift 2 ;;
    --connect=*)
      CONNECT_STRING="${1#*=}"
      if [[ -z "$CONNECT_STRING" ]]; then
        err "--connect requires a cpilot_<connection-string> value."
        echo "Usage: install.sh --connect cpilot_<connection-string>"
        exit 2
      fi
      shift ;;
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
  AI-powered Linux crash forensics  v0.2
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

_sudo() {
  if [[ $EUID -eq 0 ]]; then
    "$@"
  else
    sudo "$@"
  fi
}

install_python() {
  _sudo apt-get install -y python3 python3-pip python3-venv
}

if ! command -v python3 &>/dev/null; then
  warn "Python 3 not found: installing..."
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
  if [[ "${kernel_major:-0}" -ge 5 ]]; then
    echo "rasdaemon"
  else
    echo "mcelog"
  fi
}

# Try to install a single package; never exits: returns 0/1.
_try_install_pkg() {
  local pkg="$1"
  _sudo apt-get install -y "$pkg" &>/dev/null && return 0
  return 1
}

install_optional_tools() {
  # Ubuntu apt package names for optional tools.
  declare -A PKG_APT=(
    [smartctl]=smartmontools
    [sensors]=lm-sensors
    [mcelog]="$(_mce_package)"
  )

  local missing=()
  for tool in smartctl sensors journalctl dmesg nvidia-smi; do
    if command -v "$tool" &>/dev/null; then
      ok "  $tool"
    else
      warn "  $tool: not found"
      local pkg=""
      pkg="${PKG_APT[$tool]:-}"
      [[ -n "$pkg" ]] && missing+=("$pkg")
    fi
  done

  # MCE tool (separate because the package name varies by kernel version)
  local mce_pkg
  mce_pkg="$(_mce_package)"
  if ! command -v mcelog &>/dev/null && ! command -v rasdaemon &>/dev/null; then
    warn "  mcelog/rasdaemon: not found"
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

  if [[ ! -t 0 ]]; then
    warn "Non-interactive install detected: skipping optional packages (${unique[*]})"
    warn "Install them later if needed: sudo apt-get install ${unique[*]}"
    return
  fi

  read -rp "$(echo -e "${YELLOW}Install missing packages (${unique[*]})?${RESET} [y/N] ")" ans
  if [[ ! "$ans" =~ ^[Yy]$ ]]; then
    warn "Skipping optional packages: some collectors will be limited"
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

install_speedtest_cli() {
  if command -v speedtest-cli &>/dev/null; then
    ok "  speedtest-cli"
    return
  fi

  info "Installing speedtest-cli for internet capacity checks (best-effort)..."
  if _try_install_pkg speedtest-cli; then
    ok "  speedtest-cli"
  else
    warn "  speedtest-cli: not available (passive network throughput will still work)"
  fi
}

install_speedtest_cli

# ── journalctl permission check ───────────────────────────────────────────────
section "Checking log access"

if command -v journalctl &>/dev/null; then
  if journalctl --lines=1 &>/dev/null; then
    ok "journalctl readable"
  elif [[ $EUID -eq 0 ]]; then
    ok "journalctl readable (root)"
  else
    warn "journalctl restricted: adding $USER to systemd-journal group"
    if getent group systemd-journal &>/dev/null; then
      sudo usermod -aG systemd-journal "$USER" && ok "Added to systemd-journal (re-login required)"
    fi
  fi
fi

if [[ $IS_WSL -eq 1 && ${WSL_VER:-1} -eq 1 ]]; then
  warn "WSL1 detected: journalctl and dmesg are not available"
  warn "Analysis will use Windows Event Log via PowerShell interop"
fi

# ── Create directories and config ─────────────────────────────────────────────
section "Setting up configuration"

mkdir -p "$CONFIG_DIR" "$DATA_DIR"
# Root installs keep the directory traversable for packaged tooling, but the
# .env file itself contains secrets once push mode is configured.
if [[ $EUID -eq 0 ]]; then
  chmod 755 "$CONFIG_DIR"
else
  chmod 700 "$CONFIG_DIR"
fi

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

# Internet capacity test. Passive network throughput is always collected.
# The installer installs speedtest-cli when available; results are cached.
CRASHPILOT_BANDWIDTH_SPEEDTEST_ENABLED=true
CRASHPILOT_BANDWIDTH_SPEEDTEST_INTERVAL_SECONDS=21600
CRASHPILOT_BANDWIDTH_SPEEDTEST_TIMEOUT_SECONDS=90

# Data storage
# CRASHPILOT_DATA_DIR=/var/lib/crashpilot  # uncomment for system-wide install
ENVEOF
  # Private because push-mode credentials are stored here after configure.
  # User installs: 600 (private: only the owning user needs it)
  chmod 600 "$CONFIG_DIR/.env"
  ok "Created config: $CONFIG_DIR/.env"
  echo -e "\n  ${YELLOW}ACTION REQUIRED:${RESET} Add your Anthropic API key:"
  echo -e "  ${CYAN}  nano $CONFIG_DIR/.env${RESET}"
else
  ok "Config exists: $CONFIG_DIR/.env"
fi

ensure_env_default() {
  local key="$1"
  local value="$2"
  if ! grep -Eq "^[[:space:]]*${key}=" "$CONFIG_DIR/.env"; then
    printf '\n%s=%s\n' "$key" "$value" >> "$CONFIG_DIR/.env"
    ok "Added ${key}=${value}"
  fi
}

ensure_env_default CRASHPILOT_BANDWIDTH_SPEEDTEST_ENABLED true
ensure_env_default CRASHPILOT_BANDWIDTH_SPEEDTEST_INTERVAL_SECONDS 21600
ensure_env_default CRASHPILOT_BANDWIDTH_SPEEDTEST_TIMEOUT_SECONDS 90

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
  python3 -m venv "$VENV_DIR" || { err "Cannot create venv: install python3-venv manually"; exit 1; }
fi

# Bootstrap pip: Ubuntu 24.04 venvs sometimes ship without it
if [[ ! -x "$VENV_DIR/bin/pip" ]]; then
  info "pip missing from venv: bootstrapping with ensurepip..."
  "$VENV_DIR/bin/python3" -m ensurepip --upgrade 2>/dev/null || \
  curl -sSf https://bootstrap.pypa.io/get-pip.py | "$VENV_DIR/bin/python3" || \
  { err "Cannot bootstrap pip: install python3-pip manually"; exit 1; }
fi

info "Upgrading pip..."
"$VENV_DIR/bin/pip" install --quiet --upgrade pip || true

info "Installing CrashPilot package..."
# Use a regular (non-editable) install so the package is fully copied into the
# venv's site-packages. An editable install (-e) would leave a .pth pointer
# back to the source directory, which breaks when that directory is deleted
# (e.g. after a curl-pipe install where we cloned to a temp dir).
if ! "$VENV_DIR/bin/pip" install --quiet "$REPO_DIR/agent"; then
  err "pip install failed: check output above"
  exit 1
fi

# Verify the binary exists before declaring success
if [[ ! -x "$VENV_DIR/bin/crashpilot" ]]; then
  err "crashpilot binary not found in venv after install: something went wrong"
  exit 1
fi
ok "CrashPilot installed in venv: $VENV_DIR"

# Create a wrapper script in a system PATH location
create_wrapper() {
  local target="$1"
  mkdir -p "$(dirname "$target")"
  cat > "$target" << WRAPPER
#!/bin/bash
# CrashPilot wrapper: generated by install.sh
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

  # Install the verified daily update check.
  if [[ -f "$service_src/crashpilot-update.service" && -f "$service_src/crashpilot-update.timer" ]]; then
    sed "s|__CRASHPILOT_BIN__|$VENV_DIR/bin/crashpilot|g" \
      "$service_src/crashpilot-update.service" > /tmp/crashpilot-update.service
    sudo cp /tmp/crashpilot-update.service "$svc_dir/crashpilot-update.service"
    sudo cp "$service_src/crashpilot-update.timer" "$svc_dir/crashpilot-update.timer"
    sudo systemctl daemon-reload
    sudo systemctl enable --now crashpilot-update.timer 2>/dev/null || true
  fi

  # Install the rolling flight recorder.
  if [[ -f "$service_src/crashpilot-snapshot.service" && -f "$service_src/crashpilot-snapshot.timer" ]]; then
    sed "s|__CRASHPILOT_BIN__|$VENV_DIR/bin/crashpilot|g" \
      "$service_src/crashpilot-snapshot.service" > /tmp/crashpilot-snapshot.service
    sudo cp /tmp/crashpilot-snapshot.service "$svc_dir/crashpilot-snapshot.service"
    sudo cp "$service_src/crashpilot-snapshot.timer" "$svc_dir/crashpilot-snapshot.timer"
    sudo systemctl daemon-reload
    sudo systemctl enable --now crashpilot-snapshot.timer 2>/dev/null || true
  fi

  ok "systemd services installed and API server started"
  echo -e "  Boot analysis enabled: will run once per boot"
  echo -e "  Heartbeat timer: enabled (will ping dashboard every 60 s once configured)"
  echo -e "  Automatic updates: enabled (verified daily update check)"
  echo -e "  Flight recorder: enabled (rolling one-minute snapshots)"
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

if [[ $IS_WSL -eq 1 && "$INIT_SYS" == "systemd" ]]; then
  if [[ $EUID -eq 0 ]]; then
    info "WSL with systemd detected: installing heartbeat timer"
    install_systemd_services
  else
    read -rp "Install systemd services for WSL (requires sudo)? [Y/n] " ans
    if [[ ! "$ans" =~ ^[Nn]$ ]]; then
      install_systemd_services
    else
      info "Skipping systemd services"
      echo -e "  ${DIM}Run manually after connecting: crashpilot heartbeat${RESET}"
    fi
  fi

elif [[ $IS_WSL -eq 1 ]]; then
  info "WSL without systemd detected: skipping service installation"
  echo -e "  ${DIM}Run manually after connecting: crashpilot heartbeat${RESET}"
  echo -e "  ${DIM}Or enable systemd in WSL2 for automatic heartbeat timers.${RESET}"

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
  warn "Unknown init system '$INIT_SYS': skipping service installation"
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
    err "Cannot connect: the CrashPilot CLI is not available."
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
  echo -e "  ${GREEN}✓ Connected to the dashboard${RESET}: view it at:"
  echo -e "     ${CYAN}https://crashpilotx.com/${RESET}"
  echo ""
  echo -e "  ${BOLD}1.${RESET} ${DIM}(Optional)${RESET} Add an Anthropic API key for AI root-cause analysis:"
  echo -e "     ${CYAN}sudo nano $CONFIG_DIR/.env${RESET}   ${DIM}# set CRASHPILOT_ANTHROPIC_API_KEY${RESET}"
  echo ""
  echo -e "  ${BOLD}2.${RESET} Run your first analysis:"
  echo -e "     ${CYAN}sudo crashpilot analyze${RESET}"
else
  echo -e "  ${BOLD}1.${RESET} Connect to the dashboard ${DIM}- one command, no open ports needed:${RESET}"
  echo -e "     a. Sign in at ${CYAN}https://crashpilotx.com/${RESET}"
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
echo -e "  ${DIM}Something not working? Run ${RESET}${CYAN}sudo crashpilot doctor${RESET}${DIM}: it diagnoses config, connection, and the timer.${RESET}"
echo ""

# Clean up the temp clone directory if we created one during curl-pipe install
if [[ -n "${CLONE_DIR:-}" && -d "${CLONE_DIR:-}" ]]; then
  rm -rf "$CLONE_DIR"
fi
