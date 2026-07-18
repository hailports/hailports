#!/usr/bin/env python3
"""reauth_social.py — dead-simple, one-command re-auth for the faceless social accounts.

    tools/reauth_social.py x            # re-login the persona1 X / twitter account
    tools/reauth_social.py reddit       # re-login the persona1 Reddit account
    tools/reauth_social.py instagram    # re-login the persona1 Instagram account
    tools/reauth_social.py youtube      # re-consent the AI Built Fast YouTube channel

    tools/reauth_social.py <p> --check  # just report IN/OUT, no window (used by the table)
    tools/reauth_social.py --check-all  # IN/OUT table for every platform at once

What it does for x / reddit / instagram:
  * opens a VISIBLE chromium (you're watching the mini over Screens) on that
    platform's login page, using the SAME canonical auth path the posting agents
    read (single source of truth = core.auth_maintenance.BROWSER_PLATFORMS), so a
    fresh login can never drift away from where the poster looks.
  * waits (generous timeout) for you to finish login + 2FA, polling for the
    logged-in DOM state.
  * on success: saves the fresh storage_state to the canonical auth.json and
    re-verifies headless. Idempotent — if you don't finish, the existing good
    session is left untouched (never clobbered).

YouTube uses a Google OAuth refresh token (not a browser cookie), so `youtube`
runs the loopback consent flow (scripts/youtube_auth.py) instead.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

BASE = Path(os.environ.get("CLAUDE_STACK_DIR") or (Path.home() / "claude-stack"))
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from core.chrome_launch import require_interactive_headful
from core.vpn_egress import chrome_proxy_args


def _load_env() -> None:
    env = BASE / ".env"
    try:
        from dotenv import load_dotenv

        load_dotenv(env, override=False)
        return
    except Exception:
        pass
    try:
        for ln in env.read_text().splitlines():
            if "=" in ln and not ln.lstrip().startswith("#"):
                k, _, v = ln.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except Exception:
        pass


# friendly cli name -> canonical auth_maintenance platform key
ALIASES = {
    "x": "twitter_persona1",
    "twitter": "twitter_persona1",
    "twitter_persona1": "twitter_persona1",
    "reddit": "reddit",
    "tiktok": "tiktok",
    "youtube": "youtube",
    "yt": "youtube",
}

YT_BRAND = os.environ.get("IMA_YT_BRAND", "redacted")

# canonical auth_maintenance platform key -> (totp_platform, totp_account) for keychain lookup.
# The totp_platform/account is what tools/totp_enroll.py enrolls a seed under. Env-overridable
# per platform via REAUTH_TOTP_<PLATFORMKEY>="platform:account".
TOTP_ACCOUNTS = {
    "twitter_persona1": ("x", "persona1"),
    "reddit": ("reddit", "hailportss"),
    "tiktok": ("tiktok", "hailportshq"),
    "youtube": ("youtube", YT_BRAND),
}


def _totp_account(platform_key: str) -> tuple[str, str] | None:
    """(totp_platform, totp_account) for a platform's 2FA keychain seed, or None."""
    ov = os.environ.get(f"REAUTH_TOTP_{platform_key.upper()}")
    if ov and ":" in ov:
        pf, acct = ov.split(":", 1)
        return (pf.strip(), acct.strip())
    return TOTP_ACCOUNTS.get(platform_key)


def _totp_code(platform_key: str) -> str | None:
    """Live 6-digit code if a seed is enrolled for this platform's account, else None."""
    acct = _totp_account(platform_key)
    if not acct:
        return None
    try:
        from core import totp_store

        return totp_store.current_code(acct[0], acct[1])
    except Exception:
        return None


# ---------------------------------------------------------------- youtube (oauth)
def _yt_token_live() -> tuple[bool, str]:
    """Probe whether the YouTube refresh token can still mint an access token."""
    _load_env()
    rt = os.environ.get(f"YT_REFRESH_TOKEN_{YT_BRAND.upper()}")
    cid = os.environ.get("YT_CLIENT_ID") or os.environ.get("GOOGLE_CLIENT_ID")
    cs = os.environ.get("YT_CLIENT_SECRET") or os.environ.get("GOOGLE_CLIENT_SECRET")
    if not rt:
        return False, f"YT_REFRESH_TOKEN_{YT_BRAND.upper()} not set"
    if not cid or not cs:
        return False, "YT client id/secret not set"
    data = urllib.parse.urlencode(
        {"client_id": cid, "client_secret": cs, "refresh_token": rt, "grant_type": "refresh_token"}
    ).encode()
    try:
        req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data)
        with urllib.request.urlopen(req, timeout=20) as r:
            tok = json.loads(r.read())
        return ("access_token" in tok), ("token refreshes ok" if "access_token" in tok else str(tok)[:160])
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:160]}"


