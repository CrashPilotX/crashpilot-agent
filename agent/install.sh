#!/usr/bin/env bash
# CrashPilot Universal Installer
# Supports: Ubuntu Linux and Ubuntu on WSL1/WSL2.
set -uo pipefail   # no -e: we handle errors explicitly so one bad package can't abort

# Everything runs inside main(), called on the last line. Under `curl | bash`
# a script runs as it arrives, so a download cut short ran a prefix of the
# installer and usually exited 0. Bash reads a function to its closing brace
# before running any of it, so a cut-off download now runs nothing.
main() {

# ── Locate the repo ───────────────────────────────────────────────────────────
# When run as `bash script.sh` from inside the repo, the script file is the
# one that called main() (BASH_SOURCE[1]; [0] is where main is defined) and
# REPO_DIR is its parent directory.
# When piped in via `bash -c "$(curl ...)"` or `curl | bash`, there is no such
# file ([1] is unset), so we download the agent bundle to a temp directory instead.
_src="${BASH_SOURCE[1]:-}"
if [[ -n "$_src" && "$_src" != "bash" && -f "$_src" ]]; then
  REPO_DIR="$(cd "$(dirname "$_src")/.." && pwd)"
fi

# Every temporary file this script writes lives in one private directory
# (mktemp -d is mode 0700) that is removed however the script exits. Fixed
# names under /tmp let a local user pre-create or race the files that are
# later copied into /etc/systemd/system as root.
WORK_DIR="$(mktemp -d)" || { echo "[err ]  could not create a temporary directory"; exit 1; }
cleanup_work_dir() { rm -rf "$WORK_DIR"; }
trap cleanup_work_dir EXIT

if [[ -z "${REPO_DIR:-}" || ! -f "$REPO_DIR/agent/pyproject.toml" ]]; then
  # curl-pipe install: fetch the agent bundle the website publishes, which is
  # built from a pinned commit of this repository.
  BUNDLE_PARENT="$WORK_DIR/bundle"
  mkdir -p "$BUNDLE_PARENT"
  CLONE_DIR="$BUNDLE_PARENT/CrashPilot"
  BUNDLE_URL="${CRASHPILOT_BUNDLE_URL:-https://crashpilotx.com/crashpilot-agent.tar.gz}"
  BUNDLE_SHA_URL="${CRASHPILOT_BUNDLE_SHA_URL:-${BUNDLE_URL}.sha256}"
  # The website's publish step writes the bundle's digest into this line, so
  # the installer and the bundle it installs are checked against each other
  # rather than only against a .sha256 fetched from the same place. Left as
  # the placeholder (running from a checkout, or a mirror), the published
  # .sha256 is used instead.
  EXPECTED_BUNDLE_SHA256="${CRASHPILOT_BUNDLE_SHA256:-__CRASHPILOT_BUNDLE_SHA256__}"
  echo "[info]  Standalone installer detected: downloading agent bundle..."

  case "$BUNDLE_URL$BUNDLE_SHA_URL" in
    *http://*)
      echo "[err ]  the agent bundle must be fetched over https:// (got $BUNDLE_URL)"
      exit 1 ;;
  esac

  if ! command -v curl &>/dev/null || ! command -v tar &>/dev/null; then
    echo "[err ]  curl and tar are required for curl-pipe installs. Install them with:"
    echo "        sudo apt-get install curl tar"
    exit 1
  fi
  # This bundle is unpacked and installed as root, so a corrupted or
  # substituted download is not something to find out about later. Verification
  # is mandatory rather than best effort: with no sha256sum there is no way to
  # check, so stop rather than install something unverified.
  if ! command -v sha256sum &>/dev/null; then
    echo "[err ]  sha256sum is required to verify the agent bundle. Install it with:"
    echo "        sudo apt-get install coreutils"
    exit 1
  fi

  # Download to a file rather than piping into tar: a stream cannot be checked
  # until it has already been extracted.
  BUNDLE_FILE="$BUNDLE_PARENT/crashpilot-agent.tar.gz"
  CURL_SAFE=(curl --proto '=https' --tlsv1.2 -fsSL --retry 3 --connect-timeout 15 --max-time 300)
  "${CURL_SAFE[@]}" "$BUNDLE_URL" -o "$BUNDLE_FILE" \
    || { echo "[err ]  agent bundle download failed: $BUNDLE_URL"; exit 1; }
  if [[ "$EXPECTED_BUNDLE_SHA256" =~ ^[0-9a-f]{64}$ ]]; then
    printf '%s  crashpilot-agent.tar.gz\n' "$EXPECTED_BUNDLE_SHA256" > "${BUNDLE_FILE}.sha256"
  else
    "${CURL_SAFE[@]}" "$BUNDLE_SHA_URL" -o "${BUNDLE_FILE}.sha256" \
      || { echo "[err ]  agent bundle checksum download failed: $BUNDLE_SHA_URL"; exit 1; }
  fi

  # The published .sha256 is in sha256sum's own format and names the tarball,
  # so check it from the directory holding both files.
  if ! ( cd "$BUNDLE_PARENT" && sha256sum -c crashpilot-agent.tar.gz.sha256 >/dev/null 2>&1 ); then
    echo "[err ]  agent bundle failed checksum verification."
    echo "        Expected: $(cut -d' ' -f1 < "${BUNDLE_FILE}.sha256")"
    echo "        Actual:   $(sha256sum "$BUNDLE_FILE" | cut -d' ' -f1)"
    echo "        Refusing to install. Retry, and report this if it persists."
    exit 1
  fi
  echo "[ok  ]  agent bundle checksum verified"

  tar -xzf "$BUNDLE_FILE" -C "$BUNDLE_PARENT" \
    || { echo "[err ]  agent bundle could not be extracted"; exit 1; }

  REPO_DIR="$CLONE_DIR"
  if [[ ! -f "$REPO_DIR/agent/pyproject.toml" ]]; then
    echo "[err ]  downloaded bundle did not contain the CrashPilot agent"
    exit 1
  fi
fi

INSTALL_SYSTEMD="${INSTALL_SYSTEMD:-auto}"  # auto | yes | no
INSTALL_USER="${SUDO_USER:-$(id -un)}"

# When running as root (sudo), install to system-wide paths so any user can
# invoke `crashpilot`.  When running as a normal user, install to $HOME.
# CRASHPILOT_INSTALL_DIR moves the install. It used to read
# CRASHPILOT_DATA_DIR, which is also the agent's own data-directory setting,
# so exporting it for the agent and re-running this script silently moved
# the venv and rewrote every unit to point at the new path.
if [[ $EUID -eq 0 ]]; then
  CONFIG_DIR="${CRASHPILOT_CONFIG_DIR:-/etc/crashpilot}"
  DATA_DIR="${CRASHPILOT_INSTALL_DIR:-/opt/crashpilot}"
else
  CONFIG_DIR="${CRASHPILOT_CONFIG_DIR:-$HOME/.config/crashpilot}"
  DATA_DIR="${CRASHPILOT_INSTALL_DIR:-$HOME/.local/share/crashpilot}"
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
# --enroll <cpjoin_…>   : after installing, enroll this machine with a join
#                         token (cloud-init, machine images, automated fleets).
# Bare cpilot_… / cpjoin_… positional arguments are also accepted, and both can
# come from the CRASHPILOT_CONNECT / CRASHPILOT_ENROLL environment variables.
CONNECT_STRING="${CRASHPILOT_CONNECT:-}"
ENROLL_STRING="${CRASHPILOT_ENROLL:-}"
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
    --enroll)
      if [[ $# -lt 2 || -z "${2:-}" || "${2:-}" == --* ]]; then
        err "--enroll requires a cpjoin_<join-token> value."
        echo "Usage: install.sh --enroll cpjoin_<join-token>"
        exit 2
      fi
      ENROLL_STRING="$2"; shift 2 ;;
    --enroll=*)
      ENROLL_STRING="${1#*=}"
      if [[ -z "$ENROLL_STRING" ]]; then
        err "--enroll requires a cpjoin_<join-token> value."
        echo "Usage: install.sh --enroll cpjoin_<join-token>"
        exit 2
      fi
      shift ;;
    cpilot_*)       CONNECT_STRING="$1"; shift ;;
    cpjoin_*)       ENROLL_STRING="$1"; shift ;;
    -h|--help)
      echo "Usage: install.sh [--connect cpilot_<connection-string> | --enroll cpjoin_<join-token>]"
      echo "  --connect <string>   Install, then connect this system to the dashboard (push mode)."
      echo "  --enroll <token>     Install, then enroll this machine with a join token (automated fleets)."
      echo "  (bare cpilot_…/cpjoin_… arguments, or \$CRASHPILOT_CONNECT / \$CRASHPILOT_ENROLL, also work)"
      exit 0 ;;
    *)              warn "Ignoring unknown argument: $1"; shift ;;
  esac
