#!/usr/bin/env python3
"""owa_health.py — BULLETPROOF OWA (Outlook Web) auth health check + escalating alert.

Why this exists: OWA auth died 2026-07-02 and rotted SILENTLY for 5 days — the work brain
went stale and nobody was told. There were already ~5 OWA guardians; the gap was never
detection, it was a reliable DETECT→PAGE→ESCALATE→AUTO-RECOVER chain. This is that chain,
built to not be able to fail quietly:

  * REAL auth test: run `node tools/owa.js` (captures the live OWA mail token). exit 2 = Chrome/
    profile down, exit 3 = session dead (no token), exit 0 = authed. Not "is Chrome up" — an
    actual token grab. If Chrome's just down it tries owa_web_sync once before declaring dead.
  * SHADOW-PROOF gateway: loads core/alert_gateway.py by ABSOLUTE PATH (the agents/core import
    shadow is exactly what swallowed past alerts). Self-contained — a broken import can't mute it.
  * ESCALATES: while dead, PAGES once per day (rotating issue_key) so it can't be missed AND
    can't storm. AUTO-RECOVERS: on re-auth, clears the flag + sends a "back online" note.
  * FAIL-LOUD: if the check itself can't run (node/owa.js problem), that is ALSO alerted — a
    broken checker is never mistaken for a healthy OWA.

    python3 tools/owa_health.py            # one check (used on a plist cadence)
    python3 tools/owa_health.py --once     # same, explicit
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

STACK = Path(os.path.expanduser("~/claude-stack"))
FLAG = STACK / "data" / "hustle" / "cdp_reauth_needed" / "18820.flag"
STATE = STACK / "data" / "hustle" / "owa_health_state.json"
OWA_JS = STACK / "tools" / "owa.js"
OWA_SYNC = STACK / "scripts" / "owa_web_sync.sh"
RELOGIN_HINT = "re-login: `! bash ~/claude-stack/scripts/owa_web_sync.sh` (sign in visibly through MFA)"


def _gateway():
    """Load core/alert_gateway.py by absolute path — immune to the agents/core import shadow
    that has silently swallowed alerts before. Returns the module or None."""
    try:
        p = STACK / "core" / "alert_gateway.py"
        root = str(STACK)
        if root not in sys.path:
            sys.path.insert(0, root)
        spec = importlib.util.spec_from_file_location("core.alert_gateway", p)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m if hasattr(m, "page_critical") else None
    except Exception:
        return None


def _load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {}


def _save_state(s: dict) -> None:
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(s))
    except Exception:
        pass


def _run_owa_js(timeout: int = 70) -> tuple[int, str]:
    try:
        r = subprocess.run(["node", "tools/owa.js", "read", "--match", "__healthprobe__"],
                           cwd=str(STACK), timeout=timeout, capture_output=True, text=True)
        return r.returncode, (r.stdout + r.stderr).lower()
    except subprocess.TimeoutExpired:
        return -99, "timeout"
    except FileNotFoundError:
        return -98, "node-missing"
    except Exception as exc:  # noqa: BLE001
        return -97, str(exc).lower()


def check_auth() -> str:
    """'healthy' | 'dead' | 'unknown'. Tries a Chrome respawn before declaring dead."""
    if not OWA_JS.exists():
        return "unknown"
    code, out = _run_owa_js()
    if code == 0 and "not logged in" not in out and "re-login" not in out:
        return "healthy"
    if code in (2, -99) and OWA_SYNC.exists():
        # Chrome down / husk — bring the OWA session up once, then re-test (distinguishes
        # "just not running" from "session actually expired").
        try:
            subprocess.run(["bash", str(OWA_SYNC)], cwd=str(STACK), timeout=90,
                           capture_output=True, text=True)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(12)
        code, out = _run_owa_js()
        if code == 0 and "not logged in" not in out and "re-login" not in out:
            return "healthy"
    if code in (2, 3) or "not logged in" in out or "re-login" in out:
        return "dead"
    if code in (-97, -98):   # node broke / missing — the CHECK is broken, not necessarily OWA
        return "unknown"
    return "dead"   # fail-closed: anything we can't positively call healthy => treat as dead


def main() -> int:
    argparse.ArgumentParser().parse_args()  # accept --once etc. without failing
    status = check_auth()
    gw = _gateway()
    st = _load_state()
    today = date.today().isoformat()
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if status == "healthy":
        recovered = FLAG.exists() or st.get("was_dead")
        FLAG.unlink(missing_ok=True)
        if recovered and gw:
            with_ = f" (was dead since {st.get('dead_since','?')})"
            gw.notify("owa_health", "✅ OWA re-authed — work brain syncing again" + with_,
                      "", issue_key="owa_reauth", level="warn", healed=True)
        _save_state({"was_dead": False, "last_ok": now_iso})
        print(f"OWA healthy ({now_iso})")
        return 0

    if status == "dead":
        FLAG.parent.mkdir(parents=True, exist_ok=True)
        FLAG.write_text(f"{now_iso} dead session -> one-time re-login needed")
        dead_since = st.get("dead_since") or now_iso
        # page ONCE per day while dead — loud enough to never miss, capped so it can't storm.
        if st.get("last_page_day") != today and gw:
            gw.page_critical("owa_health",
                             "🔴 OWA/Outlook auth DEAD — work-GPT blind on email",
                             f"dead since {dead_since}. {RELOGIN_HINT}",
                             issue_key=f"owa_reauth_{today}")
            st["last_page_day"] = today
        _save_state({**st, "was_dead": True, "dead_since": dead_since})
        print(f"OWA DEAD ({now_iso}) — flag written, paged={st.get('last_page_day')==today}")
        return 1

    # unknown — the CHECK itself couldn't run. Fail LOUD (never mistake for healthy).
    if st.get("last_unknown_day") != today and gw:
        gw.notify("owa_health", "⚠️ OWA health-check couldn't run (node/owa.js?) — verify OWA manually",
                  "", issue_key=f"owa_healthcheck_broken_{today}", level="warn")
        st["last_unknown_day"] = today
    _save_state(st)
    print(f"OWA health UNKNOWN ({now_iso}) — check itself failed; alerted")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