def reauth_youtube(check_only: bool) -> int:
    live, detail = _yt_token_live()
    print(f"{'IN ' if live else 'OUT'}  youtube ({YT_BRAND}) — {detail}")
    if check_only:
        return 0 if live else 1
    if live:
        print("✅ youtube already authed — refresh token is healthy, nothing to do.")
        return 0
    script = BASE / "scripts" / "youtube_auth.py"
    if not script.exists():
        print(f"⚠️  {script} missing — cannot run YouTube consent flow.")
        return 1
    print(f"\nopening YouTube consent for brand '{YT_BRAND}'.")
    print(f"a browser will open — sign in to the channel owner, then on 'Select a channel'")
    print(f"pick the one matching '{YT_BRAND}'.\n")
    rc = subprocess.call([sys.executable, str(script), YT_BRAND], cwd=str(BASE))
    if rc == 0:
        live, detail = _yt_token_live()
        if live:
            print("✅ youtube re-authed")
            return 0
        print(f"⚠️  consent finished but token still not minting: {detail}")
        return 1
    print(f"⚠️  youtube consent flow exited {rc}")
    return rc


# ---------------------------------------------------------------- browser platforms
def _cfg(platform_key: str) -> dict:
    from core import auth_maintenance

    return auth_maintenance.BROWSER_PLATFORMS[platform_key]


async def _verify(platform_key: str) -> dict:
    from core import auth_maintenance

    return await auth_maintenance.verify_browser_session(
        platform_key, headless=True, save_refreshed=True
    )


def _print_status_row(platform_key: str, row: dict) -> bool:
    ok = bool(row.get("ok"))
    print(f"{'IN ' if ok else 'OUT'}  {platform_key} — {row.get('hint') or row.get('status')}")
    return ok


def _login_markers(cfg: dict) -> tuple[str, ...]:
    return tuple(str(m).lower() for m in (cfg.get("login_markers") or ()))


def _account_creds(platform_key: str):
    """(username, password) for a platform's account from registered_accounts.json, or None.
    Which brand-account maps to each platform is overridable via env (e.g. REAUTH_REDDIT_ACCOUNT)."""
    want = {
        "reddit": os.environ.get("REAUTH_REDDIT_ACCOUNT", "reddit:hailports"),
        "twitter_persona1": os.environ.get("REAUTH_X_ACCOUNT", "twitter:persona1"),
    }.get(platform_key)
    if not want:
        return None
    try:
        p = Path(os.path.expanduser("~/claude-stack/data/hustle/registered_accounts.json"))
        data = json.loads(p.read_text())
        rows = data if isinstance(data, list) else data.get("accounts", [])
        for a in rows:
            if str(a.get("platform", "")).strip().lower() == want.lower():
                u, pw = (a.get("username") or a.get("email")), a.get("password")
                if u and pw:
                    return (u, pw)
    except Exception:
        pass
    return None


async def _fill_first(page, sels, val) -> bool:
    for s in sels:
        try:
            el = page.locator(s).first
            if await el.count() and await el.is_visible():
                await el.click()
                await el.fill(val)
                return True
        except Exception:
            continue
    return False


async def _submit(page) -> None:
    """Best-effort submit: click a login/submit button, else press Enter on the active field."""
    for s in ['button[type="submit"]', 'button[data-testid="LoginForm_Login_Button"]',
              'button:has-text("Log in")', 'button:has-text("Sign in")',
              'button:has-text("Verify")', 'button:has-text("Next")', 'button:has-text("Continue")']:
        try:
            el = page.locator(s).first
            if await el.count() and await el.is_visible():
                await el.click()
                return
        except Exception:
            continue
    try:
        await page.keyboard.press("Enter")
    except Exception:
        pass


async def _prefill_login(page, platform_key: str) -> bool:
    """Fill username+password. Returns True iff both fields were filled."""
    creds = _account_creds(platform_key)
    if not creds:
        return False
    user, pw = creds
    await page.wait_for_timeout(2800)  # let the (often shadow-DOM) form mount

    u = await _fill_first(page, ['input[name="username"]', '#login-username', 'input#loginUsername',
                     'faceplate-text-input[name="username"] input', 'input[autocomplete="username"]',
                     'input[name="text"]', 'input[name="email"]', 'input[autocomplete="email"]'], user)
    p = await _fill_first(page, ['input[name="password"]', '#login-password', 'input#loginPassword',
                     'faceplate-text-input[name="password"] input', 'input[type="password"]'], pw)
    if u and p:
        print(f"  ✅ prefilled {platform_key} username+password")
    else:
        print(f"  ⚠️  couldn't auto-fill ({platform_key}: user={u} pw={p}) — log in manually (creds in chat)")
    return bool(u and p)


