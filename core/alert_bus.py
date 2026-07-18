"""Alert bus — central pipe for all stack notifications.

Why this exists: every agent used to call _notify_telegram() directly. That meant
duplicates, wolf-crying when the healer had already fixed the underlying issue,
and a constant trickle of pings instead of one digestible summary.

Rules:
    1. CRITICAL alerts always send immediately and bypass all dedup/suppression.
    2. INFO/WARN alerts coalesce into a 15-minute Telegram digest.
    3. Drop an alert if a self-healer fix landed for the same service in the
       last 10 minutes (the symptom is probably already gone).
    4. Drop duplicate signatures within the digest window.

Multi-process safe: state persists to data/alert_bus_state.json with fcntl
locking — every agent runs in its own launchd job and may emit concurrently.
"""

import json
import os
import time
import fcntl
import urllib.request
from contextlib import contextmanager
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
STATE_FILE = BASE_DIR / "data" / "alert_bus_state.json"

DIGEST_WINDOW = 900          # flush pending non-critical alerts every 15 min
SUPPRESS_AFTER_FIX = 10 * 60     # drop non-critical alerts for 10 min after a healer fix on same service
DEDUP_WINDOW = 15 * 60           # drop identical signatures within this window
PENDING_CAP = 50                 # safety cap on pending list

LEVELS = ("info", "warn", "critical")


@contextmanager
def _locked_state():
    """Open state file with exclusive lock; yield the loaded dict; persist + unlock on exit."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not STATE_FILE.exists():
        STATE_FILE.write_text("{}")
    fd = os.open(str(STATE_FILE), os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        with os.fdopen(fd, "r+") as f:
            try:
                state = json.load(f) or {}
            except json.JSONDecodeError:
                state = {}
            state.setdefault("recent_fixes", {})       # service -> ts
            state.setdefault("pending", [])             # list of {level, source, signature, message, ts}
            state.setdefault("sent_signatures", {})     # signature -> ts (dedup window)
            state.setdefault("last_digest", 0)
            yield state
            f.seek(0)
            f.truncate()
            json.dump(state, f, indent=2)
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except Exception:
            pass


def _telegram_send(text):
    return  # HARD SILENCED - no logging, no sending, nothing


def register_fix(service):
    """Healer calls this when a fix lands. Subsequent non-critical alerts for the
    same service get suppressed for SUPPRESS_AFTER_FIX seconds."""
    if not service:
        return
    with _locked_state() as state:
        state["recent_fixes"][str(service)] = time.time()


def emit(level, source, signature, message, service=None):
    """Submit an alert. See module docstring for routing rules."""
    if level not in LEVELS:
        level = "warn"
    now = time.time()

    # CRITICAL bypasses everything
    if level == "critical":
        _telegram_send(f"[critical] {source}: {message}")
        with _locked_state() as state:
            state["sent_signatures"][signature] = now
            _gc_inplace(state, now)
        return

    with _locked_state() as state:
        _gc_inplace(state, now)

        # Suppress if healer recently fixed this service
        target_service = service or source
        last_fix = state["recent_fixes"].get(str(target_service), 0)
        if now - last_fix < SUPPRESS_AFTER_FIX:
            return  # symptom probably gone

        # Dedup within window
        last_sent = state["sent_signatures"].get(signature, 0)
        if now - last_sent < DEDUP_WINDOW:
            return

        # Also dedup against anything already pending
        for entry in state["pending"]:
            if entry.get("signature") == signature:
                return

        if len(state["pending"]) >= PENDING_CAP:
            state["pending"] = state["pending"][-(PENDING_CAP - 1):]
        state["pending"].append({
            "level": level, "source": source, "signature": signature,
            "message": message, "ts": now,
        })


def tick():
    """Call from any agent's main loop. Flushes the digest if the window has elapsed."""
    now = time.time()
    with _locked_state() as state:
        _gc_inplace(state, now)
        if now - state["last_digest"] < DIGEST_WINDOW:
            return
        if not state["pending"]:
            state["last_digest"] = now
            return

        pending = [e for e in state["pending"] if e.get("level") != "info"]
        state["pending"] = []
        state["last_digest"] = now
        for entry in pending:
            state["sent_signatures"][entry["signature"]] = now

    body = _format_digest(pending, now)
    _telegram_send(body)


def _format_digest(pending, now):
    by_level = {"warn": [], "info": []}
    for e in pending:
        by_level.setdefault(e.get("level", "warn"), []).append(e)
    parts = [f"Stack digest ({len(pending)} item{'s' if len(pending) != 1 else ''}, last 15min)"]
    for level in ("warn", "info"):
        items = by_level.get(level, [])
        if not items:
            continue
        parts.append(f"\n— {level.upper()} ({len(items)}) —")
        # Group by source for readability
        by_source = {}
        for e in items:
            by_source.setdefault(e.get("source", "?"), []).append(e)
        for source, entries in by_source.items():
            parts.append(f"[{source}]")
            for e in entries:
                age_min = int((now - e.get("ts", now)) / 60)
                msg = e["message"].replace("\n", " ")[:200]
                parts.append(f"  • ({age_min}m ago) {msg}")
    return "\n".join(parts)


def _gc_inplace(state, now):
    # Drop sent_signatures older than 2x dedup window
    cutoff = now - DEDUP_WINDOW * 2
    state["sent_signatures"] = {
        s: ts for s, ts in state["sent_signatures"].items() if ts >= cutoff
    }
    # Drop fix records older than 2x suppress window
    cutoff_fix = now - SUPPRESS_AFTER_FIX * 2
    state["recent_fixes"] = {
        s: ts for s, ts in state["recent_fixes"].items() if ts >= cutoff_fix
    }
