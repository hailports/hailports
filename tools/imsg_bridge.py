#!/usr/bin/env python3.12
"""
imsg_bridge.py — dedicated-Apple-ID iMessage command bridge (no LLM, no cloud, $0).

Watches the mini's Messages DB for new texts from Operator (XPHONEX), maps them to
keyword commands, runs the Outlook tools, and texts back a preview. Draft-only unless
the text says "send". Invoked on an interval by launchd (no long-running loop).
"""
import os, re, json, glob, sqlite3, subprocess, html

PY = "/opt/homebrew/bin/python3.12"
TOOLS = "/home/user/claude-stack/tools"
FOLLOWUP = TOOLS + "/outlook_followup.py"
TRIP_RECEIPTS = TOOLS + "/trip_receipts.py"
FINDTIMES = TOOLS + "/find_times.py"
PACK = "/home/user/claude-stack/packs/dpp_followups.json"
PACKS_DIR = "/home/user/claude-stack/packs"
BURN_GUARD = "/home/user/claude-stack/core/burn_rate_guard.py"
DB = os.path.expanduser("~/Library/Messages/chat.db")
STATE = os.path.expanduser("~/.imsg_bridge_state.json")
TARGET = "XPHONEX"          # only this handle is honored
HELP = ("Stack commands: 'followups dpp' | 'preview' | 'times next week' | 'send it'\n"
        "Burn guard: 'burn reset' / 'false alarm' / 'no charges'")

def _run(args):
    try:
        r = subprocess.run([PY] + args, capture_output=True, text=True, timeout=120)
        return (r.stdout or r.stderr or "").strip()
    except Exception as e:
        return "error: " + str(e)

def _burn_reset():
    try:
        r = subprocess.run([PY, BURN_GUARD, "--reset"], capture_output=True, text=True, timeout=30)
        return r.stdout.strip() or r.stderr.strip() or "Burn guard reset. Jobs reloaded."
    except Exception as e:
        return f"Reset failed: {e}"


def route(text):
    t = (text or "").lower().strip()
    if not t:
        return None

    # Burn rate guard — explicit reset triggers (false alarm / no charges / manual reset)
    # These must come before any other routing since burn kills jobs and user needs fast recovery.
    BURN_RESET_TRIGGERS = ("burn reset", "false alarm", "no charges", "no new charges",
                           "not charging", "no charge", "reset burn", "reset guard",
                           "no openrouter", "openrouter fine", "router fine")
    if any(k in t for k in BURN_RESET_TRIGGERS):
        return _burn_reset()

    # Trip receipts — 'receipts' / 'receipts omaha' -> itemized summary; add 'email'/'stage' to
    # write the digest + stage an Outlook draft-to-self (draft only; Operator forwards to Concur).
    if "receipt" in t:
        if "pdf" in t or "attach" in t:  # render a folder of attachable PDFs
            return _run([TRIP_RECEIPTS, "--pdf", "--imsg", text])
        if any(k in t for k in ("email", "stage", "draft", "bundle")):
            return _run([TRIP_RECEIPTS, "--stage", "--imsg", text])
        return _run([TRIP_RECEIPTS, "--imsg", text])

    if "send" in t:
        out = _run([FOLLOWUP, "send"])
        try:
            d = json.loads(out)
            sent = ", ".join(d.get("sent") or []) or "none"
            skip = ", ".join(d.get("skipped") or []) or "none"
            return "Sent: %s\nHeld (needs address): %s" % (sent, skip)
        except Exception:
            return out
    if "preview" in t:
        return _run([FOLLOWUP, "preview"])
    # Calendar lookup ONLY on an explicit scheduling phrase. Bare "when"/"time"/"free"
    # hijacked normal questions ("when will X finish", "free to chat?") into a dumb slot
    # dump — and since the smart LLM relay ALSO answers, every such text got double-replied
    # with garbage. Require a real calendar ask; otherwise fall through to None (stay silent,
    # let the smart relay handle it).
    CAL_TRIGGERS = ("find times", "open slots", "free slots", "my availability",
                    "times next", "slots next", "when am i free", "when am i available",
                    "show my calendar", "my schedule", "what's open", "whats open")
    if any(k in t for k in CAL_TRIGGERS):
        out = _run([FINDTIMES, "next-week"])
        try:
            d = json.loads(out)
            slots = d.get("free_slots") or []
            if not slots:
                return "No open slots found in %s." % d.get("window", "")
            return "Open slots (%s):\n" % d.get("window", "") + "\n".join("• " + s["label"] for s in slots[:6])
        except Exception:
            return out
    if any(k in t for k in ("followup", "follow up", "follow-up", "draft")):
        packs = {}
        for f in glob.glob(PACKS_DIR + "/*.json"):
            key = os.path.splitext(os.path.basename(f))[0].lower()
            theme = key.replace("_followups", "").replace("_", " ").strip()
            packs[theme or key] = f
        chosen = None
        for theme, f in packs.items():
            if theme and theme in t:
                chosen = f; break
        if not chosen:
            names = ", ".join(sorted(packs.keys())) or "(none)"
            return "Which follow-up set? Available: " + names + "\nUsage: 'followups <name>'"
        _run([FOLLOWUP, "pack", chosen])
        return "Drafts rebuilt.\n\n" + _run([FOLLOWUP, "preview"])
    if "packs" in t or t in ("help", "?", "commands"):
        names = ", ".join(sorted(os.path.splitext(os.path.basename(f))[0].replace("_followups","") for f in glob.glob(PACKS_DIR + "/*.json"))) or "(none)"
        return HELP + "\nFollow-up sets available: " + names
    # Unrecognized — don't dump a menu. Silently ignore so LLM fallback or openclaw handles it.
    return None

