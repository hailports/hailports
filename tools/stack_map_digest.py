#!/usr/bin/env python3
"""Auto-generated STACK MAP digest for the claw brain's memorySearch.

Writes ~/.openclaw/workspace/CompanyA-local/digests/STACK_MAP.md (already in
memorySearch extraPaths) so the chat brain can always retrieve a fresh,
complete inventory of the stack: every agent, tool, launchd job, frontend,
port, and key data file. Re-run via launchd com.claude-stack.stack-map every 4h.
"""
import ast
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path

STACK = Path.home() / "claude-stack"
OUT = Path.home() / ".openclaw/workspace/CompanyA-local/digests/STACK_MAP.md"

PORT_NOTES = {
    "8330": "integration gateway (Outlook/Zoom/LittleBird/SF/Monday bridges)",
    "8890": "SearXNG local web search (docker: searxng)",
    "18801": "chrome CDP (persona1)", "18802": "chrome CDP (persona2)", "18803": "chrome CDP (x2)",
}


def first_doc_line(path: Path) -> str:
    try:
        tree = ast.parse(path.read_text(errors="ignore"))
        doc = ast.get_docstring(tree) or ""
        return doc.strip().splitlines()[0][:140] if doc.strip() else ""
    except Exception:
        return ""


def py_inventory(dirname: str) -> list[str]:
    lines = []
    d = STACK / dirname
    if not d.is_dir():
        return lines
    for f in sorted(d.glob("*.py")):
        if f.name.startswith("__"):
            continue
        desc = first_doc_line(f)
        lines.append(f"- `{dirname}/{f.name}`" + (f" — {desc}" if desc else ""))
    return lines


def launchd_jobs() -> list[str]:
    lines = []
    la = Path.home() / "Library/LaunchAgents"
    for plist in sorted(la.glob("com.*.plist")):
        if not re.match(r"com\.(claude-stack|imma|persona3)\.", plist.name):
            continue
        txt = plist.read_text(errors="ignore")
        prog = "?"
        m = re.search(r"<array>\s*<string>([^<]+)</string>\s*<string>([^<]*)</string>", txt)
        if m:
            prog = " ".join(p for p in m.groups() if p)
        interval = ""
        mi = re.search(r"StartInterval</key>\s*<integer>(\d+)</integer>", txt)
        if mi:
            interval = f" every {int(mi.group(1)) // 60 or 1}{'min' if int(mi.group(1)) >= 60 else 's'}"
        loaded = subprocess.run(["launchctl", "list", plist.stem],
                                capture_output=True).returncode == 0
        lines.append(f"- `{plist.stem}`{interval} — {prog}"
                     + ("" if loaded else " **(NOT LOADED)**"))
    return lines


def main():
    sections = [
        f"# STACK MAP — full live inventory (auto-generated {datetime.now():%Y-%m-%d %H:%M})",
        "",
        "You ARE this stack. Everything below is yours to read, run, and edit.",
        "Source root: `~/claude-stack/`. Regenerate: `python3 tools/stack_map_digest.py`.",
        "",
        "## Agents (`agents/`) — revenue / social / outreach automations",
        *py_inventory("agents"),
        "",
        "## Core (`core/`) — engine, routing, quality gates, health",
        *py_inventory("core"),
        "",
        "## Tools (`tools/`) — operator utilities & bridges",
        *py_inventory("tools"),
        "",
        "## Frontends (`frontends/`) — chat surfaces (iMessage, telegram, web)",
        *py_inventory("frontends"),
        "",
        "## Scripts (`scripts/`)",
        *py_inventory("scripts"),
        "",
        "## Launchd jobs (live schedule)",
        *launchd_jobs(),
        "",
        "## Ports",
        *[f"- `127.0.0.1:{p}` — {n}" for p, n in PORT_NOTES.items()],
        "",
        "## Key data",
        "- `data/hustle/` — intent leads, strike lists, persona3 queue/friends, growth logs",
        "- `data/hustle/persona3_friends.txt` — friend handles (always-FYN, never judged)",
        "- `products/gumroad_ready/_packaged/` — staged Gumroad zips",
        "- `~/.openclaw/pin-stable-config.py` — THE config (openclaw.json is read-only+reverted)",
        "- `~/AGENTS.md` — your in-window self-model (first 5000 chars injected)",
        "- `~/.openclaw/workspace/CompanyA-local/digests/RECENT_CHAT_CONTEXT.md` — sanitized rolling recent chat context",
        "- `~/.openclaw/workspace/CompanyA-local/digests/REPORTING_INDEX.md` — direct links/paths for cost, savings, ROI, automation, social, and revenue reporting",
        "- `tools/openclaw_session_guard.py` — quarantines NUL/oversized poisoned OpenClaw session files",
    ]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(sections) + "\n")
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
