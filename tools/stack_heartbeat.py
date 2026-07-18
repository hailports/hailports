#!/usr/bin/env python3
"""tools/stack_heartbeat.py — LAST line of defense.

Independent, stdlib-only liveness probe for Operator's revenue stack. Designed to
keep working even when the stack itself (gateway, ollama, venv deps) is broken:
no third-party imports, absolute paths, its own launchd job. On any critical
failure it texts Operator directly via iMessage (owner alert = allowed).

Honest limit: if this whole Mac is down/offline, THIS cannot fire. That blind
spot can only be covered by an OFF-BOX uptime monitor hitting a public health
URL (see HONEST_LIMIT note at bottom / heartbeat report).
"""
import json
import os
import socket
import ssl
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone

ROOT = "/home/user/claude-stack"
LOG_DIR = "/home/user/Library/Logs/claude-stack"
LOG_FILE = os.path.join(LOG_DIR, "stack-heartbeat.log")
JSONL_FILE = os.path.join(LOG_DIR, "stack-heartbeat.jsonl")
STATE_FILE = os.path.join(LOG_DIR, "stack-heartbeat.state")
HB_STATE_FILE = os.path.join(LOG_DIR, "stack-heartbeat.hysteresis.json")

SELF_SERVE_URL = "http://127.0.0.1:8300/"
PUBLIC_URLS = ("https://www.hailports.com", "https://scannerapp.dev")
SCOREBOARD = os.path.join(ROOT, "data/hustle/revenue_scoreboard.jsonl")
# The writer (com.claude-stack.revenue-scoreboard) runs ~every 12h (08:30 & 20:30),
# so 6h was a false-DOWN generator: the file is legitimately >6h old for most of
# the day. Give the probe margin past the real cadence; a true pipeline stall is
# caught when even the 12h cadence is missed.
SCOREBOARD_MAX_AGE_H = 13.0
DISK_MAX_PCT = 90
ALEX_PHONE = "XPHONEX"

# Hysteresis: a check must FAIL this many consecutive runs before it pages, so a
# single transient blip (slow probe, momentary network) never texts Operator.
N_CONSEC_FAIL = 2

_IMSG_SCRIPT = (
    'on run {targetPhone, targetMsg}\n'
    '  tell application "Messages"\n'
    '    set svc to 1st service whose service type = iMessage\n'
    '    send targetMsg to buddy targetPhone of svc\n'
    '  end tell\n'
    'end run'
)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _log(line):
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(f"{_now()} {line}\n")
    except Exception:
        pass


