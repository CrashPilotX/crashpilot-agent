#!/usr/bin/env python3
"""Render the nightly OS compatibility matrix into README.md.

Reads one result.json per distro (written by .github/workflows/compat-matrix.yml,
one per matrix entry, downloaded into subdirectories of the given results dir),
builds a markdown table, and replaces the content between the
COMPAT-MATRIX marker comments in README.md.

This only ever reports what the workflow actually checked: does the agent
install and does `crashpilot --help` / `crashpilot snapshot --quiet` run
without an unhandled error, inside a plain container (no real reboot, no
real dmesg boot history, no real hardware sensors). It is not a claim that
crash detection itself was verified on that distro - see the comment this
script writes into the README alongside the table.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

START_MARKER = "<!-- COMPAT-MATRIX:START -->"
END_MARKER = "<!-- COMPAT-MATRIX:END -->"


def load_results(results_dir: Path) -> list[dict]:
    results = []
    for result_file in sorted(results_dir.glob("**/result.json")):
        try:
            results.append(json.loads(result_file.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"warning: could not read {result_file}: {exc}", file=sys.stderr)
    return results


def render_table(results: list[dict]) -> str:
    if not results:
        return "_No results were collected on the last run._"

    results = sorted(results, key=lambda r: r.get("name", ""))
    lines = ["| Distro | Install + CLI smoke test |", "|---|---|"]
    for row in results:
        name = row.get("name", "unknown")
        status = row.get("status", "unknown")
        badge = "PASS" if status == "pass" else "FAIL" if status == "fail" else "UNKNOWN"
        lines.append(f"| {name} | {badge} |")
    return "\n".join(lines)


def build_section(results: list[dict]) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    table = render_table(results)
    return (
        f"{START_MARKER}\n"
        f"_Last checked: {timestamp}. This checks that the agent installs and its CLI "
        f"runs without an unhandled error inside a plain container on each distro - "
        f"containers have no real reboot, no real dmesg boot history, and no real "
        f"hardware sensors, so this is not a claim that crash detection itself was "
        f"verified there. See [Supported distributions](#supported-distributions) for "
        f"what is actually supported end to end._\n\n"
        f"{table}\n"
        f"{END_MARKER}"
    )


def update_readme(readme_path: Path, section: str) -> bool:
    text = readme_path.read_text(encoding="utf-8")
    if START_MARKER not in text or END_MARKER not in text:
        raise SystemExit(
            f"README is missing {START_MARKER} / {END_MARKER} markers - "
            "add them once before this script can update the table."
        )
    start = text.index(START_MARKER)
    end = text.index(END_MARKER) + len(END_MARKER)
    new_text = text[:start] + section + text[end:]
    if new_text == text:
        return False
    readme_path.write_text(new_text, encoding="utf-8")
    return True


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit(f"usage: {sys.argv[0]} <results-dir> <readme-path>")

    results_dir = Path(sys.argv[1])
    readme_path = Path(sys.argv[2])

    results = load_results(results_dir)
    section = build_section(results)
    changed = update_readme(readme_path, section)
    print("README updated." if changed else "No change to the compatibility table.")


if __name__ == "__main__":
    main()
