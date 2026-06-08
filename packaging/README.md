# CrashPilot packaging

Builds a **self-contained agent binary** (no Python required on the target) and
Ubuntu packages, so users can install with apt instead of
`curl | sudo bash`.

## What's here

| File | Purpose |
|---|---|
| `build.sh` | Build the PyInstaller binary + `.deb` (via nfpm) + `SHA256SUMS` into `dist/` |
| `nfpm.yaml` | Package metadata + file layout for deb |
| `postinstall.sh` / `preremove.sh` | Enable/disable systemd units on install/remove |
| `../.github/workflows/release.yml` | Builds amd64 + arm64 on a `v*` tag and publishes a GitHub Release |

## How a release works

1. Tag a version: `git tag v0.1.1 && git push --tags`.
2. `release.yml` builds on `ubuntu-latest` (amd64) and `ubuntu-24.04-arm` (arm64),
   runs `build.sh`, and publishes `crashpilot-linux-{amd64,arm64}`, the `.deb`,
   and `SHA256SUMS` to the GitHub Release.
3. Users install the Ubuntu package with apt.

## Status / known follow-ups

This is the first packaging pass. Because the agent bundles native wheels
(pydantic-core, psutil, uvloop), the binary is built per-arch with PyInstaller.
The **first real tag build is the validation step** — expect to tune:

- **PyInstaller hidden imports.** If the binary errors at runtime with
  `ModuleNotFoundError` (common culprits: `anthropic`, `uvicorn` workers,
  `pydantic` plugins), add the module to the `--collect-all` / `--hidden-import`
  list in `build.sh`. A smoke test (`crashpilot --help`, `crashpilot doctor`) is
  wired implicitly via the workflow logs.
- **Binary size** (~30–60 MB is normal for a bundled-Python one-file binary).
- **arm64 runner availability** — `ubuntu-24.04-arm` is GA; swap to a QEMU build
  if your org lacks arm runners.
- **Signing** — add cosign/minisign over `SHA256SUMS` once the build is green.

Local build (needs `python3`, `pip`, `pyinstaller`, `nfpm`):

```bash
VERSION=0.1.0 ARCH=amd64 bash packaging/build.sh
```
