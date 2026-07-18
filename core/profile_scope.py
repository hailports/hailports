"""Profile/source scoping helpers for shared WorkOS and LifeOS surfaces."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Iterable


PARTNER_USERS = {"partner", "Operator2"}

NICOLE_WORK_TOKENS = (
    "kipi",
    "kipi.ai",
    "cnovate",
    "cnovate.io",
)

NICOLE_LIFE_TOKENS = (
    "gmail.com",
    "icloud.com",
    "yahoo.com",
    "family",
    "home",
    "kids",
    "school",
    "birthday",
    "delivery",
    "pickup",
    "walmart",
    "usps",
)


def is_partner_user(user_id: str | None) -> bool:
    return str(user_id or "").strip().lower() in PARTNER_USERS


def normalize_profile_user(user_id: str | None) -> str:
    token = str(user_id or "").strip().lower()
    if token in PARTNER_USERS:
        return "Operator2"
    return token or "Operator"


def _flatten_values(value) -> Iterable[str]:
    if value is None:
        return
    if isinstance(value, dict):
        for nested in value.values():
            yield from _flatten_values(nested)
        return
    if isinstance(value, (list, tuple, set)):
        for nested in value:
            yield from _flatten_values(nested)
        return
    yield str(value)


def item_text(item: dict | None) -> str:
    if not isinstance(item, dict):
        return ""
    return " ".join(_flatten_values(item)).lower()


def classify_nicole_item(item: dict | None) -> str:
    """Return work, life, or unknown for Operator2-profile data.

    Work requires an explicit Kipi/Cnovate signal. Unknown items stay out of
    WorkOS and can appear in LifeOS so personal queues are not silently dropped.
    """

    text = item_text(item)
    if not text:
        return "unknown"
    scope_hint = str((item or {}).get("scope") or "").strip().lower()
    if scope_hint in {"work", "life"}:
        return scope_hint
    if bool((item or {}).get("work_tier")):
        return "work"
    if any(token in text for token in NICOLE_WORK_TOKENS):
        return "work"
    if any(token in text for token in NICOLE_LIFE_TOKENS):
        return "life"
    return "unknown"


def is_nicole_work_item(item: dict | None) -> bool:
    return classify_nicole_item(item) == "work"


def is_nicole_life_item(item: dict | None) -> bool:
    return classify_nicole_item(item) != "work"


def scoped_item(item: dict, *, default_scope: str = "unknown") -> dict:
    out = dict(item or {})
    scope = classify_nicole_item(out)
    out["scope"] = scope if scope != "unknown" else default_scope
    return out


def filter_nicole_work_items(items: Iterable[dict]) -> list[dict]:
    return [scoped_item(item, default_scope="work") for item in items if is_nicole_work_item(item)]


def filter_nicole_life_items(items: Iterable[dict]) -> list[dict]:
    return [scoped_item(item, default_scope="life") for item in items if is_nicole_life_item(item)]


def parse_loose_datetime(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except Exception:
            return None
    text = str(value).strip()
    if not text:
        return None
    for candidate in (
        text,
        text.replace("Z", "+00:00"),
    ):
        try:
            dt = datetime.fromisoformat(candidate)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            pass
    try:
        dt = parsedate_to_datetime(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def item_within_days(item: dict, days: int = 7, now: datetime | None = None) -> bool:
    """Best-effort freshness filter. Unparseable open items remain visible."""

    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    cutoff = now - timedelta(days=max(1, int(days or 7)))
    for key in ("ts_seen", "last_seen", "date", "received_at", "updated_at", "when", "start", "start_raw"):
        dt = parse_loose_datetime((item or {}).get(key))
        if not dt:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc) >= cutoff
    return True