done

if [[ -n "$CONNECT_STRING" && -n "$ENROLL_STRING" ]]; then
  err "Use either --connect (one system from the dashboard) or --enroll (a join token), not both."
  exit 2
fi

# A root install writes these paths into systemd units, where whitespace
# splits ExecStart and % starts a specifier, and through sed, where & and |
# are special. Refuse anything else up front rather than write broken units.
unit_safe_path() {
  [[ "$1" =~ ^/[A-Za-z0-9._/@+:-]+$ ]]
}

if [[ $EUID -eq 0 ]]; then
  for path in "$DATA_DIR" "$CONFIG_DIR"; do
    if ! unit_safe_path "$path"; then
      err "Unusable install or config directory: '$path'."
      err "Use an absolute path of letters, digits and . _ - / @ + : (CRASHPILOT_INSTALL_DIR, CRASHPILOT_CONFIG_DIR)."
      exit 2
    fi
  done
fi

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
  # A fresh minimal image has empty package lists, so install alone fails.
  _sudo env DEBIAN_FRONTEND=noninteractive apt-get update -qq \
    || warn "apt-get update failed; trying to install from the existing package lists"
  _sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-pip python3-venv
}

if ! command -v python3 &>/dev/null; then
  warn "Python 3 not found: installing..."
  install_python
  if ! command -v python3 &>/dev/null; then
    err "Python 3 could not be installed. Install python3 (3.10 or newer) and run this again."
    exit 1
  fi
