#!/usr/bin/env python3
"""Error Memory — the stack learns from every failure and never repeats the same mistake.

Every time an agent crashes, this module:
1. Captures the error signature (traceback hash + agent name)
2. Checks if we've seen this exact error before
3. If seen before: applies the KNOWN FIX automatically
4. If new: logs it, and the stack-engineer will generate a fix
5. Stores the fix so it's instant next time

Error patterns are stored in data/hustle/error_memory.json
Format: {
    "error_hash": {
        "agent": "outreach_cron",
        "error": "ModuleNotFoundError: No module named 'products'",
        "traceback": "...",
        "fix": "touch products/__init__.py products/outreach/__init__.py",
        "fix_type": "command",  # or "code_patch" or "config"
        "seen_count": 3,
        "first_seen": "2026-04-27T...",
        "last_seen": "2026-04-30T...",
        "auto_fixed": true
    }
}
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("error-memory")

BASE = Path.home() / "claude-stack"
MEMORY_FILE = BASE / "data" / "hustle" / "error_memory.json"

# Known error patterns and their auto-fixes
# These are hardcoded from lessons learned — the stack-engineer adds more dynamically
KNOWN_FIXES = {
    "ModuleNotFoundError: No module named": {
        "fix_type": "auto",
        "action": "_fix_missing_module",
    },
    "unsupported operand type(s) for |: 'type' and 'NoneType'": {
        "fix_type": "auto",
        "action": "_fix_future_annotations",
    },
    "Page.goto: Target page, context or browser has been closed": {
        "fix_type": "known",
        "note": "Browser crashed mid-session. Browser pool serialization prevents this.",
    },
    "Ollama queue is full": {
        "fix_type": "known",
        "note": "Too many concurrent LLM requests. Ollama queue handles backpressure.",
    },
    "HTTP Error 503": {
        "fix_type": "retry",
        "note": "Service temporarily unavailable. Agent will retry on next cycle.",
    },
    "SMTP not configured": {
        "fix_type": "config",
        "note": "Missing SMTP env vars. Check .env for OUTREACH_SMTP_* vars.",
    },
    "exit code 0": {
        "fix_type": "ignore",
        "note": "Clean exit. Not an error.",
    },
}


def _load_memory():
    if MEMORY_FILE.exists():
        try:
            return json.loads(MEMORY_FILE.read_text())
        except Exception:
            pass
    return {}


def _save_memory(memory):
    MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    MEMORY_FILE.write_text(json.dumps(memory, indent=2))


def _error_hash(agent, error_msg):
    """Create a stable hash for an error signature."""
    # Normalize: strip line numbers, paths, timestamps
    normalized = re.sub(r"line \d+", "line N", error_msg)
    normalized = re.sub(r"/Users/\S+/", "/PATH/", normalized)
    normalized = re.sub(r"\d{4}-\d{2}-\d{2}", "DATE", normalized)
    key = f"{agent}:{normalized[:200]}"
    return hashlib.md5(key.encode()).hexdigest()[:12]


def _fix_missing_module(agent, error_msg):
    """Auto-fix missing module errors by creating __init__.py files."""
    match = re.search(r"No module named '(\S+)'", error_msg)
    if not match:
        return False
    module = match.group(1)
    parts = module.split(".")

    fixed = False
    current = BASE
    for part in parts:
        current = current / part
        init = current / "__init__.py"
        if current.is_dir() and not init.exists():
            init.touch()
            log.info("Created %s", init)
            fixed = True

    return fixed


def _fix_future_annotations(agent, error_msg):
    """Auto-fix Python 3.9 type annotation errors."""
    # Find the agent's source file
    agent_file = BASE / "agents" / f"{agent}.py"
    if not agent_file.exists():
        agent_file = BASE / "core" / f"{agent}.py"
    if not agent_file.exists():
        return False

    content = agent_file.read_text()
    if "from __future__ import annotations" in content:
        return False

    # Add after shebang line
    lines = content.split("\n")
    insert_at = 1 if lines[0].startswith("#!") else 0
    lines.insert(insert_at, "from __future__ import annotations")
    agent_file.write_text("\n".join(lines))
    log.info("Added future annotations to %s", agent_file)
    return True


def record_error(agent, error_msg, traceback_text=""):
    """Record an error and attempt auto-fix if we know how."""
    memory = _load_memory()
    ehash = _error_hash(agent, error_msg)
    now = datetime.now(timezone.utc).isoformat()

    # Check if we've seen this before
    if ehash in memory:
        entry = memory[ehash]
        entry["seen_count"] = entry.get("seen_count", 0) + 1
        entry["last_seen"] = now
        log.info("KNOWN error (#%d): %s — %s", entry["seen_count"], agent, error_msg[:80])

        # If we have a fix, apply it
        if entry.get("auto_fixed"):
            log.info("Auto-fix already applied for this error")
            _save_memory(memory)
            return entry

    else:
        # New error
        entry = {
            "agent": agent,
            "error": error_msg[:500],
            "traceback": traceback_text[:2000],
            "seen_count": 1,
            "first_seen": now,
            "last_seen": now,
            "auto_fixed": False,
            "fix": None,
        }
        log.warning("NEW error: %s — %s", agent, error_msg[:80])

    # Try to auto-fix using known patterns
    for pattern, fix_info in KNOWN_FIXES.items():
        if pattern in error_msg:
            entry["fix_type"] = fix_info["fix_type"]

            if fix_info["fix_type"] == "auto":
                action = fix_info["action"]
                if action == "_fix_missing_module":
                    fixed = _fix_missing_module(agent, error_msg)
                elif action == "_fix_future_annotations":
                    fixed = _fix_future_annotations(agent, error_msg)
                else:
                    fixed = False

                if fixed:
                    entry["auto_fixed"] = True
                    entry["fix"] = f"Auto-applied: {action}"
                    log.info("AUTO-FIXED: %s for %s", action, agent)

            elif fix_info["fix_type"] == "known":
                entry["fix"] = fix_info.get("note", "Known issue")

            elif fix_info["fix_type"] == "ignore":
                entry["fix"] = "Not a real error"

            break

    memory[ehash] = entry
    _save_memory(memory)
    return entry


def get_unfixed_errors():
    """Return errors that haven't been auto-fixed yet."""
    memory = _load_memory()
    return {k: v for k, v in memory.items()
            if not v.get("auto_fixed") and v.get("fix_type") != "ignore"
            and v.get("seen_count", 0) >= 2}


def get_error_stats():
    """Return summary stats about error patterns."""
    memory = _load_memory()
    total = len(memory)
    auto_fixed = sum(1 for v in memory.values() if v.get("auto_fixed"))
    recurring = sum(1 for v in memory.values() if v.get("seen_count", 0) >= 3)
    return {
        "total_patterns": total,
        "auto_fixed": auto_fixed,
        "recurring_unfixed": recurring,
        "top_offenders": sorted(
            [(v.get("agent", "?"), v.get("error", "?")[:60], v.get("seen_count", 0))
             for v in memory.values()],
            key=lambda x: -x[2],
        )[:10],
    }
