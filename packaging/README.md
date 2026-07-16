# CrashPilot packaging

Builds a self-contained agent binary and Ubuntu `.deb` packages, so users can
install with apt without requiring Python on the target.

## Files

| File | Purpose |
|---|---|
| `build.sh` | Builds the PyInstaller binary, `.deb`, and checksums into `dist/` |
| `nfpm.yaml` | Package metadata and file layout |
| `postinstall.sh` / `preremove.sh` | Enable and disable systemd units |
| `../.github/workflows/release.yml` | Publishes amd64 and arm64 release artifacts |

## Release process

1. Tag a version: `git tag v0.2.1 && git push --tags`.
2. `release.yml` builds on native amd64 and arm64 Ubuntu runners.
3. The workflow smoke-tests the bundled CLI, snapshot storage, and `.deb`
   contents before publishing.
4. Users install the package with apt and connect it to the dashboard.

## Continuous validation

`Platform CI` builds and executes both architectures before a release tag. It
checks:

- PyInstaller hidden imports through `crashpilot --help` and `snapshot`.
- Native arm64 execution on the self-hosted Ubuntu 24.04 ARM64 runner.
- Package metadata and expected filesystem contents.
- systemd unit syntax on Ubuntu 22.04 and 24.04.

The Debian package recommends `smartmontools`, `lm-sensors`, and
`speedtest-cli`. CrashPilot works without them, but they improve disk health,
temperature, and internet-capacity telemetry.

Binary size around 30–60 MB is normal for a bundled-Python one-file binary.
Artifact signing remains a future hardening item.

## Local build

Install Python, pip, PyInstaller, and nfpm, then run:

```bash
VERSION=0.2.0 ARCH=amd64 bash packaging/build.sh
```
