"""Configuration management for CrashPilot agent."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


def _find_env_file() -> Path:
    """
    Search for .env in order of preference:
      1. $CRASHPILOT_CONFIG_DIR/.env   (explicit override)
      2. ~/.config/crashpilot/.env     (per-user install)
      3. /etc/crashpilot/.env          (system-wide / sudo install)
    Returns the first path that exists AND is readable by the current process.
    Skipping unreadable files prevents PermissionError when a non-root user
    runs `crashpilot token` after a system-wide (root) install where
    /etc/crashpilot/.env is owned by root.
    """
    candidates: list[Path] = []
    if "CRASHPILOT_CONFIG_DIR" in os.environ:
        candidates.append(Path(os.environ["CRASHPILOT_CONFIG_DIR"]) / ".env")
    candidates.append(Path.home() / ".config" / "crashpilot" / ".env")
    candidates.append(Path("/etc/crashpilot/.env"))

    for p in candidates:
        if p.exists() and os.access(p, os.R_OK):
            return p
    # Default (may not exist yet — pydantic-settings silently skips missing files)
    return Path.home() / ".config" / "crashpilot" / ".env"


def _default_data_dir() -> Path:
    """
    System-wide install (root wrote to /opt/crashpilot) → /opt/crashpilot/data
    Per-user install                                     → ~/.local/share/crashpilot
    """
    system_dir = Path("/opt/crashpilot/data")
    if system_dir.parent.exists() and os.access(system_dir.parent, os.W_OK):
        return system_dir
    return Path.home() / ".local" / "share" / "crashpilot"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CRASHPILOT_",
        env_file=_find_env_file(),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Anthropic
    anthropic_api_key: str = ""
    claude_model: str = "claude-opus-4-7"

    # Storage — use Optional[Path] so None is unambiguously "not set yet"
    # (Path("") evaluates to PosixPath('.') which is truthy — don't use it as sentinel)
    data_dir: Optional[Path] = None
    db_path: Optional[Path] = None

    # API server
    api_host: str = "127.0.0.1"
    api_port: int = 7878
    api_cors_origins: list[str] = ["*"]

    # Collection limits
    journal_lines: int = 5000
    dmesg_lines: int = 2000
    max_report_age_days: int = 90

    # Analysis
    confidence_threshold: float = 0.4
    analysis_timeout: int = 120

    # Agent API authentication token.
    # Leave empty to auto-generate a token on first run (stored in data_dir/agent.token).
    # Override with CRASHPILOT_API_TOKEN env var or in .env.
    api_token: str = ""

    # Public URL of this agent (e.g. a Cloudflare Tunnel URL).
    # Set automatically by install.sh when cloudflared is configured.
    # Used by `crashpilot token` to build the one-click dashboard link.
    public_url: str = ""

    def model_post_init(self, __context: object) -> None:
        # Resolve data_dir
        if self.data_dir is None:
            object.__setattr__(self, "data_dir", _default_data_dir())
        self.data_dir.mkdir(parents=True, exist_ok=True)  # type: ignore[union-attr]

        # Resolve db_path
        if self.db_path is None:
            object.__setattr__(self, "db_path", self.data_dir / "crashpilot.db")


_settings: Optional[Settings] = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
