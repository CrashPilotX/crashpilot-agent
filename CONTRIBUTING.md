# Contributing

## Right now

**Issues are open and genuinely wanted.** Bug reports, distro-specific breakage, false positives in crash detection, confusing output, missing hardware support: all useful, please file them.

**Pull requests are by invitation for the moment.** Not because outside code isn't welcome, but because merging code into an agent that runs as root on other people's machines deserves a real review process, and that process isn't set up yet. Opening an issue first is the fastest path: if a fix is straightforward and we agree on the approach, you'll be invited to send the PR.

Security issues go to **saqibkhan@crashpilotx.com**, not the issue tracker. See [SECURITY.md](SECURITY.md).

## Good bug reports

The agent's job is reconstructing what happened on a machine you have and we don't, so context matters more than usual:

- Distro and version, kernel version (`uname -a`), Python version.
- How it's installed: native, WSL, Docker, or Kubernetes.
- Output of `sudo crashpilot doctor`, which reports tool availability and config state.
- For a wrong or missing diagnosis: what actually happened, and what the agent said instead.

Redact anything sensitive before pasting logs. The agent redacts secrets in what it *sends*, but a terminal paste is on you.

## Development

```bash
cd agent
pip install -e ".[dev]"
```

Before anything is merged it has to pass what CI runs:

```bash
ruff check crashpilot/ tests/
mypy crashpilot/
pytest tests/ -v
```

Tests run against Python 3.10, 3.11, and 3.12. Coverage has a floor that CI enforces; it goes up over time, never quietly down.

## Things worth knowing

- **Collectors must degrade gracefully.** No GPU, no lm-sensors, no smartctl, no systemd: every one of those is a normal machine, not an error. A collector that raises on missing hardware is a bug.
- **The agent must not crash.** Its entire value is surviving to report on a failure. An unhandled exception in the collection loop is a serious bug, not a cosmetic one.
- **Redaction is a contract.** If you add a code path that writes log-derived text to disk, to the AI analyzer, or to the cloud, it goes through `redaction.py` first. Add a test to `tests/test_redaction.py` for any new credential format.
- **No em dashes, en dashes, or curly quotes** in code, comments, or output. Plain ASCII punctuation only.
