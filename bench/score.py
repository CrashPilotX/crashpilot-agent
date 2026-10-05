#!/usr/bin/env python3
"""Score the heuristic crash detector against kernel logs from real issues.

  python3 bench/score.py
      Score the detector in this checkout.
  python3 bench/score.py --against 3b1e1ba
      Score it and the detector at a git ref side by side, and list every log
      whose answer changed - so a change is judged by more than its totals.
  python3 bench/score.py --misses misses.jsonl
      Also write each wrong answer out, for review by hand.

Reads bench/corpus/issues.jsonl, written by fetch.py. Only text shaped like
kernel output is scored, never the prose around it: the issues were found by
searching for these exact strings, so scoring prose would only prove that a
regex matches its own search term.

Each log is scored two ways:
  untruncated  placed where the detector reads it whole; measures the rules
  as shipped   placed in dmesg.full_tail, which the detector cuts to its last
               10,000 characters; measures what a user actually gets
"""

from __future__ import annotations

import argparse
import collections
import importlib.util
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from types import ModuleType
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
DETECTOR = "agent/crashpilot/analyzers/crash_detector.py"

# The signature that must appear inside the pasted log, and the right answer.
# A family whose answer is not a crash type the detector has is reported as a
# spread of what it said instead of being scored.
FAMILIES: dict[str, tuple[str, str]] = {
    "xid79": (r"fallen off the bus", "gpu_fault"),
    "xid": (r"NVRM: Xid", "gpu_fault"),
    "oom": (r"Out of memory: Killed process", "oom_kill"),
    "oomkiller": (r"invoked oom-killer", "oom_kill"),
    "panic": (r"Kernel panic - not syncing", "kernel_panic"),
    "softlockup": (r"soft lockup - CPU", "soft_lockup"),
    # Real PCIe faults only. Most PCIe lines in crash reports are *correctable*
    # errors - routine link noise the hardware already recovered from - and
    # labelling those as faults would reward the detector for false alarms.
    # Current kernels say "Uncorrectable", older ones "Uncorrected".
    "pcie": (r"Uncorrect(?:ed|able) \((?:Non-)?Fatal\)|AER: (?:device recovery failed|can.t recover)",
             "pcie_fault"),
}

FENCE = re.compile(r"(?:```|~~~)[^\n]*\n(.*?)(?:```|~~~)", re.S)
# A line counts as log output only if it is shaped like one: a dmesg
# timestamp, a syslog or journal "host kernel:" prefix, or a line-leading
# driver prefix. Accepting any line that merely mentions a keyword sweeps in
# prose ("I think I got a kernel panic") and pattern lists copied from other
# tools, which roughly doubled the apparent corpus when first tried.
LOGLINE = re.compile(
    r"^\s*(?:"
    r"\[\s*\d+\.\d+\]"                                          # dmesg        [  123.456]
    r"|<\d+>\[\s*\d+\.\d+\]"                                    # dmesg -r     <4>[ 123.4]
    r"|\[\w{3} \w{3}\s+\d+ \d\d:\d\d:\d\d \d{4}\]"              # dmesg -T
    r"|\w{3}\s+\d+\s+\d\d:\d\d:\d\d\s+\S+\s+kernel:"            # syslog / journalctl
    r"|\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d\S*\s+\S+\s+kernel:"    # journalctl -o short-iso
    r"|kernel:\s|NVRM:\s"                                       # bare prefixes
    r")",
    re.M,
)


def load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # dataclass resolves its own module through sys.modules
    spec.loader.exec_module(mod)
    return mod


def load_at(ref: str) -> ModuleType:
    src = subprocess.run(["git", "-C", str(REPO), "show", f"{ref}:{DETECTOR}"],
                         capture_output=True, text=True, check=True).stdout
    path = Path(tempfile.mkdtemp()) / "crash_detector_at_ref.py"
    path.write_text(src)
    return load(path, "crash_detector_" + re.sub(r"\W", "_", ref))


def logs_from(text: str) -> str:
    """Pasted kernel output only: fenced blocks containing at least one real
    log line, plus real log lines pasted without a fence."""
    if not text:
        return ""
    parts = [block for block in FENCE.findall(text) if LOGLINE.search(block)]
    loose = [line for line in FENCE.sub("", text).splitlines() if LOGLINE.match(line)]
    if loose:
        parts.append("\n".join(loose))
    return "\n".join(parts)


