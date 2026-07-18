#!/usr/bin/env python3
"""cdp_minimize.py PORT — reliably hide an offscreen automation Chrome by minimizing its
window via CDP. `--window-position=-32000,-32000` gets clamped/raised back on some macOS
display setups, so an automation window ends up on the operator's screen. Minimizing through
the browser's own debug port is targeted (only that window) and reliable — no other windows
touched, no display rebuild. Idempotent; safe to call after every keepalive relaunch.

    python3 tools/cdp_minimize.py 18823
"""
import sys
import urllib.request


def minimize(port: int) -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # noqa: BLE001
        print(f"playwright unavailable ({exc})")
        return False
    # only proceed if a debugger is actually up on that port
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=3)
    except Exception:
        print(f"no CDP on :{port}")
        return False
    try:
        with sync_playwright() as p:
            b = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}", timeout=8000)
            # Browser-level CDP session: NEVER attaches to or creates a page, so it can
            # never call bringToFront / open a tab / raise the window. This is the whole
            # point — the old code did ctx.new_page() when no tab existed, which opened a
            # tab and yanked the window onscreen before minimizing it (the "flash").
            s = b.new_browser_cdp_session()
            targets = s.send("Target.getTargets").get("targetInfos", [])
            page = next((t for t in targets if t.get("type") == "page"), None)
            if page is None:
                # No page target at all — nothing with a window to minimize.
                b.close()
                print(f"no page target on :{port}")
                return True
            win = s.send("Browser.getWindowForTarget", {"targetId": page["targetId"]})
            wid = win["windowId"]
            # Skip if already minimized — makes a re-minimize loop a cheap read that only
            # writes when an OAuth/consent raise has actually un-minimized the window.
            if win.get("bounds", {}).get("windowState") == "minimized":
                b.close()
                return True
            s.send("Browser.setWindowBounds", {"windowId": wid, "bounds": {"windowState": "minimized"}})
            b.close()
        print(f"minimized window on :{port}")
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"minimize failed on :{port} ({exc})")
        return False


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: cdp_minimize.py PORT"); raise SystemExit(2)
    raise SystemExit(0 if minimize(int(sys.argv[1])) else 1)