fi

PYTHON_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
if python3 -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)"; then
  ok "Python $PYTHON_VER"
else
  warn "Python $PYTHON_VER found but 3.10+ recommended. Attempting upgrade..."
  install_python
  PYTHON_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || echo "unknown")
  if python3 -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" 2>/dev/null; then
    ok "Python $PYTHON_VER"
  else
    err "Python 3.10 or newer is required (found $PYTHON_VER)."
    exit 1
  fi
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
    warn "journalctl restricted: adding $INSTALL_USER to systemd-journal group"
    if getent group systemd-journal &>/dev/null; then
      _sudo usermod -aG systemd-journal "$INSTALL_USER" && ok "Added to systemd-journal (re-login required)"
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
  if ! "$VENV_DIR/bin/python3" -m ensurepip --upgrade 2>/dev/null; then
    # No unverified get-pip.py piped into Python as root: use the distro's
    # own packages, then rebuild the venv so it picks them up.
    _try_install_pkg python3-pip || true
    _try_install_pkg python3-venv || true
    rm -rf "$VENV_DIR"
    python3 -m venv "$VENV_DIR" && "$VENV_DIR/bin/python3" -m ensurepip --upgrade 2>/dev/null \
      || { err "Cannot bootstrap pip: install python3-pip and python3-venv, then run this again"; exit 1; }
  fi
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
  # Other users need the venv to run the CLI, so share exactly that. The data
  # directory holds the local API token (agent.token), the crash database and
  # journal/dmesg caches; the old recursive chmod over all of $DATA_DIR made
  # them world-readable on every install and upgrade. Create it private now,
  # before a unit can create it 0755, and re-close anything a previous run
  # opened.
  chmod a+rX "$DATA_DIR"
  chmod -R a+rX "$VENV_DIR"
  install -d -m 0700 "$DATA_DIR/data"
  chmod -R go-rwx "$DATA_DIR/data"
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

# Problems worth failing the run over are collected here and reported at the
# end, so automation driving this script gets a non-zero exit instead of
# "Installation complete!" over a half-installed agent.
INSTALL_PROBLEMS=()

# Render one unit into the private work dir and install it root-owned 0644.
# Writing straight into a fixed /tmp name first let a local user substitute
# their own unit file between the render and the copy.
install_unit() {
  local src="$1" dest_name="$2"
  local rendered="$WORK_DIR/$dest_name"
  sed "s|__CRASHPILOT_BIN__|$VENV_DIR/bin/crashpilot|g" "$src" > "$rendered" || return 1
  _sudo install -m 0644 -o root -g root "$rendered" "/etc/systemd/system/$dest_name"
}

