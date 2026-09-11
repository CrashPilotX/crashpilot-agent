"""Keep every test away from the machine it runs on.

Settings fall back to ~/.config/crashpilot/.env and /etc/crashpilot/.env, and
the data directory falls back to ~/.local/share/crashpilot. A test that did
not override all of them read, and wrote, the developer's real agent: its
credentials, its crash database, and its caches. This fixture points HOME,
the config directory and the data directory at the test's own temp dir
before anything else runs; individual tests can still override them.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _hermetic_agent_paths(monkeypatch, tmp_path_factory):
    # A directory of its own, not the test's tmp_path: several tests scan
    # tmp_path or expect it to start empty.
    root = tmp_path_factory.mktemp("agent-env")
    home = root / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CRASHPILOT_CONFIG_DIR", str(root))
    monkeypatch.setenv("CRASHPILOT_DATA_DIR", str(root / "data"))
    # Left to derive from whichever data dir the test ends up using, so a test
    # that sets only CRASHPILOT_DATA_DIR still gets its database beside it.
    monkeypatch.delenv("CRASHPILOT_DB_PATH", raising=False)
    # An existing file here is found before any fallback outside the sandbox.
    (root / ".env").write_text("")
    for name in (
        "CRASHPILOT_SUPABASE_URL", "CRASHPILOT_SUPABASE_ANON_KEY",
        "CRASHPILOT_SUPABASE_SYSTEM_ID", "CRASHPILOT_SUPABASE_TOKEN",
        "CRASHPILOT_ENROLL_TOKEN", "CRASHPILOT_NODE_NAME", "CRASHPILOT_EXTERNAL_ID",
    ):
        monkeypatch.delenv(name, raising=False)

    import crashpilot.config as cfg_mod

    cfg_mod._settings = None
    yield
    cfg_mod._settings = None
