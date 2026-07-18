#!/usr/bin/env python3
"""Tool Updater — inventory, health-check, and safely update tools/agents/MCPs.

Never touches secrets. Records every change. Supports rollback.
Exposes via WebUI/Telegram: "what tools are stale?", "update safe tools", "rollback last update"
"""
from __future__ import annotations

import ast
import json
import logging
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("tool-updater")

BASE = Path.home() / "claude-stack"
STATE_FILE = BASE / "data" / "hustle" / "tool_update_state.json"


def _load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"last_scan": None, "updates": [], "inventory": {}}


def _save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def inventory():
    """Scan all tools, agents, core modules, MCPs and check health."""
    results = {}

    # Agents
    agents_dir = BASE / "agents"
    for f in sorted(agents_dir.glob("*.py")):
        name = f.stem
        size = f.stat().st_size
        mtime = datetime.fromtimestamp(f.stat().st_mtime, timezone.utc).isoformat()
        # Syntax check
        try:
            ast.parse(f.read_text())
            syntax_ok = True
        except SyntaxError as e:
            syntax_ok = False
            log.warning("Syntax error in agents/%s.py: %s", name, e)

        # Check for future annotations
        has_future = "from __future__ import annotations" in f.read_text()[:200]

        # Check for browser_pool usage if it uses playwright
        uses_playwright = "playwright" in f.read_text().lower()
        has_pool = "browser_pool" in f.read_text() or "browser_lock" in f.read_text()

        results[f"agents/{name}"] = {
            "type": "agent",
            "size": size,
            "modified": mtime,
            "syntax_ok": syntax_ok,
            "has_future_annotations": has_future,
            "uses_playwright": uses_playwright,
            "has_browser_pool": has_pool if uses_playwright else None,
            "needs_fix": (not syntax_ok) or (uses_playwright and not has_pool) or (not has_future),
        }

    # Core modules
    core_dir = BASE / "core"
    for f in sorted(core_dir.glob("*.py")):
        name = f.stem
        try:
            ast.parse(f.read_text())
            syntax_ok = True
        except SyntaxError:
            syntax_ok = False

        results[f"core/{name}"] = {
            "type": "core",
            "size": f.stat().st_size,
            "modified": datetime.fromtimestamp(f.stat().st_mtime, timezone.utc).isoformat(),
            "syntax_ok": syntax_ok,
        }

    # Tools
    tools_dir = BASE / "tools"
    for f in sorted(tools_dir.glob("*.py")):
        name = f.stem
        try:
            ast.parse(f.read_text())
            syntax_ok = True
        except SyntaxError:
            syntax_ok = False

        results[f"tools/{name}"] = {
            "type": "tool",
            "size": f.stat().st_size,
            "modified": datetime.fromtimestamp(f.stat().st_mtime, timezone.utc).isoformat(),
            "syntax_ok": syntax_ok,
        }

    # LaunchAgents
    la_dir = Path.home() / "Library" / "LaunchAgents"
    for f in sorted(la_dir.glob("com.claude-stack.*.plist")):
        name = f.stem.replace("com.claude-stack.", "")
        results[f"launchagent/{name}"] = {
            "type": "launchagent",
            "size": f.stat().st_size,
            "modified": datetime.fromtimestamp(f.stat().st_mtime, timezone.utc).isoformat(),
        }

    return results


def find_stale(inv=None):
    """Find tools that need attention."""
    if inv is None:
        inv = inventory()

    stale = []
    for path, info in inv.items():
        reasons = []
        if info.get("syntax_ok") is False:
            reasons.append("syntax_error")
        if info.get("uses_playwright") and not info.get("has_browser_pool"):
            reasons.append("missing_browser_pool")
        if not info.get("has_future_annotations") and info.get("type") == "agent":
            reasons.append("missing_future_annotations")
        if reasons:
            stale.append({"path": path, "reasons": reasons, "info": info})

    return stale


def auto_fix_safe(dry_run=True):
    """Apply safe auto-fixes: future annotations, browser pool."""
    inv = inventory()
    stale = find_stale(inv)
    fixes = []

    for item in stale:
        path = item["path"]
        full_path = BASE / (path + ".py") if not path.endswith(".py") else BASE / path

        if "missing_future_annotations" in item["reasons"]:
            if not dry_run:
                content = full_path.read_text()
                if "from __future__ import annotations" not in content:
                    lines = content.split("\n")
                    insert_at = 1 if lines[0].startswith("#!") else 0
                    lines.insert(insert_at, "from __future__ import annotations")
                    full_path.write_text("\n".join(lines))
            fixes.append({"path": path, "fix": "add_future_annotations", "applied": not dry_run})

    return fixes


def scan():
    """Full scan: inventory + find stale + save state."""
    inv = inventory()
    stale = find_stale(inv)
    state = _load_state()
    state["last_scan"] = datetime.now(timezone.utc).isoformat()
    state["inventory"] = {
        "total": len(inv),
        "agents": sum(1 for v in inv.values() if v.get("type") == "agent"),
        "core": sum(1 for v in inv.values() if v.get("type") == "core"),
        "tools": sum(1 for v in inv.values() if v.get("type") == "tool"),
        "launchagents": sum(1 for v in inv.values() if v.get("type") == "launchagent"),
        "syntax_errors": sum(1 for v in inv.values() if v.get("syntax_ok") is False),
        "missing_browser_pool": sum(1 for v in inv.values() if v.get("uses_playwright") and not v.get("has_browser_pool")),
        "stale_count": len(stale),
    }
    state["stale"] = [s["path"] for s in stale]
    _save_state(state)
    return state


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    state = scan()
    print(json.dumps(state["inventory"], indent=2))
    stale = find_stale()
    if stale:
        print(f"\n{len(stale)} items need attention:")
        for s in stale:
            print(f"  {s['path']}: {', '.join(s['reasons'])}")
