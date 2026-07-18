"""Credentials page backend — status + retest + reauth per provider.

One source of truth for what's configured, what works, and what needs your
attention. The Exec Assistant surfaces CREDENTIAL_EXPIRED items; this page
is the place you click through from those items to resolve them.

Pattern:
    - status_all() attempts a silent refresh for every configured provider
      and returns structured {provider, configured, ok, last_refreshed, hint}
    - retest(provider) re-runs the silent refresh on demand (same flow as
      the diagnostician's loop)
    - ms_start_device_code() kicks off Microsoft device-code flow — returns
      the URL + code for the user to enter; a background task polls for
      completion and persists the token via token_store
"""

import asyncio
import os
import time
from core.api_client import ensure_external_api_allowed
from pathlib import Path
from typing import Dict, Any

BASE_DIR = Path(__file__).resolve().parent.parent
TOKEN_DIR = BASE_DIR / "data" / "tokens"

# (provider, env_gate, description) — same list the diagnostician uses
OAUTH_PROVIDERS = [
    ("salesforce", "SALESFORCE_CONSUMER_KEY", "Salesforce (JWT bearer — server-to-server)"),
    ("microsoft",  "MICROSOFT_CLIENT_ID",     "Microsoft 365 (Outlook, SharePoint, Teams)"),
    ("zoom",       "ZOOM_ACCOUNT_ID",         "Zoom (server-to-server)"),
]

# API keys aren't OAuth — we probe them by making a real call to the service.
API_KEY_PROVIDERS = [
    ("openrouter", "OPENROUTER_API_KEY", "OpenRouter API"),
    ("telegram",  "TELEGRAM_BOT_TOKEN",  "Telegram Bot"),
    ("monday",    "MONDAY_API_TOKEN",    "Monday.com"),
]

def _mask(s: str) -> str:
    if not s:
        return ""
    if len(s) <= 10:
        return "*" * len(s)
    return s[:4] + "…" + s[-4:]

def _stored_token_age(provider: str):
    path = TOKEN_DIR / f"{provider}.json"
    if not path.exists():
        return None
    try:
        return path.stat().st_mtime
    except Exception:
        return None

async def _probe_openrouter() -> Dict[str, Any]:
    ensure_external_api_allowed("OpenRouter credential probe")
    import httpx
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        return {"ok": False, "hint": "OPENROUTER_API_KEY not set"}
    try:
        async with httpx.AsyncClient(timeout=6) as c:
            r = await c.get(
                "https://openrouter.ai/api/v1/models",
                headers={"Authorization": f"Bearer {key}"},
            )
        if r.status_code == 200:
            return {"ok": True, "hint": f"key {_mask(key)} · /v1/models 200"}
        return {"ok": False, "hint": f"/v1/models returned {r.status_code} — rotate OpenRouter key"}
    except Exception as e:
        return {"ok": False, "hint": f"probe failed: {e}"}

async def _probe_telegram() -> Dict[str, Any]:
    ensure_external_api_allowed("Telegram credential probe")
    import httpx
    tok = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not tok:
        return {"ok": False, "hint": "TELEGRAM_BOT_TOKEN not set"}
    try:
        async with httpx.AsyncClient(timeout=6) as c:
            r = await c.get(f"https://api.telegram.org/bot{tok}/getMe")
        if r.status_code == 200 and r.json().get("ok"):
            bot = r.json()["result"].get("username", "?")
            return {"ok": True, "hint": f"bot @{bot}"}
        return {"ok": False, "hint": f"getMe returned {r.status_code} — re-issue token via @BotFather"}
    except Exception as e:
        return {"ok": False, "hint": f"probe failed: {e}"}

