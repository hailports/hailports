#!/usr/bin/env python3
"""tenant_browser.py — per-tenant ISOLATED browser contexts.

The browser analog of tenant_secrets: every tenant's captured session lives under
data/tenants/<tenant>/sessions/<account>/auth.json, and this launches a browser that
loads ONLY that tenant+account session — never the operator's .chrome-cdp-profile-*,
never another tenant's data. Each launch is a fresh browser seeded from just that one
auth.json, so there is no cross-tenant or operator leakage by construction. Headless /
off-screen by default (the automation rail); the one-time interactive login-capture is
the only headed step (that lives in onboarding_wizard.capture_session).

    from playwright.sync_api import sync_playwright
    from tools.tenant_browser import launch_tenant
    with sync_playwright() as p:
        browser, ctx = launch_tenant(p, "Operator2", "slack")   # headless, isolated
        page = ctx.new_page(); ...
        browser.close()
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import tenant_secrets

_OFFSCREEN = ["--window-position=-32000,-32000", "--window-size=10,10"]


def has_session(tenant_id: str, account: str) -> bool:
    return tenant_secrets.session_path(tenant_id, account).exists()


def launch_tenant(p, tenant_id: str, account: str, *, headless: bool = True):
    """Return (browser, context) isolated to (tenant_id, account), seeded from that
    tenant's captured auth.json. Fresh browser per call — no shared/operator profile.
    Caller is responsible for browser.close(). `p` is a started sync_playwright()."""
    if not tenant_secrets.tenant_id_ok(tenant_id):
        raise ValueError(f"bad tenant_id: {tenant_id!r}")
    sp = tenant_secrets.session_path(tenant_id, account)
    # isolation guard: the session path must resolve inside this tenant's own dir
    tdir = str(tenant_secrets.tenant_dir(tenant_id).resolve())
    if not str(sp.resolve()).startswith(tdir):
        raise RuntimeError("isolation violation: session path escapes tenant dir")
    args = ["--disable-blink-features=AutomationControlled"]
    if not headless:
        args += _OFFSCREEN
    browser = p.chromium.launch(headless=headless, args=args)
    ctx = browser.new_context(storage_state=str(sp) if sp.exists() else None)
    return browser, ctx


if __name__ == "__main__":
    # smoke: prove isolation guard + path wiring without launching a browser
    t, a = "Operator2", "slack"
    print("session path:", tenant_secrets.session_path(t, a))
    print("has_session:", has_session(t, a))
    try:
        tenant_secrets.tenant_id_ok("../evil") or print("tenant_id_ok rejects traversal: ok")
    except Exception as e:
        print("guard err", e)
