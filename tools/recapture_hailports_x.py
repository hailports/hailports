#!/usr/bin/env python3
"""Re-capture the @hailports X (case_study_x) session after X invalidated it during the lock.
Opens a VISIBLE Chrome to x.com — log in as @hailports (2FA if asked) — then it saves the fresh
cookies to data/hustle/browser_sessions/case_study_x/auth.json (what the engage agent + mirror read).
Run:  cd ~/claude-stack && .venv/bin/python tools/recapture_hailports_x.py
"""
import json, sys, time
from pathlib import Path
from playwright.sync_api import sync_playwright
from core import chrome_launch

ROOT = Path(__file__).resolve().parent.parent
DEST = ROOT / "data" / "hustle" / "browser_sessions" / "case_study_x" / "auth.json"
PROFILE = str(Path.home() / ".chrome-cdp-profile-x2")   # persistent so it survives next time

def _handle(page) -> str:
    try:
        el = page.query_selector('[data-testid="SideNav_AccountSwitcher_Button"]')
        if el:
            import re
            m = re.search(r"@([A-Za-z0-9_]+)", el.inner_text() or "")
            return (m.group(1) if m else "").lower()
    except Exception:
        pass
    return ""

def main() -> int:
    DEST.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        # non-persistent (fresh) context: avoids the "profile already in use" SingletonLock when
        # a stack CDP Chrome is holding -x2. You log in fresh once; we save the cookies.
        chrome_launch.require_interactive_headful("X/hailports login")
        br = p.chromium.launch(headless=False,  # headful-ok: interactive login
                               args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
        ctx = br.new_context(user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                             "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
        page = ctx.new_page()
        page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=30000)
        print("\n  >>> A Chrome window opened. LOG IN as @hailports (do 2FA if asked).")
        print("      Waiting up to 5 min for a logged-in session...\n")
        deadline = time.time() + 300
        saved = False
        while time.time() < deadline:
            time.sleep(4)
            try:
                state = ctx.storage_state()
            except Exception:
                continue
            has_auth = any(c["name"] == "auth_token" and "x.com" in c["domain"] for c in state["cookies"])
            if not has_auth:
                continue
            h = _handle(page)
            if h and "hailport" not in h:
                print(f"  ⚠️  logged in as @{h} — that's NOT @hailports. Switch accounts to @hailports, then wait.")
                continue
            DEST.write_text(json.dumps({"cookies": state["cookies"]}))
            print(f"  ✓ saved {len(state['cookies'])}-cookie @hailports session -> {DEST.relative_to(ROOT)}"
                  + (f" (handle @{h})" if h else ""))
            saved = True
            break
        ctx.close()
        if not saved:
            print("  ✗ timed out — no @hailports login captured. Re-run and finish the login.")
            return 1
    print("\n  Done. The engage agent + mirror will pick up the fresh session on their next run"
          " (or tell Claude to kick them now).")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
