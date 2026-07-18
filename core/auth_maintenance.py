#!/usr/bin/env python3
"""Shared auth/session maintenance for local autonomous agents.

This module is the single place agents should use to answer:
- can this provider token be refreshed silently?
- is this browser session still logged in?
- does this platform need a human login because of 2FA/challenge/captcha?

It does not bypass login challenges. It refreshes tokens and cookies when the
platform supports it, then writes durable status for the rest of the stack.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - dotenv is optional in tiny installs
    load_dotenv = None


BASE = Path(os.environ.get("CLAUDE_STACK_DIR") or (Path.home() / "claude-stack"))
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))
if load_dotenv:
    load_dotenv(BASE / ".env", override=False)

HUSTLE = BASE / "data" / "hustle"
RUNTIME = BASE / "data" / "runtime"
SESSION_DIR = HUSTLE / "browser_sessions"
STATE_FILE = HUSTLE / "auth_maintenance_state.json"
SESSION_STATUS_FILE = HUSTLE / "session_status.json"
TOKEN_DIR = BASE / "data" / "tokens"

log = logging.getLogger("auth-maintenance")

KEY_COOKIE_RE = re.compile(
    r"(auth|access|refresh|session|sid|token|oauth|ct0|auth_token|ds_user_id|"
    r"sessionid|c_user|xs|li_at|SAPISID|SSID|HSID)",
    re.I,
)
CHALLENGE_RE = re.compile(
    r"(captcha|challenge|verify you are|verification required|suspicious activity|"
    r"two-factor|2fa|passkey|security code)",
    re.I,
)
# "Loaded but can't ACT" signals — the class the read-only verify missed: cookies
# present + the verify page loads (no login redirect), but the account is write-locked,
# suspended, or access-gated, i.e. dead FOR POSTING. Matched ONLY against the final URL
# and page <title>, never the feed body — a tweet in the timeline can literally say
# "suspended", so body-matching here would false-trip a healthy session. A healthy home
# load stays on /home with title "Home / X" etc., so this can only flip false-INs to OUT.
RESTRICTED_URL_RE = re.compile(
    r"(/account/access|/account/suspended|/suspended|/login/challenge|/i/flow/single_sign_on)",
    re.I,
)
RESTRICTED_TITLE_RE = re.compile(
    r"(account suspended|account locked|account restricted|your account is (?:suspended|locked|restricted))",
    re.I,
)

BROWSER_PLATFORMS: dict[str, dict[str, Any]] = {
    "tiktok": {
        "label": "TikTok persona1",
        "auth_file": SESSION_DIR / "tiktok" / "auth.json",
        "verify_url": "https://www.tiktok.com/messages",
        "login_url": "https://www.tiktok.com/login",
        "login_markers": ("login", "signup"),
        "auto_login": False,
    },
    # instagram — DROPPED 2026-06-20 (Operator: "we don't use it"). Removed from the warmer,
    # watchdog, and reauth tool so it's no longer tracked/flagged. Re-add this block to revive.
    "twitter_persona1": {
        "label": "X persona1",
        "auth_file": SESSION_DIR / "twitter_persona1" / "auth.json",
        "verify_url": "https://x.com/home",
        "login_url": "https://x.com/i/flow/login",
        "login_markers": ("flow/login", "/login"),
        "auto_login": True,
        "username_env": ("X_USERNAME", "TWITTER_USERNAME"),
        "password_env": ("X_PASSWORD", "TWITTER_PASSWORD"),
    },
    "twitter": {
        "label": "X docsapp",
        "auth_file": SESSION_DIR / "twitter" / "auth.json",
        "verify_url": "https://x.com/home",
        "login_url": "https://x.com/i/flow/login",
        "login_markers": ("flow/login", "/login"),
        "auto_login": True,
        "username_env": ("X_USERNAME", "TWITTER_USERNAME"),
        "password_env": ("X_PASSWORD", "TWITTER_PASSWORD"),
    },
    "youtube": {
        "label": "YouTube Studio",
        "auth_file": SESSION_DIR / "youtube" / "auth.json",
        "verify_url": "https://studio.youtube.com",
        "login_url": "https://accounts.google.com/",
        "login_markers": ("accounts.google.com", "signin", "ServiceLogin"),
        "auto_login": False,
    },
    "linkedin": {
        "label": "LinkedIn",
        "auth_file": SESSION_DIR / "linkedin" / "auth.json",
        "verify_url": "https://www.linkedin.com/feed/",
        "login_url": "https://www.linkedin.com/login",
        "login_markers": ("login", "checkpoint"),
        "auto_login": False,
    },
    "reddit": {
        "label": "Reddit",
        "auth_file": SESSION_DIR / "reddit" / "auth.json",
        "verify_url": "https://www.reddit.com/",
        "login_url": "https://www.reddit.com/login/",
        "login_markers": ("login", "register"),
        "auto_login": False,
    },
    "gumroad": {
        "label": "Gumroad",
        "auth_file": SESSION_DIR / "gumroad" / "auth.json",
        "verify_url": "https://gumroad.com/products",
        "login_url": "https://gumroad.com/login",
        "login_markers": ("login", "signin"),
        "auto_login": False,
    },
    "substack": {
        "label": "Substack",
        "auth_file": SESSION_DIR / "substack" / "auth.json",
        "verify_url": "https://substack.com/home",
        "login_url": "https://substack.com/sign-in",
        "login_markers": ("sign-in", "login"),
        "auto_login": False,
    },
    "quora": {
        "label": "Quora",
        "auth_file": SESSION_DIR / "quora" / "auth.json",
        "verify_url": "https://www.quora.com/",
        "login_url": "https://www.quora.com/",
        "login_markers": ("login", "signup"),
        "auto_login": False,
    },
}

OAUTH_PROVIDERS: dict[str, dict[str, Any]] = {
    "salesforce": {
        "label": "Salesforce",
        "env_all": ("SALESFORCE_CONSUMER_KEY", "SALESFORCE_USERNAME"),
        "kind": "jwt_bearer",
    },
    "microsoft": {
        "label": "Microsoft 365",
        "env_all": ("MICROSOFT_CLIENT_ID",),
        "kind": "refresh_token",
        "needs_refresh_token": True,
        "reauth_hint": "Open /credentials and start Microsoft device-code reauth.",
    },
    "google": {
        "label": "Google",
        "env_all": ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"),
        "kind": "refresh_token",
        "needs_refresh_token": True,
        "reauth_hint": "Run the Google OAuth setup flow to store a refresh_token.",
    },
    "zoom": {
        "label": "Zoom",
        "env_all": ("ZOOM_ACCOUNT_ID", "ZOOM_CLIENT_ID", "ZOOM_CLIENT_SECRET"),
        "kind": "server_to_server",
    },
    "slack": {
        "label": "Slack",
        "env_any": ("SLACK_BOT_TOKEN", "SLACK_CLIENT_ID"),
        "kind": "bot_or_refresh_token",
    },
}


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path, fallback: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text())
    except Exception:
        pass
    return fallback


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str))
    tmp.replace(path)


def _env_value(names: tuple[str, ...] | list[str] | str | None) -> str:
    if not names:
        return ""
    if isinstance(names, str):
        names = (names,)
    for name in names:
        value = os.environ.get(str(name), "").strip()
        if value:
            return value
    return ""


def _configured_from_env(names: tuple[str, ...] | list[str] | str | None) -> bool:
    if not names:
        return False
    if isinstance(names, str):
        names = (names,)
    return any(os.environ.get(str(name), "").strip() for name in names)


def _configured_for_provider(cfg: dict[str, Any]) -> bool:
    env_all = cfg.get("env_all")
    if env_all:
        if isinstance(env_all, str):
            env_all = (env_all,)
        return all(os.environ.get(str(name), "").strip() for name in env_all)
    return _configured_from_env(cfg.get("env_any"))


def _safe_storage_state(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    data = _read_json(path, {})
    if isinstance(data, list):
        return [c for c in data if isinstance(c, dict)], None
    if isinstance(data, dict):
        cookies = data.get("cookies")
        if isinstance(cookies, list):
            return [c for c in cookies if isinstance(c, dict)], data
    return [], None


def cookie_expiry(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "auth_file": str(path),
            "exists": False,
            "cookie_count": 0,
            "status": "missing",
        }
    cookies, _ = _safe_storage_state(path)
    now_s = time.time()
    all_expiries: list[float] = []
    key_expiries: list[float] = []
    for cookie in cookies:
        try:
            expires = float(cookie.get("expires") or 0)
        except Exception:
            expires = 0
        if expires <= 0:
            continue
        all_expiries.append(expires)
        if KEY_COOKIE_RE.search(str(cookie.get("name") or "")):
            key_expiries.append(expires)
    picked = key_expiries or all_expiries
    if not picked:
        return {
            "auth_file": str(path),
            "exists": True,
            "cookie_count": len(cookies),
            "status": "session_only",
            "expires_in_s": None,
            "expires_at": None,
        }
    min_exp = min(picked)
    return {
        "auth_file": str(path),
        "exists": True,
        "cookie_count": len(cookies),
        "status": "expired" if min_exp <= now_s else "ok",
        "expires_in_s": round(min_exp - now_s, 1),
        "expires_at": min_exp,
    }


def _human_login_command(platform: str) -> str:
    return (
        f"cd {BASE} && .venv/bin/python tools/import_social_session.py "
        f"{platform} --interactive-login --sync-mini"
    )


def _state_with_updates(
    *,
    oauth: dict[str, Any] | None = None,
    browser_sessions: dict[str, Any] | None = None,
) -> dict[str, Any]:
    state = _read_json(STATE_FILE, {})
    if not isinstance(state, dict):
        state = {}
    state.setdefault("oauth", {})
    state.setdefault("browser_sessions", {})
    if oauth:
        state["oauth"].update(oauth)
    if browser_sessions:
        state["browser_sessions"].update(browser_sessions)
    # prune rows for platforms no longer tracked (e.g. instagram, dropped 2026-06-20) so a stale
    # 'dead session' can't linger forever after a platform leaves BROWSER_PLATFORMS.
    state["browser_sessions"] = {
        k: v for k, v in state["browser_sessions"].items() if k in BROWSER_PLATFORMS
    }
    state["generated_at"] = _now_iso()
    state["summary"] = _summarize_state(state)
    _write_json(STATE_FILE, state)
    _write_session_status_compat(state)
    return state


def _summarize_state(state: dict[str, Any]) -> dict[str, Any]:
    oauth_rows = state.get("oauth") if isinstance(state.get("oauth"), dict) else {}
    browser_rows = state.get("browser_sessions") if isinstance(state.get("browser_sessions"), dict) else {}
    all_rows = list(oauth_rows.values()) + list(browser_rows.values())
    configured = [r for r in all_rows if r.get("configured") is not False]
    ok = [r for r in configured if r.get("ok")]
    human = [r for r in configured if r.get("human_required")]
    broken = [
        r for r in configured
        if not r.get("ok") and str(r.get("status") or "") not in {"unconfigured", "missing"}
    ]
    missing = [r for r in configured if str(r.get("status") or "") == "missing"]
    return {
        "ok_count": len(ok),
        "configured_count": len(configured),
        "broken_count": len(broken),
        "missing_count": len(missing),
        "human_required_count": len(human),
        "browser_ok_count": sum(1 for r in browser_rows.values() if r.get("ok")),
        "browser_total": len(browser_rows),
        "oauth_ok_count": sum(1 for r in oauth_rows.values() if r.get("ok")),
        "oauth_total": len(oauth_rows),
    }


def _write_session_status_compat(state: dict[str, Any]) -> None:
    rows = {}
    for platform, row in (state.get("browser_sessions") or {}).items():
        rows[platform] = {
            "status": row.get("status"),
            "ok": row.get("ok"),
            "checked_at": row.get("checked_at"),
            "human_required": row.get("human_required", False),
            "detail": row.get("hint") or row.get("url") or "",
        }
    _write_json(SESSION_STATUS_FILE, rows)


def status_snapshot() -> dict[str, Any]:
    state = _read_json(STATE_FILE, {})
    if not isinstance(state, dict):
        state = {}
    state.setdefault("oauth", {})
    state.setdefault("browser_sessions", {})
    state["summary"] = _summarize_state(state)
    return state


def browser_session_ok(platform: str, max_age_s: int = 24 * 3600) -> bool:
    row = (status_snapshot().get("browser_sessions") or {}).get(platform) or {}
    if not row.get("ok"):
        return False
    try:
        checked = float(row.get("checked_at_s") or 0)
    except Exception:
        checked = 0
    return checked > 0 and time.time() - checked <= max_age_s


def browser_auth_blockers(max_age_s: int = 24 * 3600) -> dict[str, str]:
    blocked: dict[str, str] = {}
    for platform, row in (status_snapshot().get("browser_sessions") or {}).items():
        try:
            checked = float(row.get("checked_at_s") or 0)
        except Exception:
            checked = 0
        if checked and time.time() - checked > max_age_s:
            continue
        if row.get("ok"):
            continue
        status = str(row.get("status") or "")
        if status in {"auth_blocked", "challenge", "expired", "error", "login_failed", "restricted"}:
            blocked[platform] = str(row.get("hint") or status)
    return blocked


async def _new_context(playwright, auth_file: Path, *, headless: bool):
    browser = await playwright.chromium.launch(
        headless=headless,
        args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
    )
    context_kwargs = {
        "viewport": {"width": 1280, "height": 900},
        "locale": "en-US",
        "timezone_id": os.environ.get("TZ", "America/Chicago"),
        "user_agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        ),
    }
    cookies, storage = _safe_storage_state(auth_file)
    if storage is not None:
        context_kwargs["storage_state"] = str(auth_file)
        context = await browser.new_context(**context_kwargs)
    else:
        context = await browser.new_context(**context_kwargs)
        if cookies:
            await context.add_cookies(cookies)
    return browser, context


def _login_blocked(cfg: dict[str, Any], url: str, body: str) -> bool:
    low_url = str(url or "").lower()
    low_body = str(body or "").lower()
    markers = tuple(str(m).lower() for m in cfg.get("login_markers") or ())
    if any(marker and marker in low_url for marker in markers):
        return True
    if any(token in low_body for token in ("log in", "sign in")) and any(
        token in low_body for token in ("sign up", "create account", "password", "username", "email or phone", "join today")
    ):
        return True
    if "happening now" in low_body and "join today" in low_body:
        return True
    return False


async def verify_browser_session(
    platform: str,
    *,
    headless: bool = True,
    save_refreshed: bool = True,
    timeout_ms: int = 45000,
) -> dict[str, Any]:
    cfg = BROWSER_PLATFORMS[platform]
    auth_file = Path(cfg["auth_file"])
    expiry = cookie_expiry(auth_file)
    row: dict[str, Any] = {
        "platform": platform,
        "label": cfg.get("label") or platform,
        "kind": "browser_session",
        "configured": auth_file.exists(),
        "ok": False,
        "status": "missing" if not auth_file.exists() else "unchecked",
        "auth_file": str(auth_file),
        "checked_at": _now_iso(),
        "checked_at_s": time.time(),
        "expires": expiry,
        "human_required": False,
        "reauth_command": _human_login_command(platform),
    }
    if not auth_file.exists():
        row.update({
            "hint": f"Missing browser session at {auth_file}",
            "human_required": True,
        })
        return row

    try:
        from playwright.async_api import async_playwright
        from core.browser_pool import browser_lock
    except Exception as exc:
        row.update({"status": "error", "hint": f"Playwright unavailable: {exc}"})
        return row

    try:
        with browser_lock("auth_maintenance", max_wait_s=600, stale_after_s=900):
            async with async_playwright() as pw:
                browser, ctx = await _new_context(pw, auth_file, headless=headless)
                page = await ctx.new_page()
                try:
                    await page.goto(str(cfg["verify_url"]), wait_until="domcontentloaded", timeout=timeout_ms)
                    await page.wait_for_timeout(2500)
                    url = page.url
                    title = await page.title()
                    body = ""
                    try:
                        body = (await page.locator("body").inner_text(timeout=2500))[:1000]
                    except Exception:
                        pass
                    row.update({"url": url, "title": title, "body_hint": body[:260]})
                    if CHALLENGE_RE.search(f"{url}\n{title}\n{body}"):
                        row.update({
                            "status": "challenge",
                            "hint": "Platform challenge, captcha, passkey, or 2FA prompt detected",
                            "human_required": True,
                        })
                    elif RESTRICTED_URL_RE.search(str(url)) or RESTRICTED_TITLE_RE.search(str(title)):
                        row.update({
                            "status": "restricted",
                            "hint": f"Loaded but account is write-locked / suspended / access-gated (dead for posting): {url}",
                            "human_required": True,
                        })
                    elif _login_blocked(cfg, url, body):
                        row.update({
                            "status": "auth_blocked",
                            "hint": f"Redirected to login or unauthenticated page: {url}",
                            "human_required": True,
                        })
                    else:
                        row.update({
                            "ok": True,
                            "status": "ok",
                            "hint": "session verified and refreshed",
                            "human_required": False,
                        })
                        if save_refreshed:
                            auth_file.parent.mkdir(parents=True, exist_ok=True)
                            await ctx.storage_state(path=str(auth_file))
                            row["expires"] = cookie_expiry(auth_file)
                finally:
                    await ctx.close()
                    await browser.close()
    except Exception as exc:
        row.update({
            "status": "error",
            "hint": f"{type(exc).__name__}: {str(exc)[:220]}",
        })
    return row


async def attempt_browser_login(platform: str, *, headless: bool = True) -> dict[str, Any]:
    cfg = BROWSER_PLATFORMS[platform]
    auth_file = Path(cfg["auth_file"])
    row = {
        "platform": platform,
        "label": cfg.get("label") or platform,
        "kind": "browser_session",
        "configured": True,
        "ok": False,
        "status": "human_required",
        "checked_at": _now_iso(),
        "checked_at_s": time.time(),
        "auth_file": str(auth_file),
        "human_required": True,
        "reauth_command": _human_login_command(platform),
    }
    if not cfg.get("auto_login"):
        row["hint"] = "Auto-login is not supported for this platform; use interactive login."
        return row
    username = _env_value(cfg.get("username_env"))
    password = _env_value(cfg.get("password_env"))
    if not username or not password:
        row["hint"] = "No username/password env configured; use interactive login."
        return row

    try:
        from playwright.async_api import async_playwright
        from core.browser_pool import browser_lock
    except Exception as exc:
        row.update({"status": "error", "hint": f"Playwright unavailable: {exc}"})
        return row

    try:
        with browser_lock("auth_maintenance_login", max_wait_s=600, stale_after_s=900):
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(
                    headless=headless,
                    args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
                )
                ctx = await browser.new_context(
                    viewport={"width": 1280, "height": 900},
                    locale="en-US",
                    timezone_id=os.environ.get("TZ", "America/Chicago"),
                )
                page = await ctx.new_page()
                try:
                    await page.goto(str(cfg["login_url"]), wait_until="domcontentloaded", timeout=45000)
                    await page.wait_for_timeout(2500)
                    if platform == "instagram":
                        await _instagram_login(page, username, password)
                    elif platform in {"twitter", "twitter_persona1"}:
                        await _x_login(page, username, password)
                    else:
                        row["hint"] = "No login driver for this platform."
                        return row
                    await page.wait_for_timeout(6000)
                    body = ""
                    try:
                        body = (await page.locator("body").inner_text(timeout=2500))[:1000]
                    except Exception:
                        pass
                    url = page.url
                    if CHALLENGE_RE.search(f"{url}\n{body}"):
                        row.update({
                            "status": "challenge",
                            "hint": "Login reached challenge/2FA; human login required.",
                        })
                    elif _login_blocked(cfg, url, body):
                        row.update({
                            "status": "login_failed",
                            "hint": f"Login did not reach an authenticated page: {url}",
                        })
                    else:
                        auth_file.parent.mkdir(parents=True, exist_ok=True)
                        await ctx.storage_state(path=str(auth_file))
                        row.update({
                            "ok": True,
                            "status": "ok",
                            "hint": "credential login refreshed browser session",
                            "human_required": False,
                            "url": url,
                            "expires": cookie_expiry(auth_file),
                        })
                finally:
                    await ctx.close()
                    await browser.close()
    except Exception as exc:
        row.update({"status": "error", "hint": f"{type(exc).__name__}: {str(exc)[:220]}"})
    return row


async def _instagram_login(page, username: str, password: str) -> None:
    user = await page.query_selector('input[name="username"]')
    pwd = await page.query_selector('input[name="password"]')
    if not user or not pwd:
        raise RuntimeError("Instagram login form not found")
    await user.fill(username)
    await page.wait_for_timeout(500)
    await pwd.fill(password)
    await page.wait_for_timeout(500)
    button = await page.query_selector('button[type="submit"]')
    if button:
        await button.click()
    else:
        await page.keyboard.press("Enter")


async def _x_login(page, username: str, password: str) -> None:
    user = await page.query_selector('input[autocomplete="username"]')
    if not user:
        user = await page.query_selector('input[name="text"]')
    if not user:
        raise RuntimeError("X username input not found")
    await user.fill(username)
    await page.wait_for_timeout(500)
    await page.keyboard.press("Enter")
    await page.wait_for_timeout(3000)
    pwd = await page.query_selector('input[type="password"]')
    if not pwd:
        raise RuntimeError("X password input not found; extra verification may be required")
    await pwd.fill(password)
    await page.wait_for_timeout(500)
    await page.keyboard.press("Enter")


async def maintain_browser_sessions(
    platforms: list[str] | None = None,
    *,
    attempt_login: bool = True,
    headless: bool = True,
) -> dict[str, Any]:
    selected = platforms or list(BROWSER_PLATFORMS)
    rows: dict[str, Any] = {}
    for platform in selected:
        if platform not in BROWSER_PLATFORMS:
            rows[platform] = {
                "platform": platform,
                "ok": False,
                "status": "unknown_platform",
                "hint": "No browser session config exists for this platform.",
                "checked_at": _now_iso(),
                "checked_at_s": time.time(),
            }
            continue
        row = await verify_browser_session(platform, headless=headless)
        if (
            attempt_login
            and not row.get("ok")
            and row.get("status") in {"auth_blocked", "expired", "missing"}
            and BROWSER_PLATFORMS[platform].get("auto_login")
        ):
            login_row = await attempt_browser_login(platform, headless=headless)
            if login_row.get("ok"):
                row = await verify_browser_session(platform, headless=headless)
            else:
                row = login_row
        rows[platform] = row
        _state_with_updates(browser_sessions={platform: row})
    state = _state_with_updates(browser_sessions=rows)
    return {
        "ok": True,
        "generated_at": state.get("generated_at"),
        "summary": state.get("summary") or {},
        "browser_sessions": rows,
    }


def _stored_token(provider: str) -> dict[str, Any] | None:
    try:
        from auth import token_store

        return token_store.load(provider)
    except Exception:
        path = TOKEN_DIR / f"{provider}.json"
        data = _read_json(path, None)
        return data if isinstance(data, dict) else None


async def refresh_oauth_provider(provider: str, *, timeout_s: int = 35) -> dict[str, Any]:
    cfg = OAUTH_PROVIDERS[provider]
    configured = _configured_for_provider(cfg)
    stored = _stored_token(provider) or {}
    row: dict[str, Any] = {
        "provider": provider,
        "label": cfg.get("label") or provider,
        "kind": "oauth",
        "auth_kind": cfg.get("kind"),
        "configured": configured,
        "ok": False,
        "status": "unconfigured" if not configured else "unchecked",
        "checked_at": _now_iso(),
        "checked_at_s": time.time(),
        "human_required": False,
    }
    if not configured:
        row["hint"] = "Required env vars are not configured."
        return row
    if cfg.get("needs_refresh_token") and not stored.get("refresh_token"):
        row.update({
            "status": "human_required",
            "hint": cfg.get("reauth_hint") or "Initial OAuth setup is required.",
            "human_required": True,
        })
        return row
    if provider == "slack" and not os.environ.get("SLACK_BOT_TOKEN") and not stored.get("refresh_token"):
        row.update({
            "status": "human_required",
            "hint": "Set SLACK_BOT_TOKEN or store a Slack refresh_token.",
            "human_required": True,
        })
        return row

    try:
        from auth.oauth_manager import OAuthManager

        manager = OAuthManager()
        token = await asyncio.wait_for(manager.get_token(provider), timeout=timeout_s)
        stored_after = _stored_token(provider) or {}
        expires_at = stored_after.get("expires_at")
        expires_in = None
        try:
            expires_in = float(expires_at) - time.time() if expires_at else None
        except Exception:
            expires_in = None
        row.update({
            "ok": bool(token),
            "status": "ok" if token else "empty_token",
            "hint": "token refreshed" if token else "refresh returned empty token",
            "expires_at": expires_at,
            "expires_in_s": round(expires_in, 1) if expires_in is not None else None,
        })
        if expires_in is not None and expires_in <= 0:
            row.update({
                "ok": False,
                "status": "expired",
                "hint": "token refresh returned an expired token",
            })
    except Exception as exc:
        stale = bool(stored.get("access_token"))
        row.update({
            "ok": False,
            "status": "refresh_failed",
            "hint": f"{type(exc).__name__}: {str(exc)[:220]}",
            "has_stale_token": stale,
            "human_required": provider in {"microsoft", "google", "slack"},
        })
    return row


async def maintain_oauth(providers: list[str] | None = None) -> dict[str, Any]:
    selected = providers or list(OAUTH_PROVIDERS)
    rows: dict[str, Any] = {}
    for provider in selected:
        if provider not in OAUTH_PROVIDERS:
            rows[provider] = {
                "provider": provider,
                "ok": False,
                "status": "unknown_provider",
                "hint": "No OAuth maintenance config exists for this provider.",
                "checked_at": _now_iso(),
                "checked_at_s": time.time(),
            }
            continue
        row = await refresh_oauth_provider(provider)
        rows[provider] = row
        _state_with_updates(oauth={provider: row})
    state = _state_with_updates(oauth=rows)
    return {
        "ok": True,
        "generated_at": state.get("generated_at"),
        "summary": state.get("summary") or {},
        "oauth": rows,
    }


async def maintain_all(
    *,
    include_oauth: bool = True,
    include_browser: bool = True,
    platforms: list[str] | None = None,
    providers: list[str] | None = None,
    attempt_login: bool = True,
    headless: bool = True,
) -> dict[str, Any]:
    oauth_result: dict[str, Any] = {}
    browser_result: dict[str, Any] = {}
    if include_oauth:
        oauth_result = await maintain_oauth(providers)
    if include_browser:
        browser_result = await maintain_browser_sessions(
            platforms,
            attempt_login=attempt_login,
            headless=headless,
        )
    state = status_snapshot()
    return {
        "ok": True,
        "generated_at": state.get("generated_at"),
        "summary": state.get("summary") or {},
        "oauth": oauth_result.get("oauth") or state.get("oauth") or {},
        "browser_sessions": browser_result.get("browser_sessions") or state.get("browser_sessions") or {},
        "state_file": str(STATE_FILE),
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Refresh API tokens and browser sessions.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--all", action="store_true", help="Run OAuth and browser maintenance.")
    group.add_argument("--oauth", action="store_true", help="Run OAuth/token maintenance only.")
    group.add_argument("--browser", action="store_true", help="Run browser session maintenance only.")
    group.add_argument("--status", action="store_true", help="Print last known state only.")
    parser.add_argument("--platform", action="append", choices=sorted(BROWSER_PLATFORMS), default=[])
    parser.add_argument("--provider", action="append", choices=sorted(OAUTH_PROVIDERS), default=[])
    parser.add_argument("--no-login", action="store_true", help="Do not try credential-based browser login.")
    parser.add_argument("--headful", action="store_true", help="Show browser windows while verifying.")
    return parser


async def _main_async(args: argparse.Namespace) -> int:
    if args.status:
        result = status_snapshot()
    else:
        include_oauth = args.all or args.oauth or not args.browser
        include_browser = args.all or args.browser or not args.oauth
        result = await maintain_all(
            include_oauth=include_oauth,
            include_browser=include_browser,
            providers=args.provider or None,
            platforms=args.platform or None,
            attempt_login=not args.no_login,
            headless=not args.headful,
        )
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    summary = result.get("summary") or {}
    return 0 if int(summary.get("broken_count") or 0) == 0 else 1


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = _build_parser()
    args = parser.parse_args()
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
