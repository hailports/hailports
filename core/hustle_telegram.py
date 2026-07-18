"""Side hustle Telegram channel with revenue-only notification gating.

Revenue agents may still discover opportunities, draft assets, and log work.
Telegram is reserved for confirmed money events so the operator is not paged
for every lead, published article, draft, or recoverable crash.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

BASE = Path(os.path.expanduser("~/claude-stack"))
SUPPRESSED_LOG = BASE / "data" / "hustle" / "telegram_suppressed.jsonl"
_HUSTLE_TOPIC_ID = int(os.environ.get("HUSTLE_TELEGRAM_TOPIC_ID", "0"))
_MAX_MESSAGE = 4000

# Modes:
# - revenue_only: default; only confirmed revenue/sale/payment events notify.
# - silent: never send to Telegram.
# - all: legacy behavior, useful only for manual debugging.
_MODE = os.environ.get("HUSTLE_TELEGRAM_MODE", "revenue_only").strip().lower()

_CONFIRMED_REVENUE_TERMS = (
    "revenue obtained",
    "payment received",
    "invoice paid",
    "paid invoice",
    "deposit received",
    "order placed",
    "order received",
    "gig purchased",
    "purchase confirmed",
    "sale confirmed",
    "closed won",
    "payout received",
    "stripe payment",
    "paypal payment",
    "cash received",
    "contract signed and paid",
)

_NOISY_TERMS = (
    "new job",
    "new jobs",
    "new lead",
    "new leads",
    "lead list",
    "added to outreach",
    "published:",
    "manual post",
    "manual creation",
    "draft",
    "queued",
    "crashed",
    "failed",
    "needed",
    "need ",
    "skip",
    "proposal:",
)


def alerts_disabled() -> bool:
    try:
        from core import telegram_disabled

        if telegram_disabled():
            return True
    except Exception:
        pass
    return False


def _load_token() -> str:
    if alerts_disabled():
        return ""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if token:
        return token
    try:
        from dotenv import load_dotenv

        load_dotenv(BASE / ".env")
    except Exception:
        pass
    return os.environ.get("TELEGRAM_BOT_TOKEN", "")


def _load_chat_id() -> int | str:
    if alerts_disabled():
        return 0
    try:
        import tomllib
    except ImportError:  # pragma: no cover - py<3.11 fallback
        import tomli as tomllib
    try:
        with (BASE / "config" / "users.toml").open("rb") as f:
            users = tomllib.load(f)
        return users.get("Operator", {}).get("telegram_id", 0)
    except Exception:
        return 0


def _chunk(text: str, size: int = _MAX_MESSAGE):
    if len(text) <= size:
        yield text
        return
    remaining = text
    while remaining:
        if len(remaining) <= size:
            yield remaining
            return
        cut = remaining.rfind("\n", 0, size)
        if cut == -1:
            cut = size
        yield remaining[:cut]
        remaining = remaining[cut:].lstrip()


def _event_id(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()[:16]


def _is_confirmed_revenue(text: str) -> bool:
    lowered = text.lower()
    if any(term in lowered for term in _NOISY_TERMS):
        return any(term in lowered for term in _CONFIRMED_REVENUE_TERMS)
    return any(term in lowered for term in _CONFIRMED_REVENUE_TERMS)


def _record_suppressed(text: str, reason: str) -> None:
    try:
        SUPPRESSED_LOG.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "ts": time.time(),
            "reason": reason,
            "event_id": _event_id(text),
            "preview": text[:500],
        }
        with SUPPRESSED_LOG.open("a") as f:
            f.write(json.dumps(row, ensure_ascii=True) + "\n")
    except Exception:
        log.exception("Failed writing suppressed hustle Telegram event")


def should_notify_hustle(text: str) -> bool:
    """Return True only when the operator should be paged on Telegram."""
    if _MODE == "all":
        return True
    return _is_confirmed_revenue(text or "")


def send_hustle(text: str, parse_mode=None) -> int:
    """Send confirmed revenue events to the side hustle topic/channel.

    Non-revenue events are intentionally suppressed and written to the local
    audit log. The return value stays compatible with the old helper: number
    of Telegram chunks actually sent.

    PURGED 2026-06-12: Telegram removed from the stack. This is a hard no-op — the API
    is kept so the 90+ callers don't break, but nothing is ever sent to Telegram.
    """
    return 0
    text = str(text or "").strip()
    if not text:
        return 0
    if alerts_disabled():
        _record_suppressed(text, "telegram_disabled")
        return 0
    if _MODE == "silent":
        _record_suppressed(text, "silent_mode")
        return 0
    if not should_notify_hustle(text):
        _record_suppressed(text, "revenue_only_policy")
        return 0

    token = _load_token()
    chat_id = _load_chat_id()
    if not token or not chat_id:
        _record_suppressed(text, "missing_telegram_config")
        return 0

    sent = 0
    for chunk in _chunk(text):
        if not _HUSTLE_TOPIC_ID:
            chunk = f"[REVENUE] {chunk}"

        payload = {"chat_id": chat_id, "text": chunk}
        if _HUSTLE_TOPIC_ID:
            payload["message_thread_id"] = _HUSTLE_TOPIC_ID
        if parse_mode:
            payload["parse_mode"] = parse_mode

        try:
            data = json.dumps(payload).encode()
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data=data,
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=10)
            sent += 1
        except Exception as e:
            log.warning("Hustle Telegram send failed: %s", e)
    return sent
