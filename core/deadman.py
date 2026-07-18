#!/usr/bin/env python3
"""Dead man's switch — quietly watches for "the time", then activates the continuity plan.

THE DESIGN, and why each guard exists:

  Operator proves he's here by checking in (replying to a text, or tapping a link). Every check-in
  resets the clock. He never has to remember — the machine pings HIM.

  If he goes silent, it escalates SLOWLY, because the worst possible failure is telling his
  family he's gone while he's alive:
    days 0..GRACE_DAYS      -> nothing (busy, travelling, off-grid).
    GRACE_DAYS..SILENT_DAYS -> ping him, gently then urgently, on every channel. Any reply resets.
    SILENT_DAYS reached     -> ask the TRUSTED HELPER to confirm (a human gate). Helper can VETO
                               ("he's fine") which resets, or CONFIRM which activates now.
    + HELPER_DAYS no answer -> if even the helper is unreachable, activate anyway (so it still
                               works if everyone's silent) — but only after the full window.

  ACTIVATION (idempotent, fires once): send her his message + remind her the key is on her
  headphones + the link to talk to the machine (his words). Switch the stack to legacy mode.

All state lives in ~/.legacy (private, never synced). Defaults are deliberately conservative;
edit ~/.legacy/deadman.json to tune.

  python3 -m core.deadman checkin     # "I'm here" — resets the clock (also via /alive link)
  python3 -m core.deadman status      # where things stand
  python3 -m core.deadman tick        # one watch cycle (run by cron, daily)
  python3 -m core.deadman test-ping   # send yourself a check-in ping right now
"""
from __future__ import annotations
import json, os, secrets, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path

LEGACY_DIR = Path(os.path.expanduser("~/.legacy"))
STATE = LEGACY_DIR / "deadman.json"
FINAL_MSG = LEGACY_DIR / "final_message.txt"   # what she reads first; edit any time

DEFAULTS = {
    "alex_name": "",                        # how she'd refer to him in the email subject
    "alex_number": "XPHONEX",          # where check-in pings go (his phone)
    "partner_name": "", "partner_number": "", "partner_email": "",   # her — FILL IN
    "helper_name": "", "helper_number": "", "helper_email": "",       # trusted confirmer — FILL IN
    "legacy_url": "https://scannerapp.dev/forever",  # where she talks to his words
    "grace_days": 4,        # silence before we even start pinging
    "silent_days": 21,      # total silence before we ask the helper to confirm
    "helper_days": 4,       # extra days we wait on the helper before activating anyway
    "max_contact_attempts": 30,   # after activation, keep trying to reach her this many cycles
    "last_checkin": None, "last_ping": None,
    "helper_asked": None, "armed": True, "activated": False, "activated_at": None,
    "contact": None,        # persistent delivery state {attempts, reached_once, confirmed, log}
}


def _now():
    return datetime.now(timezone.utc)


def _load() -> dict:
    LEGACY_DIR.mkdir(mode=0o700, exist_ok=True)
    d = dict(DEFAULTS)
    if STATE.exists():
        try:
            d.update(json.loads(STATE.read_text()))
        except Exception:
            pass
    if d["last_checkin"] is None:
        d["last_checkin"] = _now().isoformat()   # arm from first run
    return d


def _save(d: dict):
    STATE.write_text(json.dumps(d, indent=2))
    try:
        os.chmod(STATE, 0o600)
    except OSError:
        pass


def _days_since(iso: str | None) -> float:
    if not iso:
        return 1e9
    try:
        return (_now() - datetime.fromisoformat(iso)).total_seconds() / 86400.0
    except Exception:
        return 1e9


def _imessage(number: str, text: str) -> bool:
    """Send via Messages.app (works headless while Messages is signed in)."""
    if not number:
        return False
    script = (
        'tell application "Messages"\n'
        ' set svc to 1st service whose service type = iMessage\n'
        f' send "{text.replace(chr(34), chr(39))}" to buddy "{number}" of svc\n'
        'end tell'
    )
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=25)
        return r.returncode == 0
    except Exception:
        return False


def _checkin_link(d: dict) -> str:
    """Tappable check-in URL with a private token (so only Operator can reset the clock)."""
    tf = LEGACY_DIR / "checkin_token.txt"
    if not tf.exists():
        tf.write_text(secrets.token_urlsafe(12))
        try:
            os.chmod(tf, 0o600)
        except OSError:
            pass
    return d["legacy_url"].replace("/forever", "/alive") + "?t=" + tf.read_text().strip()


def checkin(note: str = "") -> dict:
    """Operator is here. Reset everything."""
    d = _load()
    d["last_checkin"] = _now().isoformat()
    d["helper_asked"] = None
    if d.get("activated"):                     # a false alarm was caught — stand down
        d["activated"] = False; d["activated_at"] = None; d["contact"] = None
    _save(d)
    return {"ok": True, "checked_in": d["last_checkin"], "note": note}


def _email(to: str, subject: str, body: str) -> bool:
    """Best-effort email via whatever sender the stack already has. Degrades to False."""
    if not to:
        return False
    html = "<div style='font-family:-apple-system,sans-serif;white-space:pre-wrap;font-size:16px;line-height:1.6'>" + body + "</div>"
    for mod, fn in (("agents.site_care_monitor", "send_email"), ("agents.intent_service_deliver", "send_email")):
        try:
            m = __import__(mod, fromlist=[fn])
            if getattr(m, fn)(to, subject, html):
                return True
        except Exception:
            continue
    return False


