"""Detect recent operator activity so background local-model jobs can yield.

The goal is simple: if the Mini is actively being used, background LLM-heavy
jobs should avoid competing with interactive work. Remote-control sessions count
as active use as well.
"""

from __future__ import annotations

import os
import re
import subprocess
import time

from core.runtime_pressure import get_runtime_pressure
from core.ui_automation_guard import get_ui_automation_guard

_CACHE_TTL_S = 5
_cache_ts = 0.0
_cache_value: dict | None = None


def _run(args: list[str]) -> str:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=3)
    except Exception:
        return ""
    if result.returncode != 0:
        return ""
    return (result.stdout or "").strip()


def _active_idle_threshold_s() -> int:
    raw = os.environ.get("WORKOS_OPERATOR_ACTIVE_IDLE_S", "180").strip()
    try:
        return max(15, min(3600, int(raw)))
    except Exception:
        return 180


def _hid_idle_seconds() -> float | None:
    out = _run(["/usr/sbin/ioreg", "-c", "IOHIDSystem"])
    if not out:
        return None
    match = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', out)
    if not match:
        return None
    try:
        return int(match.group(1)) / 1_000_000_000.0
    except Exception:
        return None


def get_operator_activity(force: bool = False) -> dict:
    global _cache_ts, _cache_value

    now = time.time()
    if not force and _cache_value is not None and (now - _cache_ts) < _CACHE_TTL_S:
        return dict(_cache_value)

    remote = get_ui_automation_guard(force=force)
    idle_seconds = _hid_idle_seconds()
    threshold = _active_idle_threshold_s()

    active = bool(remote.get("remote_control_active"))
    reasons = []
    if active:
        reasons.append("remote control active")

    if bool(remote.get("claude_ui_active")):
        active = True
        reasons.append("Claude app UI automation active")

    if idle_seconds is not None and idle_seconds < threshold:
        active = True
        reasons.append(f"local operator active within {int(idle_seconds)}s")

    _cache_value = {
        "operator_active": active,
        "idle_seconds": idle_seconds,
        "idle_threshold_s": threshold,
        "reasons": reasons,
        "as_of": now,
    }
    _cache_ts = now
    return dict(_cache_value)


def background_llm_jobs_should_yield(force: bool = False) -> bool:
    return bool(get_operator_activity(force=force).get("operator_active"))


def background_llm_jobs_pause_reason(force: bool = False) -> str:
    reasons = get_operator_activity(force=force).get("reasons") or []
    return "; ".join(reasons) if reasons else "operator not active"


def background_jobs_should_defer(force: bool = False) -> bool:
    """Return True when heavyweight background work should defer.

    Heavy background work should yield both to a live operator and to hard
    runtime pressure so the Mini stays responsive under active use.
    """
    if bool(get_operator_activity(force=force).get("operator_active")):
        return True
    pressure = get_runtime_pressure(force=force)
    return str(pressure.get("mode") or "").lower() == "overloaded"


def background_jobs_defer_reason(force: bool = False) -> str:
    reasons = list(get_operator_activity(force=force).get("reasons") or [])
    pressure = get_runtime_pressure(force=force)
    if str(pressure.get("mode") or "").lower() == "overloaded":
        pressure_reasons = list(pressure.get("reasons") or [])
        if pressure_reasons:
            reasons.append("runtime overload: " + ", ".join(pressure_reasons))
        else:
            reasons.append("runtime overload")
    return "; ".join(reasons) if reasons else "background jobs allowed"


def wait_until_background_jobs_allowed(
    max_wait_s: int = 300,
    poll_s: int = 15,
    *,
    force: bool = False,
) -> dict:
    """Wait until heavyweight background jobs are allowed again.

    Returns the latest operator-activity snapshot, with `waited_s` included.
    """

    started = time.time()
    poll_interval = max(1, int(poll_s))
    while True:
        activity = get_operator_activity(force=True)
        if not activity.get("operator_active"):
            activity = dict(activity)
            activity["waited_s"] = round(time.time() - started, 1)
            activity["deferred"] = False
            return activity
        waited = time.time() - started
        if waited >= max(0, int(max_wait_s)):
            activity = dict(activity)
            activity["waited_s"] = round(waited, 1)
            activity["deferred"] = True
            return activity
        time.sleep(poll_interval)
