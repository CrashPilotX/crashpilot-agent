"""
Self-enrollment: a node joins the dashboard with a join token instead of a
connection string someone copied for that one machine.

A join token (``cpjoin_...``) carries the Supabase URL, the project's public
anon key, and a secret. ``agent_enroll`` exchanges the secret, plus a stable
identity for this machine, for the node's own system ID and agent token,
which are then stored exactly like ``crashpilot configure`` stores them. So
heartbeats, reports and everything downstream are unchanged.

The same identity enrolling again through the same token gets the same
system back with fresh credentials, which is what makes a reinstall, a lost
disk, or a Kubernetes pod restart carry on as the same node.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import re
import socket
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)

JOIN_PREFIX = "cpjoin_"
_RPC_TIMEOUT = httpx.Timeout(15.0)
_SIGN_OFF_TIMEOUT = httpx.Timeout(4.0)
# Cloud metadata services answer in milliseconds when present. Anywhere else
# the address is unroutable, so keep the wait short enough that a bare-metal
# machine does not stall its first boot.
_METADATA_TIMEOUT = httpx.Timeout(1.0, connect=0.5)
# A re-enrollment after rejected credentials is attempted at most this often,
# so a node that keeps failing does not hammer the enrollment endpoint.
REENROLL_COOLDOWN_SECONDS = 600


class EnrollmentError(RuntimeError):
    """Enrollment failed in a way retrying will not fix."""


class EnrollmentRateLimited(RuntimeError):
    """The token saw too many enrollments in the last minute; retry shortly."""


@dataclass(frozen=True)
class JoinToken:
    url: str
    anon_key: str
    secret: str


def parse_join_token(value: str) -> JoinToken:
    """Decode a ``cpjoin_`` string. Raises EnrollmentError with a usable message."""
    raw = (value or "").strip().strip("'\"")
    if not raw.startswith(JOIN_PREFIX):
        raise EnrollmentError(
            "That is not a join token. Join tokens start with cpjoin_; create one on the "
            "dashboard's Systems page. (A cpilot_ connection string goes to `crashpilot configure`.)"
        )
    body = raw[len(JOIN_PREFIX):]
    try:
        decoded = json.loads(base64.b64decode(body + "=" * (-len(body) % 4), validate=False))
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise EnrollmentError(f"The join token could not be decoded: {exc}") from exc
    if not isinstance(decoded, dict):
        raise EnrollmentError("The join token is malformed.")
    missing = [key for key in ("url", "key", "join") if not decoded.get(key)]
    if missing:
        raise EnrollmentError(f"The join token is missing: {', '.join(missing)}.")
    url = str(decoded["url"]).rstrip("/")
    # Every heartbeat and report goes to this URL, so never downgrade to
    # plaintext because of a tampered or mistyped token.
    if not url.startswith("https://"):
        raise EnrollmentError("The join token's Supabase URL must be https://; refusing to enroll.")
    return JoinToken(url=url, anon_key=str(decoded["key"]), secret=str(decoded["join"]))


# ── Identity ──────────────────────────────────────────────────────────────────

def _aws_instance_id(client: httpx.Client) -> str | None:
    # IMDSv2 only: a session token first, then the instance ID with it.
    token = client.put(
        "http://169.254.169.254/latest/api/token",
        headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"},
    )
    if token.status_code != 200 or not token.text:
        return None
    ident = client.get(
        "http://169.254.169.254/latest/meta-data/instance-id",
        headers={"X-aws-ec2-metadata-token": token.text},
    )
    if ident.status_code == 200 and re.fullmatch(r"i-[0-9a-f]{8,32}", ident.text.strip()):
        return ident.text.strip()
    return None


def _gcp_instance_id(client: httpx.Client) -> str | None:
    resp = client.get(
        "http://metadata.google.internal/computeMetadata/v1/instance/id",
        headers={"Metadata-Flavor": "Google"},
    )
    if resp.status_code == 200 and resp.text.strip().isdigit():
        return resp.text.strip()
    return None


def _azure_vm_id(client: httpx.Client) -> str | None:
    resp = client.get(
        "http://169.254.169.254/metadata/instance/compute/vmId",
        params={"api-version": "2021-02-01", "format": "text"},
        headers={"Metadata": "true"},
    )
    if resp.status_code == 200 and re.fullmatch(r"[0-9a-fA-F-]{36}", resp.text.strip()):
        return resp.text.strip().lower()
    return None


def _cloud_instance_id() -> str | None:
    probes = (("aws", _aws_instance_id), ("gcp", _gcp_instance_id), ("azure", _azure_vm_id))
    try:
        with httpx.Client(timeout=_METADATA_TIMEOUT, follow_redirects=False) as client:
            for provider, probe in probes:
                try:
                    found = probe(client)
                except httpx.HTTPError:
                    continue
                if found:
                    return f"{provider}:{found}"
    except httpx.HTTPError:
        return None
    return None


def _machine_id(path: Path = Path("/etc/machine-id")) -> str | None:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value if re.fullmatch(r"[0-9a-f]{32}", value) else None


def resolve_identity(explicit: str = "", node_name: str = "") -> str:
    """A stable identity for this machine, most specific source first.

    1. An explicit ``--external-id`` / ``CRASHPILOT_EXTERNAL_ID``.
    2. The Kubernetes node name (``CRASHPILOT_NODE_NAME`` from the downward API).
    3. The cloud instance ID (AWS IMDSv2, GCP, Azure).
    4. ``/etc/machine-id``. Last among the real sources because images that
       were cloned without being generalized often share one.
    5. The hostname, so enrollment never fails for want of an identity.
    """
    if explicit.strip():
        return explicit.strip()[:255]
    if node_name.strip():
        return f"k8s:{node_name.strip()}"[:255]
    cloud = _cloud_instance_id()
    if cloud:
        return cloud
    machine = _machine_id()
    if machine:
        return f"machine:{machine}"
    return f"host:{socket.gethostname()}"[:255]


# ── RPC calls ─────────────────────────────────────────────────────────────────

def _headers(anon_key: str) -> dict[str, str]:
    return {
        "apikey": anon_key,
        "Authorization": f"Bearer {anon_key}",
        "Content-Type": "application/json",
    }


def _server_message(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text.strip()
    if isinstance(body, dict):
        return str(body.get("message") or body.get("hint") or body)
    return str(body)


def enroll_once(token: JoinToken, external_id: str, hostname: str | None, version: str) -> dict[str, Any]:
    """Call agent_enroll once. Returns {system_id, agent_token, name}."""
    resp = httpx.post(
        f"{token.url}/rest/v1/rpc/agent_enroll",
        headers=_headers(token.anon_key),
        json={
            "p_join_secret": token.secret,
            "p_external_id": external_id,
            "p_hostname": hostname,
            "p_version": version,
        },
        timeout=_RPC_TIMEOUT,
    )
    if resp.status_code == 200:
        data = resp.json()
        if not isinstance(data, dict) or not data.get("system_id") or not data.get("agent_token"):
            raise EnrollmentError(f"Unexpected response from agent_enroll: {resp.text[:200]}")
        return data

    message = _server_message(resp)
    if resp.status_code == 404 or "Could not find the function" in message or "PGRST202" in resp.text:
        raise EnrollmentError(
            "This Supabase project does not support join tokens yet. Run supabase/schema.sql "
            "in its SQL editor, then enroll again."
        )
    if resp.status_code in (401, 403):
        raise EnrollmentError(f"Supabase rejected the join token's anon key (HTTP {resp.status_code}).")
    if "Too many enrollments" in message:
        raise EnrollmentRateLimited(message)
    if "Invalid enrollment token" in message:
        raise EnrollmentError(
            "This join token is not valid: it may have been revoked, expired, or used up. "
            "Create a new one on the dashboard's Systems page."
        )
    raise EnrollmentError(message or f"HTTP {resp.status_code} from agent_enroll")


def enroll(
    token: JoinToken,
    external_id: str,
    hostname: str | None,
    version: str,
    *,
    max_wait_seconds: float = 300.0,
    sleep: Any = time.sleep,
) -> dict[str, Any]:
    """Enroll, retrying with jittered backoff while the token is rate-limited.

    A large scale-out can exceed the per-token limit of 60 enrollments a
    minute; backing off lets every node through within a few minutes.
    """
    delay = 2.0
    waited = 0.0
    while True:
        try:
            return enroll_once(token, external_id, hostname, version)
        except EnrollmentRateLimited:
            if waited >= max_wait_seconds:
                raise EnrollmentError(
                    "Gave up after the join token stayed rate-limited for five minutes."
                ) from None
            # Jitter from os.urandom keeps a fleet that booted together from
            # retrying in lockstep, without pulling in the random module.
            pause = min(delay, max_wait_seconds - waited) * (0.5 + int.from_bytes(os.urandom(1), "big") / 510)
            log.info("Join token is rate-limited; retrying enrollment in %.0f s", pause)
            sleep(pause)
            waited += pause
            delay = min(delay * 2, 60.0)


def sign_off(url: str, anon_key: str, system_id: str, agent_token: str) -> None:
    """Tell the dashboard this node is shutting down cleanly.

    Only means "expect silence": the next heartbeat undoes it, because the
    agent cannot tell a reboot from a power-off or a pod rollout from a drain.
    """
    resp = httpx.post(
        f"{url.rstrip('/')}/rest/v1/rpc/agent_sign_off",
        headers=_headers(anon_key),
        json={"p_system_id": system_id, "p_agent_token": agent_token},
        timeout=_SIGN_OFF_TIMEOUT,
    )
    if resp.status_code not in (200, 204):
        raise RuntimeError(_server_message(resp) or f"HTTP {resp.status_code} from agent_sign_off")


# ── Credentials on disk ───────────────────────────────────────────────────────

def env_file_for_write(found: Path) -> Path:
    """Where to store credentials.

    ``config._find_env_file`` returns the first *existing* file, so on a
    first boot it falls back to ``~/.config``. In a container that path does
    not survive a restart; honour ``CRASHPILOT_CONFIG_DIR`` (the DaemonSet
    points it at the node's persistent hostPath) even before the file exists.
    """
    if found.exists():
        return found
    config_dir = os.environ.get("CRASHPILOT_CONFIG_DIR")
    if config_dir:
        return Path(config_dir) / ".env"
    return found


def write_env_values(env_path: Path, values: dict[str, str]) -> None:
    """Set KEY=value lines in a .env file, privately and atomically.

    The file holds the agent token, so it is written to a 0600 temp file and
    renamed over the original: never readable by others, not even briefly,
    and never left half-written.
    """
    existing = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    for key, value in values.items():
        if "\n" in value or "\r" in value:
            raise ValueError(f"{key} must be a single line")
        pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
        line = f"{key}={value}"
        if pattern.search(existing):
            # Escaped for re.sub so backslashes in the value stay literal.
            existing = pattern.sub(line.replace("\\", "\\\\"), existing)
        else:
            existing = existing.rstrip("\n") + ("\n" if existing.strip() else "") + line + "\n"
    if not existing.endswith("\n"):
        existing += "\n"

    env_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".env.", dir=env_path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(existing)
        os.replace(tmp_name, env_path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def reenroll_allowed(marker: Path, now: float | None = None) -> bool:
    """Whether a re-enrollment may be attempted now, recording the attempt."""
    now = time.time() if now is None else now
    try:
        last = float(marker.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        last = 0.0
    if now - last < REENROLL_COOLDOWN_SECONDS:
        return False
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(str(now), encoding="utf-8")
    except OSError:
        log.debug("Could not record the re-enrollment attempt at %s", marker)
    return True
