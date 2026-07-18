#!/usr/bin/env python3
"""survival_probes — untended-stack early-warning.

Runs periodically (launchd). Each probe checks a survival anchor the horizon-audit
flagged and routes a CRITICAL alert — which now actually PAGES (alert_gateway fix
44dc6e3de) — when the anchor is at risk. The point is to hear about a dying credential/
session/box BEFORE it silently zeroes the money loop.

Fail-soft: a probe raising never crashes the runner; it's recorded as a probe error.
Alerts go through core.alert_gateway (dedup + cooldown handle repeats). --once for one pass.

    python3 tools/survival_probes.py --once
"""
from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

STATUS = ROOT / "data" / "runtime" / "survival_probes.json"
OR_SPEND = ROOT / "data" / "openrouter_spend_history.jsonl"
OR_LOW_USD = float(os.environ.get("SURVIVAL_OR_LOW_USD", "5"))
SESSION_STALE_DAYS = float(os.environ.get("SURVIVAL_SESSION_STALE_DAYS", "21"))
HEARTBEAT_URL = os.environ.get("SURVIVAL_HEARTBEAT_URL", "").strip()


def _page(subject: str, body: str, issue_key: str) -> None:
    try:
        from core import alert_gateway
        alert_gateway.route("critical", "survival_probes", subject, body, issue_key=issue_key)
    except Exception:
        pass


def _heal(subject: str, issue_key: str) -> None:
    try:
        from core import alert_gateway
        alert_gateway.route("critical", "survival_probes", subject, "", issue_key=issue_key, healed=True)
    except Exception:
        pass


def _sh(cmd: list[str], timeout: int = 15) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except Exception:
        return ""


# ── probes: return (ok: bool, detail: str) ─────────────────────────────────────
def probe_codex_auth() -> tuple[bool, str]:
    """Post-Max, personal Codex is the primary agentic brain. If its OAuth dies, the
    self-improvement/build lane goes dark — and re-login is a human anchor (2FA)."""
    try:
        from core.codex_cli_bridge import personal_codex_auth_status
        st = personal_codex_auth_status()
        alive = bool(st.get("authenticated") or st.get("alive") or st.get("ok"))
        if alive:
            _heal("Codex auth recovered", "survival:codex_auth")
            return True, "codex authenticated"
        _page("Codex auth DEAD — re-login needed (post-Max brain down)",
              "personal ChatGPT/Codex session expired. run `codex login` to restore the agentic lane.",
              "survival:codex_auth")
        return False, f"codex auth not alive: {st}"
    except Exception as e:
        return False, f"codex probe error: {e}"


def probe_openrouter_balance() -> tuple[bool, str]:
    """The metered Opus lane runs on OpenRouter credit. When it drains, hard-fix bugs
    fall to free/local — good, but worth knowing before it happens."""
    try:
        rem = None
        if OR_SPEND.exists():
            for line in reversed(OR_SPEND.read_text().splitlines()):
                try:
                    rem = float(json.loads(line).get("remaining_usd"))
                    break
                except Exception:
                    continue
        if rem is None:
            return True, "balance unknown (skip)"
        if rem < OR_LOW_USD:
            _page(f"OpenRouter balance low (${rem:.2f})",
                  "metered Opus lane will stop soon; self-patch fails over to free/local. top up or ignore (floor survives).",
                  "survival:or_balance")
            return False, f"${rem:.2f} < ${OR_LOW_USD}"
        _heal("OpenRouter balance recovered", "survival:or_balance")
        return True, f"${rem:.2f}"
    except Exception as e:
        return False, f"or balance error: {e}"


