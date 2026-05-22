"""Docker container crash and OOM collector."""

from __future__ import annotations

import json
from typing import Any

from .base import BaseCollector, cmd_available, run_cmd


class DockerCollector(BaseCollector):
    name = "docker"

    async def collect(self) -> dict[str, Any]:
        if not cmd_available("docker"):
            return {"available": False}

        daemon_ok = await self._check_daemon()
        if not daemon_ok:
            return {"available": True, "daemon_running": False}

        crashed = await self._get_crashed_containers()
        oom_killed = await self._get_oom_killed()
        daemon_logs = await self._get_daemon_logs()

        return {
            "available": True,
            "daemon_running": True,
            "crashed_containers": crashed,
            "oom_killed_containers": oom_killed,
            "daemon_logs_tail": daemon_logs,
        }

    async def _check_daemon(self) -> bool:
        _, _, rc = await run_cmd("docker", "info", "--format", "{{.ServerVersion}}", timeout=10)
        return rc == 0

    async def _get_crashed_containers(self) -> list[dict]:
        stdout, _, rc = await run_cmd(
            "docker", "ps", "-a",
            "--filter", "status=exited",
            "--filter", "exited=1",
            "--format", "{{json .}}",
            timeout=15,
        )
        if rc != 0:
            return []
        containers = []
        for line in stdout.strip().splitlines():
            try:
                c = json.loads(line)
                containers.append({
                    "id": c.get("ID", ""),
                    "name": c.get("Names", ""),
                    "image": c.get("Image", ""),
                    "status": c.get("Status", ""),
                    "exit_code": c.get("ExitCode"),
                })
            except json.JSONDecodeError:
                continue
        return containers[:20]

    async def _get_oom_killed(self) -> list[dict]:
        stdout, _, rc = await run_cmd(
            "docker", "ps", "-a",
            "--format", "{{json .}}",
            timeout=15,
        )
        if rc != 0:
            return []
        oom_containers = []
        for line in stdout.strip().splitlines():
            try:
                c = json.loads(line)
                cid = c.get("ID", "")
                if not cid:
                    continue
                # Inspect for OOMKilled flag
                inspect_out, _, ir = await run_cmd(
                    "docker", "inspect", "--format",
                    "{{.State.OOMKilled}}:{{.State.ExitCode}}",
                    cid, timeout=5,
                )
                if ir == 0 and inspect_out.startswith("true"):
                    oom_containers.append({
                        "id": cid,
                        "name": c.get("Names", ""),
                        "image": c.get("Image", ""),
                        "oom_killed": True,
                    })
            except json.JSONDecodeError:
                continue
        return oom_containers[:10]

    async def _get_daemon_logs(self) -> str:
        stdout, _, _ = await run_cmd(
            "journalctl", "-u", "docker", "--no-pager",
            "--lines=200", "--output=short-iso", "-b", "-1",
            timeout=20,
        )
        return stdout