# The agent finds its config and data by itself only in the default places
# (/etc/crashpilot, /opt/crashpilot/data); a moved install used to send the
# data to /root, where the sandboxed boot analysis cannot write. Tell each
# service where they are in a drop-in, which an agent update refreshing the
# units leaves alone. With the defaults, a drop-in from an earlier run is
# removed.
install_paths_dropin() {
  local unit="$1" dir="/etc/systemd/system/$1.d" conf="$WORK_DIR/$1.paths.conf"
  local lines=""
  [[ "$CONFIG_DIR" != "/etc/crashpilot" ]] && lines+="Environment=CRASHPILOT_CONFIG_DIR=$CONFIG_DIR"$'\n'
  if [[ "$DATA_DIR" != "/opt/crashpilot" ]]; then
    lines+="Environment=CRASHPILOT_DATA_DIR=$DATA_DIR/data"$'\n'
    # The boot analysis runs with /var read-only and home directories protected.
    [[ "$unit" == "crashpilot.service" ]] && lines+="ReadWritePaths=-$DATA_DIR/data"$'\n'
  fi
  if [[ -z "$lines" ]]; then
    _sudo rm -f "$dir/10-crashpilot-paths.conf"
    return 0
  fi
  printf '[Service]\n%s' "$lines" > "$conf" || return 1
  _sudo install -d -m 0755 "$dir" \
    && _sudo install -m 0644 -o root -g root "$conf" "$dir/10-crashpilot-paths.conf"
}

install_systemd_services() {
  local service_src="$REPO_DIR/systemd"
  local unit timer failed=0

  if ! install_unit "$service_src/crashpilot.service" crashpilot.service \
     || ! install_unit "$service_src/crashpilot-api.service" crashpilot-api@.service; then
    INSTALL_PROBLEMS+=("could not install the core systemd units")
    return 1
  fi
  # Signs off on a clean shutdown, so a reboot or scale-in reads as expected
  # quiet rather than an outage. It has to be started now for its ExecStop to
  # run at shutdown.
  if [[ -f "$service_src/crashpilot-signoff.service" ]]; then
    install_unit "$service_src/crashpilot-signoff.service" crashpilot-signoff.service || failed=1
  fi

  # Heartbeat, the verified hourly update check, and the rolling flight
  # recorder. Each ships as a service plus a timer; install whichever this
  # bundle has.
  local timers=(crashpilot-heartbeat.timer crashpilot-update.timer crashpilot-snapshot.timer)
  for timer in "${timers[@]}"; do
    unit="${timer%.timer}.service"
    if [[ -f "$service_src/$unit" && -f "$service_src/$timer" ]]; then
      install_unit "$service_src/$unit" "$unit" || failed=1
      install_unit "$service_src/$timer" "$timer" || failed=1
    fi
  done

  for unit in crashpilot.service crashpilot-api@.service crashpilot-signoff.service \
              crashpilot-heartbeat.service crashpilot-update.service crashpilot-snapshot.service; do
    if [[ -f "/etc/systemd/system/$unit" ]]; then
      install_paths_dropin "$unit" || failed=1
    fi
  done

  _sudo systemctl daemon-reload || failed=1
  _sudo systemctl enable crashpilot.service >/dev/null 2>&1 || failed=1
  _sudo systemctl enable --now "crashpilot-api@root" >/dev/null 2>&1 || failed=1
  # An upgrade replaced the code under a running API server; enable --now is a
  # no-op for an active unit, so restart it onto the new version.
  _sudo systemctl try-restart "crashpilot-api@root" >/dev/null 2>&1 || true
  for timer in "${timers[@]}"; do
    if [[ -f "/etc/systemd/system/$timer" ]]; then
      _sudo systemctl enable --now "$timer" >/dev/null 2>&1 || failed=1
    fi
  done
  if [[ -f /etc/systemd/system/crashpilot-signoff.service ]]; then
    _sudo systemctl enable --now crashpilot-signoff.service >/dev/null 2>&1 || failed=1
  fi

  if [[ $failed -ne 0 ]]; then
    INSTALL_PROBLEMS+=("some systemd units could not be installed or enabled; check: systemctl status 'crashpilot*'")
    warn "Some systemd units could not be installed or enabled"
    return 1
  fi

  ok "systemd services installed and API server started"
  echo -e "  Boot analysis enabled: will run once per boot"
  echo -e "  Heartbeat timer: enabled (will ping dashboard every 60 s once configured)"
  echo -e "  Automatic updates: enabled (verified hourly update check)"
  echo -e "  Flight recorder: enabled (rolling one-minute snapshots)"
}

