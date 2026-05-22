"""Claude-powered crash root-cause analysis with structured output."""

from __future__ import annotations

import json
import textwrap
from typing import Any

import anthropic

from ..config import get_settings

# ---------------------------------------------------------------------------
# Async client — instantiated lazily so it's never created during import
# ---------------------------------------------------------------------------
_async_client: anthropic.AsyncAnthropic | None = None


def _get_client(api_key: str) -> anthropic.AsyncAnthropic:
    global _async_client
    if _async_client is None or _async_client.api_key != api_key:
        _async_client = anthropic.AsyncAnthropic(api_key=api_key)
    return _async_client

SYSTEM_PROMPT = textwrap.dedent("""
You are CrashPilot, an expert Linux kernel and systems reliability engineer.
Your job is to perform forensic root-cause analysis of Linux system crashes, reboots, and hangs.

You support ALL deployment environments:
- Bare-metal Linux (all distros: Ubuntu, RHEL/CentOS, Fedora, Arch, Alpine, openSUSE, Void…)
- WSL 1 and WSL 2 (Windows Subsystem for Linux — limited kernel visibility)
- Docker containers (privileged mode with host filesystem mounts)
- Kubernetes pods / DaemonSet nodes (cluster-level events + node telemetry)
- VMs: KVM/QEMU, VMware, VirtualBox, Hyper-V, Xen
- Cloud VMs: AWS EC2, GCP, Azure, DigitalOcean

You analyze telemetry evidence collected immediately after an abnormal event:
- systemd journal logs (if init=systemd; not available on WSL1 or Alpine)
- kernel ring buffer / dmesg (not available on WSL1)
- SMART disk health (not available in containers without host device access)
- GPU error logs: NVIDIA Xid codes, AMD amdgpu faults
- Thermal sensor readings (sysfs hwmon, lm-sensors; not available in VMs/containers)
- Docker daemon events and container OOM kills
- Kubernetes pod crashes: CrashLoopBackOff, OOMKilled, node conditions
- VM-specific: balloon driver pressure, virtio errors, live migration stalls
- Cloud-specific: Spot interruption notices, instance health events
- PCIe AER error reports, MCE (Machine Check Exceptions)
- Windows Event Log (WSL environments via PowerShell interop)

PLATFORM-SPECIFIC AWARENESS:
- WSL1: No kernel — crashes are Windows host issues. Analyze Windows Event Log data.
- WSL2: Has a kernel but no ACPI/hardware sensors. OOM is often .wslconfig memory limit.
- Docker: Privileged container accessing host paths. Check if --privileged was set.
- Kubernetes: OOMKilled means k8s memory limit, not OS OOM. Check Pod resource limits.
- VMware: Balloon driver (vmmemctl) can cause host-induced OOM. Check vMotion events.
- Cloud: Spot/preemptible interruption looks like a clean shutdown in logs.

Your analysis must be:
1. EVIDENCE-BASED: Every conclusion must cite specific log lines or metrics
2. PLATFORM-AWARE: Tailor analysis to the detected platform (WSL/k8s/VM/cloud)
3. CALIBRATED: Assign realistic confidence scores (0.0-1.0) — be honest about uncertainty
4. ACTIONABLE: Provide concrete remediation steps ranked by priority
5. CONCISE: Infrastructure engineers need fast answers, not essays

You respond ONLY with valid JSON matching the specified schema.
""").strip()

ANALYSIS_SCHEMA = {
    "root_cause": "string — one sentence answer to 'why did this machine crash?'",
    "crash_type": "string — kernel_panic|oom_kill|thermal_shutdown|power_loss|watchdog_reset|gpu_fault|disk_error|machine_check_exception|soft_lockup|clean_shutdown|unknown",
    "severity": "string — critical|high|medium|low|info",
    "confidence": "float 0.0-1.0 — your confidence in root_cause",
    "summary": "string — 2-3 sentence executive summary for a sysadmin",
    "timeline": [
        {
            "timestamp": "ISO-8601 or null",
            "event": "string — what happened",
            "significance": "string — why this matters",
        }
    ],
    "evidence": [
        {
            "source": "string — journal|dmesg|smart|gpu|thermal|docker|system",
            "excerpt": "string — exact log line or metric value",
            "interpretation": "string — what this tells us",
            "weight": "float 0.0-1.0 — how strongly this supports root_cause",
        }
    ],
    "contributing_factors": ["string — additional issues that may have contributed"],
    "remediation": [
        {
            "priority": "integer 1-5 (1=most urgent)",
            "action": "string — specific command or step",
            "rationale": "string — why this fixes or prevents the issue",
        }
    ],
    "monitoring_suggestions": ["string — metrics or alerts to set up"],
    "confidence_explanation": "string — explain what would increase or decrease your confidence",
}


