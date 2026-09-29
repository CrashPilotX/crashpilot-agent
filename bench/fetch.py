#!/usr/bin/env python3
"""Collect kernel crash logs that people posted in public GitHub issues.

  python3 bench/fetch.py
      Search GitHub now for each crash signature and write bench/corpus/.
  python3 bench/fetch.py --manifest bench/manifest.jsonl
      Re-fetch exactly the issues in a manifest, to reproduce published
      numbers. Search results drift as issues are opened and edited, so a
      fresh search will not give the same corpus twice.

For each issue it keeps the body and any comments from the person who opened
it: maintainers routinely ask for dmesg, and it arrives in a later comment.
Needs an authenticated GitHub CLI (`gh auth login`).

The corpus is other people's logs. It goes to bench/corpus/, which is
gitignored; never commit it or quote from it. A manifest holds only URLs.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
SINCE = "2025-01-01"

# Exact kernel strings, so each issue found has an unambiguous right answer.
QUERIES: dict[str, str] = {
    "xid79": '"fallen off the bus"',
    "xid": "NVRM Xid",
    "oom": '"Out of memory: Killed process"',
    "oomkiller": '"invoked oom-killer"',
    "panic": '"Kernel panic - not syncing"',
    "softlockup": '"soft lockup - CPU"',
    "pcie": '"PCIe Bus Error"',
}


def gh(*args: str) -> str:
    for attempt in range(4):
        proc = subprocess.run(["gh", *args], capture_output=True, text=True)
        if proc.returncode == 0:
            return proc.stdout
        if "rate limit" in proc.stderr.lower():
            time.sleep(30 * (attempt + 1))
            continue
        raise RuntimeError(proc.stderr.strip()[:300])
    raise RuntimeError("rate limited repeatedly")


def record(raw: dict[str, Any], families: list[str]) -> dict[str, Any]:
    return {
        "url": raw["html_url"],
        "repo": raw["repository_url"].split("/repos/", 1)[1],
        "number": raw["number"],
        "title": raw["title"],
        "author": (raw.get("user") or {}).get("login", ""),
        "created": raw["created_at"][:10],
        "families": families,
        "body": raw.get("body") or "",
        "author_comments": [],
    }


def search(per_family: int) -> dict[str, dict[str, Any]]:
    issues: dict[str, dict[str, Any]] = {}
    for family, query in QUERIES.items():
        # The raw search API with the exact query string: `gh search` re-quotes
        # its arguments, and a benchmark has to know precisely what it asked.
        out = gh("api", "-X", "GET", "search/issues",
                 "-f", f"q={query} created:>{SINCE} is:issue",
                 "-f", f"per_page={per_family}")
        items = json.loads(out)["items"]
        for raw in items:
            issues.setdefault(raw["html_url"], record(raw, []))["families"].append(family)
        print(f"{family:11} {len(items):4} issues   ({len(issues)} unique so far)", flush=True)
        time.sleep(2.5)  # the search API allows 30 requests a minute
    return issues


def from_manifest(path: Path) -> dict[str, dict[str, Any]]:
    wanted = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def get(entry: dict[str, Any]) -> dict[str, Any] | None:
        owner_repo, number = entry["url"].split("github.com/", 1)[1].split("/issues/")
        try:
            raw = json.loads(gh("api", f"repos/{owner_repo}/issues/{number}"))
        except RuntimeError:
            return None  # deleted, transferred or made private since
        return record(raw, list(entry["families"]))

    with cf.ThreadPoolExecutor(max_workers=4) as pool:
        got = [r for r in pool.map(get, wanted) if r]
    print(f"{len(got)} of {len(wanted)} manifest issues still available", flush=True)
    return {r["url"]: r for r in got}


def add_author_comments(issue: dict[str, Any]) -> None:
    only_author = f'[.[] | select(.user.login == "{issue["author"]}") | .body]'
    out = gh("api", f"repos/{issue['repo']}/issues/{issue['number']}/comments",
             "--paginate", "-q", only_author)
    for line in out.splitlines():  # --paginate prints one array per page
        if line.strip():
            issue["author_comments"].extend(json.loads(line))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", type=Path, help="re-fetch exactly the issues in this manifest")
    ap.add_argument("--per-family", type=int, default=100,
                    help="search results per signature (at most 100)")
    ap.add_argument("--out", type=Path, default=HERE / "corpus")
    args = ap.parse_args()

    issues = from_manifest(args.manifest) if args.manifest else search(args.per_family)
    failed = 0
    # Four at a time: enough to be quick, few enough to stay clear of
    # GitHub's secondary rate limits on concurrent requests.
    with cf.ThreadPoolExecutor(max_workers=4) as pool:
        for fut in [pool.submit(add_author_comments, it) for it in issues.values()]:
            try:
                fut.result()
            except RuntimeError:
                failed += 1

    args.out.mkdir(parents=True, exist_ok=True)
    with (args.out / "issues.jsonl").open("w") as f:
        for it in issues.values():
            f.write(json.dumps(it) + "\n")
    with (args.out / "manifest.jsonl").open("w") as f:
        for it in sorted(issues.values(), key=lambda r: r["url"]):
            entry = {"url": it["url"], "families": sorted(set(it["families"])),
                     "created": it["created"]}
            f.write(json.dumps(entry) + "\n")
    print(f"wrote {len(issues)} issues to {args.out / 'issues.jsonl'} "
          f"({failed} comment fetches failed)")
    return 0 if issues else 1


if __name__ == "__main__":
    sys.exit(main())