async def _probe_monday() -> Dict[str, Any]:
    ensure_external_api_allowed("Monday credential probe")
    import httpx
    tok = os.environ.get("MONDAY_API_TOKEN", "")
    if not tok:
        return {"ok": False, "hint": "MONDAY_API_TOKEN not set"}
    try:
        async with httpx.AsyncClient(timeout=6) as c:
            r = await c.post(
                "https://api.monday.com/v2",
                headers={"Authorization": tok, "Content-Type": "application/json"},
                json={"query": "{ me { id } }"},
            )
        if r.status_code == 200 and "errors" not in r.json():
            return {"ok": True, "hint": "me { id } OK"}
        return {"ok": False, "hint": "API rejected token — rotate at monday.com Admin → API"}
    except Exception as e:
        return {"ok": False, "hint": f"probe failed: {e}"}

API_PROBES = {
    "openrouter": _probe_openrouter,
    "telegram":  _probe_telegram,
    "monday":    _probe_monday,
}

async def _status_oauth(provider: str, gate: str, desc: str) -> Dict[str, Any]:
    row = {
        "provider": provider, "kind": "oauth", "description": desc,
        "configured": bool(os.environ.get(gate)),
        "ok": False, "hint": "", "last_refreshed": None, "age_h": None,
    }
    if not row["configured"]:
        row["hint"] = f"{gate} not set — unconfigured"
        return row
    row["last_refreshed"] = _stored_token_age(provider)
    if row["last_refreshed"]:
        row["age_h"] = round((time.time() - row["last_refreshed"]) / 3600, 1)
    try:
        from auth.oauth_manager import OAuthManager
        oauth = OAuthManager()
        tok = await asyncio.wait_for(oauth.get_token(provider), timeout=20)
        if tok:
            row["ok"] = True
            row["hint"] = "silent refresh OK"
        else:
            row["hint"] = "silent refresh returned empty"
    except Exception as e:
        row["hint"] = f"{type(e).__name__}: {str(e)[:200]}"
    return row

async def _status_api(provider: str, gate: str, desc: str) -> Dict[str, Any]:
    row = {
        "provider": provider, "kind": "api_key", "description": desc,
        "configured": bool(os.environ.get(gate)),
        "ok": False, "hint": "",
    }
    if not row["configured"]:
        row["hint"] = f"{gate} not set"
        return row
    probe = API_PROBES.get(provider)
    if probe:
        try:
            r = await probe()
            row["ok"] = r.get("ok", False)
            row["hint"] = r.get("hint", "")
        except Exception as e:
            row["hint"] = f"probe error: {e}"
    return row

async def status_all() -> Dict[str, Any]:
    oauth_rows = await asyncio.gather(
        *[_status_oauth(p, g, d) for p, g, d in OAUTH_PROVIDERS],
        return_exceptions=False,
    )
    api_rows = await asyncio.gather(
        *[_status_api(p, g, d) for p, g, d in API_KEY_PROVIDERS],
        return_exceptions=False,
    )
    all_rows = list(oauth_rows) + list(api_rows)
    try:
        from core import auth_maintenance

        auth_state = auth_maintenance.status_snapshot()
    except Exception:
        auth_state = {"oauth": {}, "browser_sessions": {}, "summary": {}}
    return {
        "as_of": time.time(),
        "providers": all_rows,
        "auth_maintenance": auth_state,
        "browser_sessions": list((auth_state.get("browser_sessions") or {}).values()),
        "critical_count": sum(1 for r in all_rows if r["configured"] and not r["ok"]),
        "ok_count": sum(1 for r in all_rows if r["ok"]),
        "total_configured": sum(1 for r in all_rows if r["configured"]),
    }

# ---------------------------------------------------------------------------
# Microsoft device-code flow — interactive reauth
# ---------------------------------------------------------------------------
_ms_pending = {}   # code_state -> {url, user_code, device_code, interval, started}

