"""Emergency-only Opus authorization for stack self-repair."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import time
from typing import Any, Dict

from core import BASE_DIR


LOG_PATH = BASE_DIR / "data" / "logs" / "emergency_opus_gate.jsonl"
TRUE_VALUES = {"1", "true", "yes", "on"}
ALLOWED_SOURCES = {"self_healer:emergency_fix_generation"}
ALLOWED_SEVERITIES = {"critical", "defcon1", "outage", "emergency"}
ALLOWED_SCOPES = {"stack_repair", "service_restore", "credit_containment"}


def _env_enabled(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in TRUE_VALUES


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, str(default)) or default))
    except Exception:
        return default


def emergency_opus_enabled() -> bool:
    return _env_enabled("CLAUDE_STACK_ENABLE_EMERGENCY_OPUS", True)


def is_opus_model(model: str) -> bool:
    return "opus" in str(model or "").strip().lower()


def _recent_allowed_count(window_s: int) -> int:
    if window_s <= 0 or not LOG_PATH.exists():
        return 0
    cutoff = time.time() - window_s
    count = 0
    try:
        for line in LOG_PATH.read_text(errors="ignore").splitlines()[-1000:]:
            try:
                entry = json.loads(line)
            except Exception:
                continue
            if entry.get("allowed") and float(entry.get("ts") or 0) >= cutoff:
                count += 1
    except Exception:
        return 0
    return count


def _decision_id(source: str, metadata: Dict[str, Any]) -> str:
    raw = "%s:%s:%s:%s" % (
        time.time(),
        os.getpid(),
        source,
        metadata.get("signature") or metadata.get("source_file") or "",
    )
    return hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()[:16]


def _compact_metadata(metadata: Dict[str, Any]) -> Dict[str, Any]:
    safe_keys = (
        "severity",
        "repair_scope",
        "log_file",
        "source_file",
        "signature",
        "reason",
        "prior_occurrences",
    )
    compact = {}
    for key in safe_keys:
        value = metadata.get(key)
        if value is not None:
            compact[key] = str(value)[:300]
    compact["emergency"] = bool(metadata.get("emergency"))
    return compact


def _record_decision(entry: Dict[str, Any]) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            handle.write(json.dumps(entry, sort_keys=True) + "\n")
    except Exception:
        pass


def evaluate_emergency_opus_request(
    model: str,
    source: str = "",
    metadata: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Return an allow/block decision for an Opus call."""

    metadata = dict(metadata or {})
    source_token = str(source or "").strip().lower()
    decision = {
        "id": _decision_id(source_token, metadata),
        "allowed": False,
        "model": str(model or ""),
        "source": source_token,
        "reason": "",
        "metadata": _compact_metadata(metadata),
    }

    if not is_opus_model(model):
        decision["reason"] = "not_opus"
        return decision
    if not emergency_opus_enabled():
        decision["reason"] = "emergency_opus_disabled"
        _record_decision({"ts": time.time(), **decision})
        return decision
    if source_token not in ALLOWED_SOURCES:
        decision["reason"] = "source_not_authorized_for_emergency_opus"
        _record_decision({"ts": time.time(), **decision})
        return decision
    if metadata.get("emergency") is not True:
        decision["reason"] = "missing_emergency_metadata"
        _record_decision({"ts": time.time(), **decision})
        return decision

    severity = str(metadata.get("severity") or "").strip().lower()
    scope = str(metadata.get("repair_scope") or "").strip().lower()
    if severity not in ALLOWED_SEVERITIES:
        decision["reason"] = "severity_not_authorized"
        _record_decision({"ts": time.time(), **decision})
        return decision
    if scope not in ALLOWED_SCOPES:
        decision["reason"] = "repair_scope_not_authorized"
        _record_decision({"ts": time.time(), **decision})
        return decision

    hourly_cap = _env_int("CLAUDE_STACK_EMERGENCY_OPUS_MAX_CALLS_PER_HOUR", 2)
    daily_cap = _env_int("CLAUDE_STACK_EMERGENCY_OPUS_MAX_CALLS_PER_DAY", 6)
    if hourly_cap and _recent_allowed_count(3600) >= hourly_cap:
        decision["reason"] = "hourly_cap_exceeded"
        _record_decision({"ts": time.time(), **decision})
        return decision
    if daily_cap and _recent_allowed_count(86400) >= daily_cap:
        decision["reason"] = "daily_cap_exceeded"
        _record_decision({"ts": time.time(), **decision})
        return decision

    decision["allowed"] = True
    decision["reason"] = "emergency_self_healer_stack_repair"
    _record_decision({"ts": time.time(), **decision})
    return decision
