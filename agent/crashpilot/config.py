"""Configuration management for CrashPilot agent."""

from __future__ import annotations

import os
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CRASHPILOT_",
        env_file=Path.home() / ".config" / "crashpilot" / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Anthropic
    anthropic_api_key: str = ""
    claude_model: str = "claude-opus-4-7"

    # Storage
    data_dir: Path = Path.home() / ".local" / "share" / "crashpilot"
    db_path: Path = Path("")  # resolved in __init__

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
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if not self.db_path or str(self.db_path) == "":
            object.__setattr__(self, "db_path", self.data_dir / "crashpilot.db")


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
