"""Policy levers for agent/service external-call behavior."""

from __future__ import annotations

import json
import time
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict

from core import BASE_DIR

_POLICY_FILE = BASE_DIR / "data" / "policy_levers.json"
_MODES = {"allow_paid", "emergency_only", "local_only", "block_external"}

_AGENT_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "exec_assistant": {"label": "Exec Assistant", "mode": "allow_paid"},
    "diagnostician": {"label": "Diagnostician", "mode": "allow_paid"},
    "self_healer": {"label": "Self-Healer", "mode": "allow_paid"},
    "infra_intelligence": {"label": "Infra Intelligence", "mode": "allow_paid"},
    "team_router": {"label": "Team Router", "mode": "allow_paid"},
    "webui": {"label": "WebUI", "mode": "allow_paid"},
}

_SERVICE_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "anthropic_api": {"label": "Anthropic API", "mode": "allow_paid"},
    "salesforce_api": {"label": "Salesforce API", "mode": "allow_paid"},
    "salesforce_metadata_api": {"label": "Salesforce Metadata API", "mode": "allow_paid"},
    "monday_api": {"label": "Monday API", "mode": "allow_paid"},
    "microsoft_graph_api": {"label": "Microsoft Graph API", "mode": "allow_paid"},
    "microsoft_device_auth": {"label": "Microsoft Device Auth", "mode": "allow_paid"},
    "zoom_api": {"label": "Zoom API", "mode": "allow_paid"},
    "web_search": {"label": "Web Search", "mode": "allow_paid"},
    "google_drive_api": {"label": "Google Drive API", "mode": "allow_paid"},
    "telegram_api": {"label": "Telegram API", "mode": "allow_paid"},
    "travel_api": {"label": "Travel API", "mode": "allow_paid"},
}

_SERVICE_MATCHERS = [
    ("salesforce_metadata_api", ("metadata api",)),
    ("salesforce_api", ("salesforce api",)),
    ("monday_api", ("monday api", "monday credential probe")),
    ("microsoft_graph_api", ("microsoft graph api",)),
    ("microsoft_device_auth", ("microsoft device code auth",)),
    ("anthropic_api", ("anthropic api", "anthropic credential probe")),
    ("zoom_api", ("zoom api",)),
    ("web_search", ("web search", "url fetch")),
    ("google_drive_api", ("google drive api",)),
    ("telegram_api", ("telegram api", "telegram credential probe")),
    ("travel_api", ("travel api",)),
]


def _default_state() -> Dict[str, Any]:
    return {
        "updated_at": 0.0,
        "agents": deepcopy(_AGENT_DEFAULTS),
        "services": deepcopy(_SERVICE_DEFAULTS),
    }


def _normalize_mode(mode: Any) -> str:
    token = str(mode or "").strip().lower()
    return token if token in _MODES else "allow_paid"


def _load_saved() -> Dict[str, Any]:
    try:
        if _POLICY_FILE.exists():
            data = json.loads(_POLICY_FILE.read_text())
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def _save_state(state: Dict[str, Any]) -> None:
    _POLICY_FILE.parent.mkdir(parents=True, exist_ok=True)
    _POLICY_FILE.write_text(json.dumps(state, indent=2))


def get_policy_state() -> Dict[str, Any]:
    state = _default_state()
    saved = _load_saved()
    if isinstance(saved.get("updated_at"), (int, float)):
        state["updated_at"] = float(saved.get("updated_at") or 0.0)
    for bucket_name, defaults in (("agents", _AGENT_DEFAULTS), ("services", _SERVICE_DEFAULTS)):
        saved_bucket = saved.get(bucket_name)
        if not isinstance(saved_bucket, dict):
            continue
        for key, default in defaults.items():
            row = dict(default)
            candidate = saved_bucket.get(key)
            if isinstance(candidate, dict):
                row["mode"] = _normalize_mode(candidate.get("mode"))
            state[bucket_name][key] = row
    return state


def save_policy_updates(updates: Dict[str, Any]) -> Dict[str, Any]:
    state = get_policy_state()
    for bucket_name in ("agents", "services"):
        bucket_updates = updates.get(bucket_name)
        if not isinstance(bucket_updates, dict):
            continue
        for key, payload in bucket_updates.items():
            if key not in state[bucket_name]:
                continue
            if isinstance(payload, dict):
                state[bucket_name][key]["mode"] = _normalize_mode(payload.get("mode"))
            else:
                state[bucket_name][key]["mode"] = _normalize_mode(payload)
    state["updated_at"] = time.time()
    _save_state(state)
    return state


def classify_agent_source(source: str = "") -> str:
    token = str(source or "").strip().lower()
    if not token:
        return ""
    return token.split(":", 1)[0]


def classify_service_action(action: str = "") -> str:
    token = str(action or "").strip().lower()
    if not token:
        return ""
    for service_key, keywords in _SERVICE_MATCHERS:
        if any(keyword in token for keyword in keywords):
            return service_key
    return ""


def evaluate_external_request(action: str = "", source: str = "", emergency: bool = False) -> Dict[str, Any]:
    state = get_policy_state()
    agent_key = classify_agent_source(source)
    service_key = classify_service_action(action)
    agent_mode = state["agents"].get(agent_key, {}).get("mode", "allow_paid") if agent_key else "allow_paid"
    service_mode = state["services"].get(service_key, {}).get("mode", "allow_paid") if service_key else "allow_paid"

    effective_mode = "allow_paid"
    if "block_external" in {agent_mode, service_mode}:
        effective_mode = "block_external"
    elif "local_only" in {agent_mode, service_mode}:
        effective_mode = "local_only"
    elif "emergency_only" in {agent_mode, service_mode}:
        effective_mode = "emergency_only"

    allowed = effective_mode == "allow_paid" or (effective_mode == "emergency_only" and bool(emergency))
    reason = ""
    if not allowed:
        reasons = []
        if agent_key and agent_mode != "allow_paid":
            reasons.append(f"agent {agent_key} is set to {agent_mode}")
        if service_key and service_mode != "allow_paid":
            reasons.append(f"service {service_key} is set to {service_mode}")
        detail = " and ".join(reasons) if reasons else f"mode {effective_mode}"
        suffix = " (emergency metadata missing or rejected)" if effective_mode == "emergency_only" else ""
        reason = f"{action or 'External API call'} blocked by policy: {detail}{suffix}"

    return {
        "allowed": allowed,
        "effective_mode": effective_mode,
        "agent_key": agent_key,
        "agent_mode": agent_mode,
        "service_key": service_key,
        "service_mode": service_mode,
        "reason": reason,
        "emergency": bool(emergency),
        "state": state,
    }
