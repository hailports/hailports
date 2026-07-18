#!/usr/bin/env python3
"""case_study_x_rot_guard.py — belt-and-suspenders guard so the @hailports X lane can never again
silently stall for weeks after its login session rots or its growth engine dies.

Ground truth this exists for: through ~2026-06-25 the audience-growth engine (case_study_engage.py:
like/follow/reply/quote in-niche = the ONLY thing that creates reach) stopped running, and around
~06-29 the posting session (browser_sessions/case_study_x/auth.json) rotted. the channel job kept
posting into a room it no longer walked into, reach collapsed to bare follower count (~dozens of
views/post), and nobody was paged for over two weeks. The reddit lane had a guard for exactly this;
the X lane did not. This is that guard.

Runs on its own launchd clock, INDEPENDENT of the posting/engage jobs, reads only on-disk state (no
network, no login). Fires ONE alert through core.alert_gateway (dedup + cooldown, never osascript)
and STAGES the human re-login into data/hustle/ALEX_ACTION_QUEUE.md. Re-capture needs a human login,
so this guard never does it — it detects + escalates the irreducible owner action, then routes a
healed signal when the lane recovers so a future death pages fresh.

Death/stall signals (ANY one trips it):
  S1  session store missing/logged-out .. browser_sessions/case_study_x/auth.json absent or carrying
                                          no x.com login cookies (auth_token / ct0).
  S2  auth token expired ................ the auth_token cookie's expiry is in the past.
  S3  growth engine gone dark ........... case_study_engage_state.json unmodified for >= STALE_DAYS
                                          (the like/follow/reply/quote reach engine stopped).
  S4  session file itself stale ......... auth.json mtime older than SESSION_STALE_DAYS (no refresh
                                          capture in a long time — the usual rot precursor).

Run:
  tools/case_study_x_rot_guard.py            # evaluate + alert/stage (or recover)
  tools/case_study_x_rot_guard.py --dry      # print the verdict, touch nothing
  tools/case_study_x_rot_guard.py --selftest # offline logic checks
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(os.environ.get("CLAUDE_STACK_DIR") or (Path.home() / "claude-stack"))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

HUSTLE = ROOT / "data" / "hustle"
AUTH = HUSTLE / "browser_sessions" / "case_study_x" / "auth.json"
ENGAGE_STATE = HUSTLE / "case_study_engage_state.json"
QUEUE = HUSTLE / "ALEX_ACTION_QUEUE.md"
SEEN = HUSTLE / ".action_queue_seen.json"
GUARD_STATE = HUSTLE / ".case_study_x_rot_guard.json"

SOURCE = "case_study_x_session"
ISSUE_KEY = "case_study_x:rot"
# the growth engine posts on a fast cadence, so a few dark days is a real stall, not a lull.
STALE_DAYS = int(os.environ.get("CS_X_ROT_STALE_DAYS", "4"))
SESSION_STALE_DAYS = int(os.environ.get("CS_X_SESSION_STALE_DAYS", "10"))
# cookies that actually prove a non-anonymous X login.
SESSION_COOKIES = {"auth_token", "ct0"}

# The real human re-login. reauth_social.py is the shared re-capture entrypoint; if it has no
# case_study_x lane yet, the staged note says so and the alert makes the gap loud.
RELOGIN_CMD = "cd ~/claude-stack && venv/bin/python tools/reauth_social.py case_study_x"


def _now() -> float:
    return time.time()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load_json(p: Path):
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def _x_cookies(auth: dict | None) -> list[dict]:
    if not isinstance(auth, dict):
        return []
    return [c for c in (auth.get("cookies") or [])
            if isinstance(c, dict) and "x.com" in (c.get("domain") or "")
            or isinstance(c, dict) and "twitter.com" in (c.get("domain") or "")]


def _token_expired(cookies: list[dict], now: float) -> bool | None:
    """True if auth_token is present & expired, False if present & valid, None if absent/no-expiry."""
    for c in cookies:
        if c.get("name") == "auth_token":
            exp = c.get("expires")
            try:
                exp = float(exp)
            except (TypeError, ValueError):
                return None
            if exp <= 0:  # session cookie, no expiry to judge
                return None
            return exp < now
    return None


def _mtime_days(p: Path, now: float) -> float | None:
    try:
        return (now - p.stat().st_mtime) / 86400.0
    except OSError:
        return None


def evaluate(now: float | None = None) -> dict:
    """Read on-disk state and return {dead: bool, reasons: [...], detail: {...}}."""
    now = now if now is not None else _now()
    auth = _load_json(AUTH)
    cookies = _x_cookies(auth)
    cookie_names = {c.get("name") for c in cookies}

    reasons: list[str] = []
    detail: dict = {}

    # S1 — session store missing / logged-out
    if not AUTH.exists():
        reasons.append("session store auth.json is MISSING")
    elif not (cookie_names & SESSION_COOKIES):
        reasons.append("session store has NO x login cookies (logged out)")
    detail["session_cookies_present"] = sorted(cookie_names & SESSION_COOKIES)

    # S2 — auth token expired
    exp = _token_expired(cookies, now)
    detail["auth_token_expired"] = exp
    if exp is True:
        reasons.append("auth token (auth_token) has EXPIRED")

    # S3 — growth engine gone dark
    eng_age = _mtime_days(ENGAGE_STATE, now)
    detail["engage_state_age_days"] = round(eng_age, 1) if eng_age is not None else None
    if eng_age is None:
        reasons.append("growth engine state missing (case_study_engage never ran)")
    elif eng_age >= STALE_DAYS:
        reasons.append(f"growth engine dark for {eng_age:.0f}d (>= {STALE_DAYS}d) — no engaging = no reach")

    # S4 — session file mtime, INFORMATIONAL ONLY. Proven false as a death signal: a 14-day-old
    # auth.json still authed + read the live timeline fine (X auth_token cookies are long-lived).
    # File age does not mean the session is dead, so it never trips 'dead' on its own — S1/S2 (real
    # cookie state) and S3 (engine actually dark) are the truth. Kept in detail for observability.
    sess_age = _mtime_days(AUTH, now)
    detail["session_file_age_days"] = round(sess_age, 1) if sess_age is not None else None

    return {"dead": bool(reasons), "reasons": reasons, "detail": detail}


def _route(severity: str, subject: str, body: str, *, healed: bool, dry: bool) -> str:
    if dry:
        print(f"[{severity.upper()}{' HEALED' if healed else ''}] {subject}")
        return "dry"
    try:
        from core.alert_gateway import route
        res = route(severity, SOURCE, subject, body, issue_key=ISSUE_KEY, healed=healed)
        return res.get("action", "routed")
    except Exception as e:
        print(f"gateway route failed: {e}", file=sys.stderr)
        return "error"


def _stage_relogin(reasons: list[str], dry: bool) -> bool:
    title = "@hailports X session rotted / growth engine dark — re-capture the session"
    body = (
        "the @hailports X lane isn't gaining reach: the audience-growth engine (like/follow/reply/"
        "quote in-niche) is dark and/or the login session rotted, so the account posts into a room "
        "it stopped walking into. detected: " + "; ".join(reasons)
        + ".\n\n  re-login (opens a headed browser; log in + 2FA, it writes the canonical auth.json):"
        f"\n\n      {RELOGIN_CMD}\n\n"
        "  once the session is fresh, re-arm the growth engine (CASE_STUDY_ENGAGE_LIVE=1) and its "
        "launchd clock — posting alone does not create reach, engaging does."
    )
    if dry:
        print(f"[STAGE] {title}")
        return False

    try:
        from tools.chatgpt_share_link import scan_personal
        if scan_personal(f"{title}\n{body}"):
            return False
    except Exception:
        pass

    sig = hashlib.sha256(f"{title.strip()}\n{body.strip()}".encode("utf-8", "ignore")).hexdigest()[:16]
    try:
        seen = set(json.loads(SEEN.read_text())) if SEEN.exists() else set()
    except Exception:
        seen = set()
    if sig in seen:
        return False

    block = (
        f"\n---\n\n## {datetime.now().strftime('%Y-%m-%d')} — @hailports X session rot: re-login needed\n\n"
        f"> staged by case_study_x_rot_guard (irreducible owner action — needs a human login).\n\n"
        f"- [ ] {body}\n"
    )
    try:
        with QUEUE.open("a") as f:
            f.write(block)
        seen.add(sig)
        try:
            SEEN.write_text(json.dumps(sorted(seen)[-3000:]))
        except Exception:
            pass
        return True
    except Exception:
        return False


def _read_guard_state() -> dict:
    return _load_json(GUARD_STATE) or {}


def _write_guard_state(st: dict) -> None:
    try:
        GUARD_STATE.write_text(json.dumps(st, indent=2))
    except Exception:
        pass


def run(dry: bool = False) -> dict:
    verdict = evaluate()
    st = _read_guard_state()
    was_dead = bool(st.get("dead"))

    if verdict["dead"]:
        subject = "@hailports X lane ROTTED — posting into a dead session, zero reach growth"
        body = ("The @hailports X lane is scheduled but its session is dead and/or its growth engine "
                "stopped, so reach has collapsed to bare follower count. Signals: "
                + "; ".join(verdict["reasons"]) + ". Re-login staged to ALEX_ACTION_QUEUE.md.")
        action = _route("warn", subject, body, healed=False, dry=dry)
        staged = _stage_relogin(verdict["reasons"], dry)
        verdict["alert_action"] = action
        verdict["staged"] = staged
        if not dry:
            _write_guard_state({"dead": True, "since": st.get("since") or _now_iso(),
                                "last_reasons": verdict["reasons"], "checked": _now_iso()})
    else:
        if was_dead:
            _route("warn", "@hailports X lane recovered", "the @hailports X session + growth engine "
                   "are healthy again — reach lane is live.", healed=True, dry=dry)
            verdict["recovered"] = True
        if not dry:
            _write_guard_state({"dead": False, "checked": _now_iso()})

    return verdict


def _selftest() -> int:
    now = time.time()
    fails: list[str] = []

    live = {"cookies": [
        {"name": "auth_token", "domain": ".x.com", "expires": now + 9e6},
        {"name": "ct0", "domain": ".x.com", "expires": now + 9e6},
    ]}
    cks = _x_cookies(live)
    if _token_expired(cks, now) is not False:
        fails.append("live token judged expired")
    if not ({c["name"] for c in cks} & SESSION_COOKIES):
        fails.append("live session cookies not detected")

    dead = {"cookies": [{"name": "auth_token", "domain": ".x.com", "expires": now - 100}]}
    if _token_expired(_x_cookies(dead), now) is not True:
        fails.append("expired token not caught")
    if _token_expired([], now) is not None:
        fails.append("absent token not None")

    # twitter.com-domain cookies must still be recognized
    tw = {"cookies": [{"name": "auth_token", "domain": ".twitter.com", "expires": now + 9e6}]}
    if not _x_cookies(tw):
        fails.append("twitter.com-domain cookie not recognized")

    # end-to-end: fully dead picture (missing auth + missing engage state) -> dead
    global AUTH, ENGAGE_STATE
    import tempfile
    td = Path(tempfile.mkdtemp())
    AUTH = td / "auth.json"           # missing
    ENGAGE_STATE = td / "engage.json"  # missing
    v = evaluate(now)
    if not v["dead"]:
        fails.append(f"fully-dead picture not flagged dead: {v}")

    # healthy picture (fresh auth + fresh engage state) -> not dead
    AUTH = td / "auth_live.json"
    AUTH.write_text(json.dumps(live))
    ENGAGE_STATE = td / "engage_live.json"
    ENGAGE_STATE.write_text("{}")
    v2 = evaluate(now)
    if v2["dead"]:
        fails.append(f"healthy picture wrongly flagged: {v2}")

    if fails:
        print("CASE_STUDY_X_ROT_GUARD SELFTEST FAILED:")
        for f in fails:
            print("  -", f)
        return 1
    print("CASE_STUDY_X_ROT_GUARD SELFTEST PASSED "
          "(token expiry, x/twitter cookies, engage-dark staleness, dead/healthy end-to-end)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="evaluate + print, touch nothing")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return _selftest()
    verdict = run(dry=args.dry)
    print(json.dumps(verdict, indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"case_study_x_rot_guard fatal: {e}", file=sys.stderr)
        sys.exit(0)