async def ms_start_device_code() -> Dict[str, Any]:
    ensure_external_api_allowed("Microsoft device code auth")
    import httpx
    client_id = os.environ.get("MICROSOFT_CLIENT_ID", "")
    tenant    = os.environ.get("MICROSOFT_TENANT_ID", "common")
    scopes    = "Mail.ReadWrite Calendars.ReadWrite Contacts.Read offline_access"
    if not client_id:
        return {"ok": False, "error": "MICROSOFT_CLIENT_ID not set"}
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(
                f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/devicecode",
                data={"client_id": client_id, "scope": scopes},
            )
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        return {"ok": False, "error": "Microsoft device code flow failed"}

    state = data["device_code"][:16]
    _ms_pending[state] = {
        "url":         data["verification_uri"],
        "user_code":   data["user_code"],
        "device_code": data["device_code"],
        "interval":    data.get("interval", 5),
        "expires_in":  data.get("expires_in", 900),
        "started":     time.time(),
        "status":      "pending",
    }
    # Start background poller so user only has to click through in browser
    asyncio.create_task(_ms_poll_until_done(state))
    return {
        "ok": True,
        "state": state,
        "url": data["verification_uri"],
        "user_code": data["user_code"],
        "expires_in": data.get("expires_in", 900),
    }

async def _ms_poll_until_done(state: str):
    import httpx
    entry = _ms_pending.get(state)
    if not entry:
        return
    client_id     = os.environ.get("MICROSOFT_CLIENT_ID", "")
    client_secret = os.environ.get("MICROSOFT_CLIENT_SECRET", "")
    tenant        = os.environ.get("MICROSOFT_TENANT_ID", "common")
    token_url     = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"

    deadline = entry["started"] + entry["expires_in"]
    while time.time() < deadline:
        await asyncio.sleep(entry["interval"])
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.post(token_url, data={
                    "client_id":     client_id,
                    "client_secret": client_secret,
                    "device_code":   entry["device_code"],
                    "grant_type":    "urn:ietf:params:oauth:grant-type:device_code",
                })
            result = r.json()
            if "access_token" in result:
                from auth import token_store
                token_store.save("microsoft", {
                    "access_token":  result["access_token"],
                    "refresh_token": result.get("refresh_token", ""),
                    "expires_at":    time.time() + result.get("expires_in", 3600),
                })
                entry["status"] = "done"
                return
            if result.get("error") not in ("authorization_pending", "slow_down"):
                entry["status"] = f"error: {result.get('error_description', result.get('error'))}"
                return
        except Exception as e:
            entry["status"] = f"error: {e}"
            return
    entry["status"] = "expired"

def ms_device_code_status(state: str) -> Dict[str, Any]:
    e = _ms_pending.get(state)
    if not e:
        return {"ok": False, "error": "unknown state"}
    return {"ok": True, "status": e["status"], "user_code": e["user_code"], "url": e["url"]}

# ---------------------------------------------------------------------------
# Hints & troubleshooting copy per provider
# ---------------------------------------------------------------------------
PROVIDER_HINTS = {
    "salesforce": (
        "JWT bearer flow — no interactive consent. If silent refresh fails, "
        "check: (1) SALESFORCE_CONSUMER_KEY and SALESFORCE_USERNAME env vars set, "
        "(2) cert at data/certs/salesforce.pem matches the one uploaded to the "
        "Connected App's 'Use digital signatures' field, (3) user has permission to "
        "the Connected App (Setup → Manage Connected Apps → edit → pre-authorized "
        "profiles/permission sets)."
    ),
    "microsoft": "Click 'Reauth' to start device-code flow — you'll enter a code at microsoft.com/devicelogin.",
    "zoom":      "Server-to-server — check ZOOM_ACCOUNT_ID, ZOOM_CLIENT_ID, ZOOM_CLIENT_SECRET env vars.",
    "openrouter": "Rotate key at openrouter.ai/settings/keys and update OPENROUTER_API_KEY.",
    "telegram":  "Re-issue token via @BotFather: /revoke → /newbot. Update TELEGRAM_BOT_TOKEN in .env.",
    "monday":    "Rotate at monday.com → Admin → API. Update MONDAY_API_TOKEN in .env.",
}
