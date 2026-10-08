"""Classify a pasted or saved kernel/journal log with the heuristic detector.

Unlike `analyze`, nothing here touches the host: no journalctl, no dmesg, no
nvidia-smi, no database, no network. The text goes straight into
`detect_crash_type`, so the answer is the same on any machine.
"""

from __future__ import annotations

from typing import Any

from .analyzers.crash_detector import detect_crash_type


def classify_text(text: str) -> dict[str, Any]:
    """Run the heuristic detector over `text` and return a plain dict.

    The log goes in the journal's error field, which the detector reads whole.
    The dmesg tail field is cut to its last 10,000 characters, which would
    silently drop the start of a long excerpt.
    """
    telemetry: dict[str, Any] = {
        "journal": {"previous_boot_errors": text},
        "dmesg": {},
        "gpu": {"nvidia": {}},
        "smart": {},
        "thermal": {},
    }
    result = detect_crash_type(telemetry)
    return {
        "category": result.crash_type.value,
        "severity": result.severity.value,
        "confidence": round(result.confidence, 2),
        "evidence": list(result.evidence),
        "alternatives": [
            {
                "category": alt["crash_type"],
                "confidence": round(alt["confidence"], 2),
                "evidence": list(alt["evidence"]),
            }
            for alt in result.alternatives
        ],
    }
