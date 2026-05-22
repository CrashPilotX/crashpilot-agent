"""Configuration management for CrashPilot agent."""

from __future__ import annotations

import os
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


def _find_env_file() -> Path:
    """
    Search for .env in order of preference:
      1. $CRASHPILOT_CONFIG_DIR/.env   (explicit override)
      2. ~/.config/crashpilot/.env     (per-user install)
      3. /etc/crashpilot/.env          (system-wide / sudo install)
    Returns the first one that exists, or the user path as default.
    """
    candidates = [
        Path(os.environ["CRASHPILOT_CONFIG_DIR"]) / ".env"
        if "CRASHPILOT_CONFIG_DIR" in os.environ else None,
        Path.home() / ".config" / "crashpilot" / ".env",
        Path("/etc/crashpilot/.env"),
    ]
    for p in candidates:
        if p and p.exists():
            return p
    # Default (may not exist yet — pydantic-settings handles missing files)
    return Path.home() / ".config" / "crashpilot" / ".env"


def _default_data_dir() -> Path:
    """
    System-wide install (root) → /opt/crashpilot/data
    Per-user install         → ~/.local/share/crashpilot
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

    # Storage
    data_dir: Path = Path("")   # resolved in model_post_init
    db_path: Path = Path("")    # resolved in model_post_init

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

    def model_post_init(self, __context: object) -> None:
        # Resolve data_dir default if not set via env
        if not self.data_dir or str(self.data_dir) == "":
            object.__setattr__(self, "data_dir", _default_data_dir())
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if not self.db_path or str(self.db_path) == "":
            object.__setattr__(self, "db_path", self.data_dir / "crashpilot.db")


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
