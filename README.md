# CrashPilot Agent

> The Linux crash-forensics agent behind [CrashPilot](https://crashpilotx.com). Open source, because you shouldn't have to trust a black box you run as root.

[![Release](https://img.shields.io/github/v/release/CrashPilotX/crashpilot-agent)](https://github.com/CrashPilotX/crashpilot-agent/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10+-blue)](https://python.org)
[![Tests](https://github.com/CrashPilotX/crashpilot-agent/actions/workflows/test.yml/badge.svg)](https://github.com/CrashPilotX/crashpilot-agent/actions/workflows/test.yml)
[![Live demo](https://img.shields.io/badge/Try%20it-no%20signup%20required-orange)](https://crashpilotx.com/demo)

## Try it in 60 seconds

No root, no account, no network once it is installed. Classify a kernel log you already have:

```bash
pip install "git+https://github.com/CrashPilotX/crashpilot-agent.git@v0.2.0#subdirectory=agent"

cat > dmesg.txt <<'EOF'
[ 8123.456789] NVRM: Xid (PCI:0000:01:00): 79, pid='<unknown>', name=<unknown>, GPU has fallen off the bus.
[ 8123.456800] NVRM: GPU 0000:01:00.0: GPU has fallen off the bus.
EOF

crashpilot classify dmesg.txt
```

```
Category:   gpu_fault
Severity:   HIGH
Confidence: 70%
Evidence:
  [ 8123.456789] NVRM: Xid (PCI:0000:01:00): 79, pid='<unknown>', name=<unknown>, GPU has fallen off the bus.
  [ 8123.456800] NVRM: GPU 0000:01:00.0: GPU has fallen off the bus.
```

The same check as a step in a GitHub Actions workflow ([docs/github-action.md](docs/github-action.md)):

```yaml
- uses: CrashPilotX/crashpilot-agent@v0.2.0
  with:
    file: artifacts/dmesg.txt
```

**What it is:** rule-based classification of crash logs (OOM kills, kernel panics, NVIDIA Xid and AMD GPU faults, thermal shutdowns, disk errors) and a Linux agent that does the same on a machine's last abnormal boot. **What it isn't:** a general log search or metrics store, and a log with no known crash signature comes back as `unknown`, not a guess. See [What it detects](#what-it-detects) for the exact list.

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
| GPU fault (NVIDIA Xid; AMD ring timeout, GPU reset, failed start) | nvidia-smi + kernel log |
| Thermal shutdown | ACPI THERMTRIP + lm-sensors |
| Machine Check Exception | mcelog / rasdaemon + dmesg |
| Sudden stop (power loss or hard lock-up) | A boot whose log ends with no shutdown sequence and no error; see below |
| Disk I/O error | dmesg + smartctl SMART data |
| Watchdog reset / soft lockup | kernel hung task detection |
| PCIe fault | Uncorrectable AER errors raise a live alert and go to the AI analysis; the offline heuristics do not classify them |

### Sudden stops

A machine that loses power, or locks up hard, cannot log why. Its journal just ends. CrashPilot tells that apart from a normal shutdown or reboot by whether the end of the log shows the shutdown sequence systemd writes (the shutdown target, `systemd-shutdown`, and journald's last "Journal stopped"), and for a log with none it reads what surrounds the cut-off instead of stopping at "unknown":

- what started just before it: a stress test, a benchmark, a CI job, a scheduled command;
- how long the machine stayed down, which separates an automatic reset (a minute or two) from one that stayed dead until it was restarted by hand;
- whether earlier boots ended the same way. One stop can be anything; the same ending on several boots with nothing in the logs points at hardware or power, not software.

The report states what is known, not a cause: the logs cannot tell a failing power supply from a hardware lock-up. Its first steps depend on the evidence (cap the GPU's power if a GPU load had just started; check the supply's rating and the power connectors; read the BMC event log if there is one; compare how earlier boots ended). Confidence rises only with corroboration and never reaches certainty.

### GPU load

Connected to a dashboard, the heartbeat reports each GPU's utilization, memory, temperature and power.

| GPU | Read through | Needs |
|---|---|---|
| NVIDIA | `nvidia-smi` | the NVIDIA driver's tool |
| AMD | the kernel's `amdgpu` driver (sysfs) | nothing extra; `rocm-smi` is not required |

An idle discrete AMD GPU is powered down by the driver after a few seconds. The agent never wakes a sleeping card to read it (the vendor tools do, and a wake-up every minute would change the machine's power behaviour). It reports such a card as `suspended`, 0% utilization, with its memory size, and shows temperature and power whenever the card is awake. Crash detection for GPU faults is separate; the table above lists what is classified.

### Temperatures

Connected to a dashboard, the heartbeat also reports CPU, memory and disk temperatures, each inside that component's own readings. They are read from the kernel's hwmon files (`/sys/class/hwmon`): no root, no extra tools, and nothing is run or opened beyond reading those files.

| Component | Sensor | Needs |
|---|---|---|
| CPU | `k10temp` (AMD), `coretemp` (Intel), `zenpower`, `cpu_thermal` (ARM) | nothing extra |
| Memory | the DIMM's `spd5118` or `jc42` sensor | a board and kernel that expose it; many machines, and most virtual machines, have none |
| Disk | `nvme` for every NVMe drive; `drivetemp` for SATA and SAS drives | `sudo modprobe drivetemp` for SATA and SAS drives (add `drivetemp` to `/etc/modules-load.d/` to keep it) |

GPU temperature comes with the GPU readings above. A sensor that cannot be read, or answers with a value no real component has (below -40 C or above 150 C), is left out instead of reported, and a component with no usable sensor reports none. At most 16 CPU sensors, 8 memory sensors and 8 disk sensors are sent; the component's maximum covers every sensor read.

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
git clone https://github.com/CrashPilotX/crashpilot-agent.git
cd crashpilot-agent/agent
pip install .
```

Then analyze the last boot:

```bash
sudo crashpilot analyze
```

To connect it to a dashboard, see [Connecting to the dashboard](#connecting-to-the-dashboard).

### Classify a log you already have

`analyze` reads the machine it runs on. `classify` reads text you give it, so it works on a log from another
machine, a bug report, or CI. No sudo, no account, no network, nothing written to disk:

```bash
crashpilot classify dmesg.txt          # a file
journalctl -k -b -1 | crashpilot classify -   # stdin
crashpilot classify dmesg.txt --json   # category, severity, confidence, evidence, alternatives
```

It runs the same heuristic detector as `analyze` (no AI step). See [docs/github-action.md](docs/github-action.md)
for a GitHub Action that classifies a log file in a workflow and writes a job summary.

### Docker

```bash
docker run -d --name crashpilot --restart unless-stopped --stop-timeout 30 \
  --pid host --privileged -p 7878:7878 \
  -v crashpilot-data:/var/lib/crashpilot -v /etc/machine-id:/etc/machine-id:ro \
  -v /var/log:/var/log:ro -v /run/log/journal:/run/log/journal:ro \
  -v /sys:/sys:ro -v /dev:/dev \
  --env-file /etc/crashpilot/.env \
  ghcr.io/crashpilotx/crashpilot-agent:latest
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

### Automated fleets

For machines that come and go on their own (autoscaling groups, spot instances, CI runners, Kubernetes nodes), create a **join token** on the dashboard's Systems page instead. Every machine enrolls itself with it at first boot:

```bash
curl -fsSL https://crashpilotx.com/install.sh | sudo bash -s -- --enroll 'cpjoin_<your-join-token>'
# or, with the agent already installed:
sudo crashpilot enroll 'cpjoin_<your-join-token>'
```

Each machine enrolls under a stable identity: `--external-id` if you pass one, otherwise the Kubernetes node name, the cloud instance ID (AWS IMDSv2, GCP, Azure), or `/etc/machine-id`. That identity is saved, so a reinstall or a pod restart carries on as the same system rather than creating a new one. A detected identity is checked once per boot, so each machine made from an image of an enrolled one enrolls as itself; an `--external-id` is never replaced. A system retired by hand in the dashboard is not re-enrolled automatically: restore it, then run `sudo crashpilot enroll` on the machine. On a clean shutdown the agent signs off (`crashpilot-signoff.service`, or the container's entrypoint when the container or pod is stopped), so the dashboard shows the machine as shut down rather than offline. Machines enrolled with an ephemeral token are retired automatically once they are gone, keeping their history until retention ends. See [k8s/README.md](k8s/README.md) for the DaemonSet.

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
_Last checked: 2026-10-25 13:09 UTC. This checks that the agent installs and its CLI runs without an unhandled error inside a plain container on each distro - containers have no real reboot, no real dmesg boot history, and no real hardware sensors, so this is not a claim that crash detection itself was verified there. See [Supported distributions](#supported-distributions) for what is actually supported end to end._

| Distro | Install + CLI smoke test |
|---|---|
| Alpine 3.20 | PASS |
| Debian 12 | PASS |
| Fedora 41 | PASS |
| Ubuntu 22.04 | PASS |
| Ubuntu 24.04 | PASS |
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
