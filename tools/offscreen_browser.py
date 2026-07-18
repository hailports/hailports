#!/usr/bin/env python3
"""Off-screen, focus-safe Chrome launcher — one dedicated persistent profile per
marketplace, on its own debug port, rendered OFF the visible screen so listings
publish themselves without ever appearing or stealing the owner's focus.

Why off-screen HEADFUL (not pure --headless): Etsy/PromptBase/Whop run bot
detection that trips on the headless UA/feature fingerprint (same lesson the OWA
work-bridge learned — CA blocks headless, so we run headful but off-screen). We
position a real rendered window far outside every display (--window-position
-32000,-32000) and never activate/raise it, so it draws nothing visible and
takes no focus. A --headless+stealth path is available for environments that
tolerate it.

This module is the shared launcher used by the harvester and the publisher.
It NEVER uses port 18806 (reserved for another live session) or the X/work
bridge ports — each marketplace gets its own dedicated port + profile.

CLI (smoke test, opens then closes an off-screen window):
    python3 -m tools.offscreen_browser --site whop --smoke
"""
from __future__ import annotations

import argparse
import contextlib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.vpn_egress import chrome_proxy_args

# Far enough off-screen that no physical or virtual display contains it.
OFFSCREEN_POS = "-32000,-32000"
WINDOW_SIZE = "1440,900"

CHROME_BIN = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

# Dedicated debug ports — deliberately clear of 18800-18820 (X / work bridges)
# and 18806 (reserved live publish run).
MARKETPLACES: dict[str, dict] = {
    "whop": {
        "port": 18861,
        "profile": ".chrome-cdp-profile-whop",
        "login_url": "https://whop.com/login",
        "home_url": "https://whop.com/dashboard",
        # selector / url-substring proving we are logged in (publisher refines)
        "logged_in_url_has": "dashboard",
        "logged_out_url_has": "login",
    },
    "promptbase": {
        "port": 18862,
        "profile": ".chrome-cdp-profile-promptbase",
        "login_url": "https://promptbase.com/login",
        "home_url": "https://promptbase.com/dashboard",
        "logged_in_url_has": "dashboard",
        "logged_out_url_has": "login",
    },
    "etsy": {
        "port": 18863,
        "profile": ".chrome-cdp-profile-etsy",
        "login_url": "https://www.etsy.com/signin",
        "home_url": "https://www.etsy.com/your/shops/me/dashboard",
        "logged_in_url_has": "/your/shops",
        "logged_out_url_has": "signin",
    },
}

# Auxiliary off-screen profiles that are NOT publish targets but support the
# marketplace flows: the owner's PERSONAL ChatGPT (used to mint PromptBase
# verification share-links from a clean chat) and his EXISTING KYC'd Stripe
# account (reused as the payout method — never a fresh KYC). Each gets its own
# dedicated profile + port, clear of 18806 (gumroad cover-upload run) and the
# X/work bridges.
AUX_PROFILES: dict[str, dict] = {
    "chatgpt": {
        "port": 18864,
        "profile": ".chrome-cdp-profile-chatgpt",
        "login_url": "https://chatgpt.com/auth/login",
        "home_url": "https://chatgpt.com/",
        # logged-out ChatGPT bounces to /auth/login
        "logged_in_url_has": "chatgpt.com",
        "logged_out_url_has": "auth",
    },
    "stripe": {
        "port": 18865,
        "profile": ".chrome-cdp-profile-stripe",
        "login_url": "https://dashboard.stripe.com/login",
        "home_url": "https://dashboard.stripe.com/",
        "logged_in_url_has": "dashboard.stripe.com",
        "logged_out_url_has": "login",
    },
}

# Everything the launcher/harvester can drive (publish targets + aux profiles).
ALL_PROFILES: dict[str, dict] = {**MARKETPLACES, **AUX_PROFILES}

# Where persisted storage_state lives (see marketplace_session_harvest).
SESSION_ROOT = ROOT / "data" / "marketplace_sessions"


def marketplace(site: str) -> dict:
    """Resolve a profile spec by name across publish targets + aux profiles."""
    site = site.lower()
    if site not in ALL_PROFILES:
        raise KeyError(f"unknown profile {site!r}; known: {', '.join(ALL_PROFILES)}")
    return ALL_PROFILES[site]


def profile_dir(site: str) -> Path:
    return ROOT / marketplace(site)["profile"]


def _chrome_args(port: int, headless: bool, stealth: bool) -> list[str]:
    args = [
        f"--remote-debugging-port={port}",
        f"--window-position={OFFSCREEN_POS}",
        f"--window-size={WINDOW_SIZE}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-dev-shm-usage",
        # keep the window from ever coming forward / stealing focus
        "--disable-features=CalculateNativeWinOcclusion",
    ]
    if stealth:
        # mask the most common automation fingerprints
        args += [
            "--disable-blink-features=AutomationControlled",
            "--disable-features=IsolateOrigins,site-per-process",
        ]
    return args


def _cdp_port_up(port: int) -> bool:
    import urllib.request
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2)
        return True
    except Exception:
        return False


