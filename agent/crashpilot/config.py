"""Configuration management for CrashPilot agent."""

from __future__ import annotations

import os
import stat
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
    # Default (may not exist yet: pydantic-settings silently skips missing files)
    return Path.home() / ".config" / "crashpilot" / ".env"


class InsecureSupabaseURL(ValueError):
    """The Supabase URL is not https://, so nothing is sent to it."""


def require_https(url: str) -> str:
    """The Supabase URL, if it is https://.

    Every call to it carries the agent token. configure and join tokens
    already insist on https://, and a URL set in the environment (a
    Kubernetes Secret, a docker .env) must not bypass that. Checked where
    requests are made rather than when settings load, so a bad URL stops
    uploads without also stopping local crash analysis.
    """
    if not url.lower().startswith("https://"):
        raise InsecureSupabaseURL(
            "CRASHPILOT_SUPABASE_URL must start with https:// (refusing to send the agent "
            "token in plaintext). Fix it in the environment or .env."
        )
    return url


_INSTALLER_DATA_DIR = Path("/opt/crashpilot/data")
_PACKAGE_DATA_DIR = Path("/var/lib/crashpilot")


def _default_data_dir() -> Path:
    """
    System-wide install (root wrote to /opt/crashpilot) → /opt/crashpilot/data
    Ubuntu package (its services use /var/lib/crashpilot) → /var/lib/crashpilot
    Per-user install                                     → ~/.local/share/crashpilot
    """
    system_dir = _INSTALLER_DATA_DIR
    if system_dir.parent.exists() and os.access(system_dir.parent, os.W_OK):
        return system_dir
    if _PACKAGE_DATA_DIR.is_dir() and os.access(_PACKAGE_DATA_DIR, os.W_OK):
        return _PACKAGE_DATA_DIR
    return Path.home() / ".local" / "share" / "crashpilot"


def _make_private_dir(path: Path) -> None:
    """Create the data dir 0700, and close one that is open.

    It holds the crash database and journal/dmesg caches. Found open when a
    Kubernetes hostPath (DirectoryOrCreate) or an older install created it
    0755. Only a directory of our own is tightened: owned by this user, not
    sticky, and named for crashpilot, so a shared directory someone set
    CRASHPILOT_DATA_DIR to by mistake (/var/lib, /tmp) is left alone.
    """
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        st = path.stat()
        if (
            st.st_mode & 0o077
            and st.st_uid == os.geteuid()
            and not st.st_mode & stat.S_ISVTX
            and any("crashpilot" in part.lower() for part in path.resolve().parts[-2:])
        ):
            path.chmod(0o700)
    except OSError:
        pass


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

    # Storage: use Optional[Path] so None is unambiguously "not set yet"
    # (Path("") evaluates to PosixPath('.') which is truthy: don't use it as sentinel)
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

    # The local API's bearer token is generated on first run and kept in
    # data_dir/agent.token (`crashpilot token` shows it); it is not a setting.

    # Cloud push mode: set by `crashpilot configure <connection-string>`.
    # When configured, the agent pushes heartbeats and reports to Supabase
    # outbound (no public URL or open ports needed).
    supabase_url: str = ""
    supabase_anon_key: str = ""
    supabase_system_id: str = ""
    supabase_token: str = ""  # agent token stored in the Supabase systems table

    # Self-enrollment. A cpjoin_ join token lets the node enroll itself (and
    # enroll again if its credentials are ever rejected). node_name comes from
    # the Kubernetes downward API; external_id overrides identity detection.
    # external_id_source records whether the pinned external_id was
    # "detected" or "explicit": only a detected one is replaced when the
    # machine turns out to be a copy of the one that enrolled.
    enroll_token: str = ""
    node_name: str = ""
    external_id: str = ""
    external_id_source: str = ""

    # Optional outbound incident notification. Only HTTPS endpoints are used.
    webhook_url: str = ""
    webhook_secret: str = ""
    maintenance_until: str = ""

    # Egress budget: bytes this agent may send to Supabase per UTC day.
    # Above egress_soft_limit_mb the agent switches to slim mode (live metrics only,
    # no dmesg/flight_recorder/agent_health).  Above egress_daily_limit_mb heartbeats
    # switch to an ultra-tiny keepalive until UTC midnight. Crash reports are always sent.
    # Set either to 0 to disable that tier.
    # Defaults preserve online heartbeats during Supabase Free egress pressure:
    # slim mode after 16 MB/day, minimal keepalive after 32 MB/day per system.
    egress_soft_limit_mb: int = 16
    egress_daily_limit_mb: int = 32

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
        _make_private_dir(data_dir)
        object.__setattr__(self, "data_dir", data_dir)

        if self.db_path is None:
            object.__setattr__(self, "db_path", data_dir / "crashpilot.db")


_settings: Optional[Settings] = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        # env_file=_find_env_file() in Settings.model_config above is only
        # ever evaluated once, when this module's Settings class body first
        # executes - it does NOT re-run discovery on every Settings()
        # instantiation. Callers that set CRASHPILOT_CONFIG_DIR (or
        # otherwise change which .env should take precedence) after this
        # module was already imported, then reset _settings = None
        # expecting a fresh reload, would silently keep reading the
        # original path. Passing _env_file explicitly re-runs discovery on
        # every call, which is what the _settings-reset pattern used
        # throughout this codebase and its tests actually relies on.
        # BaseSettings accepts _env_file at runtime, but pydantic-settings
        # stopped declaring underscore-prefixed kwargs on the generated
        # __init__, so the call needs an explicit ignore to type-check.
        _settings = Settings(_env_file=_find_env_file())  # type: ignore[call-arg]
    return _settings
