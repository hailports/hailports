#!/usr/bin/env python3
"""Co-browse engine — provider-agnostic remote login capture.

The robust answer to federated/varied logins (Operator 2026-07-16): instead of the machine
typing the password (fragile per-provider), we run a REAL browser on the mini, stream it to
the person's device via CDP screencast, forward their taps/keystrokes back, and let THEM
complete their own login natively — any provider, any federation (GoDaddy/Okta/Azure/Google),
any 2FA. On success we capture storage_state to the tenant vault. No selectors to chase.

A CobrowseSession holds a live Chrome (real channel, headless — no window on the mini) + a
CDP session. Screencast frames go out via on_frame; input comes in via input_*; try_capture
detects a logged-in state and saves the session. Mobile viewport (the person is on a phone).
"""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any, Callable, Optional

ROOT = Path(__file__).resolve().parent.parent

_STEALTH_ARGS = []  # patchright handles stealth; extra flags are themselves tells
_UA_MOBILE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
              "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")
VIEW_W, VIEW_H = 500, 900   # coherent DESKTOP viewport (no mobile-signal straddle — DataDome fix)

# success markers (reuse the relay's intent): logged into mail / past the login
_SUCCESS = ("inbox", "mail.google.com/mail", "outlook.office.com/mail", "/owa",
            "office.com/", "myaccount.google.com", "signed in")


_OPEN_LEDGER = {"ts": 0.0, "count": 0, "win": 0.0}
_MIN_INTERVAL_S = 90
_MAX_PER_WINDOW = 3
_WINDOW_S = 1200


