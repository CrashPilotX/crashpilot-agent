"""Detect and redact secret-looking text before it reaches AI analysis,
cloud storage, or the dashboard.

This is the mechanism behind the "automatic secret detection and redaction"
claim on the Data Handling page - it must run before analyze_crash() sends
anything to Anthropic, and before the telemetry is persisted anywhere.

Patterns are deliberately broad ("looks like a credential" rather than
"is definitely a credential"): the cost of over-redacting a config value
that merely looks like a secret is a slightly less specific evidence line;
the cost of under-redacting a real one is a leaked credential. When in
doubt, redact.
"""

from __future__ import annotations

import re
from typing import Any

# Each pattern either has exactly one capture group (the secret to mask,
# with everything else in the match kept as-is) or none (the whole match is
# the secret). category names are what the dashboard/docs surface to users.
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("stripe_key", re.compile(r"\b[sr]k_live_[A-Za-z0-9]{16,}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9\-_]{20,}\b")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("private_key_block", re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        re.DOTALL,
    )),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    ("bearer_token", re.compile(r"\bBearer\s+([A-Za-z0-9\-_.=]{20,})\b", re.IGNORECASE)),
    # user:password@host in a URL - keep the scheme/host, mask the credential.
    ("basic_auth_url", re.compile(r"(?<=://)[^:/\s@]+:([^@/\s]+)@")),
    # key=value / key: "value" where the key name looks like a credential.
    # Group 1 is the value only, so the key name stays visible for context.
    ("labeled_credential", re.compile(
        r"(?i)\b(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|"
        r"auth[_-]?token|credential)s?\s*[:=]\s*['\"]?([A-Za-z0-9\-_./+=]{6,})['\"]?"
    )),
]

_MASK = "[REDACTED:{category}]"

# Fields that are already-parsed identifiers, not free-text log lines - never
# worth scanning (avoids e.g. redacting a boot_id that happens to start with
# characters resembling a pattern) and keeps the pass fast on large payloads.
_SKIP_KEYS = {"boot_id", "id", "system_id", "report_id"}


def redact_text(text: str) -> tuple[str, dict[str, int]]:
    """Redact secret-looking substrings in `text`.

    Returns (redacted_text, counts) where counts maps category -> number of
    redactions made for that category (categories with zero matches are
    omitted).
    """
    counts: dict[str, int] = {}

    def _sub(pattern: re.Pattern[str], category: str, text: str) -> str:
        def _replace(match: re.Match[str]) -> str:
            counts[category] = counts.get(category, 0) + 1
            mask = _MASK.format(category=category)
            if match.groups():
                # Replace only the captured secret, preserving the rest of
                # the match (e.g. the "Bearer " prefix or "user:" in a URL).
                start, end = match.span(1)
                return match.group(0)[: start - match.start()] + mask + match.group(0)[end - match.start():]
            return mask
        return pattern.sub(_replace, text)

    for category, pattern in _PATTERNS:
        text = _sub(pattern, category, text)

    return text, counts


def redact_value(value: Any, key: str | None = None) -> tuple[Any, dict[str, int]]:
    """Recursively redact strings anywhere inside a JSON-like structure
    (the shape telemetry/flight-recorder data is always in: dicts, lists,
    strings, and primitives)."""
    total: dict[str, int] = {}
    if isinstance(value, str):
        if key in _SKIP_KEYS:
            return value, total
        redacted, counts = redact_text(value)
        for category, n in counts.items():
            total[category] = total.get(category, 0) + n
        return redacted, total
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for k, v in value.items():
            redacted, counts = redact_value(v, key=k)
            result[k] = redacted
            for category, n in counts.items():
                total[category] = total.get(category, 0) + n
        return result, total
    if isinstance(value, list):
        result_list: list[Any] = []
        for item in value:
            redacted, counts = redact_value(item)
            result_list.append(redacted)
            for category, n in counts.items():
                total[category] = total.get(category, 0) + n
        return result_list, total
    return value, total


def redact_telemetry(telemetry: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Redact an entire telemetry dict before it reaches crash detection,
    AI analysis, or cloud storage.

    Returns (redacted_telemetry, summary) where summary is
    {"count": int, "categories": [str, ...]} - the same shape stored on
    the report's analysis.redaction field and shown in the dashboard.
    """
    redacted, counts = redact_value(telemetry)
    summary = {
        "count": sum(counts.values()),
        "categories": sorted(counts.keys()),
    }
    return redacted, summary
