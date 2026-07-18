"""
PANIC MODE — emergency lockdown and restore for the entire stack.

Trigger:  "PANIC!" from any authenticated frontend (iMessage, Telegram, WebUI chat)
Restore:  "OFF!" from any authenticated frontend

Lockdown actions:
  1. Kill Cloudflare tunnel (stop public internet exposure)
  2. Wipe all webui sessions (invalidate any stolen cookies)
  3. Enable firewall block-all + stealth mode
  4. Stop all non-essential LaunchAgents

Restore actions:
  1. Disable firewall block-all + stealth mode
  2. Restart Cloudflare tunnel
  3. Restart all stopped LaunchAgents
  4. Restore webui sessions file (empty — users re-login)
"""

import json
import logging
import os
import subprocess
import time
from pathlib import Path

log = logging.getLogger("panic")

_BASE = Path(__file__).resolve().parent.parent
_PANIC_STATE_FILE = _BASE / "data" / "panic_state.json"
_SESSIONS_FILE = _BASE / "data" / "webui_sessions.json"

# Services to stop in panic mode (order: public-facing first, then agents)
_PANIC_STOP_SERVICES = [
    "com.claude-stack.webui",
    "com.claude-stack.webui-backend-a",
    "com.claude-stack.webui-backend-b",
    "com.claude-stack.telegram",
    "com.claude-stack.imessage",
    "com.claude-stack.landing",
    "com.claude-stack.mcp-gateway",
    "com.claude-stack.llm-api",
    "com.claude-stack.email-triage",
    "com.claude-stack.sf-agent",
    "com.claude-stack.pipeline-monitor",
    "com.claude-stack.exec-assistant",
    "com.claude-stack.meeting-prep",
    "com.claude-stack.followup-chaser",
    "com.claude-stack.healer-digest",
    "com.claude-stack.nightly-update",
    "com.claude-stack.morning-brief",
    "com.claude-stack.weekly-report",
    "com.claude-stack.news-pulse",
    "com.claude-stack.mail-cleaner",
    "com.claude-stack.cost-tracker",
    "com.claude-stack.optimizer",
    "com.claude-stack.infra-intel",
    "com.claude-stack.diagnostician",
    "com.claude-stack.workos-insights",
    "com.claude-stack.sf-monday-sync",
    "com.claude-stack.sf-admin-copilot",
]

_FIREWALL = "/usr/libexec/ApplicationFirewall/socketfilterfw"


def is_panic_active() -> bool:
    """Check if panic mode is currently engaged."""
    try:
        if _PANIC_STATE_FILE.exists():
            state = json.loads(_PANIC_STATE_FILE.read_text())
            return state.get("active", False)
    except Exception:
        pass
    return False