class CobrowseSession:
    def __init__(self):
        self.pw = None
        self.browser = None
        self.context = None
        self.page = None
        self.cdp = None
        self.ts = 0.0
        self._closed = False

    async def open(self, login_url: str, on_frame: Callable[[str], Any], *,
                   headless: Optional[bool] = None) -> bool:
        # RUBRIC (2) — always headless: this session never paints a window on the mini by
        # design (see module docstring, "headless — no window on the mini"); the remote
        # viewer sees the CDP screencast, not an OS window, so headless Chrome is sufficient
        # and behaves identically for screencast/input purposes. `headless` kwarg kept for API
        # compat but no longer honored — no-foreground-steal invariant
        # (tools/check_no_headful_steal.py) requires a provably-headless literal here.
        _now = time.monotonic()
        if _now - _OPEN_LEDGER["win"] > _WINDOW_S:
            _OPEN_LEDGER["win"] = _now; _OPEN_LEDGER["count"] = 0
        if _now - _OPEN_LEDGER["ts"] < _MIN_INTERVAL_S or _OPEN_LEDGER["count"] >= _MAX_PER_WINDOW:
            self._cooling = True
            return False   # throttle: avoid re-training DataDome velocity flag
        _OPEN_LEDGER["ts"] = _now; _OPEN_LEDGER["count"] += 1
        from patchright.async_api import async_playwright
        proxy = None
        _purl = os.environ.get("COBROWSE_PROXY", "").strip()
        if _purl:
            from urllib.parse import urlparse as _up
            _u = _up(_purl)
            proxy = {"server": f"{_u.scheme}://{_u.hostname}:{_u.port}"}
            if _u.username: proxy["username"] = _u.username
            if _u.password: proxy["password"] = _u.password

        self.pw = await async_playwright().start()
        launched = False
        try:
            self.context = await self.pw.chromium.launch_persistent_context(
                str(ROOT / "data" / "tenants" / "_cobrowse" / f"cb-{int(time.time()*1000)%100000}"),
                channel="chrome", headless=True, args=_STEALTH_ARGS, proxy=proxy,
                viewport={"width": VIEW_W, "height": VIEW_H},
                locale="en-US")
            self.browser = self.context.browser
            self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
            launched = True
        except Exception:
            self.browser = await self.pw.chromium.launch(headless=True, args=_STEALTH_ARGS, proxy=proxy)
            self.context = await self.browser.new_context(
                viewport={"width": VIEW_W, "height": VIEW_H},
                locale="en-US")
            self.page = await self.context.new_page()
            launched = True
        if not launched:
            return False
        try:
            await self.context.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        except Exception:
            pass
        self.cdp = await self.context.new_cdp_session(self.page)

        async def _on_screencast(params):
            try:
                on_frame(params.get("data", ""))
                await self.cdp.send("Page.screencastFrameAck", {"sessionId": params.get("sessionId")})
            except Exception:
                pass

        def _frame_cb(params):
            asyncio.ensure_future(_on_screencast(params))

        self.cdp.on("Page.screencastFrame", _frame_cb)

        async def _restart_cast():
            # CDP screencast pauses/stops across navigations (login -> redirect -> mailbox);
            # re-issue it so the stream never freezes mid-login.
            try:
                await asyncio.sleep(0.6)
                await self.cdp.send("Page.startScreencast",
                                    {"format": "jpeg", "quality": 60, "maxWidth": VIEW_W, "maxHeight": VIEW_H})
            except Exception:
                pass

        self.page.on("framenavigated", lambda fr: (fr == self.page.main_frame) and asyncio.ensure_future(_restart_cast()))
        try:
            await self.page.goto(login_url, wait_until="domcontentloaded", timeout=45000)
        except Exception:
            pass
        await self.cdp.send("Page.startScreencast",
                            {"format": "jpeg", "quality": 60, "maxWidth": VIEW_W, "maxHeight": VIEW_H})
        self.ts = time.monotonic()
        return True

    async def input_mouse(self, kind: str, x: float, y: float, button: str = "left") -> None:
        try:
            await self.cdp.send("Input.dispatchMouseEvent", {
                "type": kind, "x": float(x), "y": float(y),
                "button": button, "clickCount": 1 if kind in ("mousePressed", "mouseReleased") else 0})
        except Exception:
            pass

    async def input_scroll(self, x: float, y: float, dy: float) -> None:
        try:
            await self.cdp.send("Input.dispatchMouseEvent",
                                {"type": "mouseWheel", "x": float(x), "y": float(y),
                                 "deltaX": 0, "deltaY": float(dy)})
        except Exception:
            pass

    async def input_text(self, text: str) -> None:
        try:
            await self.cdp.send("Input.insertText", {"text": text})
        except Exception:
            pass

    async def input_key(self, key: str) -> None:
        # special keys (Enter/Backspace/Tab) via keyDown/keyUp
        code_map = {"Enter": ("Enter", 13), "Backspace": ("Backspace", 8), "Tab": ("Tab", 9)}
        k, kc = code_map.get(key, (key, 0))
        try:
            for t in ("keyDown", "keyUp"):
                await self.cdp.send("Input.dispatchKeyEvent",
                                    {"type": t, "key": k, "code": k, "windowsVirtualKeyCode": kc})
        except Exception:
            pass

    async def try_capture(self, tenant: str = "", account: str = "") -> bool:
        """If the page looks logged-in, save storage_state to the tenant vault."""
        try:
            url = (self.page.url or "").lower()
            body = ""
            try:
                body = (await self.page.inner_text("body"))[:4000].lower()
            except Exception:
                pass
            hay = url + "\n" + body
            if not any(m in hay for m in _SUCCESS):
                return False
            if "password" in body and "sign" in body:
                return False  # still on a login form
            from tools import tenant_secrets
            auth = tenant_secrets.session_path(tenant, account) if (tenant and account) else \
                (ROOT / "data" / "tenants" / "_cobrowse" / "last.auth.json")
            auth.parent.mkdir(parents=True, exist_ok=True)
            await self.context.storage_state(path=str(auth))
            try:
                os.chmod(auth, 0o600)
            except Exception:
                pass
            return auth.exists() and auth.stat().st_size > 100
        except Exception:
            return False

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for obj, meth in ((self.cdp, None), (self.context, "close"), (self.browser, "close"), (self.pw, "stop")):
            try:
                if obj is None:
                    continue
                if meth == "close":
                    await obj.close()
                elif meth == "stop":
                    await obj.stop()
            except Exception:
                pass
