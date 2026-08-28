#!/usr/bin/env python3
"""Packaging contract checks for release artifacts."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def require(text: str, needle: str, reason: str) -> None:
    if needle not in text:
        raise AssertionError(f"Missing `{needle}`: {reason}")


def main() -> None:
    nfpm = (ROOT / "packaging" / "nfpm.yaml").read_text(encoding="utf-8")
    postinstall = (ROOT / "packaging" / "postinstall.sh").read_text(encoding="utf-8")
    build = (ROOT / "packaging" / "build.sh").read_text(encoding="utf-8")

    require(nfpm, "- smartmontools", "package should recommend SMART telemetry tools")
    require(nfpm, "- lm-sensors", "package should recommend temperature telemetry tools")
    require(nfpm, "- speedtest-cli", "package should recommend internet capacity telemetry helper")
    require(postinstall, "https://crashpilotx.com/", "package install guidance should point to the public dashboard")
    if "kdigitalsystems.github.io/CrashPilot" in postinstall:
        raise AssertionError("postinstall should not point users to the old GitHub Pages URL")
    require(build, "__CRASHPILOT_BIN__", "packaging should render systemd unit binary placeholders")


if __name__ == "__main__":
    main()