install_openrc_services() {
  # OpenRC is unreachable while support is Ubuntu-only.
  cat > "$WORK_DIR/crashpilot-rc" << RCEOF
#!/sbin/openrc-run
description="CrashPilot crash analysis"
command="$VENV_DIR/bin/crashpilot"
command_args="analyze"
command_user="$INSTALL_USER"
depend() { need localmount logger; }
RCEOF
  _sudo install -m 0755 -o root -g root "$WORK_DIR/crashpilot-rc" /etc/init.d/crashpilot
  _sudo rc-update add crashpilot default
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

# Whether to install the systemd units: only on a root install (the
# dashboard one-liner runs under sudo). The units run as root, and a
# non-root install's venv is owned by that user, so units pointing into it
# would run code the user can change, as root; they would also read root's
# config, not the user's. INSTALL_SYSTEMD=no is checked first, so it holds
# on WSL as well.
systemd_install_consented() {
  [[ $EUID -eq 0 ]] && return 0
  warn "Skipping systemd services: they run as root, so only a root install sets them up."
  warn "To install them, re-run the installer with sudo."
  if [[ "$INSTALL_SYSTEMD" == "yes" ]]; then
    INSTALL_PROBLEMS+=("INSTALL_SYSTEMD=yes needs a root install: re-run with sudo")
  fi
  return 1
}

if [[ "$INSTALL_SYSTEMD" == "no" ]]; then
  info "Systemd install skipped (INSTALL_SYSTEMD=no)"

elif [[ $IS_WSL -eq 1 && "$INIT_SYS" == "systemd" ]]; then
  if systemd_install_consented; then
    info "WSL with systemd detected: installing heartbeat timer"
    install_systemd_services
  else
    echo -e "  ${DIM}Run manually after connecting: crashpilot heartbeat${RESET}"
  fi

elif [[ $IS_WSL -eq 1 ]]; then
  info "WSL without systemd detected: skipping service installation"
  echo -e "  ${DIM}Run manually after connecting: crashpilot heartbeat${RESET}"
  echo -e "  ${DIM}Or enable systemd in WSL2 for automatic heartbeat timers.${RESET}"

elif [[ "$INIT_SYS" == "systemd" ]]; then
  if systemd_install_consented; then
    install_systemd_services
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
  INSTALL_PROBLEMS+=("the crashpilot CLI did not run after installation")
fi

# ── Auto-connect to the dashboard (push mode) ──────────────────────────────────
# Triggered by the one-liner the dashboard shows:
#   curl -fsSL .../install.sh | sudo bash -s -- --connect cpilot_<string>
CONNECTED=0
if [[ -n "$CONNECT_STRING" ]]; then
  section "Connecting to dashboard"
  if [[ -z "$CRASHPILOT_BIN" ]]; then
    err "Cannot connect: the CrashPilot CLI is not available."
  # On stdin, not as an argument: every local user can read another
  # process's arguments in /proc/<pid>/cmdline.
  elif printf '%s\n' "$CONNECT_STRING" | "$CRASHPILOT_BIN" configure -; then
    # `configure` enables the heartbeat timer and sends the first heartbeat itself,
    # so the system is online as soon as this returns.
    CONNECTED=1
  else
    err "Could not connect with that connection string."
    err "Get a fresh one from the dashboard → Systems → Add system."
    INSTALL_PROBLEMS+=("connecting to the dashboard failed")
  fi
fi

# ── Enroll with a join token (automated fleets) ────────────────────────────────
#   curl -fsSL .../install.sh | sudo bash -s -- --enroll cpjoin_<token>
if [[ -n "$ENROLL_STRING" ]]; then
  section "Enrolling with the dashboard"
  if [[ -z "$CRASHPILOT_BIN" ]]; then
    err "Cannot enroll: the CrashPilot CLI is not available."
    INSTALL_PROBLEMS+=("enrolling with the dashboard failed")
  # On stdin (see configure above), which `enroll` also saves the token from,
  # before it tries, so a later heartbeat can still enroll.
  elif printf '%s\n' "$ENROLL_STRING" | "$CRASHPILOT_BIN" enroll -; then
    # `enroll` saves this machine's own credentials, enables the timers, and
    # sends the first heartbeat, so it is online as soon as this returns.
    CONNECTED=1
  else
    err "Could not enroll with that join token yet. It is saved, and the heartbeat timer tries again."
    err "If that keeps failing, check it on the dashboard's Systems page: it may be revoked, expired, or used up."
    INSTALL_PROBLEMS+=("enrolling with the dashboard failed")
  fi
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
if [[ ${#INSTALL_PROBLEMS[@]} -gt 0 ]]; then
  echo -e "${RED}${BOLD}✗ Installation finished with problems:${RESET}"
  for problem in "${INSTALL_PROBLEMS[@]}"; do
    echo -e "  ${RED}-${RESET} $problem"
  done
  echo ""
  echo -e "  ${DIM}Run ${RESET}${CYAN}sudo crashpilot doctor${RESET}${DIM} for details.${RESET}"
  exit 1
fi
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
}

main "$@"

