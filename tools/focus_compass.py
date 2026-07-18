#!/usr/bin/env python3
"""focus_compass — the stack keeps Operator pointed at where he's going.

Holds ONE current destination (the thing that matters now) + what he's doing right this moment
+ the next concrete steps. Queryable anytime ("where am I going / whats next / refocus me") and
re-surfaced on a gentle cadence so he doesn't drift. State is the single source of truth that the
openclaw brain and the work-GPT both read.

  focus_compass.py show                      # print the compass
  focus_compass.py destination "ship X" --why "because Y"
  focus_compass.py now "wiring the onedrive sync"
  focus_compass.py next "test it" "schedule it" "tell Operator"
  focus_compass.py done                       # complete the top next-step, advance
  focus_compass.py nudge                       # compose + iMessage a re-orient (idle/quiet-aware)
"""
import json, sys, os, time, datetime, subprocess

ROOT = os.path.expanduser("~/claude-stack")
FDIR = os.path.join(ROOT, "data", "focus")
STATE = os.path.join(FDIR, "compass.json")
MD = os.path.join(FDIR, "COMPASS.md")
QUIET_START, QUIET_END = 22, 7   # no nudges 10pm-7am local

def _now_ts(): return int(time.time())
def _human(ts):
    if not ts: return "—"
    d = _now_ts() - int(ts)
    if d < 90: return "just now"
    if d < 3600: return f"{d//60}m ago"
    if d < 86400: return f"{d//3600}h {(d%3600)//60}m ago"
    return f"{d//86400}d ago"

def load():
    try:
        with open(STATE) as f: return json.load(f)
    except Exception:
        return {"destination": "", "why": "", "now": "", "now_set": 0,
                "next_steps": [], "updated": 0, "history": []}

def save(s):
    os.makedirs(FDIR, exist_ok=True)
    s["updated"] = _now_ts()
    with open(STATE, "w") as f: json.dump(s, f, indent=2)
    render(s)

def render(s):
    lines = ["# 🧭 Focus Compass", ""]
    lines.append(f"**where im going:** {s.get('destination') or '(not set)'}")
    if s.get("why"): lines.append(f"**why:** {s['why']}")
    lines.append("")
    lines.append(f"**right now:** {s.get('now') or '(not set)'}  ·  _{_human(s.get('now_set'))}_")
    lines.append("")
    steps = s.get("next_steps") or []
    if steps:
        lines.append("**next:**")
        for i, st in enumerate(steps):
            lines.append(f"{i+1}. {st}")
    else:
        lines.append("**next:** (nothing queued — set the next step)")
    if s.get("history"):
        lines.append("")
        lines.append("**done recently:** " + " · ".join(f"✓ {h['step']}" for h in s["history"][-5:]))
    lines.append("")
    lines.append(f"_updated {_human(s.get('updated'))}_")
    os.makedirs(FDIR, exist_ok=True)
    with open(MD, "w") as f: f.write("\n".join(lines) + "\n")

def compose_nudge(s):
    dest = s.get("destination") or "(no destination set — tell me what we're aiming at)"
    now = s.get("now") or "—"
    steps = s.get("next_steps") or []
    nxt = steps[0] if steps else "set your next step"
    drift = ""
    if s.get("now_set"):
        gap = _now_ts() - int(s["now_set"])
        if gap > 5400:  # >1.5h on the same "now"
            drift = f"  (youve been on this {gap//3600}h{(gap%3600)//60:02d}m — still the right thing?)"
    return f"🧭 where youre going: {dest}\nright now: {now}{drift}\nnext: {nxt}"

def in_quiet_hours():
    h = datetime.datetime.now().hour
    return h >= QUIET_START or h < QUIET_END

def send_imessage(body):
    try:
        sys.path.insert(0, ROOT)
        from tools.imsg_bridge import send_imessage as _s
        _s(body); return True
    except Exception as e:
        sys.stderr.write(f"[compass] imsg failed: {e}\n"); return False

def main():
    a = sys.argv[1:] or ["show"]
    cmd = a[0]
    s = load()
    if cmd == "show":
        render(s); print(open(MD).read())
    elif cmd == "destination":
        s["destination"] = a[1] if len(a) > 1 else s.get("destination", "")
        if "--why" in a: s["why"] = a[a.index("--why")+1]
        save(s); print(open(MD).read())
    elif cmd == "now":
        s["now"] = " ".join(x for x in a[1:] if not x.startswith("--")); s["now_set"] = _now_ts()
        save(s); print(open(MD).read())
    elif cmd == "next":
        s["next_steps"] = [x for x in a[1:] if not x.startswith("--")]
        save(s); print(open(MD).read())
    elif cmd == "done":
        steps = s.get("next_steps") or []
        if steps:
            done = steps.pop(0)
            s.setdefault("history", []).append({"step": done, "at": _now_ts()})
            s["next_steps"] = steps
            save(s); print(f"✓ {done}\n"); print(open(MD).read())
        else:
            print("no next step queued")
    elif cmd == "nudge":
        if not s.get("destination"):
            sys.stderr.write("[compass] no destination set — skipping nudge\n"); return
        if in_quiet_hours() and "--force" not in a:
            sys.stderr.write("[compass] quiet hours — skipping nudge\n"); return
        msg = compose_nudge(s)
        if "--dry" in a: print(msg)
        else: send_imessage(msg); print("[compass] nudged")
    else:
        print(__doc__)

if __name__ == "__main__":
    main()
