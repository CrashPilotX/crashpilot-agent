#!/usr/bin/env bash
# Build a self-contained CrashPilot binary and Ubuntu .deb package.
#
# The agent pulls in native wheels (pydantic-core, psutil, uvloop), so we ship a
# PyInstaller one-file binary that bundles Python + all deps: no python3 needed
# on the target. nfpm then wraps it with the systemd units
# and a default config.
#
# Requirements (installed by the release workflow): python3, pip, pyinstaller, nfpm.
# Output: dist/crashpilot (binary), dist/*.deb, dist/SHA256SUMS
#
# NOTE: this is the first packaging pass. PyInstaller hidden-import coverage may
# need tuning on the first real CI run: see packaging/README.md.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# nfpm resolves `src:` paths relative to CWD, so always run from the repo root.
cd "$REPO_ROOT"
AGENT_DIR="$REPO_ROOT/agent"
DIST="$REPO_ROOT/dist"
STAGE="$DIST/stage"

VERSION="${VERSION:-$(grep -m1 '^version' "$AGENT_DIR/pyproject.toml" | sed -E 's/.*"(.*)".*/\1/')}"
ARCH="${ARCH:-$(dpkg --print-architecture)}"
export ARCH VERSION

echo "==> Building CrashPilot $VERSION for $ARCH"
rm -rf "$DIST"
mkdir -p "$DIST" "$STAGE/usr/bin" "$STAGE/lib/systemd/system" "$STAGE/etc/crashpilot"

# 1. Install the agent + PyInstaller into the build environment
pip install --quiet --upgrade pip
pip install --quiet pyinstaller "$AGENT_DIR"

# 2. Build the one-file binary. --collect-all pulls in packages PyInstaller's
#    static analysis tends to miss (lazy imports, data files, plugins).
pyinstaller --onefile --clean --noconfirm \
  --name crashpilot \
  --distpath "$DIST" \
  --workpath "$DIST/build" \
  --specpath "$DIST" \
  --collect-all anthropic \
  --collect-all typer \
  --collect-all click \
  --collect-all rich \
  --collect-all pydantic \
  --collect-all pydantic_settings \
  --collect-all uvicorn \
  --collect-all fastapi \
  --collect-submodules crashpilot \
  "$AGENT_DIR/crashpilot/__main__.py"

cp "$DIST/crashpilot" "$STAGE/usr/bin/crashpilot"
chmod 0755 "$STAGE/usr/bin/crashpilot"

# 3. Stage systemd units with the binary path baked in (/usr/bin/crashpilot)
for unit in \
  crashpilot.service \
  crashpilot-heartbeat.service crashpilot-heartbeat.timer \
  crashpilot-snapshot.service crashpilot-snapshot.timer \
  crashpilot-update.service crashpilot-update.timer; do
  sed "s|__CRASHPILOT_BIN__|/usr/bin/crashpilot|g; s|/usr/local/bin/crashpilot|/usr/bin/crashpilot|g" \
    "$REPO_ROOT/systemd/$unit" > "$STAGE/lib/systemd/system/$unit"
done

# 4. Default config (nfpm marks it config|noreplace so upgrades don't clobber it)
cat > "$STAGE/etc/crashpilot/.env" <<'ENVEOF'
# CrashPilot configuration
# AI root-cause analysis is OPTIONAL: heuristic analysis works without a key.
# CRASHPILOT_ANTHROPIC_API_KEY=sk-ant-...
#
# Push mode (connect to the dashboard) is configured by `crashpilot configure`.
ENVEOF

# 5. Build packages for the current arch
nfpm package --config "$REPO_ROOT/packaging/nfpm.yaml" --packager deb --target "$DIST"

# 6. Checksums for the binary + packages
( cd "$DIST" && sha256sum crashpilot ./*.deb > SHA256SUMS )

echo "==> Done. Artifacts in $DIST:"
find "$DIST" -maxdepth 1 -mindepth 1 \
  ! -name build ! -name stage ! -name '*.spec' \
  -printf '%f\n'
