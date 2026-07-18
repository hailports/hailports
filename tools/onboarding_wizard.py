#!/usr/bin/env python3
"""onboarding_wizard.py — white-glove, text-driven credential onboarding.

Generalizes the imsg_bridge conversational pattern OFF its single hardcoded
TARGET (imsg_bridge.py:20) into a multi-sender wizard: Operator explicitly ENROLLS a
handle for a tenant, and only enrolled handles can drive onboarding. The wizard
texts to gather non-secret info, then for EACH secret texts a one-time TTL magic
link (core.magic_link.create_magic_link) — NEVER a plaintext secret and NEVER an
SMS OTP (create_login_code's SMS leg is deliberately not used for secrets). The
link opens a capture form (the products/self_serve/app.py surface); on submit,
capture_secret() writes the secret encrypted + chmod600 via tenant_secrets /
client_vault, burns the token, mints a session, and texts the next step.

Rails preserved:
  - authorization gate: unknown handle -> silently ignored (like imsg_bridge)
  - secrets never traverse SMS, never logged
  - OAuth / device-code / session-token preferred over raw passwords
  - per-tenant isolation via tools.tenant_secrets + client_vault per-tenant key

CLI:
    onboarding_wizard.py enroll <tenant_id> <handle>
    onboarding_wizard.py status <tenant_id>
    onboarding_wizard.py accounts <tenant_id>
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.chrome_launch import require_interactive_headful
from core.magic_link import create_magic_link, validate_magic_token, _send_messages_text
from products.self_serve import client_vault
from tools import tenant_secrets

STATE_PATH = Path.home() / ".onboarding_wizard_state.json"
_UID_SEP = "::"

# Where the capture form lives. The magic link's path (/auth/magic?token=…) is
# built by core.magic_link; this base is the public origin the link resolves to.
# NEVER the CompanyA work domain (work.Operator.com) — that would leak the operator
# + cross the work/hustle airgap. Onboarding links must resolve to a BrandA-fronted or
# neutral origin. No external BrandA domain exists yet, so default to the LOCAL
# self-serve funnel for the Operator2 dogfood; set ONBOARDING_BASE_URL for a real host.
DEFAULT_BASE_URL = os.environ.get("ONBOARDING_BASE_URL", "http://127.0.0.1:8300")


# ---------------------------------------------------------------------------
# Per-tenant onboarding plans. method ∈ {oauth, device_code, app_password,
# session_capture}. human_login=True means a one-time human login/2FA is
# structurally unavoidable. secret_field is what capture_secret stores.
# ---------------------------------------------------------------------------
ONBOARDING_PLANS: dict[str, list[dict]] = {
    "Operator2": [
        {"key": "kipi_m365", "label": "Kipi.ai Microsoft 365 (Outlook/Teams/SharePoint)",
         "method": "credentials", "provider": "microsoft", "secret_field": "",
         "human_login": True},
        {"key": "cnovate_gws", "label": "Cnovate.io Google Workspace mail",
         "method": "credentials", "provider": "google", "secret_field": "",
         "human_login": True},
        {"key": "slack", "label": "Slack",
         "method": "credentials", "provider": "slack", "secret_field": "",
         "human_login": True},
        {"key": "littlebird_cal", "label": "LittleBird shared calendar",
         "method": "session_capture", "provider": "littlebird", "secret_field": "session",
         "human_login": True},
    ],
}


# Persisted per-tenant CUSTOM accounts — lets a person add ANY system beyond the
# hardcoded plan during onboarding ("also connect my QuickBooks / Notion / X"). The
# universal method is session_capture: a one-time human login (2FA and all) whose
# session persists in a dedicated CDP profile — works for almost any web platform
# without a bespoke integration.
CUSTOM_PLANS_PATH = Path.home() / ".onboarding_custom_plans.json"


def _slug(text: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "_", str(text or "").strip().lower()).strip("_") or "system"


def _load_custom() -> dict:
    try:
        data = json.loads(CUSTOM_PLANS_PATH.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_custom(data: dict) -> None:
    CUSTOM_PLANS_PATH.write_text(json.dumps(data, indent=2, sort_keys=True))
    try:
        CUSTOM_PLANS_PATH.chmod(0o600)
    except Exception:
        pass


def add_account(tenant_id: str, label: str, *, method: str = "session_capture",
                provider: str = "", key: str = "", login_url: str = "") -> dict:
    """Append an arbitrary system to a tenant's onboarding plan. Default method =
    session_capture (universal: one human 2FA login -> persisted session), so a person
    can name ANY platform and it just works. Idempotent on key."""
    tenant_id = str(tenant_id or "").strip().lower()
    if not tenant_id or not str(label).strip():
        return {"ok": False, "error": "tenant_id and label required"}
    key = _slug(key or label)
    spec = {"key": key, "label": str(label).strip(), "method": method,
            "provider": provider or _slug(label),
            "secret_field": "session" if method == "session_capture" else "",
            "human_login": True}
    if login_url:
        spec["login_url"] = login_url
    data = _load_custom()
    accounts = data.setdefault(tenant_id, [])
    accounts[:] = [a for a in accounts if a.get("key") != key] + [spec]
    _save_custom(data)
    return {"ok": True, "tenant": tenant_id, "account": spec}


def plan_for(tenant_id: str) -> list[dict]:
    tid = str(tenant_id or "").strip().lower()
    base = list(ONBOARDING_PLANS.get(tid, []))
    seen = {a["key"] for a in base}
    for a in _load_custom().get(tid, []):
        if a.get("key") not in seen:
            base.append(a)
    return base


# ---------------------------------------------------------------------------
# State: {"enrolled": {handle: tenant}, "sessions": {handle: {...}}}
# ---------------------------------------------------------------------------
def _load() -> dict:
    try:
        data = json.loads(STATE_PATH.read_text())
        if isinstance(data, dict):
            data.setdefault("enrolled", {})
            data.setdefault("sessions", {})
            return data
    except Exception:
        pass
    return {"enrolled": {}, "sessions": {}}


def _save(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True))
    try:
        STATE_PATH.chmod(0o600)
    except Exception:
        pass


def is_authorized(handle: str) -> bool:
    return str(handle or "").strip() in _load().get("enrolled", {})


def tenant_for(handle: str) -> str:
    return _load().get("enrolled", {}).get(str(handle or "").strip(), "")


def enroll(tenant_id: str, handle: str) -> dict:
    """Operator-only: authorize a handle to be onboarded for a tenant."""
    tenant_id = str(tenant_id or "").strip().lower()
    handle = str(handle or "").strip()
    if not tenant_id or not handle:
        return {"ok": False, "error": "tenant_id and handle required"}
    state = _load()
    state["enrolled"][handle] = tenant_id
    pending = [a["key"] for a in plan_for(tenant_id)]
    state["sessions"][handle] = {
        "tenant": tenant_id,
        "step": "greet",
        "pending_accounts": pending,
        "captured": {},
    }
    _save(state)
    return {"ok": True, "tenant": tenant_id, "handle": handle, "pending_accounts": pending}


# ---------------------------------------------------------------------------
# Delivery — reuse magic_link's Messages sender (arbitrary handle, no hardcode)
# ---------------------------------------------------------------------------
def _text(handle: str, body: str) -> bool:
    ok, _ = _send_messages_text(body, [handle])
    return ok


def _account_spec(tenant_id: str, key: str) -> dict | None:
    for a in plan_for(tenant_id):
        if a["key"] == key:
            return a
    return None


# Per-tenant onboarding origin. OWNER (Operator onboarding his OWN accounts) uses his real
# Operator.com domain — his identity, his tool, no reason to front behind a neutral
# host. EVERY other tenant (Operator2, clients) uses a neutral origin: a real person must
# NEVER receive a Operator.com link (that leaks the operator's name to a third party).
OWNER_TENANTS = {"Operator", "owner", "Operator"}
# Family/household tenants (Operator's people) also front on his real Operator.com domain — no
# anonymity concern (they know him). Only arm's-length EXTERNAL client tenants use the neutral
# host. Add external clients to EXTERNAL_TENANTS to force neutral; default here is Operator.
FAMILY_TENANTS = {"Operator2", "relaytest"}
EXTERNAL_TENANTS: set[str] = set()
OWNER_BASE_URL = os.environ.get("ONBOARDING_OWNER_BASE_URL", "https://setup.Operator.com")


def base_url_for(tenant_id: str) -> str:
    t = str(tenant_id or "").strip().lower()
    if t in EXTERNAL_TENANTS:
        return DEFAULT_BASE_URL
    if t in OWNER_TENANTS or t in FAMILY_TENANTS:
        return OWNER_BASE_URL
    return DEFAULT_BASE_URL


def _send_secret_link(handle: str, tenant_id: str, spec: dict) -> bool:
    """Text a one-time TTL magic link for ONE credential. Never a secret/OTP."""
    field = spec.get("secret_field") or "credential"
    user_id = _UID_SEP.join([tenant_id, spec["key"], field, handle])
    # create_magic_link mints a one-time 10-min token and, given a delivery
    # handle, texts the link itself (a URL — safe over SMS; the secret is only
    # ever entered on the form the link opens).
    ok, _ = create_magic_link(
        username=spec["label"],
        user_id=user_id,
        base_url=base_url_for(tenant_id),
        delivery_handles=[handle],
    )
    return ok


def _advance(handle: str) -> str:
    """Send the link for the next uncaptured account, or finish."""
    state = _load()
    sess = state["sessions"].get(handle) or {}
    tenant = sess.get("tenant", "")
    captured = sess.get("captured", {})
    for key in sess.get("pending_accounts", []):
        if captured.get(key):
            continue
        spec = _account_spec(tenant, key)
        if not spec:
            continue
        sess["step"] = "capturing"
        state["sessions"][handle] = sess
        _save(state)
        _send_secret_link(handle, tenant, spec)
        note = " (you'll do a one-time login there)" if spec.get("human_login") else ""
        return f"sent you a secure one-time link for {spec['label']}{note}. it expires in 10 min."
    sess["step"] = "done"
    state["sessions"][handle] = sess
    _save(state)
    return "all set — every system is connected. thanks!"


def _affirmative(t: str) -> bool:
    return any(w in t for w in ("go", "yes", "yep", "ready", "start", "ok", "sure"))


def _greeting(tenant: str) -> str:
    labels = "\n".join(f"  • {a['label']}" for a in plan_for(tenant)) or "  (nothing configured yet)"
    return ("hi! setting up your work systems so everything's connected. i'll send a secure "
            "one-time link per system — you never text a password.\n\nsystems:\n"
            f"{labels}\n\nreply 'go' to start.")


def start_onboarding(handle: str) -> dict:
    """OPERATOR-INITIATED kickoff (hands-off #3): proactively text the greeting so the person
    never has to know to text first. After this they just reply 'go' and tap the links that
    auto-arrive. Enrollment-gated. Returns without sending if the handle isn't enrolled."""
    handle = str(handle or "").strip()
    if not is_authorized(handle):
        return {"ok": False, "error": "handle not enrolled — run `enroll <tenant> <handle>` first"}
    tenant = tenant_for(handle)
    state = _load()
    sess = state["sessions"].setdefault(handle, {
        "tenant": tenant, "step": "greet",
        "pending_accounts": [a["key"] for a in plan_for(tenant)], "captured": {}})
    sess["step"] = "confirm"
    state["sessions"][handle] = sess
    _save(state)
    sent = _text(handle, _greeting(tenant))
    return {"ok": bool(sent), "handle": handle, "tenant": tenant, "greeted": bool(sent),
            "pending": sess["pending_accounts"]}


def handle_message(handle: str, text: str) -> str | None:
    """Per-sender wizard router. Returns reply text, or None to stay silent."""
    handle = str(handle or "").strip()
    if not is_authorized(handle):
        return None  # authorization gate — unknown handle ignored (imsg_bridge parity)
    t = (text or "").lower().strip()
    state = _load()
    sess = state["sessions"].setdefault(handle, {
        "tenant": tenant_for(handle), "step": "greet",
        "pending_accounts": [a["key"] for a in plan_for(tenant_for(handle))], "captured": {},
    })
    tenant = sess.get("tenant", "")
    step = sess.get("step", "greet")

    if t in ("stop", "cancel", "pause"):
        sess["step"] = "paused"
        state["sessions"][handle] = sess
        _save(state)
        return "paused — text 'go' whenever you're ready to pick back up."

    if step in ("greet", "paused"):
        sess["step"] = "confirm"
        state["sessions"][handle] = sess
        _save(state)
        return _greeting(tenant)

    if step == "confirm":
        if _affirmative(t):
            return _advance(handle)
        return "no rush — reply 'go' when you're ready."

    if step == "capturing":
        if "skip" in t:
            # mark current pending as skipped and move on
            for key in sess.get("pending_accounts", []):
                if not sess.get("captured", {}).get(key):
                    sess.setdefault("captured", {})[key] = "skipped"
                    break
            state["sessions"][handle] = sess
            _save(state)
            return _advance(handle)
        if _affirmative(t) or "done" in t or "next" in t:
            return _advance(handle)
        return "when you've finished that link, text 'next'. or 'skip' to do it later."

    if step == "done":
        return "you're fully set up. text 'go' to re-run if a system needs reconnecting."
    return None


# ---------------------------------------------------------------------------
# Server-side capture handler — invoked by the app.py form POST behind the link.
# ---------------------------------------------------------------------------
def capture_secret(token: str, value: str) -> dict:
    """Validate+burn the magic token, store the secret encrypted (chmod600, never
    logged), mint a session, advance the wizard. Called by the capture form."""
    user_id = validate_magic_token(token)  # one-time: burns the token
    if not user_id:
        return {"ok": False, "error": "link expired or already used"}
    parts = user_id.split(_UID_SEP)
    if len(parts) != 4:
        return {"ok": False, "error": "malformed token target"}
    tenant, account, field, handle = parts
    spec = _account_spec(tenant, account) or {}
    method = spec.get("method", "app_password")

    # 1) Persist the secret — keychain namespace + per-tenant encrypted token.
    stored = tenant_secrets.set_tenant_secret(tenant, account, field or "credential", value)
    try:
        client_vault.store_tenant_token(tenant, account, {"field": field or "credential",
                                                          "method": method})
    except Exception:
        pass

    # 2) Mint a session from the captured material (best-effort, method-aware).
    minted = _mint_session(tenant, account, field, value, method)

    # 3) Advance the wizard and notify the sender.
    state = _load()
    sess = state["sessions"].get(handle)
    if sess is not None:
        sess.setdefault("captured", {})[account] = True
        state["sessions"][handle] = sess
        _save(state)
    reply = _advance(handle) if sess is not None else ""
    return {"ok": bool(stored), "tenant": tenant, "account": account,
            "session_minted": minted, "next": reply}


_CRED_FIELDS = ("username", "password", "totp_seed")


def capture_credentials(token: str, values: dict) -> dict:
    """IT-FREE posture (Operator 2026-07-16): capture the person's normal login credentials
    (username + password + optional TOTP base32 seed) via one simple form, store them
    ENCRYPTED per-tenant, then advance. The machine later DRIVES the real login headlessly
    with these (+ a TOTP code generated from the seed) and captures the session for the local
    bridge — same pattern the stack already uses for the operator's own accounts
    (ms_username/ms_password/ms_totp_secret). Never logs values."""
    user_id = validate_magic_token(token)  # one-time: burns the token
    if not user_id:
        return {"ok": False, "error": "link expired or already used"}
    parts = user_id.split(_UID_SEP)
    if len(parts) != 4:
        return {"ok": False, "error": "malformed token target"}
    tenant, account, _field, handle = parts
    stored = 0
    for k in _CRED_FIELDS:
        v = str(values.get(k) or "").strip().replace(" ", "") if k == "totp_seed" else str(values.get(k) or "").strip()
        if v:
            tenant_secrets.set_tenant_secret(tenant, account, k, v)
            stored += 1
    if stored == 0:
        return {"ok": False, "error": "no credentials provided"}
    try:
        client_vault.store_tenant_token(tenant, account, {"method": "credentials", "fields": stored})
    except Exception:
        pass
    state = _load()
    sess = state["sessions"].get(handle)
    reply = ""
    if sess is not None:
        sess.setdefault("captured", {})[account] = True
        state["sessions"][handle] = sess
        _save(state)
        reply = _advance(handle)
    return {"ok": True, "tenant": tenant, "account": account, "stored": stored, "next": reply}


def complete_oauth(token: str) -> dict:
    """Finalize an OAuth / device-code capture whose token was already persisted by
    the provider flow (app.py Google callback / core.credentials_ops device-code
    poller → client_vault / token_store). Burns the magic token (single-use), marks
    the account captured, advances the wizard, texts the next step. Handles NO secret
    here — the real credential is already stored per-tenant; this only moves the flow
    forward. Records lightweight method metadata in client_vault for tracking."""
    user_id = validate_magic_token(token)  # one-time: burns the token
    if not user_id:
        return {"ok": False, "error": "link expired or already used"}
    parts = user_id.split(_UID_SEP)
    if len(parts) != 4:
        return {"ok": False, "error": "malformed token target"}
    tenant, account, field, handle = parts
    spec = _account_spec(tenant, account) or {}
    method = spec.get("method", "oauth")
    try:
        client_vault.store_tenant_token(tenant, account, {"field": field or "credential",
                                                          "method": method})
    except Exception:
        pass
    state = _load()
    sess = state["sessions"].get(handle)
    reply = ""
    if sess is not None:
        sess.setdefault("captured", {})[account] = True
        state["sessions"][handle] = sess
        _save(state)
        reply = _advance(handle)
    return {"ok": True, "tenant": tenant, "account": account, "next": reply}


def capture_session(token: str, login_url: str, *, timeout_s: int = 480) -> dict:
    """Headed one-time login capture behind a session-capture (or OAuth-fallback)
    link. Opens a VISIBLE browser to login_url, lets the operator sign in, saves the
    storage_state to the tenant auth.json (chmod600), then burns the magic token and
    advances the wizard via capture_secret. Mirrors the reddit/persona3 login-capture
    pattern (a login can't be captured headless); never handles a raw password. The
    operator ends the capture by closing the window once signed in."""
    from core.magic_link import peek_magic_token
    user_id = peek_magic_token(token)
    if not user_id:
        return {"ok": False, "error": "link expired or already used"}
    parts = user_id.split(_UID_SEP)
    if len(parts) != 4:
        return {"ok": False, "error": "malformed token target"}
    tenant, account, _field, _handle = parts
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return {"ok": False, "error": "capture runtime unavailable"}
    import time as _t
    storage = None
    try:
        require_interactive_headful(f"session capture ({tenant}/{account})")
        with sync_playwright() as p:
            browser = p.chromium.launch(  # headful-ok
                headless=False, args=["--disable-blink-features=AutomationControlled"])
            ctx = browser.new_context()
            page = ctx.new_page()
            try:
                page.goto(login_url, wait_until="domcontentloaded", timeout=30000)
            except Exception:
                pass
            deadline = _t.time() + timeout_s
            # Keep the latest non-empty snapshot; the operator closes the window
            # (disconnect) when the login lands, or the deadline caps the wait.
            while browser.is_connected() and _t.time() < deadline:
                _t.sleep(2)
                try:
                    cur = ctx.storage_state()
                except Exception:
                    break
                if cur.get("cookies"):
                    storage = cur
            try:
                browser.close()
            except Exception:
                pass
    except Exception:
        return {"ok": False, "error": "capture failed"}
    if not storage or not storage.get("cookies"):
        return {"ok": False, "error": "no session captured"}
    # Write the tenant auth.json regardless of method so OAuth-fallback captures are
    # also usable (capture_secret._mint_session only writes it for session_capture).
    try:
        sp = tenant_secrets.session_path(tenant, account)
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text(json.dumps(storage))
        sp.chmod(0o600)
    except Exception:
        pass
    return capture_secret(token, json.dumps(storage))


def _mint_session(tenant: str, account: str, field: str, value: str, method: str) -> bool:
    """For session_capture, persist a storage_state auth.json (chmod600), mirroring
    the login-capture pattern. OAuth/device-code sessions are minted by their own
    flows (credentials_ops), not here."""
    if method != "session_capture":
        return False
    try:
        p = tenant_secrets.session_path(tenant, account)
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            payload = json.loads(value)
            if not isinstance(payload, dict):
                payload = {"raw": value}
        except Exception:
            payload = {"raw": value}
        p.write_text(json.dumps(payload))
        p.chmod(0o600)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Inbound-text listener — generalizes imsg_bridge.py's read-only chat.db poll off
# its single hardcoded TARGET to watch EVERY enrolled handle. A message from a
# non-enrolled handle is never even selected (and handle_message re-gates it), so
# unknown senders are silently ignored — imsg_bridge parity.
# ---------------------------------------------------------------------------
_LISTEN_STATE = Path.home() / ".onboarding_wizard_listen.json"
_MESSAGES_DB = os.path.expanduser("~/Library/Messages/chat.db")


def _listen_load_last():
    try:
        return int(json.loads(_LISTEN_STATE.read_text()).get("last_rowid"))
    except Exception:
        return None


def _listen_save_last(rowid: int) -> None:
    try:
        _LISTEN_STATE.write_text(json.dumps({"last_rowid": int(rowid)}))
        _LISTEN_STATE.chmod(0o600)
    except Exception:
        pass


def _handle_variants(handle: str) -> list[str]:
    """Match a stored handle against chat.db handle.id spellings (imsg_bridge does
    the +1 strip for its one TARGET; do it for every enrolled handle)."""
    h = str(handle or "").strip()
    out = {h}
    if h.startswith("+1"):
        out.add(h[2:])
    elif h.startswith("+"):
        out.add(h[1:])
    return [x for x in out if x]


def _listen_poll_once(conn) -> None:
    enrolled = list(_load().get("enrolled", {}).keys())
    if not enrolled:
        return
    hid2handle: dict[int, str] = {}
    for handle in enrolled:
        variants = _handle_variants(handle)
        if not variants:
            continue
        ph = ",".join("?" * len(variants))
        for r in conn.execute(f"SELECT ROWID FROM handle WHERE id IN ({ph})", variants).fetchall():
            hid2handle[r["ROWID"]] = handle
    if not hid2handle:
        return
    last = _listen_load_last()
    maxrow = conn.execute("SELECT MAX(ROWID) AS m FROM message").fetchone()["m"] or 0
    if last is None:
        _listen_save_last(maxrow)  # first run: never replay history (imsg_bridge parity)
        return
    hids = list(hid2handle.keys())
    ph = ",".join("?" * len(hids))
    rows = conn.execute(
        "SELECT ROWID, text, handle_id FROM message "
        "WHERE ROWID > ? AND is_from_me = 0 AND handle_id IN (%s) "
        "AND text IS NOT NULL ORDER BY ROWID ASC" % ph,
        [last] + hids).fetchall()
    new_last = last
    for r in rows:
        new_last = max(new_last, r["ROWID"])
        handle = hid2handle.get(r["handle_id"])
        if not handle:
            continue
        reply = handle_message(handle, r["text"])  # authorization-gated inside
        if reply:
            _text(handle, reply)
    _listen_save_last(max(new_last, maxrow))


def listen(interval_s: int = 5) -> int:
    """Long-running poll loop (KeepAlive plist restarts it if it dies)."""
    import sqlite3
    import time as _t
    while True:
        try:
            conn = sqlite3.connect("file:%s?mode=ro" % _MESSAGES_DB, uri=True)
            conn.row_factory = sqlite3.Row
            try:
                _listen_poll_once(conn)
            finally:
                conn.close()
        except Exception:
            pass
        _t.sleep(interval_s)
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _status(tenant_id: str) -> dict:
    state = _load()
    tenant_id = str(tenant_id or "").strip().lower()
    handles = [h for h, tid in state.get("enrolled", {}).items() if tid == tenant_id]
    out = {"tenant": tenant_id, "handles": handles, "accounts": []}
    for a in plan_for(tenant_id):
        captured_any = any(
            state.get("sessions", {}).get(h, {}).get("captured", {}).get(a["key"])
            for h in handles
        )
        out["accounts"].append({
            "key": a["key"], "label": a["label"], "method": a["method"],
            "human_login": a.get("human_login", False), "captured": bool(captured_any),
        })
    return out


def main(argv: list[str]) -> int:
    if len(argv) >= 4 and argv[1] == "enroll":
        print(json.dumps(enroll(argv[2], argv[3]), indent=2))
        return 0
    if len(argv) >= 3 and argv[1] == "status":
        print(json.dumps(_status(argv[2]), indent=2))
        return 0
    if len(argv) >= 3 and argv[1] == "accounts":
        print(json.dumps(plan_for(argv[2]), indent=2))
        return 0
    if len(argv) >= 4 and argv[1] == "add-account":
        # add-account <tenant> <label> [method] [login_url]
        print(json.dumps(add_account(argv[2], argv[3],
                                     method=(argv[4] if len(argv) >= 5 else "session_capture"),
                                     login_url=(argv[5] if len(argv) >= 6 else "")), indent=2))
        return 0
    if len(argv) >= 3 and argv[1] == "start":
        # start <handle> — operator-initiated kickoff (texts the greeting). OUTWARD: sends a real text.
        print(json.dumps(start_onboarding(argv[2]), indent=2))
        return 0
    if len(argv) >= 2 and argv[1] == "listen":
        return listen()
    if len(argv) >= 4 and argv[1] == "capture-session":
        print(json.dumps(capture_session(argv[2], argv[3]), indent=2))
        return 0
    print("usage: onboarding_wizard.py enroll <tenant_id> <handle> | status <tenant_id> "
          "| accounts <tenant_id> | listen | capture-session <token> <login_url>")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