def _run(cmd: list[str], timeout: int = 15) -> tuple[int, str]:
    """Run a shell command, return (returncode, combined output)."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except Exception as e:
        return -1, str(e)


def _uid_domain() -> tuple[int, str]:
    uid = os.getuid()
    return uid, f"gui/{uid}"


def activate_panic(triggered_by: str, frontend: str) -> dict:
    """LOCKDOWN — kill tunnel, wipe sessions, block all, stop services."""
    if is_panic_active():
        return {"ok": True, "already_active": True, "message": "Panic mode already active."}

    uid, domain = _uid_domain()
    results = {"stopped": [], "errors": [], "triggered_by": triggered_by, "frontend": frontend}

    # 1. Kill Cloudflare tunnel
    _run(["sudo", "launchctl", "bootout", "system/com.cloudflare.cloudflared"])
    _run(["sudo", "launchctl", "unload", "/Library/LaunchDaemons/com.cloudflare.cloudflared.plist"])
    _run(["killall", "cloudflared"])
    results["tunnel"] = "killed"
    log.critical("PANIC: Cloudflare tunnel killed")

    # 2. Wipe all webui sessions
    try:
        _SESSIONS_FILE.write_text("{}")
        results["sessions"] = "wiped"
    except Exception as e:
        results["errors"].append(f"session wipe: {e}")

    # 3. Firewall: block all + stealth
    _run(["sudo", _FIREWALL, "--setblockall", "on"])
    _run(["sudo", _FIREWALL, "--setstealthmode", "on"])
    results["firewall"] = "locked"
    log.critical("PANIC: Firewall locked — block-all + stealth ON")

    # 4. Stop services (the frontend that received PANIC will die too — that's expected)
    for label in _PANIC_STOP_SERVICES:
        target = f"{domain}/{label}"
        rc, out = _run(["launchctl", "bootout", target])
        if rc == 0:
            results["stopped"].append(label)
        # Don't treat "not running" as an error
        elif "No such process" not in out and "Could not find" not in out:
            results["errors"].append(f"{label}: {out}")

    log.critical("PANIC: %d services stopped. Triggered by %s via %s", len(results["stopped"]), triggered_by, frontend)

    # Save state so OFF! knows what to restore
    state = {
        "active": True,
        "activated_at": time.time(),
        "triggered_by": triggered_by,
        "frontend": frontend,
        "stopped_services": results["stopped"],
    }
    try:
        _PANIC_STATE_FILE.write_text(json.dumps(state, indent=2))
    except Exception as e:
        results["errors"].append(f"state save: {e}")

    results["ok"] = True
    results["message"] = f"PANIC MODE ACTIVATED. Tunnel killed, sessions wiped, firewall locked, {len(results['stopped'])} services stopped."
    return results


def deactivate_panic(triggered_by: str) -> dict:
    """RESTORE — re-enable firewall, restart tunnel, restart services."""
    if not is_panic_active():
        return {"ok": True, "already_inactive": True, "message": "Panic mode is not active."}

    uid, domain = _uid_domain()
    state = {}
    try:
        state = json.loads(_PANIC_STATE_FILE.read_text())
    except Exception:
        pass

    results = {"restarted": [], "errors": [], "restored_by": triggered_by}

    # 1. Firewall: restore normal
    _run(["sudo", _FIREWALL, "--setblockall", "off"])
    _run(["sudo", _FIREWALL, "--setstealthmode", "off"])
    results["firewall"] = "restored"
    log.info("PANIC OFF: Firewall restored to normal")

    # 2. Restart Cloudflare tunnel
    _run(["sudo", "launchctl", "load", "/Library/LaunchDaemons/com.cloudflare.cloudflared.plist"])
    _run(["sudo", "launchctl", "bootstrap", "system", "/Library/LaunchDaemons/com.cloudflare.cloudflared.plist"])
    results["tunnel"] = "restarted"
    log.info("PANIC OFF: Cloudflare tunnel restarted")

    # 3. Restart stopped services
    agents_dir = Path.home() / "Library" / "LaunchAgents"
    stopped = state.get("stopped_services", _PANIC_STOP_SERVICES)
    for label in stopped:
        plist = agents_dir / f"{label}.plist"
        if not plist.exists():
            continue
        target = f"{domain}/{label}"
        # Bootstrap then kickstart
        _run(["launchctl", "bootstrap", domain, str(plist)])
        rc, out = _run(["launchctl", "kickstart", "-k", target])
        if rc == 0 or "already bootstrapped" in out.lower():
            results["restarted"].append(label)
        else:
            results["errors"].append(f"{label}: {out}")

    log.info("PANIC OFF: %d services restarted. Restored by %s", len(results["restarted"]), triggered_by)

    # Clear panic state
    try:
        _PANIC_STATE_FILE.write_text(json.dumps({"active": False, "deactivated_at": time.time(), "restored_by": triggered_by}))
    except Exception as e:
        results["errors"].append(f"state clear: {e}")

    results["ok"] = True
    results["message"] = f"PANIC MODE OFF. Firewall restored, tunnel restarted, {len(results['restarted'])} services back online."
    return results


def is_panic_command(text: str) -> str | None:
    """Check if text is a panic command. Returns 'panic', 'off', or None."""
    clean = (text or "").strip().upper()
    if clean == "PANIC!":
        return "panic"
    if clean == "OFF!":
        return "off"
    return None