_OTP_SELECTORS = (
    'input[autocomplete="one-time-code"]', 'input[name="otp"]', 'input[name="totp"]',
    'input[name="verificationCode"]', 'input[name="challenge_response"]',
    'input[name="text"][inputmode="numeric"]', 'input[inputmode="numeric"]',
    'input[name="code"]', 'input[id="totpPin"]', 'input[name="2fa"]',
    'input[aria-label*="code" i]', 'input[placeholder*="code" i]', 'input[type="tel"]',
)


async def _auto_2fa(page, platform_key: str) -> bool:
    """Zero-touch 2FA: submit prefilled creds, wait for the OTP field to mount, fill the live
    TOTP code from the keychain seed, submit. Returns True if a code was filled + submitted.

    Only runs when a seed exists (caller checks). Some platforms show the OTP field only after
    the username/password submit, so we submit first, then poll for the field."""
    code = _totp_code(platform_key)
    if not code:
        return False
    # submit the prefilled creds so the 2FA step can appear (X splits user/password across pages,
    # so we submit, re-fill any freshly-shown password field, submit again, THEN look for OTP).
    await _submit(page)
    await page.wait_for_timeout(3500)
    creds = _account_creds(platform_key)
    if creds:
        if await _fill_first(page, ['input[name="password"]', 'input[type="password"]'], creds[1]):
            await _submit(page)
            await page.wait_for_timeout(3000)

    deadline = time.time() + 28
    while time.time() < deadline:
        # refresh the code each attempt — a slow mount can cross the 30s TOTP window boundary
        code = _totp_code(platform_key) or code
        if await _fill_first(page, list(_OTP_SELECTORS), code):
            await _submit(page)
            print(f"  ✅ {platform_key}: auto-filled + submitted the 2FA code (zero-touch)")
            return True
        await page.wait_for_timeout(2000)
    print(f"  ⚠️  {platform_key}: seed present but no OTP field appeared in time")
    return False


# ─────────────────────────────────────── per-account landing (reddit-style pools)
# Some lanes read PER-ACCOUNT session files, not the shared canonical auth.json reauth writes.
# reddit_warmer posts from data/hustle/browser_sessions/reddit/<handle>/auth.json and marks a
# dead one with flag_reason=="session" in reddit_warmer_state.json. After a fresh capture we must
# land the cookies in the per-account file (preserving its `username`) AND clear the session flag
# so the warmer retries it. Generalized so any pooled lane can register a landing here.
PER_ACCOUNT_LANDING = {
    # canonical platform key -> (pool_subdir, warmer_state_file relative to data/hustle)
    "reddit": ("reddit", "reddit_warmer_state.json"),
}


def _land_per_account(platform_key: str, account: str, auth_file: Path) -> None:
    land = PER_ACCOUNT_LANDING.get(platform_key)
    if not land or not account:
        return
    pool_sub, state_rel = land
    try:
        fresh = json.loads(auth_file.read_text())
    except Exception as e:
        print(f"   per-account landing skipped: can't read fresh cookies ({e})")
        return
    dest = BASE / "data" / "hustle" / "browser_sessions" / pool_sub / account / "auth.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    # preserve the per-account file's own `username` field (the warmer keys on it)
    existing = {}
    try:
        existing = json.loads(dest.read_text()) if dest.exists() else {}
    except Exception:
        existing = {}
    merged = {"cookies": fresh.get("cookies", fresh if isinstance(fresh, list) else [])}
    if isinstance(fresh, dict) and fresh.get("origins"):
        merged["origins"] = fresh["origins"]
    if isinstance(existing, dict) and existing.get("username"):
        merged["username"] = existing["username"]
    else:
        merged["username"] = account
    tmp = dest.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(merged, indent=2))
    tmp.replace(dest)
    print(f"   landed fresh cookies -> {dest} (username={merged['username']})")
    # clear the session flag so the warmer retries this account
    state_file = BASE / "data" / "hustle" / state_rel
    try:
        st = json.loads(state_file.read_text())
        rec = (st.get("accounts") or {}).get(account)
        if isinstance(rec, dict) and str(rec.get("flag_reason", "")).lower() == "session":
            rec.pop("flag_reason", None)
            rec["flagged"] = False
            state_file.write_text(json.dumps(st, indent=2))
            print(f"   cleared session flag for {account} in {state_file.name} — warmer will retry")
    except Exception as e:
        print(f"   flag-clear skipped: {e}")