def _attempt_contact(d: dict) -> dict:
    """One persistent attempt to reach the family across every channel. Called on activation
    and re-called each cycle until contact is confirmed or attempts run out."""
    msg = _final_message(d)
    c = d.get("contact") or {"attempts": 0, "reached_once": False, "confirmed": False, "log": []}
    c["attempts"] += 1
    res = {}
    if d["partner_number"]:
        res["her_text"] = _imessage(d["partner_number"], msg)
    if d["partner_email"]:
        res["her_email"] = _email(d["partner_email"],
                                  f"A message from {d['alex_name'] or 'someone who loves you'}", msg)
    helper_note = (f"This is {d['alex_name'] or 'Operator'}'s machine. He's been unreachable "
                   f"{int(_days_since(d['last_checkin']))} days and his continuity plan has activated. "
                   f"Please look in on {d['partner_name'] or 'his family'} in person — they've been given "
                   f"access to what he left them. If this is a mistake, get him to reply so it stands down.")
    if d["helper_number"]:
        res["helper_text"] = _imessage(d["helper_number"], helper_note)
    if d["helper_email"]:
        res["helper_email"] = _email(d["helper_email"], f"Please check on {d['partner_name'] or 'the family'}", helper_note)
    if res.get("her_text") or res.get("her_email"):
        c["reached_once"] = True
    c["last"] = _now().isoformat()
    c["log"].append({"ts": c["last"], "attempt": c["attempts"], "result": res})
    d["contact"] = c
    return c


def _final_message(d: dict) -> str:
    if FINAL_MSG.exists():
        body = FINAL_MSG.read_text(encoding="utf-8").strip()
    else:
        body = ("If you're reading this from the machine, it means I couldn't check in for a long "
                "time. I built this to keep taking care of you. I love you.")
    key = "Your password is on your headphones — it's the key to everything."
    link = f"You can talk to my words anytime here: {d['legacy_url']}"
    return f"{body}\n\n{key}\n{link}"


def activate(d: dict, reason: str) -> dict:
    if d.get("activated"):
        return d
    d["activated"] = True
    d["activated_at"] = _now().isoformat()
    d["activate_reason"] = reason
    d["contact"] = None
    (LEGACY_DIR / "ACTIVATED.flag").write_text(d["activated_at"])   # legacy mode marker
    _attempt_contact(d)            # first attempt now; tick() keeps trying after this
    _save(d)
    return d


def tick() -> dict:
    d = _load()
    if not d.get("armed"):
        _save(d); return {"status": "disarmed"}
    if d.get("activated"):
        # keep trying to reach the family until contact is confirmed or attempts run out
        c = d.get("contact") or {}
        if not c.get("confirmed") and c.get("attempts", 0) < d.get("max_contact_attempts", 30):
            _attempt_contact(d)
        _save(d)
        return {"status": "activated", "contact_attempts": (d.get("contact") or {}).get("attempts", 0),
                "reached_once": (d.get("contact") or {}).get("reached_once", False)}
    silent = _days_since(d["last_checkin"])

    if silent < d["grace_days"]:
        _save(d); return {"status": "ok", "silent_days": round(silent, 1)}

    if silent < d["silent_days"]:
        # escalating reminders — at most one per day
        if _days_since(d["last_ping"]) >= 1:
            urgency = "Just checking you're okay — " if silent < d["silent_days"] / 2 else \
                      "Please reply so I know you're alright — "
            _imessage(d["alex_number"],
                      f"{urgency}reply ALIVE (or tap {_checkin_link(d)}). "
                      f"If I don't hear back, the plan for the family will start in {int(d['silent_days']-silent)} days.")
            d["last_ping"] = _now().isoformat()
        _save(d); return {"status": "pinging", "silent_days": round(silent, 1)}

    # full silence reached — bring in the human confirmer
    if d["helper_number"] and not d.get("helper_asked"):
        d["helper_asked"] = _now().isoformat()
        _imessage(d["helper_number"],
                  f"This is Operator's machine. He hasn't checked in for {int(silent)} days and isn't responding. "
                  f"If something has happened, reply CONFIRM and I'll give {d['partner_name'] or 'his family'} "
                  f"what he left them. If he's fine, please get him to reply ALIVE. I'll wait {d['helper_days']} days.")
        _save(d); return {"status": "asked_helper", "silent_days": round(silent, 1)}

    # helper had their window (or there is no helper) -> activate
    if not d["helper_number"] or _days_since(d["helper_asked"]) >= d["helper_days"]:
        activate(d, reason=f"silent {int(silent)}d, helper window elapsed")
        return {"status": "ACTIVATED", "silent_days": round(silent, 1)}
    _save(d); return {"status": "awaiting_helper", "silent_days": round(silent, 1)}


def status() -> dict:
    d = _load()
    return {"armed": d["armed"], "activated": d["activated"],
            "silent_days": round(_days_since(d["last_checkin"]), 1),
            "fires_at_days": d["silent_days"] + d["helper_days"],
            "partner_set": bool(d["partner_number"]), "helper_set": bool(d["helper_number"])}


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "checkin":
        print(json.dumps(checkin(" ".join(sys.argv[2:])), indent=2))
    elif cmd == "tick":
        print(json.dumps(tick(), indent=2))
    elif cmd == "test-ping":
        d = _load(); ok = _imessage(d["alex_number"], "Test from your machine — reply ALIVE to reset your switch.")
        print("ping sent" if ok else "ping FAILED (is Messages signed in?)")
    else:
        print(json.dumps(status(), indent=2))