def _truncate_telemetry(telemetry: dict, max_chars: int = 80_000) -> dict:
    """Trim large text fields to fit within Claude's context window."""
    result = {}
    for key, val in telemetry.items():
        if isinstance(val, dict):
            result[key] = _truncate_telemetry(val, max_chars // len(telemetry))
        elif isinstance(val, str) and len(val) > 8000:
            result[key] = val[-8000:]  # keep tail (most recent)
        else:
            result[key] = val
    return result


async def analyze_crash(
    telemetry: dict[str, Any],
    detection_result: dict[str, Any],
    timeline: list[dict],
) -> dict[str, Any]:
    """Send telemetry to Claude and return structured analysis."""
    cfg = get_settings()

    if not cfg.anthropic_api_key:
        return _no_api_key_result(detection_result)

    client = _get_client(cfg.anthropic_api_key)

    truncated = _truncate_telemetry(telemetry)

    user_prompt = textwrap.dedent(f"""
    ## Crash Detection (Heuristic Pre-Analysis)
    Detected crash type: {detection_result.get("crash_type", "unknown")}
    Severity: {detection_result.get("severity", "unknown")}
    Heuristic confidence: {detection_result.get("confidence", 0):.0%}
    Initial evidence: {json.dumps(detection_result.get("evidence", []), indent=2)}

    ## Preliminary Timeline ({len(timeline)} events)
    {json.dumps(timeline[:30], indent=2)}

    ## Full Telemetry
    ```json
    {json.dumps(truncated, indent=2, default=str)[:75000]}
    ```

    ## Task
    Perform forensic root-cause analysis and respond with a JSON object matching
    this exact schema:
    ```json
    {json.dumps(ANALYSIS_SCHEMA, indent=2)}
    ```

    Be specific. Cite exact log lines. If the crash type is clear from the evidence,
    say so with high confidence. If ambiguous, explain why and what additional data
    would help disambiguate.
    """).strip()

    try:
        message = await client.messages.create(
            model=cfg.claude_model,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )

        raw_text = message.content[0].text.strip()

        # Extract JSON from the response (may be wrapped in markdown)
        json_text = _extract_json(raw_text)
        analysis = json.loads(json_text)

        # Ensure required fields exist
        analysis.setdefault("crash_type", detection_result.get("crash_type", "unknown"))
        analysis.setdefault("severity", detection_result.get("severity", "unknown"))
        analysis.setdefault("confidence", detection_result.get("confidence", 0.3))
        analysis["ai_analyzed"] = True
        analysis["model"] = cfg.claude_model
        analysis["usage"] = {
            "input_tokens": message.usage.input_tokens,
            "output_tokens": message.usage.output_tokens,
        }

        return analysis

    except anthropic.AuthenticationError:
        return _error_result("Invalid Anthropic API key", detection_result)
    except anthropic.RateLimitError:
        return _error_result("Anthropic API rate limit exceeded", detection_result)
    except json.JSONDecodeError as e:
        return _error_result(f"Failed to parse AI response as JSON: {e}", detection_result)
    except Exception as e:
        return _error_result(str(e), detection_result)


def _extract_json(text: str) -> str:
    """Extract JSON from a response that might be wrapped in markdown code fences."""
    if text.startswith("{"):
        return text
    # Try to find ```json ... ``` block
    import re
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        return m.group(1)
    # Find first { to last }
    start = text.find("{")
    end = text.rfind("}") + 1
    if start != -1 and end > start:
        return text[start:end]
    return text


def _no_api_key_result(detection: dict) -> dict:
    return {
        "root_cause": "API key not configured — heuristic analysis only",
        "crash_type": detection.get("crash_type", "unknown"),
        "severity": detection.get("severity", "unknown"),
        "confidence": detection.get("confidence", 0.3),
        "summary": (
            f"CrashPilot detected this as a {detection.get('crash_type', 'unknown')} event "
            f"with {detection.get('confidence', 0):.0%} confidence based on log pattern matching. "
            "Set CRASHPILOT_ANTHROPIC_API_KEY to enable full AI root-cause analysis."
        ),
        "evidence": [{"source": e, "excerpt": e, "interpretation": "", "weight": 0.5}
                     for e in detection.get("evidence", [])[:5]],
        "timeline": [],
        "contributing_factors": [],
        "remediation": [],
        "monitoring_suggestions": [],
        "confidence_explanation": "AI analysis unavailable — configure Anthropic API key",
        "ai_analyzed": False,
    }


def _error_result(error: str, detection: dict) -> dict:
    result = _no_api_key_result(detection)
    result["ai_error"] = error
    result["summary"] = f"AI analysis failed: {error}. Heuristic result shown."
    return result