def _send_imessage_raw(body):
    """Low-level, ungated iMessage/SMS to Operator. Used ONLY for direct replies to a
    command Operator texted the bridge (those must always send). Alerts must NOT use
    this — they go through send_imessage() so the gateway dedups/rate-caps them."""
    safe = body.replace("\\", "\\\\").replace('"', '\\"')
    script = (
        'tell application "Messages"\n'
        '  try\n'
        '    set svc to 1st service whose service type = iMessage\n'
        '    send "%s" to participant "%s" of svc\n'
        '  on error\n'
        '    set svc to 1st service whose service type = SMS\n'
        '    send "%s" to participant "%s" of svc\n'
        '  end try\n'
        'end tell'
    ) % (safe, TARGET, safe, TARGET)
    p = "/tmp/_imsg_send.applescript"
    open(p, "w").write(script)
    subprocess.run(["osascript", p], capture_output=True, text=True, timeout=30)


def send_imessage(body, *, severity=None, issue_key=None, subject=None, source="imsg_bridge"):
    """Alert Operator THROUGH the central gateway (dedup + rate cap + zero-noise
    allowlist). ~16 alerters import this single-arg helper and used to hit iMessage
    raw with no dedup — a single flapping alerter could then flood Operator's phone.

    Routing every one through core/alert_gateway makes dedup UNAVOIDABLE: a repeated
    identical alert is dropped for the cooldown regardless of the caller. Severity is
    derived from the gateway's own allowlist (money/emergency -> page; everything else
    -> 15-min digest), so no policy changes here and no risk of re-flooding. If the
    gateway can't be imported/routed, we fall back to the raw leg so a real alert is
    never silently lost."""
    subj = subject or (body.splitlines()[0][:120] if body else "alert")
    try:
        from core import alert_gateway
        sev = severity or ("critical" if alert_gateway._imsg_allowed(subj, body) else "warn")
        alert_gateway.route(sev, source, subj, body or "", issue_key=issue_key)
        return
    except Exception:
        pass
    _send_imessage_raw(body)

def load_state():
    try:
        return json.load(open(STATE))
    except Exception:
        return {}

def save_state(s):
    json.dump(s, open(STATE, "w"))

def main():
    st = load_state()
    conn = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    conn.row_factory = sqlite3.Row
    # handle ROWIDs for Operator's number (iMessage + SMS)
    hrows = conn.execute("SELECT ROWID FROM handle WHERE id = ? OR id = ?",
                         (TARGET, TARGET.replace("+1", ""))).fetchall()
    hids = [r["ROWID"] for r in hrows]
    if not hids:
        return
    ph = ",".join("?" * len(hids))
    maxrow = conn.execute("SELECT MAX(ROWID) AS m FROM message").fetchone()["m"] or 0
    if "last_rowid" not in st:
        save_state({"last_rowid": maxrow}); return   # first run: don't replay history
    last = st["last_rowid"]
    rows = conn.execute(
        "SELECT ROWID, text FROM message WHERE ROWID > ? AND is_from_me = 0 "
        "AND handle_id IN (%s) AND text IS NOT NULL ORDER BY ROWID ASC" % ph,
        [last] + hids).fetchall()
    new_last = last
    for r in rows:
        new_last = max(new_last, r["ROWID"])
        reply = route(r["text"])
        if reply:
            _send_imessage_raw(reply)  # direct command reply — must always send, never gated
    save_state({"last_rowid": max(new_last, maxrow)})

if __name__ == "__main__":
    main()
