#!/usr/bin/env python3
"""Installer smoke/contract tests for the Ubuntu-only install path."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "agent" / "install.sh"
INSTALLER_FOR_BASH = INSTALLER.relative_to(ROOT).as_posix()


def require(text: str, needle: str, reason: str) -> None:
    if needle not in text:
        raise AssertionError(f"Missing `{needle}`: {reason}")


def function_source(script: str, name: str) -> str:
    """The text of one shell function, from `name() {` to its closing brace."""
    start = script.index(f"{name}() {{")
    end = script.index("\n}\n", start) + 3
    return script[start:end]


def check_non_root_gets_no_system_units(script: str, empty: Path) -> None:
    # A non-root run used to install system units (the prompt defaulted to
    # yes, even with no terminal) whose ExecStart ran the user's own venv as
    # root: anyone who could write to it had root. `empty` stands in for
    # /etc/systemd/system, so units on the machine running this are not read.
    if os.geteuid() == 0:
        return  # the behaviour under test needs a non-root EUID
    probe = (
        'warn() { echo "WARN: $*"; }\nINSTALL_PROBLEMS=()\nVENV_DIR=/nonexistent/venv\n'
        + function_source(script, "systemd_install_consented").replace("/etc/systemd/system", str(empty))
        + '\nfor INSTALL_SYSTEMD in yes auto; do\n'
        + '  if systemd_install_consented; then echo "INSTALLS:$INSTALL_SYSTEMD"; fi\n'
        + 'done\necho "PROBLEMS:${#INSTALL_PROBLEMS[@]}"\n'
    )
    result = subprocess.run(
        ["setsid", "bash", "-s"], input=probe, capture_output=True, text=True, check=True,
    )
    if "INSTALLS:" in result.stdout:
        raise AssertionError(f"a non-root run must never install system units: {result.stdout!r}")
    require(result.stdout, "sudo", "a non-root run should say to re-run with sudo")
    require(result.stdout, "PROBLEMS:1", "INSTALL_SYSTEMD=yes without root is a problem to report")


def check_truncated_download_runs_nothing(script: str) -> None:
    # `curl | sudo bash` runs lines as they arrive, so a download cut short
    # ran a prefix of the installer and usually exited 0. With the body in a
    # function called on the last line, any cut inside it is a syntax error.
    lines = script.splitlines(keepends=True)
    last = max(i for i, line in enumerate(lines) if line.strip())
    require(lines[last], 'main "$@"', "the last line must be the only top-level call")
    opening = next(i for i, line in enumerate(lines) if line.startswith("main() {"))
    before = "".join(lines[:opening])
    for line in before.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith(("#", "set -uo pipefail")):
            raise AssertionError(f"nothing may run before main() is fully read: {line!r}")
    for cut in range(opening + 1, last, 7):
        result = subprocess.run(
            ["bash", "-n"], input="".join(lines[:cut]), capture_output=True, text=True,
        )
        if result.returncode == 0:
            raise AssertionError(f"a download cut after line {cut} would still run")


def check_leftover_user_units_are_reported(script: str, tmp: Path) -> None:
    # Units an earlier non-root install left behind still run that user's
    # venv as root; a non-root run cannot remove them, so it must say so.
    if os.geteuid() == 0:
        return
    units = tmp / "units"
    units.mkdir()
    (units / "crashpilot.service").write_text("ExecStart=/home/u/.local/share/crashpilot/venv/bin/crashpilot analyze\n")
    probe = (
        'warn() { echo "WARN: $*"; }\nINSTALL_PROBLEMS=()\nINSTALL_SYSTEMD=auto\n'
        "VENV_DIR=/home/u/.local/share/crashpilot/venv\n"
        + function_source(script, "systemd_install_consented").replace("/etc/systemd/system", str(units))
        + '\nsystemd_install_consented; echo "PROBLEMS:${#INSTALL_PROBLEMS[@]}"\n'
    )
    result = subprocess.run(["bash", "-s"], input=probe, capture_output=True, text=True, check=True)
    require(result.stdout, "PROBLEMS:1", "units running a user's venv as root must be reported")
    require(result.stdout, "as root", "and the warning should say why")


def check_moved_install_paths_reach_units_and_cli(script: str, tmp: Path) -> None:
    # Outside the default locations the agent cannot find its config and
    # data by itself: a moved install used to send the data to /root, where
    # the sandboxed boot analysis cannot write. The services learn the paths
    # from drop-ins (which an updater unit refresh leaves alone); the CLI
    # wrapper, which the installer's own configure/enroll also runs, from
    # its environment. With the defaults, nothing is set.
    etc = tmp / "etc"
    functions = "".join(
        function_source(script, name) for name in ("moved_paths", "install_paths_dropin", "create_wrapper")
    ).replace("/etc/systemd/system", str(etc))
    probe = (
        '_sudo() { local a=(); for x in "$@"; do case "$x" in -o|-g|root) ;; *) a+=("$x");; esac; done; "${a[@]}"; }\n'
        f"WORK_DIR={tmp}\nVENV_DIR=/srv/cp/venv\n" + functions
        + "\nCONFIG_DIR=/srv/cfg DATA_DIR=/srv/cp\n"
        + "install_paths_dropin crashpilot.service; install_paths_dropin crashpilot-heartbeat.service\n"
        + f'create_wrapper {tmp}/moved-wrapper "$(moved_paths)"\n'
        + "CONFIG_DIR=/etc/crashpilot DATA_DIR=/opt/crashpilot\n"
        + "install_paths_dropin crashpilot-heartbeat.service\n"
        + f'create_wrapper {tmp}/default-wrapper "$(moved_paths)"\n'
    )
    subprocess.run(["bash", "-s"], input=probe, capture_output=True, text=True, check=True)
    analysis = (etc / "crashpilot.service.d" / "10-crashpilot-paths.conf").read_text()
    for line in ("[Service]", "Environment=CRASHPILOT_CONFIG_DIR=/srv/cfg",
                 "Environment=CRASHPILOT_DATA_DIR=/srv/cp/data", "ReadWritePaths=-/srv/cp/data"):
        require(analysis, line, "the boot analysis must be told where a moved install keeps its files")
    if (etc / "crashpilot-heartbeat.service.d" / "10-crashpilot-paths.conf").exists():
        raise AssertionError("back on the defaults, a drop-in from an earlier run must be removed")
    env = {k: v for k, v in os.environ.items() if not k.startswith("CRASHPILOT_")}
    show = 'exec() { echo "CFG=${CRASHPILOT_CONFIG_DIR:-} DATA=${CRASHPILOT_DATA_DIR:-} RUN=$1"; }\n'
    for name, expected in (
        ("moved-wrapper", "CFG=/srv/cfg DATA=/srv/cp/data RUN=/srv/cp/venv/bin/crashpilot"),
        ("default-wrapper", "CFG= DATA= RUN=/srv/cp/venv/bin/crashpilot"),
    ):
        wrapper = (tmp / name).read_text().replace("#!/bin/bash\n", show, 1)
        out = subprocess.run(["bash", "-s"], input=wrapper, capture_output=True, text=True, env=env, check=True)
        if out.stdout.strip() != expected:
            raise AssertionError(f"{name}: {out.stdout.strip()!r}, expected {expected!r}")


def check_install_paths_are_validated(script: str) -> None:
    # A space in CRASHPILOT_INSTALL_DIR split ExecStart; a % is a unit
    # specifier; both went into the units unchecked.
    probe = function_source(script, "unit_safe_path") + (
        '\nfor p in /opt/crashpilot /srv/crash-pilot_2 "/opt/crash pilot" /opt/100% relative/dir "/opt/a&b"; do\n'
        '  if unit_safe_path "$p"; then echo "OK:$p"; else echo "NO:$p"; fi\n'
        "done\n"
    )
    result = subprocess.run(["bash", "-s"], input=probe, capture_output=True, text=True, check=True)
    expected = [
        "OK:/opt/crashpilot", "OK:/srv/crash-pilot_2", "NO:/opt/crash pilot",
        "NO:/opt/100%", "NO:relative/dir", "NO:/opt/a&b",
    ]
    if result.stdout.split("\n")[:-1] != expected:
        raise AssertionError(f"install path validation: {result.stdout!r}")


def main() -> None:
    script = INSTALLER.read_text(encoding="utf-8")

    help_result = subprocess.run(
        ["bash", INSTALLER_FOR_BASH, "--help"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    require(help_result.stdout, "--connect", "installer help should document dashboard connection")
    require(help_result.stdout, "--enroll", "installer help should document join-token enrollment")
    if "Standalone installer detected" in help_result.stdout:
        raise AssertionError("run from a checkout, the installer must use it rather than download the bundle")

    both = subprocess.run(
        ["bash", INSTALLER_FOR_BASH, "--connect", "cpilot_x", "--enroll", "cpjoin_y"],
        cwd=ROOT, check=False, capture_output=True, text=True,
    )
    if both.returncode == 0 or "not both" not in both.stderr:
        raise AssertionError("--connect and --enroll together must be refused before installing")

    missing_connect_result = subprocess.run(
        ["bash", INSTALLER_FOR_BASH, "--connect"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if missing_connect_result.returncode == 0:
        raise AssertionError("--connect without a value should fail before installing")
    require(
        missing_connect_result.stderr,
        "--connect requires",
        "installer should explain missing connection-string values",
    )

    require(script, '[[ "$DISTRO" != "ubuntu" ]]', "installer must stay Ubuntu-only for now")
    require(script, "Containerized installs are not supported right now.", "Docker/Kubernetes installs must stay disabled")
    require(script, "KUBERNETES_SERVICE_HOST", "Kubernetes detection should remain explicit")
    require(script, "/.dockerenv", "Docker detection should remain explicit")
    require(script, 'PKG_MGR="apt"', "Ubuntu apt path should remain the only package manager path")
    require(script, "https://crashpilotx.com/crashpilot-agent.tar.gz", "curl-pipe installs should use the public website bundle")
    require(script, "agent bundle download failed", "bundle failures should be explicit")

    # The bundle is unpacked and installed as root, so it must be checked
    # against the published digest before anything is extracted. A streamed
    # `curl | tar` cannot be verified, because it is already extracted by the
    # time the bytes could be checked.
    require(script, "sha256sum -c", "curl-pipe installs must verify the bundle against its published checksum")
    require(script, "failed checksum verification", "a checksum mismatch should be reported explicitly")
    if "curl -fsSL \"$BUNDLE_URL\" | tar" in script:
        raise AssertionError(
            "bundle must be downloaded to a file and verified, not piped straight into tar"
        )
    require(script, '[[ ! -t 0 ]]', "curl-pipe installs must not prompt from stdin")
    require(script, "Non-interactive install detected", "non-interactive installs should skip optional package prompts")
    require(script, 'WSL with systemd detected', "WSL2 with systemd should install the heartbeat timer")
    require(script, 'WSL without systemd detected', "WSL without systemd should stay on the manual heartbeat path")
    require(script, "crashpilot-update.timer", "systemd installs should enable verified automatic updates")
    require(script, "Automatic updates: enabled", "installer summary should confirm automatic updates")
    require(script, "crashpilot-snapshot.timer", "systemd installs should enable the flight recorder")
    require(script, "crashpilot-signoff.service", "systemd installs should sign off on clean shutdown")
    require(script, 'systemctl enable --now crashpilot-signoff.service', "the sign-off unit must be started so its ExecStop runs at shutdown")
    # Secrets go to the CLI on stdin: an argument is readable by every local
    # user in /proc/<pid>/cmdline, for up to five minutes while enroll retries.
    require(script, '"$ENROLL_STRING" | "$CRASHPILOT_BIN" enroll -', "--enroll should hand the join token to `crashpilot enroll` on stdin")
    require(script, '"$CONNECT_STRING" | "$CRASHPILOT_BIN" configure -', "--connect should hand the connection string to `crashpilot configure` on stdin")
    for leak in ('enroll "$ENROLL_STRING"', 'configure "$CONNECT_STRING"'):
        if leak in script:
            raise AssertionError(f"secrets must not be passed on the command line: {leak}")
    require(script, "Flight recorder: enabled", "installer summary should confirm the flight recorder")
    require(script, "speedtest-cli", "installer should set up internet capacity checks automatically")
    require(script, "install_speedtest_cli", "speedtest capacity support should be installed without an interactive prompt")
    require(script, "_try_install_pkg speedtest-cli", "installer should try to install the Ubuntu speedtest-cli package")
    require(script, "passive network throughput will still work", "missing speedtest-cli should be non-fatal")
    require(script, "CRASHPILOT_BANDWIDTH_SPEEDTEST_ENABLED=true", "new configs should enable speedtest capacity checks")
    require(script, "ensure_env_default CRASHPILOT_BANDWIDTH_SPEEDTEST_ENABLED true", "existing configs should get speedtest enabled by default")

    for unit in (ROOT / "systemd").glob("crashpilot*.service"):
        text = unit.read_text(encoding="utf-8")
        require(
            text,
            "__CRASHPILOT_BIN__",
            f"{unit.name} should use the installer-rendered crashpilot binary path",
        )

    update_service = (ROOT / "systemd" / "crashpilot-update.service").read_text(encoding="utf-8")
    update_timer = (ROOT / "systemd" / "crashpilot-update.timer").read_text(encoding="utf-8")
    require(update_service, "update --quiet", "update service should call the restricted updater command")
    require(update_timer, "Persistent=true", "missed update checks should run after the machine returns")
    require(update_timer, "OnCalendar=hourly", "the update check runs hourly")
    for text, where in ((script, "install.sh"), (update_timer, "crashpilot-update.timer")):
        if "daily" in text.lower():
            raise AssertionError(f"{where} must not describe the hourly update check as daily")

    # Files the agent creates hold journal and dmesg text. The update service
    # is the exception: pip must leave the shared venv readable.
    for unit in (ROOT / "systemd").glob("crashpilot*.service"):
        text = unit.read_text(encoding="utf-8")
        if unit.name == "crashpilot-update.service":
            if "UMask=" in text:
                raise AssertionError("the update service must not make the reinstalled venv private")
        else:
            require(text, "UMask=0077", f"{unit.name} should create its files private")

    # Units stop in reverse start order: starting before the heartbeat means
    # the sign-off runs after it has stopped, so no heartbeat can land after
    # the sign-off and mark the node online again.
    signoff = (ROOT / "systemd" / "crashpilot-signoff.service").read_text(encoding="utf-8")
    for unit in ("crashpilot-heartbeat.timer", "crashpilot-heartbeat.service", "crashpilot-update.service"):
        before = next((line for line in signoff.splitlines() if line.startswith("Before=")), "")
        require(before, unit, "the sign-off must stop after every unit that sends heartbeats")
    require(signoff, "TimeoutStopSec=8", "a dead network must never hold up a shutdown")

    # ── Hardening: each of these was a real hole in an earlier version ────────
    if "/tmp/crashpilot" in script:
        raise AssertionError(
            "units must be rendered in the private mktemp work dir, never at a fixed /tmp path "
            "a local user can pre-create before it is copied into /etc/systemd/system as root"
        )
    require(script, 'WORK_DIR="$(mktemp -d)"', "temporary files need one private directory")
    require(script, "trap cleanup_work_dir EXIT", "the work dir must be removed however the script exits")
    require(script, 'install -m 0644 -o root -g root', "units must be installed root-owned and not writable by others")
    if 'chmod -R a+rX "$DATA_DIR"' in script:
        raise AssertionError(
            "a recursive chmod over the install dir makes the API token and crash DB world-readable"
        )
    require(script, 'chmod -R a+rX "$VENV_DIR"', "only the venv should be shared with other users")
    require(script, 'install -d -m 0700 "$DATA_DIR/data"', "a fresh install must create the data dir private before any unit creates it 0755")
    require(script, 'chmod -R go-rwx "$DATA_DIR/data"', "upgrades must re-close a data dir older installers opened")
    if "bootstrap.pypa.io" in script:
        raise AssertionError("never pipe an unverified download from bootstrap.pypa.io into Python as root")
    require(script, "--proto '=https' --tlsv1.2", "bundle downloads must refuse plain http and old TLS")
    require(script, "__CRASHPILOT_BUNDLE_SHA256__", "the publish step pins the bundle digest into the installer")
    require(script, "apt-get update", "package lists must be refreshed before installing Python")
    require(script, "CRASHPILOT_INSTALL_DIR", "the install location must not reuse the agent's CRASHPILOT_DATA_DIR")
    if "CRASHPILOT_DATA_DIR=/var/lib/crashpilot" in script:
        raise AssertionError("the .env template must not suggest a data dir the service cannot write")
    require(script, 'exit 1\nfi\necho -e "${GREEN}${BOLD}✓ Installation complete!', "a run with problems must exit non-zero")
    if "read -rp \"Install systemd" in script:
        raise AssertionError("service installs must never prompt from stdin, which is the script under curl | bash")

    with tempfile.TemporaryDirectory() as tmp:
        check_non_root_gets_no_system_units(script, Path(tmp))
    check_truncated_download_runs_nothing(script)
    check_install_paths_are_validated(script)
    with tempfile.TemporaryDirectory() as tmp:
        check_leftover_user_units_are_reported(script, Path(tmp))
    with tempfile.TemporaryDirectory() as tmp:
        check_moved_install_paths_reach_units_and_cli(script, Path(tmp))

    unsupported_managers = ["dnf", "pacman", "zypper", "apk", "xbps"]
    active_installs = [
        manager for manager in unsupported_managers
        if f'{manager})' in script or f'{manager}|' in script
    ]
    if active_installs:
        raise AssertionError(
            "Unsupported package manager install branches should not be active: "
            + ", ".join(active_installs)
        )


if __name__ == "__main__":
    main()
