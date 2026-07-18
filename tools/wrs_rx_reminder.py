#!/usr/bin/env python3
"""wrs_rx_reminder — 27-day Adderall renewal reminder (WRS Health is phone-app only, no web
portal to auto-submit against, so this is a perfectly-timed nudge Operator taps; never a silent
auto-submit on a med he depends on).

Anchored 2026-06-13, fires every 27 days. Runs daily via launchd; only sends when due, then
advances the anchor. State: data/focus/wrs_rx.json.

  wrs_rx_reminder.py check        # daily: send only if due
  wrs_rx_reminder.py status       # show next due date
  wrs_rx_reminder.py send --force # send now regardless (and advance)
"""
import json, os, sys, time, datetime

ROOT = os.path.expanduser("~/claude-stack")
STATE = os.path.join(ROOT, "data", "focus", "wrs_rx.json")
ANCHOR = datetime.date(2026, 6, 13)
PERIOD = 27
MSG = ("\U0001f48a adderall renewal time -- open the WRS Health app & put in the renewal request "
       "(takes ~10s). next one auto-reminds in 27 days.")

def load():
    try:
        with open(STATE) as f: return json.load(f)
    except Exception:
        return {"last_fired": None, "anchor": ANCHOR.isoformat(), "period_days": PERIOD}

def save(s):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w") as f: json.dump(s, f, indent=2)

def next_due(s):
    last = s.get("last_fired")
    if last:
        return datetime.date.fromisoformat(last) + datetime.timedelta(days=PERIOD)
    return ANCHOR

def send():
    try:
        sys.path.insert(0, ROOT)
        from tools.imsg_bridge import send_imessage
        send_imessage(MSG); return True
    except Exception as e:
        sys.stderr.write(f"[wrs-rx] imsg failed: {e}\n"); return False

def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    s = load()
    today = datetime.date.today()
    due = next_due(s)
    if cmd == "status":
        print(f"next adderall reminder due: {due.isoformat()} (today {today.isoformat()}, "
              f"last fired {s.get('last_fired') or 'never'})")
        return
    if cmd == "send" or (cmd == "check" and today >= due):
        force = "--force" in sys.argv
        if cmd == "check" and today < due and not force:
            return
        if send():
            s["last_fired"] = today.isoformat()
            save(s)
            print(f"[wrs-rx] reminder sent; next due {today + datetime.timedelta(days=PERIOD)}")
        else:
            print("[wrs-rx] send failed; will retry next run")
    else:
        print(f"[wrs-rx] not due yet (next {due.isoformat()})")

if __name__ == "__main__":
    main()
