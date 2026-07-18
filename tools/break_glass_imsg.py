#!/usr/bin/env python3
"""iMessage-triggered break-glass. Polls chat.db for a new inbound text from Operator
containing a trigger phrase ('break glass' or '911mini'); on match, runs break_glass.sh
and texts back the result. Tailscale-independent — only needs Messages to be delivering.
Only Operator's own handle can trigger it. Idempotent via a last-ROWID watermark."""
import os, re, subprocess, sys
from pathlib import Path

ROOT = Path.home() / "claude-stack"
sys.path.insert(0, str(ROOT))
from core.imessage_db import query_rows, latest_message_rowid  # noqa: E402

ALEX_PHONE = "XPHONEX"
TRIGGER = re.compile(r"(?i)\b(break\s*glass|911mini)\b")
STATE = ROOT / "data" / "runtime" / "break_glass_imsg_last_rowid"
LOG = ROOT / "logs" / "break_glass.log"
IMSG_SCRIPT = (
    'on run {targetPhone, targetMsg}\n'
    '  tell application "Messages"\n'
    '    set svc to 1st service whose service type = iMessage\n'
    '    send targetMsg to buddy targetPhone of svc\n'
    '  end tell\n'
    'end run'
)


def _log(msg):
    from datetime import datetime, timezone
    with open(LOG, "a") as f:
        f.write(f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} {msg}\n")


def _send(text):
    subprocess.run(["/usr/bin/osascript", "-e", IMSG_SCRIPT, ALEX_PHONE, text[:600]],
                   capture_output=True, text=True, timeout=25)


def main():
    STATE.parent.mkdir(parents=True, exist_ok=True)
    if not STATE.exists():                       # first run: arm at current tip, don't fire on history
        STATE.write_text(str(latest_message_rowid()))
        return
    last = int(STATE.read_text().strip() or "0")
    rows = query_rows(
        "SELECT m.ROWID, m.text FROM message m JOIN handle h ON m.handle_id=h.ROWID "
        f"WHERE m.is_from_me=0 AND h.id='{ALEX_PHONE}' AND m.ROWID>{last} "
        "ORDER BY m.ROWID ASC LIMIT 25"
    )
    if not rows:
        return
    newmax = max(int(r[0]) for r in rows)
    fired = any(r[1] and TRIGGER.search(r[1]) for r in rows)
    STATE.write_text(str(newmax))               # advance watermark regardless (don't re-scan)
    if not fired:
        return
    _log("=== iMessage break-glass trigger received ===")
    r = subprocess.run(["/bin/zsh", str(ROOT / "scripts" / "break_glass.sh")],
                       capture_output=True, text=True, timeout=180)
    summary = (r.stdout or "").strip() or "(no output)"
    _send("🚨 break-glass ran on the mini:\n" + summary)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # never let a poll crash-loop the agent
        _log(f"imsg-trigger error: {e}")
