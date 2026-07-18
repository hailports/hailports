#!/usr/bin/env python3
"""live_health.py — ground-truth "what's actually true right now" snapshot.

The healer digest historically reported what self_healer.py *attempted* (fixes tried,
escalated) — not what is actually live. That's why mornings felt like surprises: the
report and reality disagreed. This module reports reality, and heals the common case
(a genuinely dead launchd job) BEFORE reporting, so the digest reads as
"already handled" instead of "here's a fresh fire."

Deterministic, no LLM, bounded. "Dead" matches tools/stack_heal.py exactly
(PID '-' AND nonzero last-exit) — one definition, no second opinion.

  snapshot()      -> dict of live facts (+ auto-revives dead jobs first)
  format_block()  -> plain-text block to prepend to the morning digest
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
VENV_PY = BASE_DIR / ".venv" / "bin" / "python"
STACK_HEAL = BASE_DIR / "tools" / "stack_heal.py"

# jobs whose death directly costs money — surfaced first if any are down.
MONEY_LABELS = (
    "com.claude-stack.stripe-fulfillment",
    "com.claude-stack.self-serve",
    "com.claude-stack.storefront",
    "com.claude-stack.ai-closer",
)
DISK_WARN_PCT = 88   # yellow
DISK_CRIT_PCT = 93   # red
WEBUI_ERR_LOG = BASE_DIR / "data" / "logs" / "webui-backend-a.err.log"


def _py() -> str:
    return str(VENV_PY) if VENV_PY.exists() else "python3"


def _run(args, timeout=30) -> str:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


def _dead_jobs() -> list[str]:
    """Ask stack_heal for the authoritative dead set (single source of truth)."""
    out = _run([_py(), str(STACK_HEAL), "count-dead"], timeout=25)
    # stack_heal prints "N dead: a, b, c" or "0 dead"
    for ln in out.splitlines():
        ln = ln.strip()
        if ln.endswith("dead") and ln.split()[0] == "0":
            return []
        if " dead:" in ln:
            return [j.strip() for j in ln.split("dead:", 1)[1].split(",") if j.strip()]
    return []


def _revive_dead() -> str:
    """Heal-first: revive everything genuinely dead. Returns stack_heal's line."""
    return _run([_py(), str(STACK_HEAL), "revive-dead"], timeout=90).strip()


def _disk() -> tuple[int, str]:
    """Usage of the writable Data volume (/ is a sealed system snapshot on APFS,
    so it reads a misleading ~28% — the real pressure is on /System/Volumes/Data)."""
    import re
    out = _run(["/bin/df", "-h", "/System/Volumes/Data"], timeout=10) \
        or _run(["/bin/df", "-h", "/"], timeout=10)
    for ln in out.splitlines()[1:]:
        parts = ln.split()
        # find the Capacity column (first bare "NN%" token); Avail is the token before it.
        for i, tok in enumerate(parts):
            if re.fullmatch(r"\d+%", tok):
                free = parts[i - 1] if i > 0 else "?"
                return int(tok.rstrip("%")), free
    return 0, "?"


def _anthropic_401_recent(hours=26) -> bool:
    """True if webui hit a 401 from Anthropic in the window — a dead/expired key."""
    try:
        if not WEBUI_ERR_LOG.exists():
            return False
        cutoff = time.time() - hours * 3600
        if WEBUI_ERR_LOG.stat().st_mtime < cutoff:
            return False
        tail = _run(["/usr/bin/tail", "-n", "300", str(WEBUI_ERR_LOG)], timeout=10)
        return "api.anthropic.com" in tail and "401 Unauthorized" in tail
    except Exception:
        return False


def snapshot(heal_first: bool = True) -> dict:
    revive_line = _revive_dead() if heal_first else ""
    dead = _dead_jobs()  # AFTER the revive attempt — what's still down
    disk_pct, disk_free = _disk()
    money_down = [d for d in dead if d in MONEY_LABELS]
    return {
        "revive_line": revive_line,
        "dead": dead,
        "money_down": money_down,
        "disk_pct": disk_pct,
        "disk_free": disk_free,
        "anthropic_key_401": _anthropic_401_recent(),
    }


def format_block(snap: dict | None = None) -> str:
    s = snap or snapshot()
    L = ["LIVE STATE (verified now):"]

    # what heal-first just did
    rev = s.get("revive_line", "")
    if rev and "nothing dead" not in rev.lower():
        L.append(f"  auto-revive: {rev}")

    dead = s.get("dead", [])
    money_down = s.get("money_down", [])
    if not dead:
        L.append("  ✓ 0 jobs down (all clear)")
    else:
        if money_down:
            L.append(f"  🔴 MONEY JOBS DOWN ({len(money_down)}): "
                     + ", ".join(d.replace('com.claude-stack.', '') for d in money_down))
        others = [d for d in dead if d not in money_down]
        if others:
            L.append(f"  ⚠ still down after revive ({len(others)}): "
                     + ", ".join(d.replace('com.claude-stack.', '') for d in others[:8]))

    pct = s.get("disk_pct", 0)
    tag = "🔴" if pct >= DISK_CRIT_PCT else ("⚠" if pct >= DISK_WARN_PCT else "✓")
    L.append(f"  {tag} disk {pct}% used ({s.get('disk_free','?')} free)")

    if s.get("anthropic_key_401"):
        L.append("  ⚠ Anthropic API key 401 — paid calls degraded to local ollama "
                 "(rotate ANTHROPIC_API_KEY)")

    return "\n".join(L)


def is_clean(snap: dict) -> bool:
    """True iff nothing needs Operator's eyes — gates the 'all clear' digest path."""
    return (not snap.get("dead")
            and snap.get("disk_pct", 0) < DISK_WARN_PCT
            and not snap.get("anthropic_key_401"))


if __name__ == "__main__":
    import sys
    heal = "--no-heal" not in sys.argv
    print(format_block(snapshot(heal_first=heal)))
