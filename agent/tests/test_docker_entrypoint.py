"""The container entrypoint must stop its heartbeat loop before signing off.

It ran `exec "$@"`, which replaced the shell and dropped its trap: nothing
stopped the loop on `docker stop` or pod termination, so a heartbeat could
land after the sign-off and mark the node online again.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

ENTRYPOINT = Path(__file__).resolve().parents[2] / "docker" / "entrypoint.sh"

# Records each call; a heartbeat takes a while, like one waiting on the network.
_FAKE_CLI = """#!/bin/sh
echo "start $1" >> "$CALLS"
if [ "$1" = heartbeat ]; then sleep 1; fi
echo "end $1" >> "$CALLS"
"""


@pytest.mark.skipif(not ENTRYPOINT.is_file(), reason="needs the repository checkout")
def test_a_stop_ends_the_heartbeat_loop_before_signing_off(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    cli = bin_dir / "crashpilot"
    cli.write_text(_FAKE_CLI)
    cli.chmod(0o755)
    calls = tmp_path / "calls"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "CALLS": str(calls),
        "CRASHPILOT_SNAPSHOT_INTERVAL_SECONDS": "1",
    }
    # Its own process group, so nothing it started can outlive the test.
    proc = subprocess.Popen(["bash", str(ENTRYPOINT), "sleep", "60"], env=env, start_new_session=True)
    try:
        deadline = time.monotonic() + 10
        while "start heartbeat" not in (calls.read_text() if calls.exists() else ""):
            assert time.monotonic() < deadline, "the loop never sent a heartbeat"
            time.sleep(0.05)

        proc.send_signal(signal.SIGTERM)  # mid-heartbeat, to the entrypoint only
        proc.wait(timeout=15)
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    lines = calls.read_text().splitlines()
    assert "start sign-off" in lines, lines
    signed_off = lines.index("start sign-off")
    # The heartbeat in flight finished first, and none started afterwards.
    assert lines[signed_off - 1] == "end heartbeat", lines
    assert "start heartbeat" not in lines[signed_off:], lines
