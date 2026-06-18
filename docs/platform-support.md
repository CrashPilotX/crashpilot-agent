# Platform support

CrashPilotX distinguishes between native host installs and containerized node
agents. A platform is listed as supported only when CI exercises an install,
CLI/storage smoke test, or an actual deployment.

## Tested platforms

| Platform | Architectures | CI evidence | Support level |
|---|---|---|---|
| Ubuntu 22.04 VM or host | amd64 | Full Python suite, CLI/storage smoke, systemd unit validation | Supported |
| Ubuntu 24.04 VM or host | amd64, arm64 | Full Python suite, CLI/storage smoke, package build, systemd validation | Supported |
| Ubuntu on WSL1/WSL2 | amd64, arm64 where available | WSL detection and collector contracts; installer tests | Supported, WSL2 + systemd recommended |
| Docker / OCI on Ubuntu hosts | amd64, arm64 | Ubuntu 22.04/24.04 image builds, CLI, snapshot, API health, Compose validation | Supported |
| Kubernetes Linux nodes | amd64; image also published for arm64 | Kind cluster deploys and rolls out the privileged DaemonSet | Supported node-agent deployment |
| Bare-metal Ubuntu | amd64, arm64 | Same package and collector path as Ubuntu VMs; hardware collectors covered by unit tests | Supported; hardware-specific CI is simulated |

GitHub-hosted Ubuntu jobs run in virtual machines, so the VM path is
continuously exercised. CI cannot boot WSL or attach real SMART/NVIDIA hardware;
those paths are covered by deterministic collector tests and should also be
validated on representative hardware before large fleet rollouts.

## Docker

The image runs the local API plus one-minute flight-recorder and heartbeat
loops:

```bash
docker build -f docker/Dockerfile -t crashpilot .
docker run -d --name crashpilot \
  --restart unless-stopped \
  --pid host \
  --privileged \
  -p 7878:7878 \
  -v crashpilot-data:/var/lib/crashpilot \
  -v /var/log:/var/log:ro \
  -v /run/log/journal:/run/log/journal:ro \
  -v /sys:/sys:ro \
  -v /dev:/dev \
  --env-file /etc/crashpilot/.env \
  crashpilot
```

Or use `docker compose -f docker/compose.yaml up -d`.

The published multi-architecture image is
`ghcr.io/kdigitalsystems/crashpilot:latest`.

An unprivileged container can monitor only itself. Host crash forensics requires
host PID visibility, read-only host logs/sysfs, `/dev`, and elevated privileges.

## Kubernetes

CrashPilot runs once per Linux node as a DaemonSet:

```bash
kubectl apply -f k8s/secret.example.yaml   # edit credentials first
kubectl apply -f k8s/daemonset.yaml
kubectl rollout status daemonset/crashpilot-agent -n crashpilot
```

The DaemonSet uses `hostPID`, host paths, and a privileged security context
because node-level journal, sysfs, devices, and process data are otherwise not
visible from a pod. Managed clusters that forbid privileged workloads are not
compatible with the node-forensics DaemonSet; use a native package on each
Ubuntu node instead.

## CI gates

- `Tests`: Python 3.10–3.12 behavior.
- `Platform CI`: Ubuntu 22.04/24.04 VM runners, amd64/arm64, systemd units, and
  self-contained package builds.
- `Container and Kubernetes CI`: Docker/OCI images, Compose, Kind deployment,
  and multi-architecture GHCR publication.
- `Web CI`: dashboard type checks, unit tests, production build, and desktop/mobile browser tests.
- `Schema CI`: clean PostgreSQL/Supabase schema integration.