async def reauth_browser(platform_key: str, check_only: bool, sync_mini: bool, timeout_s: int,
                         force: bool = False, headful: bool = False, land_account: str | None = None) -> int:
    cfg = _cfg(platform_key)
    auth_file = Path(cfg["auth_file"])

    if check_only:
        row = await _verify(platform_key)
        return 0 if _print_status_row(platform_key, row) else 1

    # current state first — tell Operator if it's already fine
    pre = await _verify(platform_key)
    if pre.get("ok") and not force:
        print(f"✅ {platform_key} already authed — session is live, nothing to do.")
        print("   (if posting is actually FAILING, this check is stale — re-run with --force to log in anyway.)")
        return 0
    if force and pre.get("ok"):
        print(f"{platform_key}: shallow check says authed, but --force given — opening the login window anyway.")

    # ZERO-TOUCH PATH: a 2FA seed exists -> run HEADLESS end-to-end (prefill creds + auto-TOTP +
    # submit + verify), no window, no human. No seed -> fall back to the visible human window.
    seed_present = _totp_code(platform_key) is not None
    headless = seed_present and not headful
    if seed_present:
        print(f"\n{platform_key}: session is {pre.get('status')} — 2FA seed enrolled, running "
              f"{'HEADLESS zero-touch' if headless else 'headful'} auto re-auth (no human needed).")
    else:
        print(f"\n{platform_key}: session is {pre.get('status')} — no 2FA seed; opening a visible browser to log in.")
    print(f"  login page : {cfg['login_url']}")
    print(f"  saves to   : {auth_file}")
    if not seed_present:
        print(f"  do your thing (incl. 2FA). i'll detect when you're in (up to {timeout_s//60} min).")
    print()

    from playwright.async_api import async_playwright

    # persistent profile so 2FA / device-trust survives between attempts. Headless when a seed
    # drives it end-to-end; a visible window (shown on the Screens session) when a human is needed.
    profile_dir = auth_file.parent / "reauth_profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    markers = _login_markers(cfg)
    verify_url = str(cfg["verify_url"])
    login_url = str(cfg["login_url"])

    if not headless:
        require_interactive_headful(f"{platform_key} re-auth login")

    logged_in = False
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(  # headful-ok
            str(profile_dir),
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"]
                 + ([] if headless else ["--start-maximized"])
                 + chrome_proxy_args(str(profile_dir)),
            viewport=None,
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
            ),
        )
        # seed any existing cookies so a half-alive session resumes quickly
        try:
            data = json.loads(auth_file.read_text()) if auth_file.exists() else {}
            cookies = data.get("cookies") if isinstance(data, dict) else data
            if cookies:
                await ctx.add_cookies(cookies)
        except Exception:
            pass

        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        try:
            await page.goto(login_url, wait_until="domcontentloaded", timeout=45000)
        except Exception:
            pass

        # PREFILL username+password. With a seed we also auto-submit + auto-clear 2FA (hands-off);
        # without a seed we never submit — the human reviews + taps login + does 2FA.
        try:
            filled = await _prefill_login(page, platform_key)
            if seed_present and filled:
                try:
                    await _auto_2fa(page, platform_key)
                except Exception as _e:
                    print(f"  (auto-2FA errored: {str(_e)[:80]})")
        except Exception as _e:
            print(f"  (prefill skipped: {str(_e)[:60]} — log in manually, creds in chat)")

        # a seed-driven auto run needs only a short confirmation window (creds+2FA already sent);
        # a human window gets the full generous timeout.
        deadline = time.time() + (min(timeout_s, 90) if seed_present else timeout_s)
        while time.time() < deadline:
            await asyncio.sleep(3)
            try:
                url = (page.url or "").lower()
            except Exception:
                continue
            if not url or url == "about:blank":
                continue
            on_login = any(m and m in url for m in markers)
            if on_login:
                continue
            # off the login flow — confirm authenticated by loading the verify page
            try:
                await page.goto(verify_url, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(2000)
                vurl = (page.url or "").lower()
                body = ""
                try:
                    body = (await page.locator("body").inner_text(timeout=2500))[:1200].lower()
                except Exception:
                    pass
                if any(m and m in vurl for m in markers):
                    continue
                if ("log in" in body or "sign in" in body) and (
                    "create account" in body or "join today" in body or "sign up" in body
                ):
                    continue
                logged_in = True
                break
            except Exception:
                continue

        if logged_in:
            auth_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = auth_file.with_suffix(".json.tmp")
            await ctx.storage_state(path=str(tmp))
            tmp.replace(auth_file)
        await ctx.close()

    if not logged_in:
        print(f"⚠️  {platform_key}: didn't detect a completed login before timeout — "
              f"left the existing session untouched (not clobbered). re-run when ready.")
        return 1

    # authoritative headless re-verify (also re-saves canonical cookies)
    post = await _verify(platform_key)
    if not post.get("ok"):
        print(f"⚠️  {platform_key}: saved cookies but headless re-verify says "
              f"{post.get('status')} ({post.get('hint')}). try again.")
        return 1

    print(f"✅ {platform_key} re-authed — cookies saved to {auth_file} and verified logged-in.")
    # land the fresh cookies in the right per-account file + clear its session flag (reddit pool etc.)
    acct = land_account or (_totp_account(platform_key) or (None, None))[1]
    try:
        _land_per_account(platform_key, acct, auth_file)
    except Exception as e:
        print(f"   per-account landing skipped: {e}")
    if sync_mini:
        _sync_mini(platform_key, auth_file)
    return 0


def _sync_mini(platform_key: str, auth_file: Path) -> None:
    host = os.environ.get("CLAUDE_STACK_REMOTE", "user@10.0.0.1")
    remote = f"{host}:~/claude-stack/data/hustle/browser_sessions/{platform_key}/"
    try:
        subprocess.run(
            ["ssh", host, f"mkdir -p ~/claude-stack/data/hustle/browser_sessions/{platform_key}"],
            capture_output=True, text=True, timeout=30,
        )
        r = subprocess.run(["rsync", "-az", str(auth_file), remote], capture_output=True, text=True, timeout=60)
        print("   synced to mini" if r.returncode == 0 else f"   mini sync failed: {r.stderr[:120]}")
    except Exception as e:
        print(f"   mini sync error: {str(e)[:120]}")


# ---------------------------------------------------------------- cli
async def _check_all() -> int:
    print("platform IN/OUT (faceless social):\n")
    worst = 0
    for key in ("twitter_persona1", "reddit"):
        row = await _verify(key)
        if not _print_status_row(key, row):
            worst = 1
    live, detail = _yt_token_live()
    print(f"{'IN ' if live else 'OUT'}  youtube ({YT_BRAND}) — {detail}")
    if not live:
        worst = 1
    print("\nre-auth a dead one:  tools/reauth_social.py <x|reddit|youtube>")
    return worst


def main() -> int:
    _load_env()
    p = argparse.ArgumentParser(description="One-command re-auth for the faceless social accounts.")
    p.add_argument("platform", nargs="?", help="x | reddit | tiktok | youtube")
    p.add_argument("--check", action="store_true", help="report IN/OUT only, no window")
    p.add_argument("--check-all", action="store_true", help="IN/OUT table for every platform")
    p.add_argument("--sync-mini", action="store_true", help="rsync the refreshed auth.json to the mini")
    p.add_argument("--timeout", type=int, default=600, help="seconds to wait for login (default 600)")
    p.add_argument("--force", action="store_true",
                   help="open the login window even if the shallow check reports authed — the check "
                        "is unreliable (posting can be dead while it says live)")
    p.add_argument("--auto", action="store_true",
                   help="automated (non-interactive) trigger — use the enrolled TOTP seed if present, "
                        "run headless; falls back to the visible window when no seed exists")
    p.add_argument("--headful", action="store_true",
                   help="force a visible window even when a seed exists (debugging the auto flow)")
    p.add_argument("--land-account", help="per-account handle to land fresh cookies into (e.g. hailportss)")
    args = p.parse_args()

    if args.check_all or (not args.platform):
        if args.check_all or args.check:
            return asyncio.run(_check_all())
        p.print_help()
        return 2

    key = ALIASES.get(args.platform.lower())
    if not key:
        print(f"unknown platform '{args.platform}'. use: x | reddit | tiktok | youtube")
        return 2

    if key == "youtube":
        return reauth_youtube(args.check)
    return asyncio.run(reauth_browser(key, args.check, args.sync_mini, args.timeout,
                                      args.force, headful=args.headful, land_account=args.land_account))


if __name__ == "__main__":
    raise SystemExit(main())
