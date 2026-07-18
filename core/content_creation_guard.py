#!/usr/bin/env python3
"""Shared guard for pausing local content creation jobs.

The pause is intentionally data-driven so launchd jobs, manual runs, and
coordinator dispatches all honor the same switch.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

BASE = Path(os.path.expanduser("~/claude-stack"))
PAUSE_FILE = BASE / "data" / "hustle" / "local_content_creation_paused.json"


def _read_json(path: Path, fallback: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text())
    except Exception:
        pass
    return fallback


def pause_state() -> dict[str, Any]:
    data = _read_json(PAUSE_FILE, {})
    return data if isinstance(data, dict) else {}


def content_creation_paused() -> bool:
    override = os.environ.get("LOCAL_CONTENT_CREATION_ENABLED", "").strip().lower()
    if override in {"1", "true", "yes", "on", "enabled"}:
        return False
    return bool(pause_state().get("enabled"))


def pause_reason() -> str:
    state = pause_state()
    reason = str(state.get("reason") or "").strip()
    return reason or "local content creation is paused"


def local_content_block_reason(agent_name: str = "") -> str | None:
    if not content_creation_paused():
        return None
    state = pause_state()
    replacement = str(state.get("replacement_system") or "").strip()
    detail = pause_reason()
    if replacement:
        detail = f"{detail}; replacement: {replacement}"
    if agent_name:
        return f"{agent_name} skipped because {detail}"
    return detail


def exit_if_paused(agent_name: str, logger: Any = None) -> bool:
    reason = local_content_block_reason(agent_name)
    if not reason:
        return False
    if logger:
        logger.warning(reason)
    else:
        print(reason)
    return True
