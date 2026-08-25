"""Tests for the WSL collector's Windows-profile detection.

$LOGNAME inside WSL is the Linux distro username, not the Windows account
name - these tests exist to catch a regression back to guessing the
Windows username/profile path from it, which silently breaks whenever the
two usernames differ (a very common case).
"""

from __future__ import annotations

import pytest

from crashpilot.collectors.wsl import WslCollector, _windows_user_profile_dirs


class TestWindowsUserProfileDirs:
    def test_returns_real_user_profiles(self, tmp_path):
        (tmp_path / "saqib").mkdir()
        (tmp_path / "Public").mkdir()
        (tmp_path / "Default").mkdir()
        (tmp_path / "Default User").mkdir()
        (tmp_path / "All Users").mkdir()

        profiles = _windows_user_profile_dirs(base=tmp_path)

        assert [p.name for p in profiles] == ["saqib"]

    def test_handles_multiple_real_profiles(self, tmp_path):
        (tmp_path / "alice").mkdir()
        (tmp_path / "bob").mkdir()

        profiles = _windows_user_profile_dirs(base=tmp_path)

        assert [p.name for p in profiles] == ["alice", "bob"]

    def test_returns_empty_list_when_base_does_not_exist(self, tmp_path):
        assert _windows_user_profile_dirs(base=tmp_path / "does-not-exist") == []


class TestReadInteropInfo:
    def test_sets_windows_username_only_when_exactly_one_profile(self, tmp_path, monkeypatch):
        (tmp_path / "saqib").mkdir()
        monkeypatch.setattr(
            "crashpilot.collectors.wsl._windows_user_profile_dirs",
            lambda: _windows_user_profile_dirs(base=tmp_path),
        )
        info = WslCollector()._read_interop_info()
        assert info["windows_username"] == "saqib"

    def test_leaves_windows_username_unset_when_ambiguous(self, tmp_path, monkeypatch):
        (tmp_path / "alice").mkdir()
        (tmp_path / "bob").mkdir()
        monkeypatch.setattr(
            "crashpilot.collectors.wsl._windows_user_profile_dirs",
            lambda: _windows_user_profile_dirs(base=tmp_path),
        )
        info = WslCollector()._read_interop_info()
        assert info["windows_username"] is None

    def test_leaves_windows_username_unset_when_no_profiles_found(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "crashpilot.collectors.wsl._windows_user_profile_dirs",
            lambda: _windows_user_profile_dirs(base=tmp_path / "missing"),
        )
        info = WslCollector()._read_interop_info()
        assert info["windows_username"] is None


@pytest.mark.asyncio
class TestWslCrashHints:
    async def test_finds_wslconfig_across_any_real_profile_not_just_a_guessed_one(self, tmp_path, monkeypatch):
        # Regression: the old code guessed a single path from LOGNAME and
        # gave up on OSError - a WSL2 OOM crash caused by a real
        # .wslconfig memory limit under a differently-named Windows
        # profile was silently never surfaced.
        profile = tmp_path / "not-the-linux-username"
        profile.mkdir()
        (profile / ".wslconfig").write_text("[wsl2]\nmemory=4GB\n")
        monkeypatch.setattr(
            "crashpilot.collectors.wsl._windows_user_profile_dirs",
            lambda: _windows_user_profile_dirs(base=tmp_path),
        )
        hints = await WslCollector()._get_wsl_crash_hints(version=2)
        assert any(".wslconfig found" in h and "memory" in h.lower() for h in hints)

    async def test_no_hint_when_no_wslconfig_exists(self, tmp_path, monkeypatch):
        (tmp_path / "someuser").mkdir()
        monkeypatch.setattr(
            "crashpilot.collectors.wsl._windows_user_profile_dirs",
            lambda: _windows_user_profile_dirs(base=tmp_path),
        )
        hints = await WslCollector()._get_wsl_crash_hints(version=2)
        assert not any(".wslconfig found" in h for h in hints)
