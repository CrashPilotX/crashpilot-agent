# Crash detector benchmark

Measures the agent's heuristic crash detector
(`agent/crashpilot/analyzers/crash_detector.py`) against kernel logs that
people posted in public GitHub issues, to answer one question: given a real
crash log, does it name the right cause?

It is not part of CI. It needs network access and a GitHub login, and it
exists to check a detector change before claiming anything about accuracy.

## Results

Kernel logs from issues created since 2025-01-01, collected on 2026-09-29:
644 issues, 275 with pasted kernel output, 233 scoreable log and signature
pairs.

PCIe is listed but not scored, because the detector has no PCIe crash type.
Most PCIe lines in crash reports are *correctable* errors, routine link noise
the hardware has already recovered from, and for those "unknown" is the right
answer. Five logs show a real uncorrectable fault; the detector names none of
them as PCIe (three come back "unknown", two as a GPU fault).

| Signature in the log | Logs | Before (`3b1e1ba`) | After (`62a5792`) |
|---|---:|---:|---:|
| `GPU has fallen off the bus` | 26 | 23 | **26** |
| `NVRM: Xid` | 60 | 60 | 60 |
| `Out of memory: Killed process` | 37 | 37 | 37 |
| `invoked oom-killer` | 46 | 42 | **46** |
| `Kernel panic - not syncing` | 42 | 41 | **42** |
| `soft lockup - CPU` | 22 | 2 | **17** |
| **All** | **233** | **205 (88%)** | **228 (98%)** |

Between the two versions exactly 23 logs changed answer, every one from wrong
to right, and none the other way. The five soft lockups still counted as wrong
either escalated into a real kernel panic or followed an OOM kill; there the
detector names the actual cause and the benchmark's label is too literal.

With the agent's 10,000-character log limit applied, as an installed machine
sees its own log, the current detector gets 225 of 233.

Re-fetching `manifest.jsonl` on 2026-09-29 returned all 644 issues and
reproduced every number above exactly.

## Method

- **Collect.** `fetch.py` searches issues for exact kernel strings (the
  `QUERIES` table), so each issue it finds has an unambiguous right answer. It
  keeps the issue body and any comments from the person who opened it, because
  maintainers routinely ask for dmesg and it arrives in a later comment.
- **Score only log output.** A line counts only if it is shaped like kernel
  output: a dmesg timestamp, a syslog or journal `host kernel:` prefix, or a
  line-leading `NVRM:`. The issues were found by searching for these strings,
  so scoring the prose around a log would only prove that a regex matches its
  own search term. Accepting any line that merely mentioned a keyword also
  swept in sentences like "I think I got a kernel panic" and pattern lists
  copied from other tools, and roughly doubled the apparent corpus.
- **The signature must be inside the log.** A pair counts only if the
  searched-for string is in the extracted kernel output, not only in the text
  around it.
- **Labels are a proxy, so misses are read by hand.** The expected answer is
  the crash type the signature implies. Every wrong answer above was also read
  by hand; the five soft lockups are the only cases where the detector was
  right and the label was not.
- **Two placements.** Each log is scored where the detector reads it whole,
  which measures the rules, and in `dmesg.full_tail`, which it cuts to the last
  10,000 characters, which measures what a user gets.

## Running it

Both scripts need Python 3.10+. `fetch.py` also needs an authenticated GitHub
CLI (`gh auth login`).

```bash
# The corpus behind the numbers above, then this checkout against the detector before the fix
python3 bench/fetch.py --manifest bench/manifest.jsonl
python3 bench/score.py --against 3b1e1ba
```

To check a detector change, score it against `main` and read every changed
answer and every miss, not only the totals:

```bash
python3 bench/score.py --against main --misses misses.jsonl
```

`fetch.py` without `--manifest` searches afresh. Issues are opened, edited and
deleted all the time, so a fresh search gives a different corpus, and a
manifest re-fetch can lose issues that have since gone.

## Privacy

The corpus is other people's crash logs, which can carry hostnames, process
names and hardware serials.

- It is written to `bench/corpus/`, which is gitignored. Never commit it, and
  never quote logs from it in issues, commits or docs.
- `manifest.jsonl` holds issue URLs and signature labels only.
- Report results in aggregate.
- Don't cite the underlying issues as `owner/repo#number` in commits or pull
  requests. GitHub posts a backlink on each one, which on someone else's bug
  report reads as spam.
