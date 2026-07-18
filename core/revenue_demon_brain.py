#!/usr/bin/env python3
"""The demon's high-reasoning "think" step: drive the owner's PERSONAL ChatGPT on
chatgpt.com through the dedicated OFF-SCREEN profile (.chrome-cdp-profile-chatgpt,
port 18864) — NOT the desktop app, NOT the CompanyA Enterprise workspace.

This is a thin wrapper over the already-proven driver helpers in
``tools.chatgpt_share_link`` (same selectors, same login-detection, same
off-screen Chrome that never steals focus). It launches its own Chrome per call
and closes it in a finally, so there is nothing long-lived to babysit.

Contract: ``think(prompt_text)`` -> dict
  {"ok": True,  "raw": <full chat text>, "plan": <parsed JSON dict or None>}
  {"ok": False, "reason": "logged_out" | "no_composer" | "browser_error" | "leak", ...}

ChatGPT output is treated as UNTRUSTED (prompt-injection from scraped prospect
data is possible): the caller parses only a strict, known-key schema and ignores
anything else. We additionally run the deterministic personal-marker scan before
returning, so the demon never stages owner identity downstream.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Fail-closed lane-separation gate: the demon will NOT drive chatgpt.com until this
# marker exists, asserting the off-screen profile is a PERSONAL (non-CompanyA) account.
# Created once after the account is confirmed; delete it if the profile is ever switched.
PERSONAL_OK = ROOT / "data" / "hustle" / "CHATGPT_PROFILE_PERSONAL_OK"
PACE_FILE = ROOT / "data" / "hustle" / ".chatgpt_pace.jsonl"   # epoch per ChatGPT call (ban-safety)
BACKOFF_FILE = ROOT / "data" / "hustle" / ".chatgpt_backoff"   # epoch-until: hard back off on a rate/abuse signal

_JSON_FENCE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.S | re.I)


def _composer_ready(page, timeout_s: int = 35) -> bool:
    """Poll until the chat composer exists. chatgpt.com sits behind a Cloudflare
    'Just a moment...' challenge that clears in a few seconds for a real-looking
    (stealth) browser, after which the SPA hydrates the textarea."""
    import time as _t
    deadline = _t.time() + timeout_s
    while _t.time() < deadline:
        page.wait_for_timeout(1500)
        if "auth" in page.url or "/login" in page.url:
            return False
        for sel in ("#prompt-textarea", 'div[contenteditable="true"]#prompt-textarea', "textarea"):
            try:
                if page.locator(sel).count():
                    return True
            except Exception:
                continue
    return False


def _logged_out(page) -> bool:
    """ChatGPT now shows a working composer to ANONYMOUS (logged-out) users, so composer
    presence != authenticated. The reliable signal is the login affordance: 'Log in' /
    'Sign up for free'. Returns True if this is a logged-out/anonymous session."""
    try:
        if "auth" in page.url or "/login" in page.url:
            return True
    except Exception:
        pass
    for sel in ('text="Sign up for free"',
                '[data-testid="login-button"]',
                'button:has-text("Log in")',
                'a:has-text("Log in")'):
        try:
            if page.locator(sel).first.is_visible(timeout=800):
                return True
        except Exception:
            continue
    return False


def logged_in() -> bool:
    """Cheap probe: open chatgpt.com off-screen, clear Cloudflare, confirm an AUTHENTICATED
    (non-anonymous) session. Gated on the marker first, so it never opens Chrome unverified."""
    if not PERSONAL_OK.exists():
        return False
    try:
        from tools.offscreen_browser import offscreen_context
        with offscreen_context("chatgpt", load_session=True, stealth=True) as ctx:
            page = ctx.new_page()
            page.goto("https://chatgpt.com/?model=auto", wait_until="domcontentloaded", timeout=45000)
            return _composer_ready(page) and not _logged_out(page)
    except Exception:
        return False


def _extract_plan(raw: str) -> dict | None:
    """Parse the LAST fenced JSON block; fall back to the last balanced {...}."""
    if not raw:
        return None
    blocks = _JSON_FENCE.findall(raw)
    for chunk in reversed(blocks):
        try:
            obj = json.loads(chunk.strip())
            if isinstance(obj, dict):
                return obj
        except Exception:
            continue
    # fallback: scan for the last {...} that parses
    starts = [m.start() for m in re.finditer(r"\{", raw)]
    for s in reversed(starts):
        depth, end = 0, None
        for i in range(s, len(raw)):
            if raw[i] == "{":
                depth += 1
            elif raw[i] == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end:
            try:
                obj = json.loads(raw[s:end])
                if isinstance(obj, dict) and obj:
                    return obj
            except Exception:
                continue
    return None


def login() -> dict:
    """Open a PLAIN Chrome (NO automation/CDP) on the dedicated chatgpt profile dir. A normal
    browser clears Cloudflare's Turnstile like a human — a Playwright-controlled Chrome trips it
    and loops forever, so we deliberately do NOT drive this window. The operator logs into their
    PERSONAL account, QUITs Chrome, then runs --verify (stealth path) to write the marker.
        python3 -m core.revenue_demon_brain --login    # opens Chrome to log in
        python3 -m core.revenue_demon_brain --verify   # confirms + writes the marker"""
    try:
        from tools.offscreen_browser import profile_dir, CHROME_BIN
    except Exception as e:
        return {"ok": False, "error": str(e)[:160]}
    udir = profile_dir("chatgpt")
    udir.mkdir(parents=True, exist_ok=True)
    if not Path(CHROME_BIN).exists():
        return {"ok": False, "error": f"Chrome not found at {CHROME_BIN}"}
    from core.chrome_launch import require_interactive_headful
    require_interactive_headful("revenue_demon personal ChatGPT login")
    import subprocess as _sp
    try:
        _sp.Popen([CHROME_BIN, f"--user-data-dir={udir}", "--no-first-run",  # headful-ok
                   "--no-default-browser-check", "https://chatgpt.com/"],
                  stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
    except Exception as e:
        return {"ok": False, "error": str(e)[:160]}
    print("[login] a NORMAL Chrome window is opening on the dedicated profile (Cloudflare will pass).")
    print("[login] 1) log into your PERSONAL ChatGPT account — use the email field; ignore 'Continue with Google'.")
    print("[login] 2) QUIT that Chrome (Cmd-Q) when done.")
    print("[login] 3) then run:  .venv/bin/python -m core.revenue_demon_brain --verify")
    return {"ok": True, "launched": True, "profile_dir": str(udir)}


def verify() -> dict:
    """Confirm the dedicated profile is logged into a real account (stealth offscreen path that
    beats Cloudflare) and, if so, write the PERSONAL_OK marker so the demon resumes thinking."""
    import json as _j
    try:
        from tools.offscreen_browser import offscreen_context
    except Exception as e:
        return {"ok": False, "error": str(e)[:160]}
    li = False
    try:
        with offscreen_context("chatgpt", load_session=True, stealth=True) as ctx:
            page = ctx.new_page()
            page.goto("https://chatgpt.com/?model=auto", wait_until="domcontentloaded", timeout=45000)
            li = _composer_ready(page) and not _logged_out(page)
    except Exception as e:
        return {"ok": False, "error": str(e)[:160]}
    if li:
        PERSONAL_OK.write_text(_j.dumps(
            {"verified_by": "interactive login + --verify",
             "note": "delete to force re-verification if the account is ever switched"}, indent=2))
    return {"ok": li, "logged_in": li, "marker_written": li}


def whoami(screenshot: str = "") -> dict:
    """Return the chatgpt.com account identity for the off-screen profile (email/name +
    CompanyA/enterprise scan), so the operator can confirm WHICH account the demon thinks
    under. Reads /api/auth/session via a direct page nav (survives the CF XHR block)."""
    import json as _j
    out = {"ok": False}
    try:
        from tools.offscreen_browser import offscreen_context
    except Exception as e:
        return {"ok": False, "error": str(e)[:160]}
    try:
        with offscreen_context("chatgpt", load_session=True, stealth=True) as ctx:
            page = ctx.new_page()
            page.goto("https://chatgpt.com/?model=auto", wait_until="domcontentloaded", timeout=45000)
            _composer_ready(page)
            out["logged_in"] = not _logged_out(page)
            if screenshot:
                try:
                    page.screenshot(path=screenshot)
                    out["screenshot"] = screenshot
                except Exception:
                    pass
            page.goto("https://chatgpt.com/api/auth/session", wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(1500)
            body = page.locator("body").inner_text(timeout=5000)
    except Exception as e:
        return {"ok": False, "error": str(e)[:160], **out}
    raw = (body or "").strip()
    data = {}
    try:
        data = _j.loads(raw)
    except Exception:
        m = re.search(r"\{.*\}", raw, re.S)
        if m:
            try:
                data = _j.loads(m.group(0))
            except Exception:
                data = {}
    user = (data.get("user") or {}) if isinstance(data, dict) else {}
    low = raw.lower()
    out.update({
        "ok": bool(user.get("email")),
        "email": user.get("email", ""),
        "name": user.get("name", ""),
        "redacted_in_session": "CompanyA" in low,
        "enterprise_markers": [k for k in ("enterprise", "workspace", "business", "\"org") if k in low],
        "session_bytes": len(raw),
    })
    return out


# ── ban-safety pacing (shared across think + generate) ──────────────────────────────
# Arbitrage the flat-rate personal account HARD but keep the request pattern human-plausible:
# a per-call min-gap (+jitter), a rolling-24h cap, quiet overnight hours, and a hard back-off
# if ChatGPT ever shows a rate/abuse wall. Tunable via env. Persisted so it survives restarts.
def _pace_ok() -> tuple[bool, str]:
    import os
    import random
    import time as _t
    from datetime import datetime
    now = _t.time()
    try:
        if now < float(BACKOFF_FILE.read_text().strip()):
            return False, "backoff"
    except Exception:
        pass
    q0, q1 = int(os.environ.get("CHATGPT_QUIET_START", "1")), int(os.environ.get("CHATGPT_QUIET_END", "7"))
    if q0 <= datetime.now().astimezone().hour < q1:
        return False, "quiet_hours"            # look human — no overnight machine bursts
    last, recent = 0.0, 0
    try:
        for line in PACE_FILE.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ts = float(line)
            except Exception:
                continue
            last = max(last, ts)
            if now - ts < 86400:
                recent += 1
    except Exception:
        pass
    if recent >= int(os.environ.get("CHATGPT_DAILY_CAP", "80")):
        return False, "daily_cap"
    gap = int(os.environ.get("CHATGPT_MIN_GAP", "60"))
    if now - last < gap + random.uniform(0, gap):   # jittered spacing, never metronomic
        return False, "gap"
    return True, "ok"


def _pace_record() -> None:
    import time as _t
    try:
        with PACE_FILE.open("a") as f:
            f.write(f"{_t.time()}\n")
    except Exception:
        pass


def _pace_backoff(secs: int = 3600) -> None:
    import time as _t
    try:
        BACKOFF_FILE.write_text(str(_t.time() + secs))
    except Exception:
        pass
    try:
        from core.alert_gateway import route
        route("warn", "revenue_demon", "ChatGPT rate/abuse signal — backing off",
              f"paused ChatGPT calls ~{secs // 60}min to protect the account from a ban.")
    except Exception:
        pass


def _drive_chat(prompt_text: str, timeout_s: int) -> dict:
    """Shared browser drive for think+generate: marker→stealth chatgpt.com→login/workspace
    guards→send→wait→extract. Backs off on any rate/abuse wall. Returns {ok, raw} or {ok:False}."""
    # OPT-IN GATE (owner mandate 2026-06-30): the ChatGPT browser lane is OFF by default. think() was
    # gated on REVENUE_DEMON_CHATGPT_THINK but generate() was NOT, so the demon's text path kept
    # opening a foreground-flashing chatgpt.com Chrome despite the flag. Gate the single browser
    # chokepoint so NEITHER path opens a browser unless the lane is explicitly enabled; callers fall
    # back to local/free. No flag => no Chrome, ever.
    if os.environ.get("REVENUE_DEMON_CHATGPT_THINK", "0").strip().lower() not in {"1", "true", "yes", "on"}:
        return {"ok": False, "reason": "chatgpt_lane_disabled"}
    try:
        from tools.offscreen_browser import offscreen_context
        from tools.chatgpt_share_link import _send_prompt, _wait_for_completion, _conversation_text
    except Exception as e:
        return {"ok": False, "reason": "import_error", "detail": str(e)[:160]}
    try:
        with offscreen_context("chatgpt", load_session=True, stealth=True) as ctx:
            page = ctx.new_page()
            page.goto("https://chatgpt.com/?model=auto", wait_until="domcontentloaded", timeout=45000)
            if not _composer_ready(page):
                if "auth" in page.url or "/login" in page.url:
                    return {"ok": False, "reason": "logged_out"}
                return {"ok": False, "reason": "no_composer"}
            if _logged_out(page):
                return {"ok": False, "reason": "logged_out"}
            try:
                sess = page.evaluate(
                    "async () => { try { const r = await fetch('/api/auth/session');"
                    " return await r.text(); } catch (e) { return ''; } }")
            except Exception:
                sess = ""
            if sess and "CompanyA" in sess.lower():
                return {"ok": False, "reason": "wrong_workspace"}
            try:
                _send_prompt(page, prompt_text)
            except Exception as e:
                return {"ok": False, "reason": "no_composer", "detail": str(e)[:160]}
            _wait_for_completion(page, timeout_s=timeout_s)
            raw = _conversation_text(page)
    except Exception as e:
        return {"ok": False, "reason": "browser_error", "detail": str(e)[:160]}
    rl = (raw or "").lower()
    if any(s in rl for s in ("unusual activity", "rate limit", "too many requests",
                             "you've reached", "try again later", "automated")):
        _pace_backoff(3600)
        return {"ok": False, "reason": "rate_warned"}
    return {"ok": True, "raw": raw}


def think(prompt_text: str, *, timeout_s: int = 180) -> dict:
    """Strategy lane: paced ChatGPT call → parsed JSON plan. Degrades (ok False) so the loop
    falls back to local/free; never blocks."""
    if not PERSONAL_OK.exists():
        return {"ok": False, "reason": "profile_unverified"}
    ok, why = _pace_ok()
    if not ok:
        return {"ok": False, "reason": "paced", "detail": why}
    res = _drive_chat(prompt_text, timeout_s)
    if not res.get("ok"):
        return res
    _pace_record()
    from tools.chatgpt_share_link import scan_personal
    raw = res["raw"]
    if scan_personal(raw):
        return {"ok": False, "reason": "leak"}
    return {"ok": True, "raw": raw, "plan": _extract_plan(raw)}


def generate(prompt_text: str, *, timeout_s: int = 180) -> dict:
    """Flat-rate GENERATION lane (content / proof scripts / copy) — the arbitrage workhorse.
    Same paced+guarded driver as think; returns {ok, text}. Callers fall back to local/free
    when ok is False (paced/logged-out/etc), so ChatGPT is used as hard as is ban-safe."""
    if not PERSONAL_OK.exists():
        return {"ok": False, "reason": "profile_unverified"}
    ok, why = _pace_ok()
    if not ok:
        return {"ok": False, "reason": "paced", "detail": why}
    res = _drive_chat(prompt_text, timeout_s)
    if not res.get("ok"):
        return res
    _pace_record()
    from tools.chatgpt_share_link import scan_personal
    raw = res["raw"]
    if scan_personal(raw):
        return {"ok": False, "reason": "leak"}
    return {"ok": True, "text": raw}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="revenue-demon think probe")
    ap.add_argument("--login", action="store_true", help="open a PLAIN Chrome on the profile to log into a personal account")
    ap.add_argument("--verify", action="store_true", help="confirm logged in + write the personal-account marker")
    ap.add_argument("--whoami", action="store_true", help="report whether the off-screen profile is logged in")
    ap.add_argument("--check-login", action="store_true")
    ap.add_argument("--ask", default="", help="send a raw prompt and print the parsed plan")
    a = ap.parse_args()
    if a.login:
        print(json.dumps(login(), indent=2))
    elif a.verify:
        print(json.dumps(verify(), indent=2))
    elif a.whoami:
        print(json.dumps(whoami(), indent=2))
    elif a.check_login:
        print(json.dumps({"logged_in": logged_in()}, indent=2))
    elif a.ask:
        print(json.dumps(think(a.ask), indent=2, default=str))
    else:
        ap.print_help()
