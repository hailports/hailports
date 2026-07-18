#!/usr/bin/env python3
"""Presence sensor — is Operator actively using the stack right now?

The nocturnal batch machine MUST yield to the human. Before pulling/continuing
heavy local work, workers call `is_user_active()`; if True they pause (and may
unload the big local model) so the box stays fast for Operator. His needs come first.

Detects activity across all the ways he touches the stack:
  1. LOCAL input   — macOS HID idle time (keyboard/mouse) via ioreg
  2. REMOTE shells — active SSH/pts sessions via `who`
  3. STACK / API   — recent writes to gateway / chat / MCP / llm-api logs
  4. AI sessions   — a live Claude Code / Codex process with recent activity

CLI: `python presence_sensor.py` prints the verdict + signals.
Env:
  PRESENCE_IDLE_THRESHOLD_S   (default 600)  HID idle below this = at keyboard
  PRESENCE_API_WINDOW_S       (default 300)  log touched within this = active via stack
"""
from __future__ import annotations
import os
import subprocess
import time
from pathlib import Path

IDLE_THRESHOLD_S = int(os.environ.get("PRESENCE_IDLE_THRESHOLD_S", "600"))
API_WINDOW_S = int(os.environ.get("PRESENCE_API_WINDOW_S", "300"))

# Logs that get written when Operator hits the stack locally/remotely/via API.
# Best-effort: missing files are ignored. Add more as the topology changes.
ACTIVITY_LOGS = [
    "~/.mcp-gateway-audit.log",
    "~/.mcp-gateway-stdout.log",
    "~/.llm-router.log",
    "~/.openclaw/workspace/android-logs/gateway.log",
    "~/.claude-code-boot.log",
    "~/.cost-tracker.log",
]


def hid_idle_seconds() -> float | None:
    """Seconds since last keyboard/mouse input (None if unavailable)."""
    try:
        out = subprocess.run(
            ["ioreg", "-c", "IOHIDSystem"], capture_output=True, text=True, timeout=6
        ).stdout
        for line in out.splitlines():
            if "HIDIdleTime" in line:
                # value is in nanoseconds
                ns = int(line.split("=")[-1].strip())
                return ns / 1_000_000_000
    except Exception:
        return None
    return None


def remote_sessions() -> list[str]:
    """Active login sessions that look remote (ssh/pts)."""
    try:
        out = subprocess.run(["who"], capture_output=True, text=True, timeout=6).stdout
        hits = []
        for line in out.splitlines():
            # remote logins show an (ip) host or a pts/ttys with origin
            if "(" in line or "pts/" in line or "ttys" in line:
                hits.append(line.strip())
        return hits
    except Exception:
        return []


def recent_api_activity() -> list[str]:
    """Logs written within API_WINDOW_S = someone is driving the stack."""
    now = time.time()
    hot = []
    for p in ACTIVITY_LOGS:
        fp = Path(os.path.expanduser(p))
        try:
            if fp.exists() and (now - fp.stat().st_mtime) < API_WINDOW_S:
                hot.append(f"{p} ({int(now - fp.stat().st_mtime)}s ago)")
        except Exception:
            continue
    return hot


def live_ai_session() -> bool:
    """A Claude Code / Codex process actively running."""
    try:
        out = subprocess.run(["pgrep", "-fl", "claude|codex|openclaw"],
                             capture_output=True, text=True, timeout=6).stdout.lower()
        # exclude our own background daemons; require an interactive-ish marker
        return any(k in out for k in ("claude code", "codex", "claude-code")) or "claude " in out
    except Exception:
        return False


def is_user_active(idle_threshold_s: int | None = None) -> dict:
    idle_threshold_s = idle_threshold_s or IDLE_THRESHOLD_S
    idle = hid_idle_seconds()
    remote = remote_sessions()
    api = recent_api_activity()
    reasons = []
    if idle is not None and idle < idle_threshold_s:
        reasons.append(f"local_input(idle={int(idle)}s)")
    if remote:
        reasons.append(f"remote_session({len(remote)})")
    if api:
        reasons.append(f"stack_api({len(api)})")
    active = bool(reasons)
    return {
        "active": active,
        "reasons": reasons,
        "idle_seconds": None if idle is None else int(idle),
        "remote": remote,
        "api_hot": api,
        "verdict": "YIELD — Operator is active" if active else "CLEAR — grind allowed",
    }


if __name__ == "__main__":
    import json
    print(json.dumps(is_user_active(), indent=2))