def _wait_cdp(port: int, secs: int = 40) -> bool:
    import time
    for _ in range(secs):
        if _cdp_port_up(port):
            return True
        time.sleep(1)
    return False


@contextlib.contextmanager
def offscreen_context(site: str, headless: bool = False, stealth: bool = False,
                      load_session: bool = True):
    """Yield a Playwright persistent BrowserContext for `site`, rendered
    off-screen. Headful by default (bot-safe). If a persisted storage_state
    exists and load_session is True, its cookies are injected so the run is
    already authenticated.

    The persistent user-data-dir is the source of truth for the login; the
    storage_state file is a portable backup/seed.
    """
    from playwright.sync_api import sync_playwright

    meta = marketplace(site)
    udir = profile_dir(site)
    udir.mkdir(parents=True, exist_ok=True)

    # FOREGROUND RULE (HARD, owner mandate 2026-06-30): a headful Chrome flashes the foreground on
    # launch even parked at -32000, so automations run FULLY headless. Override the caller's request
    # to headless+stealth unless the owner explicitly opts a run into headful for a specific
    # bot-detection target via OFFSCREEN_ALLOW_HEADFUL=1.
    if os.environ.get("OFFSCREEN_ALLOW_HEADFUL", "0").strip().lower() not in {"1", "true", "yes", "on"}:
        headless = True
        stealth = True

    def _persistent_headless(p):
        # No visible window needed -> plain headless persistent context: renders in
        # memory, never paints to a display, never foregrounds. Default autonomous path.
        return p.chromium.launch_persistent_context(
            str(udir),
            headless=True,
            executable_path=CHROME_BIN if Path(CHROME_BIN).exists() else None,
            args=_chrome_args(meta["port"], True, stealth) + chrome_proxy_args(str(udir)),
            viewport={"width": 1440, "height": 900},
            no_viewport=False,
        )

    with sync_playwright() as p:
        browser = None  # set only when we drive a REAL off-screen window over CDP
        port = int(meta["port"])
        if headless:
            ctx = _persistent_headless(p)
        else:
            # Opt-in headful (OFFSCREEN_ALLOW_HEADFUL=1): the automation NEEDS a real
            # rendered window (bot-detection / Cloudflare / file inputs). We STILL must
            # never jack the screen, so we do NOT let Playwright exec the binary (which
            # macOS activates). Instead spawn Chrome via core.chrome_launch
            # .launch_headful_offscreen (`open -gjn` — never activates; parked off-screen)
            # and drive it over CDP. The automation keeps a real painted window; it just
            # never comes to the foreground. If the owner is at the keyboard the helper
            # defers (ForegroundBusy) and we fall back to headless so the run STILL
            # HAPPENS (just without a window) rather than jacking focus or dying.
            from core.chrome_launch import launch_headful_offscreen, ForegroundBusy
            cmd = [CHROME_BIN, f"--user-data-dir={udir}",
                   *_chrome_args(port, False, stealth), *chrome_proxy_args(str(udir))]
            try:
                if not _cdp_port_up(port):
                    launch_headful_offscreen(cmd, port=port)
                    if not _wait_cdp(port, secs=40):
                        raise RuntimeError(f"off-screen CDP port {port} never came up")
                browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
                ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            except ForegroundBusy:
                browser = None
                ctx = _persistent_headless(p)
        if load_session:
            cookies = _session_cookies(site)
            if cookies:
                with contextlib.suppress(Exception):
                    ctx.add_cookies(cookies)
        try:
            yield ctx
        finally:
            with contextlib.suppress(Exception):
                if browser is not None:
                    browser.close()
                else:
                    ctx.close()
            if browser is not None:
                # CDP disconnect leaves the off-screen Chrome running — terminate the
                # one we spawned so it doesn't linger between runs.
                import subprocess
                with contextlib.suppress(Exception):
                    subprocess.run(["pkill", "-9", "-f", f"remote-debugging-port={port}"], check=False)


def _session_cookies(site: str) -> list[dict]:
    import json
    f = SESSION_ROOT / site / "auth.json"
    if not f.exists():
        return []
    with contextlib.suppress(Exception):
        return json.loads(f.read_text()).get("cookies", [])
    return []


def _smoke(site: str, headless: bool, stealth: bool) -> int:
    """Open an off-screen window, hit the home url, print the resulting URL,
    close. Proves the launcher renders without appearing on screen."""
    meta = marketplace(site)
    with offscreen_context(site, headless=headless, stealth=stealth) as ctx:
        page = ctx.new_page()
        page.goto(meta["home_url"], wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(1500)
        print(f"[offscreen:{site}] rendered off-screen, landed at: {page.url}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Off-screen marketplace Chrome launcher")
    ap.add_argument("--site", required=True, choices=sorted(ALL_PROFILES))
    ap.add_argument("--headless", action="store_true", help="headless+stealth (only where bot-detection tolerates it)")
    ap.add_argument("--stealth", action="store_true", help="apply automation-fingerprint masking")
    ap.add_argument("--smoke", action="store_true", help="open/close an off-screen window as a test")
    a = ap.parse_args()
    if a.smoke:
        return _smoke(a.site, a.headless, a.stealth or a.headless)
    print("nothing to do; pass --smoke for a launch test")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