def _http_code(url, timeout=8):
    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers={"User-Agent": "stack-heartbeat/1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            return r.status
    except urllib.error.HTTPError as e:  # type: ignore[attr-defined]
        return e.code
    except Exception:
        return None


# ---- checks: each returns (ok: bool, detail: str) -------------------------

def check_self_serve():
    code = _http_code(SELF_SERVE_URL, timeout=6)
    return code == 200, f"self-serve :8300 -> {code}"


def check_cloudflared():
    try:
        r = subprocess.run(["/usr/bin/pgrep", "-f", "cloudflared.*tunnel.*run"],
                           capture_output=True, text=True, timeout=8)
        up = r.returncode == 0 and r.stdout.strip() != ""
        return up, f"cloudflared {'up pid=' + r.stdout.split()[0] if up else 'DOWN'}"
    except Exception as e:
        return False, f"cloudflared probe error: {e}"


def check_public():
    results = []
    for u in PUBLIC_URLS:
        code = _http_code(u, timeout=8)
        results.append(f"{u}={code}")
        if code == 200:
            return True, "public ok (" + ", ".join(results) + ")"
    return False, "public UNREACHABLE (" + ", ".join(results) + ")"


def check_hailports_funnel():
    """Dedicated probe of the LIVE money funnel — its OWN alert key, NOT OR-collapsed with
    check_public (which greenlights on the first 200 from www.hailports.com and would stay
    green while the funnel is dead). One GET verifies tunnel + remote CF route + :8300 app +
    the host-scoped funnel page are all alive."""
    u = "https://scan.hailports.com/hailports?scan=web"
    code = _http_code(u, timeout=10)
    return code == 200, f"hailports funnel {u} = {code}"


def check_scoreboard():
    try:
        age_h = (time.time() - os.path.getmtime(SCOREBOARD)) / 3600.0
        return age_h <= SCOREBOARD_MAX_AGE_H, f"scoreboard age={age_h:.1f}h (max {SCOREBOARD_MAX_AGE_H})"
    except Exception as e:
        return False, f"scoreboard missing/unreadable: {e}"


def check_disk():
    try:
        st = os.statvfs("/")
        used_pct = round(100.0 * (st.f_blocks - st.f_bfree) / st.f_blocks)
        return used_pct <= DISK_MAX_PCT, f"disk {used_pct}% used (max {DISK_MAX_PCT})"
    except Exception as e:
        return False, f"disk probe error: {e}"


CHECKS = (
    ("self_serve", check_self_serve),
    ("cloudflared", check_cloudflared),
    ("public", check_public),
    ("hailports_funnel", check_hailports_funnel),
    ("scoreboard", check_scoreboard),
    ("disk", check_disk),
)


def _inline_imessage(subject, body):
    """Raw iMessage — the absolute last resort if even the gateway import fails.
    A broken stack must never be able to silence the last line of defense."""
    msg = f"🔔 {subject}\n{body}"[:600]
    try:
        r = subprocess.run(["/usr/bin/osascript", "-e", _IMSG_SCRIPT, ALEX_PHONE, msg],
                           capture_output=True, text=True, timeout=25)
        return r.returncode == 0
    except Exception as e:
        _log(f"inline iMessage fallback failed: {e}")
        return False


def route_alert(severity, issue_key, subject, body, healed=False):
    """Route through the central gateway (dedup + global rate-limit + digest).
    Falls back to a debounced inline iMessage if the gateway can't be imported,
    so a broken stack still gets ONE page (not one per 10-min run)."""
    try:
        sys.path.insert(0, ROOT)
        from core import alert_gateway
        res = alert_gateway.route(severity, "stack_heartbeat", subject, body,
                                  issue_key=issue_key, healed=healed)
        _log(f"gateway route {issue_key} -> {res.get('action')}")
        return True
    except Exception as e:
        _log(f"gateway unavailable, inline fallback: {e}")
    if healed:
        return True  # nothing to page on recovery in the degraded path
    # Degraded path: dedup inline pages by issue_key so we don't flood every run.
    st = _load_hb_state()
    last = st.get("inline_last_page", {}).get(issue_key, 0)
    if time.time() - last < SCOREBOARD_MAX_AGE_H * 3600:
        return False
    ok = _inline_imessage(subject, body)
    st.setdefault("inline_last_page", {})[issue_key] = time.time()
    _save_hb_state(st)
    return ok


def _load_hb_state():
    try:
        with open(HB_STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_hb_state(st):
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        tmp = HB_STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(st, f, indent=2)
        os.replace(tmp, HB_STATE_FILE)
    except Exception as e:
        _log(f"hysteresis state write failed: {e}")


def main():
    results = []
    failures = []
    hb = _load_hb_state()
    checks_state = hb.setdefault("checks", {})  # name -> {fails, down}
    confirmed_down = []   # (name, detail) failing N_CONSEC_FAIL runs in a row
    recovered = []        # (name, detail) was down, now OK

    for name, fn in CHECKS:
        try:
            ok, detail = fn()
        except Exception as e:
            ok, detail = False, f"{name} raised: {e}"
        results.append({"check": name, "ok": ok, "detail": detail})
        _log(f"{'OK ' if ok else 'FAIL'} {detail}")
        cs = checks_state.setdefault(name, {"fails": 0, "down": False})
        if ok:
            if cs.get("down"):
                recovered.append((name, detail))
            cs["fails"] = 0
            cs["down"] = False
        else:
            failures.append(detail)
            cs["fails"] = cs.get("fails", 0) + 1
            if cs["fails"] >= N_CONSEC_FAIL:
                cs["down"] = True
                confirmed_down.append((name, detail))

    record = {"ts": _now(), "ok": not failures, "results": results}
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(JSONL_FILE, "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as e:
        _log(f"jsonl write failed: {e}")

    first_run = not os.path.exists(STATE_FILE)

    # Page only CONFIRMED-down checks (N consecutive fails). Each routes through
    # the gateway under its own issue_key, so a sustained failure pages once per
    # cooldown — not every 10-min run — and a transient blip never pages at all.
    for name, detail in confirmed_down:
        route_alert("critical", f"heartbeat:{name}",
                    f"Stack heartbeat: {name} DOWN", detail)
    # A recovered check clears its alert (recovery note digests, doesn't page).
    for name, detail in recovered:
        route_alert("info", f"heartbeat:{name}",
                    f"Stack heartbeat: {name} recovered", detail, healed=True)
    if confirmed_down:
        _log(f"ALERT routed {len(confirmed_down)} confirmed-down, "
             f"{len(failures)} raw fail(s)")
    elif first_run:
        route_alert("info", "heartbeat:first_run", "Stack heartbeat: OK",
                    "[HEARTBEAT OK] Last-line-of-defense monitor is live. "
                    "You'll only hear from it again if the stack goes down.")
        _log("first-run [HEARTBEAT OK] confirmation sent")

    _save_hb_state(hb)
    try:
        with open(STATE_FILE, "w") as f:
            f.write(_now() + (" OK" if not failures else " FAIL"))
    except Exception:
        pass

    print(json.dumps(record, indent=2))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