def detect(mod: ModuleType, log: str, shipped: bool = False) -> dict[str, Any]:
    tel: dict[str, Any] = {"journal": {}, "dmesg": {}, "gpu": {"nvidia": {}},
                           "smart": {}, "thermal": {}}
    if shipped:
        tel["dmesg"]["full_tail"] = log
    else:
        tel["journal"]["previous_boot_errors"] = log
    r = mod.detect_crash_type(tel)
    return {"type": r.crash_type.value, "conf": round(r.confidence, 2),
            "alts": [a["crash_type"] for a in r.alternatives], "evidence": r.evidence[:3]}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--corpus", type=Path, default=HERE / "corpus" / "issues.jsonl")
    ap.add_argument("--against", metavar="REF", help="also score the detector at this git ref")
    ap.add_argument("--misses", type=Path, help="write each wrong answer here for review")
    args = ap.parse_args()

    if not args.corpus.exists():
        raise SystemExit(f"{args.corpus} not found - run bench/fetch.py first")
    issues = [json.loads(line) for line in args.corpus.read_text().splitlines() if line.strip()]
    current = load(REPO / DETECTOR, "crash_detector_current")
    before = load_at(args.against) if args.against else None
    ref = (args.against or "")[:12]  # a full 40-character sha wrecks the table
    known = {t.value for t in current.CrashType}

    pairs: list[dict[str, Any]] = []
    changed: list[tuple[str, str, str]] = []
    with_log = 0
    for it in issues:
        log = logs_from("\n".join([it["body"], *it.get("author_comments", [])]))
        if not log:
            continue
        with_log += 1
        now, shipped = detect(current, log), detect(current, log, shipped=True)
        then = detect(before, log) if before else None
        if then and then["type"] != now["type"]:
            changed.append((it["url"], then["type"], now["type"]))
        for fam in sorted(set(it["families"])):
            sig, expected = FAMILIES[fam]
            if re.search(sig, log, re.I):   # the signature is in the log, not just the prose
                pairs.append({"fam": fam, "expected": expected, "url": it["url"],
                              "now": now, "shipped": shipped, "then": then})

    print(f"corpus: {len(issues)} issues; {with_log} with pasted kernel output; "
          f"{len(pairs)} (issue, signature) pairs with the signature inside the log\n")
    head = f"{'signature':11} {'n':>4}  {'correct':>13}  {'as shipped':>13}"
    print(head + (f"  {'at ' + ref:>15}" if before else ""))
    tn = tc = ts = tb = 0
    for fam, (_, expected) in FAMILIES.items():
        rows = [p for p in pairs if p["fam"] == fam]
        if not rows:
            continue
        if expected not in known:
            said = collections.Counter(p["now"]["type"] for p in rows)
            print(f"{fam:11} {len(rows):>4}  not scored: the detector has no '{expected}' type; "
                  "it said " + ", ".join(f"{k} {v}" for k, v in said.most_common()))
            continue
        n = len(rows)
        c = sum(p["now"]["type"] == expected for p in rows)
        s = sum(p["shipped"]["type"] == expected for p in rows)
        line = f"{fam:11} {n:>4}  {c:>4} ({100 * c / n:3.0f}%)   {s:>4} ({100 * s / n:3.0f}%)"
        if before:
            b = sum(p["then"]["type"] == expected for p in rows)
            tb += b
            line += f"   {b:>4} ({100 * b / n:3.0f}%)"
        print(line)
        tn, tc, ts = tn + n, tc + c, ts + s
    if tn:
        line = f"{'ALL':11} {tn:>4}  {tc:>4} ({100 * tc / tn:3.0f}%)   {ts:>4} ({100 * ts / tn:3.0f}%)"
        print(line + (f"   {tb:>4} ({100 * tb / tn:3.0f}%)" if before else ""))

    wrong = [p for p in pairs if p["expected"] in known and p["now"]["type"] != p["expected"]]
    print("\nwhen wrong, what it said instead:")
    for (fam, said), n in collections.Counter(
            (p["fam"], p["now"]["type"]) for p in wrong).most_common():
        print(f"  {fam:11} -> {said:24} {n}")

    if before:
        regressed = [p for p in pairs if p["expected"] in known
                     and p["then"]["type"] == p["expected"] != p["now"]["type"]]
        print(f"\nregressions (right at {ref}, wrong now): {len(regressed)}")
        for p in regressed:
            print(f"  {p['fam']:11} {p['then']['type']} -> {p['now']['type']}")
        print(f"\nevery log whose answer changed: {len(changed)}")
        for (then_t, now_t), n in collections.Counter((a, b) for _, a, b in changed).most_common():
            print(f"  {then_t:>18} -> {now_t:<18} {n}")

    if args.misses:
        with args.misses.open("w") as f:
            for p in wrong:
                f.write(json.dumps({"family": p["fam"], "url": p["url"], "said": p["now"]["type"],
                                    "conf": p["now"]["conf"], "alts": p["now"]["alts"],
                                    "evidence": p["now"]["evidence"]}) + "\n")
        print(f"\n{len(wrong)} wrong answers written to {args.misses}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
