"""Cost-rate guardrails — detect API burn spikes and enforce "cheap retry first".

Two entry points:

  check_api_spike(window_s=120) — scan cost.jsonl for the last window_s
    seconds, compute calls-per-minute per model, return a list of spikes
    that exceed per-model thresholds. Used by self_healer to alert Operator.

  source_escalation_streak(source) — track per-source escalation counts
    in memory with a 60-second sliding window. Used by llm_router to
    force one extra local retry before paying API rates when the same
    caller has been bleeding recently.

No paid-API calls happen in this module. Pure file IO + in-memory counters.
"""

from __future__ import annotations

import json
import logging
import time
from collections import defaultdict, deque
from pathlib import Path

from core import BASE_DIR

log = logging.getLogger(__name__)

COST_LOG = BASE_DIR / "data" / "logs" / "cost.jsonl"

# Per-model sustained call-rate thresholds (calls/minute). If any model
# exceeds its threshold across the full window, it's a spike.
SPIKE_THRESHOLDS_PER_MIN = {
    "claude-haiku":  5.0,
    "claude-sonnet": 3.0,
    "claude-opus":   1.5,
}


def _model_family(model_id: str) -> str:
    if not model_id:
        return "other"
    m = model_id.lower()
    for fam in SPIKE_THRESHOLDS_PER_MIN:
        if fam in m:
            return fam
    return "other"


def check_api_spike(window_s: int = 120) -> list[dict]:
    """Read cost.jsonl tail; return per-family spikes (rate > threshold).

    Each entry: {family, calls, window_s, rate_per_min, threshold_per_min,
                 total_cost_usd, last_ts}.
    """
    if not COST_LOG.exists():
        return []

    cutoff = time.time() - window_s
    by_family = defaultdict(lambda: {"n": 0, "cost": 0.0, "last_ts": 0})

    # Read last ~500 lines — enough for a 2-minute window even at extreme rates
    try:
        lines = COST_LOG.read_text().splitlines()[-500:]
    except Exception as e:
        log.warning(f"cost_guard: failed to read cost.jsonl: {e}")
        return []

    for line in lines:
        try:
            r = json.loads(line)
        except Exception:
            continue
        ts_str = r.get("ts", "")
        try:
            if isinstance(ts_str, str):
                # ISO format with tz
                from datetime import datetime
                ts = datetime.fromisoformat(ts_str).timestamp()
            else:
                ts = float(ts_str)
        except Exception:
            continue
        if ts < cutoff:
            continue
        fam = _model_family(r.get("model", ""))
        by_family[fam]["n"] += 1
        by_family[fam]["cost"] += r.get("cost", 0.0)
        if ts > by_family[fam]["last_ts"]:
            by_family[fam]["last_ts"] = ts

    spikes = []
    for fam, v in by_family.items():
        thr = SPIKE_THRESHOLDS_PER_MIN.get(fam)
        if thr is None:
            continue
        rate_per_min = v["n"] * 60.0 / window_s
        if rate_per_min > thr:
            spikes.append({
                "family": fam,
                "calls": v["n"],
                "window_s": window_s,
                "rate_per_min": round(rate_per_min, 2),
                "threshold_per_min": thr,
                "total_cost_usd": round(v["cost"], 4),
                "last_ts": v["last_ts"],
            })
    return spikes


# -------- Per-source in-process escalation streak ---------------------------

# {source -> deque of escalation timestamps in last 60s}
_escalation_log = defaultdict(lambda: deque(maxlen=20))
STREAK_WINDOW_S = 60
STREAK_THRESHOLD = 3   # 4th escalation in a minute triggers "slow down" signal


def note_escalation(source: str):
    """Called by llm_router whenever try_local_then_api had to escalate to api_fn.
    Prunes old entries, records a fresh timestamp."""
    if not source:
        return
    now = time.time()
    cutoff = now - STREAK_WINDOW_S
    dq = _escalation_log[source]
    while dq and dq[0] < cutoff:
        dq.popleft()
    dq.append(now)


def source_escalation_streak(source: str) -> int:
    """How many times this source has escalated in the last STREAK_WINDOW_S."""
    if not source:
        return 0
    now = time.time()
    cutoff = now - STREAK_WINDOW_S
    dq = _escalation_log.get(source)
    if not dq:
        return 0
    while dq and dq[0] < cutoff:
        dq.popleft()
    return len(dq)


def should_retry_local_first(source: str) -> bool:
    """Return True if this source has been escalating aggressively — caller
    should attempt ONE more local retry (ideally on a different tier/prompt
    shape) before paying API rates."""
    return source_escalation_streak(source) >= STREAK_THRESHOLD
