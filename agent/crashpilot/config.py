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

    # Cloud push mode — set by `crashpilot configure <connection-string>`.
    # When configured, the agent pushes heartbeats and reports to Supabase
    # outbound (no public URL or open ports needed).
    supabase_url: str = ""
    supabase_anon_key: str = ""
    supabase_system_id: str = ""
    supabase_token: str = ""  # agent token stored in the Supabase systems table

    # Optional outbound incident notification. Only HTTPS endpoints are used.
    webhook_url: str = ""
    webhook_secret: str = ""
    maintenance_until: str = ""

    # Egress budget — bytes this agent may send to Supabase per UTC day.
    # Above egress_soft_limit_mb the agent switches to slim mode (live metrics only,
    # no dmesg/flight_recorder/agent_health).  Above egress_daily_limit_mb heartbeats
    # switch to an ultra-tiny keepalive until UTC midnight. Crash reports are always sent.
    # Set either to 0 to disable that tier.
    # Budget math: 5 GB/month ÷ 100 systems ÷ 30 days ≈ 1.71 MB/system/day.
    egress_soft_limit_mb: int = 4
    egress_daily_limit_mb: int = 8

    # Optional capacity test. Disabled by default because it uses external
    # speedtest servers and can consume significant bandwidth. Passive RX/TX
    # throughput is always collected from /proc/net/dev.
    bandwidth_speedtest_enabled: bool = False
    bandwidth_speedtest_interval_seconds: int = 21600
    bandwidth_speedtest_timeout_seconds: int = 90

    # Cloud heartbeat egress controls.  The one-minute heartbeat keeps systems
    # online, but detailed live metrics and status polling can run less often.
    live_metrics_interval_seconds: int = 900
    cloud_status_interval_seconds: int = 1800

    def model_post_init(self, __context: object) -> None:
        data_dir = self.data_dir or _default_data_dir()
        data_dir.mkdir(parents=True, exist_ok=True)
        object.__setattr__(self, "data_dir", data_dir)

        if self.db_path is None:
            object.__setattr__(self, "db_path", data_dir / "crashpilot.db")


_settings: Optional[Settings] = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
