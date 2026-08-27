# CrashPilot Agent

> The Linux crash-forensics agent behind [CrashPilot](https://crashpilotx.com). Open source, because you shouldn't have to trust a black box you run as root.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10+-blue)](https://python.org)
[![Tests](https://github.com/crashpilot/crashpilot-agent/actions/workflows/test.yml/badge.svg)](https://github.com/crashpilot/crashpilot-agent/actions/workflows/test.yml)
[![Live demo](https://img.shields.io/badge/Try%20it-no%20signup%20required-orange)](https://crashpilotx.com/demo)

## Why this repo is public

The agent needs privileged access to do its job: host PID namespace, `/dev` and `/sys`, the kernel journal, and SMART data. That is a lot to ask of a machine you care about, and "trust us" is not a good enough answer.

So the part that runs on your machine is open source and auditable. You can read exactly what it collects, what it redacts, and what it sends before you install it. The hosted dashboard and analysis backend remain a commercial product at [crashpilotx.com](https://crashpilotx.com).

## What it does

A crash log tells you a process died. It doesn't tell you memory had been climbing for twenty minutes first, that a specific service pushed it over the edge, or that the same pattern happened three days ago on another box.

This agent watches every boot. When it sees an abnormal one, it reconstructs the sequence: the resource trend in the minutes before, the exact kernel and journal lines that explain it, a root cause with an honest confidence score, and what changed right before things went wrong.

### What it detects

| Crash Type | Detection Method |
|---|---|
| Out-of-memory kill | journalctl OOM killer events |
| Kernel panic | dmesg + journalctl |
| GPU fault (NVIDIA Xid) | nvidia-smi + journal |
| Thermal shutdown | ACPI THERMTRIP + lm-sensors |
| Machine Check Exception | mcelog / rasdaemon + dmesg |
| Power loss | Missing journal shutdown marker |
| Disk I/O error | dmesg + smartctl SMART data |
| Watchdog reset / soft lockup | kernel hung task detection |
| PCIe fault | AER error reporting |

## What leaves your machine

This is the part worth reading closely before you install anything.

- **Outbound only.** The agent opens no inbound ports and needs no public URL. It works behind any NAT or firewall. Nothing connects *to* it.
- **Secrets are redacted before they are stored, analyzed, or sent.** API keys, passwords, bearer tokens, private keys, and AWS credentials are stripped from log text by [`crashpilot/redaction.py`](agent/crashpilot/redaction.py). The test suite in [`agent/tests/test_redaction.py`](agent/tests/test_redaction.py) is the spec.
- **The local REST API binds to `127.0.0.1`** and every `/api/v1/*` route requires a bearer token (`crashpilot token`). Only `/health` is public.
- **AI analysis is optional.** Without an API key you still get a plain-English root cause, remediation steps, and monitoring tips from the built-in heuristics. No key, no third-party AI call.
- **Cloud push is optional.** The agent is fully usable standalone: `crashpilot analyze` works with no account and no network.

If you find something here that contradicts the above, that is a security bug. See [SECURITY.md](SECURITY.md).

## Install

```bash
curl -fsSL https://crashpilotx.com/install.sh | sudo bash
```

Prefer not to pipe a script to `sudo bash`? Read [`agent/install.sh`](agent/install.sh) first, or install from source:

```bash
git clone https://github.com/crashpilot/crashpilot-agent.git
cd crashpilot-agent/agent
pip install .
```

Then analyze the last boot:

```bash
sudo crashpilot analyze
```

To connect it to a dashboard, see [Connecting to the dashboard](#connecting-to-the-dashboard).

### Docker

```bash
docker run -d --name crashpilot --restart unless-stopped \
  --pid host --privileged -p 7878:7878 \
  -v crashpilot-data:/var/lib/crashpilot \
  -v /var/log:/var/log:ro -v /run/log/journal:/run/log/journal:ro \
  -v /sys:/sys:ro -v /dev:/dev \
  --env-file /etc/crashpilot/.env \
  ghcr.io/kdigitalsystems/crashpilot:latest
```

### Kubernetes

```bash
kubectl apply -f k8s/secret.example.yaml   # edit credentials first
kubectl apply -f k8s/daemonset.yaml
```

Kubernetes uses a privileged per-node DaemonSet because host PID, journal, sysfs, and device access are required for node crash forensics.

## Connecting to the dashboard

The agent works standalone, but the hosted dashboard at [crashpilotx.com](https://crashpilotx.com) adds fleet view, history, correlation across machines, and alerting.

1. Sign in at [crashpilotx.com](https://crashpilotx.com), go to **Systems > Add system**, choose **Push mode**.
2. Run the command it gives you:

```bash
sudo crashpilot configure cpilot_<your-connection-string>
```

The agent authenticates with a `system_id` + `agent_token` pair stored in `/etc/crashpilot/.env` (mode `0600`). Writes go through `SECURITY DEFINER` Postgres RPCs that validate the token server-side, so the public anon key alone cannot modify data.

## Supported distributions

| Environment | Versions / architectures | Status |
|---|---|---|
| Ubuntu native or VM | 22.04/24.04, amd64/arm64 | Supported and CI tested |
| Ubuntu on WSL | WSL1/WSL2 | Supported; WSL2 with systemd recommended |
| Docker / OCI | Ubuntu 22.04/24.04, amd64/arm64 | Supported and CI tested |
| Kubernetes node agent | Linux nodes, amd64/arm64 image | Supported privileged DaemonSet; Kind tested |

Requires Python 3.10+ and Linux kernel 5.4+. See the [full platform support matrix](docs/platform-support.md).

### Nightly install/runtime check

A scheduled job installs the agent fresh and runs its CLI smoke test inside a plain container of each distro below every night, to catch packaging and dependency regressions early on distros beyond the officially supported list above.

<!-- COMPAT-MATRIX:START -->
_No run has completed yet._
<!-- COMPAT-MATRIX:END -->

## Development

```bash
cd agent
pip install -e ".[dev]"
ruff check crashpilot/ tests/
mypy crashpilot/
pytest tests/ -v
```

## Contributing

Bug reports and security findings are very welcome. Pull requests are currently by invitation while the review process gets set up. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT. See [LICENSE](LICENSE).
