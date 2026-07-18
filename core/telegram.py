"""Shared Telegram send helper — used by every agent that posts notifications.

Reads TELEGRAM_BOT_TOKEN from .env and chat IDs from config/users.toml.
Supports Markdown formatting (optional), long-message chunking (Telegram's
4096-char limit), and silent per-user send (never raises).
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from pathlib import Path

from core import BASE_DIR

log = logging.getLogger(__name__)

_MAX_MESSAGE = 4000  # Telegram cap is 4096; leave a safety margin.


def alerts_disabled() -> bool:
    try:
        from core import telegram_disabled

        if telegram_disabled():
            return True
    except Exception:
        pass
    return False


def _load_token():
    if alerts_disabled():
        return ""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if token:
        return token
    # Fallback: import-time env may not have loaded .env yet.
    try:
        from dotenv import load_dotenv
        load_dotenv(BASE_DIR / ".env")
    except Exception:
        pass
    return os.environ.get("TELEGRAM_BOT_TOKEN", "")


def _load_users():
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib
    try:
        with open(BASE_DIR / "config" / "users.toml", "rb") as f:
            return tomllib.load(f)
    except Exception as e:
        log.warning(f"users.toml load failed: {e}")
        return {}


def _chunk(text: str, size: int = _MAX_MESSAGE):
    """Split a long message into Telegram-safe chunks, preferring paragraph boundaries."""
    if len(text) <= size:
        yield text
        return
    remaining = text
    while remaining:
        if len(remaining) <= size:
            yield remaining
            return
        cut = remaining.rfind("\n\n", 0, size)
        if cut == -1:
            cut = remaining.rfind("\n", 0, size)
        if cut == -1:
            cut = remaining.rfind(" ", 0, size)
        if cut == -1:
            cut = size
        yield remaining[:cut]
        remaining = remaining[cut:].lstrip()


def _send_raw(chat_id, text: str, parse_mode: str | None = None) -> bool:
    return False  # PURGED 2026-06-12: Telegram removed — hard no-op, API kept for callers.
    if str(text or "").lstrip().startswith("[REVENUE]"):
        try:
            from core.hustle_telegram import _record_suppressed, should_notify_hustle

            if not should_notify_hustle(text):
                _record_suppressed(text, "generic_telegram_revenue_gate")
                return False
        except Exception:
            log.warning("Revenue Telegram gate failed closed")
            return False
    if alerts_disabled():
        return False
    token = _load_token()
    if not token:
        log.warning("TELEGRAM_BOT_TOKEN not set — skipping send")
        return False
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=data, headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception as e:
        log.warning(f"Telegram send failed to {chat_id}: {e}")
        return False


def send_to_chat(chat_id, text: str, parse_mode: str | None = None) -> int:
    """Send one or more chunks to a specific chat_id. Returns chunks sent successfully."""
    if not chat_id:
        return 0
    sent = 0
    for chunk in _chunk(text):
        if _send_raw(chat_id, chunk, parse_mode=parse_mode):
            sent += 1
    return sent


def send_to_user(user_key: str, text: str, parse_mode: str | None = None) -> int:
    """Send to a named user from users.toml (keys: 'Operator', 'partner', etc.)."""
    users = _load_users()
    profile = users.get(user_key) or {}
    chat_id = profile.get("telegram_id")
    if not chat_id:
        log.warning(f"No telegram_id for user {user_key!r}")
        return 0
    return send_to_chat(chat_id, text, parse_mode=parse_mode)


def send_to_alex(text: str, parse_mode: str | None = None) -> int:
    """Shortcut for the primary user."""
    return send_to_user("Operator", text, parse_mode=parse_mode)


def broadcast(text: str, parse_mode: str | None = None) -> int:
    """Send to every user with a telegram_id. Returns chunk count."""
    users = _load_users()
    total = 0
    for key, profile in users.items():
        cid = (profile or {}).get("telegram_id")
        if cid:
            total += send_to_chat(cid, text, parse_mode=parse_mode)
    return total
