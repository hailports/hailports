"""Local WebUI magic-link and login-code store."""

from __future__ import annotations

import json
import logging
import re
import secrets
import subprocess
import time
from pathlib import Path
from urllib.parse import urlencode


_STATE_PATH = Path(__file__).resolve().parent.parent / "data" / "runtime" / "webui_magic_links.json"
_MAGIC_TTL_S = 10 * 60
_CODE_TTL_S = 5 * 60
_RESEND_COOLDOWN_S = 30
_LOG = logging.getLogger(__name__)


def _load_state() -> dict:
    try:
        if _STATE_PATH.exists():
            payload = json.loads(_STATE_PATH.read_text())
            if isinstance(payload, dict):
                payload.setdefault("tokens", {})
                payload.setdefault("codes", {})
                return payload
    except Exception:
        pass
    return {"tokens": {}, "codes": {}}


def _save_state(payload: dict) -> None:
    _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _STATE_PATH.write_text(json.dumps(payload, indent=2, sort_keys=True))


def _delivery_handles(handles) -> list[str]:
    result: list[str] = []
    seen = set()
    if not handles:
        return result
    if isinstance(handles, str):
        handles = [handles]
    for handle in handles:
        text = str(handle or "").strip()
        digits = re.sub(r"\D+", "", text)
        key = ("phone:" + digits[-10:]) if digits and "@" not in text and len(digits) >= 10 else text.lower()
        if not text or key in seen:
            continue
        seen.add(key)
        result.append(text)
    return sorted(result, key=lambda item: 0 if any(ch.isdigit() for ch in str(item)) and "@" not in str(item) else 1)


def _osa_string(value: str) -> str:
    text = str(value or "")
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def _redact_handle(handle: str) -> str:
    text = str(handle or "").strip()
    if not text:
        return ""
    if "@" in text:
        local, _, domain = text.partition("@")
        return f"{local[:2]}***@{domain}"
    return f"{text[:3]}***{text[-2:]}" if len(text) > 5 else "***"


def _send_messages_text(text: str, handles) -> tuple[bool, str]:
    """Send auth text through Messages, using later handles only as fallback."""
    last_error = "No iMessage handle configured"
    sent_channels: list[str] = []
    for handle in _delivery_handles(handles):
        script = f'''
tell application "Messages"
    set targetText to {_osa_string(handle)}
    set bodyText to {_osa_string(text)}
    try
        set targetService to 1st service whose service type = iMessage
        set targetBuddy to buddy targetText of targetService
        send bodyText to targetBuddy
        return "imessage"
    on error imErr
        try
            set smsService to 1st service whose service type = SMS
            set smsBuddy to buddy targetText of smsService
            send bodyText to smsBuddy
            return "sms"
        on error smsErr
            error "iMessage failed: " & imErr & "; SMS failed: " & smsErr
        end try
    end try
end tell
'''
        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            last_error = f"Timed out sending to {_redact_handle(handle)}"
            _LOG.warning("Magic-link delivery timed out to %s", _redact_handle(handle))
            continue
        except Exception as exc:
            last_error = f"Messages unavailable: {exc}"
            _LOG.warning("Magic-link delivery failed before osascript: %s", exc)
            continue
        if result.returncode == 0:
            channel = (result.stdout or "messages").strip() or "messages"
            _LOG.info("Delivered WebUI auth message to %s via %s", _redact_handle(handle), channel)
            sent_channels.append(f"{channel}:{_redact_handle(handle)}")
            return True, "Sent via " + ", ".join(sent_channels)
        last_error = (result.stderr or result.stdout or "Messages send failed").strip()
        _LOG.warning(
            "Magic-link delivery failed to %s: %s",
            _redact_handle(handle),
            last_error[:240],
        )
    if sent_channels:
        return True, "Sent via " + ", ".join(sent_channels)
    return False, last_error


def _prune(payload: dict) -> dict:
    now = time.time()
    payload["tokens"] = {
        key: value
        for key, value in (payload.get("tokens") or {}).items()
        if isinstance(value, dict) and float(value.get("expires_at") or 0) > now
    }
    payload["codes"] = {
        key: value
        for key, value in (payload.get("codes") or {}).items()
        if isinstance(value, dict) and float(value.get("expires_at") or 0) > now
    }
    return payload


def _magic_path(token: str, native_scheme: str = "") -> str:
    params = {"token": token}
    scheme = str(native_scheme or "").strip().lower()
    if scheme:
        params["native_scheme"] = scheme
    return "/auth/magic?" + urlencode(params)


def _absolute_url(path: str, base_url: str = "") -> str:
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        return path
    if not base.startswith(("http://", "https://")):
        base = "https://" + base
    return base + path


def create_magic_link(
    username: str,
    user_id: str,
    native_scheme: str = "",
    *,
    base_url: str = "",
    delivery_handles=None,
) -> tuple[bool, str]:
    token = secrets.token_urlsafe(32)
    payload = _prune(_load_state())
    payload.setdefault("tokens", {})[token] = {
        "username": str(username or "").strip().lower(),
        "user_id": str(user_id or "").strip(),
        "native_scheme": str(native_scheme or "").strip().lower(),
        "expires_at": time.time() + _MAGIC_TTL_S,
    }
    _save_state(payload)
    path = _magic_path(token, native_scheme=native_scheme)
    link = _absolute_url(path, base_url=base_url)
    handles = _delivery_handles(delivery_handles)
    if not handles:
        return True, link
    ok, detail = _send_messages_text(
        f"Your ClaudeStack sign-in link expires in 10 minutes:\n{link}",
        handles,
    )
    if ok:
        return True, "Magic link sent"
    return False, f"Could not send magic link: {detail}"


def validate_magic_token(token: str) -> str:
    token = str(token or "").strip()
    if not token:
        return ""
    payload = _prune(_load_state())
    entry = (payload.get("tokens") or {}).pop(token, None)
    _save_state(payload)
    if not isinstance(entry, dict):
        return ""
    if float(entry.get("expires_at") or 0) < time.time():
        return ""
    return str(entry.get("user_id") or "")


def create_login_code(username: str, user_id: str, *, delivery_handles=None) -> tuple[bool, str]:
    key = f"{str(username or '').strip().lower()}:{str(user_id or '').strip()}"
    payload = _prune(_load_state())
    existing = (payload.get("codes") or {}).get(key)
    now = time.time()
    handles = _delivery_handles(delivery_handles)
    if (
        handles
        and isinstance(existing, dict)
        and float(existing.get("expires_at") or 0) > now
        and now - float(existing.get("last_sent_at") or 0) < _RESEND_COOLDOWN_S
    ):
        return True, "Code already sent"

    code = f"{secrets.randbelow(1_000_000):06d}"
    payload.setdefault("codes", {})[key] = {
        "code": code,
        "expires_at": now + _CODE_TTL_S,
        "last_sent_at": now if handles else 0,
    }
    _save_state(payload)
    if handles:
        ok, detail = _send_messages_text(
            f"Your ClaudeStack verification code is {code}. It expires in 5 minutes.",
            handles,
        )
        if ok:
            return True, "Code sent"
        return False, f"Could not send code: {detail}"
    return True, code


def validate_login_code(username: str, user_id: str, code: str) -> bool:
    key = f"{str(username or '').strip().lower()}:{str(user_id or '').strip()}"
    payload = _prune(_load_state())
    entry = (payload.get("codes") or {}).pop(key, None)
    _save_state(payload)
    if not isinstance(entry, dict):
        return False
    if float(entry.get("expires_at") or 0) < time.time():
        return False
    return secrets.compare_digest(str(entry.get("code") or ""), str(code or "").strip())
