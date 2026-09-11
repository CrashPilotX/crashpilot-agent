#!/usr/bin/env python3
"""Packaging contract checks for release artifacts."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
# Units the package deliberately leaves out, and why.
NOT_PACKAGED = {
    # The local API server is set up by install.sh as a per-user template;
    # the package does not run it.
    "crashpilot-api.service",
}


def require(text: str, needle: str, reason: str) -> None:
    if needle not in text:
        raise AssertionError(f"Missing `{needle}`: {reason}")


def main() -> None:
    nfpm = (ROOT / "packaging" / "nfpm.yaml").read_text(encoding="utf-8")
    postinstall = (ROOT / "packaging" / "postinstall.sh").read_text(encoding="utf-8")
    preremove = (ROOT / "packaging" / "preremove.sh").read_text(encoding="utf-8")
    build = (ROOT / "packaging" / "build.sh").read_text(encoding="utf-8")

    require(nfpm, "- smartmontools", "package should recommend SMART telemetry tools")
    require(nfpm, "- lm-sensors", "package should recommend temperature telemetry tools")
    require(nfpm, "- speedtest-cli", "package should recommend internet capacity telemetry helper")
    require(postinstall, "https://crashpilotx.com/", "package install guidance should point to the public dashboard")
    if "kdigitalsystems.github.io/CrashPilot" in postinstall:
        raise AssertionError("postinstall should not point users to the old GitHub Pages URL")
    require(build, "__CRASHPILOT_BIN__", "packaging should render systemd unit binary placeholders")

    # Every unit in systemd/ ships in the package unless listed above: the
    # sign-off unit was missing, so packaged machines never signed off.
    expected = {path.name for path in (ROOT / "systemd").iterdir()} - NOT_PACKAGED
    packaged = set(re.findall(r"dst: /lib/systemd/system/(\S+)", nfpm))
    staging_loop = build.split("for unit in", 1)[1].split("; do", 1)[0]
    staged = set(re.findall(r"crashpilot[\w@-]*\.(?:service|timer)", staging_loop))
    for name, found in (("nfpm.yaml", packaged), ("build.sh", staged)):
        if found != expected:
            raise AssertionError(
                f"{name} units differ from systemd/: missing {sorted(expected - found)}, "
                f"unexpected {sorted(found - expected)}"
            )

    # The packaged units run as root with no install dir, so the agent's own
    # default data dir was under /root/.local/share, which the sandboxed boot
    # analysis (ProtectHome=read-only) cannot write.
    require(build, "PACKAGED_DATA_DIR=/var/lib/crashpilot", "packaged data lives in /var/lib/crashpilot")
    require(build, "Environment=CRASHPILOT_DATA_DIR=$PACKAGED_DATA_DIR", "packaged units need a writable data dir")
    require(build, "ReadWritePaths=-$PACKAGED_DATA_DIR", "the sandboxed boot analysis must be able to write it")
    require(postinstall, "install -d -m 0700 /var/lib/crashpilot", "postinstall must create the data dir private")
    require(postinstall, "enable --now crashpilot-signoff.service", "the sign-off unit must be started for its ExecStop to run")
    require(preremove, "crashpilot-signoff.service", "removal must disable the sign-off unit too")


if __name__ == "__main__":
    main()
