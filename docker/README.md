# Docker deployment

CrashPilot's OCI image supports amd64 and arm64 and is tested with Ubuntu 22.04
and 24.04 bases. It runs the API, flight-recorder snapshots, and cloud heartbeat
loop in one container.

```bash
docker compose -f docker/compose.yaml up -d --build
curl http://127.0.0.1:7878/health
```

Copy the variables produced by the CrashPilotX dashboard into `docker/.env`.
Host-level crash evidence requires the privileged mounts in the Compose file.
Remove them only when intentionally monitoring the container itself.

See [platform support](../docs/platform-support.md) for security and scope.