def probe_sessions() -> tuple[bool, str]:
    """Sweep ALL chrome-cdp login sessions for cookie-rot (broadens eternal_guardian's
    named list — the audit found ~22 profiles + faceless case-study ones unmonitored)."""
    try:
        stale = []
        now = time.time()
        for prof in glob.glob(os.path.expanduser("~/.chrome-cdp-profile*")):
            cookies = os.path.join(prof, "Default", "Cookies")
            if not os.path.exists(cookies):
                continue
            age_d = (now - os.path.getmtime(cookies)) / 86400.0
            if age_d > SESSION_STALE_DAYS:
                stale.append((os.path.basename(prof), round(age_d)))
        if stale:
            faceless = [s for s in stale if "casestudy" in s[0] or "case-study" in s[0]]
            worst = sorted(stale, key=lambda s: -s[1])[:6]
            tag = " (INCL. FACELESS — anonymity rail)" if faceless else ""
            _page(f"{len(stale)} login sessions stale >{int(SESSION_STALE_DAYS)}d{tag}",
                  "re-login before they hard-expire: " + ", ".join(f"{n} ({d}d)" for n, d in worst),
                  "survival:sessions")
            return False, f"{len(stale)} stale"
        _heal("login sessions fresh", "survival:sessions")
        return True, "all sessions fresh"
    except Exception as e:
        return False, f"session probe error: {e}"


def probe_autologin() -> tuple[bool, str]:
    """The blackout landmine: all jobs are LaunchAgents needing a logged-in GUI. If
    FileVault flips ON or autoLoginUser clears (an OS update can do either), the box
    stalls at the login window on next reboot = total silent blackout. Catch the drift
    BEFORE the reboot arms it."""
    try:
        fv = _sh(["fdesetup", "status"]) or ""
        autouser = _sh(["defaults", "read", "/Library/Preferences/com.apple.loginwindow", "autoLoginUser"]) or ""
        problems = []
        if "off" not in fv.lower():
            problems.append(f"FileVault={fv.strip() or 'unknown'} (blocks autologin)")
        if not autouser.strip():
            problems.append("autoLoginUser empty (won't auto-login on reboot)")
        if problems:
            _page("Autologin AT RISK — box may not reboot unattended",
                  "; ".join(problems) + ". a reboot now could stall at the login window (total blackout). fix before rebooting.",
                  "survival:autologin")
            return False, "; ".join(problems)
        _heal("autologin healthy", "survival:autologin")
        return True, "FileVault off + autoLoginUser set"
    except Exception as e:
        return False, f"autologin probe error: {e}"


def probe_deadman_ping() -> tuple[bool, str]:
    """Outbound heartbeat to an EXTERNAL dead-man's-switch (e.g. healthchecks.io free).
    If the whole box dies (blackout / power / hardware), the box stops pinging and the
    EXTERNAL service alerts Operator — the only alarm that survives total death. No-op until
    SURVIVAL_HEARTBEAT_URL is set (one-time free setup)."""
    if not HEARTBEAT_URL:
        return True, "no heartbeat url set (external dead-man's-switch not configured)"
    try:
        import urllib.request
        urllib.request.urlopen(HEARTBEAT_URL, timeout=15).read()
        return True, "pinged"
    except Exception as e:
        return False, f"heartbeat ping failed: {e}"  # local failure; the external side handles silence


PROBES = [
    ("codex_auth", probe_codex_auth),
    ("openrouter_balance", probe_openrouter_balance),
    ("sessions", probe_sessions),
    ("autologin", probe_autologin),
    ("deadman_ping", probe_deadman_ping),
]


def run_once() -> dict:
    results = {}
    for name, fn in PROBES:
        try:
            ok, detail = fn()
        except Exception as e:
            ok, detail = False, f"probe crashed: {e}"
        results[name] = {"ok": ok, "detail": detail}
    out = {"ts": time.time(), "results": results,
           "overall_ok": all(r["ok"] for r in results.values())}
    try:
        STATUS.parent.mkdir(parents=True, exist_ok=True)
        STATUS.write_text(json.dumps(out, indent=2))
    except Exception:
        pass
    return out


def main() -> int:
    out = run_once()
    for name, r in out["results"].items():
        print(f"  {name:20} {'OK ' if r['ok'] else 'RISK'} {r['detail']}")
    print(f"overall_ok: {out['overall_ok']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
