"""Base class and shared utilities for telemetry collectors."""

from __future__ import annotations

import asyncio
import shutil
from typing import Any


async def run_cmd(
    *args: str,
    timeout: int = 30,
    input_data: str | None = None,
) -> tuple[str, str, int]:
    """Run a subprocess asynchronously and return (stdout, stderr, returncode)."""
    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE if input_data else asyncio.subprocess.DEVNULL,
        )
        stdin_bytes = input_data.encode() if input_data else None
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(stdin_bytes), timeout=timeout
        )
        return stdout.decode(errors="replace"), stderr.decode(errors="replace"), proc.returncode
    except asyncio.TimeoutError:
        # Kill the process so it doesn't linger as a zombie
        if proc is not None:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
        return "", f"command timed out after {timeout}s", -1
    except FileNotFoundError:
        return "", f"command not found: {args[0]}", -1
    except PermissionError:
        return "", f"permission denied: {args[0]}", -1


def cmd_available(name: str) -> bool:
    """Check if a CLI tool is on PATH (works on all distros including Alpine)."""
    return shutil.which(name) is not None


class BaseCollector:
    name: str = "base"

    async def collect(self) -> dict[str, Any]:
        raise NotImplementedError

    async def safe_collect(self) -> dict[str, Any]:
        try:
            return await self.collect()
        except Exception as exc:
            return {"error": str(exc), "collector": self.name}
