"""Operator2 work-scope tools backed by local mail, calendar, and Littlebird data."""

from __future__ import annotations

import json
from pathlib import Path

from core.profile_scope import (
    filter_nicole_work_items,
    item_within_days,
    is_nicole_work_item,
)
from tools.base import BaseTool, make_tool_def


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"


def _load_json(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text())
    except Exception:
        pass
    return default


def _open_mail_item(row: dict) -> bool:
    urgency = str((row or {}).get("urgency") or "").strip().lower()
    if urgency in {"urgent", "action-needed", "high"}:
        return True
    if bool((row or {}).get("needs_reply")):
        return True
    if (row or {}).get("read") is False:
        return True
    return False


def _mail_item(row: dict, source: str) -> dict:
    return {
        "source": source,
        "type": "email",
        "account": row.get("account", ""),
        "from": row.get("from", "") or row.get("from_name", ""),
        "subject": row.get("subject", ""),
        "urgency": row.get("urgency", "open"),
        "needs_reply": bool(row.get("needs_reply")),
        "date": row.get("date", "") or row.get("last_seen", "") or row.get("ts_seen", ""),
        "preview": row.get("summary", "") or row.get("preview", ""),
        "message_id": row.get("message_id", ""),
    }


def _cached_work_mail(days: int) -> list[dict]:
    rows = _load_json(DATA_DIR / "nicole_inbox_state.json", [])
    if not isinstance(rows, list):
        return []
    items = []
    for row in rows:
        if not isinstance(row, dict) or not _open_mail_item(row):
            continue
        if not item_within_days(row, days):
            continue
        item = _mail_item(row, "nicole_work_mail_cache")
        if is_nicole_work_item(item):
            items.append(item)
    return items


def _live_work_mail(days: int, count: int) -> list[dict]:
    try:
        from tools.nicole_mail import get_nicole_inbox

        rows = get_nicole_inbox(count=max(20, min(count, 100)))
    except Exception:
        return []
    items = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        item = _mail_item(row, "nicole_work_mail")
        if is_nicole_work_item(item):
            items.append(item)
    return items


def _work_calendar(days: int) -> list[dict]:
    try:
        from tools.nicole_mail import get_nicole_calendar_events

        rows = get_nicole_calendar_events(days=max(1, min(days, 14)))
    except Exception:
        return []
    items = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        item = {
            "source": "nicole_work_calendar",
            "type": "calendar",
            "subject": row.get("subject", ""),
            "calendar": row.get("calendar", ""),
            "time": row.get("time_display", "") or row.get("start_raw", ""),
            "start_raw": row.get("start_raw", ""),
        }
        if is_nicole_work_item(item):
            items.append(item)
    return items


async def _littlebird_actions(limit: int = 10) -> list[dict]:
    try:
        from tools.littlebird_local import LittlebirdLocalTool

        raw = await LittlebirdLocalTool().handle(
            "littlebird_action_items",
            {"user": "Operator2", "count": limit},
        )
    except Exception:
        return []
    text = str(raw or "").strip()
    if not text or text.startswith("No action items"):
        return []
    return [{
        "source": "nicole_littlebird",
        "type": "meeting_action_items",
        "subject": "Littlebird meeting action items",
        "preview": text[:1600],
        "scope": "work",
    }]


def _work_insights(days: int) -> list[dict]:
    payload = _load_json(DATA_DIR / "nicole_insights.json", {})
    if not isinstance(payload, dict):
        return []
    out = []
    for row in list(payload.get("action_items") or []) + list(payload.get("insights") or []):
        if not isinstance(row, dict):
            continue
        if not is_nicole_work_item(row):
            continue
        out.append({
            "source": "nicole_work_insights",
            "type": "insight",
            "scope": "work",
            "subject": row.get("text", ""),
            "urgency": row.get("urgency", row.get("level", "open")),
            "due": row.get("due", ""),
        })
    return out


def _dedupe(items: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for item in items:
        key = (
            str(item.get("source", "")),
            str(item.get("account", "")),
            str(item.get("message_id", "")),
            str(item.get("subject", "")).strip().lower(),
            str(item.get("time", "")),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


class NicoleWorkTool(BaseTool):
    name = "nicole_work"
    description = "Operator2 work-only open items across local mail, calendar, Littlebird, and cached insights."

    def get_definitions(self):
        return [
            make_tool_def(
                "nicole_work_open_items",
                "Get Operator2's open work items from clear work systems only: Kipi/Cnovate mail, work calendars, Littlebird, and work-tagged insights.",
                {
                    "days": {"type": "integer", "description": "Lookback window in days (default 7)"},
                    "count": {"type": "integer", "description": "Max mail rows to inspect (default 80)"},
                },
                [],
            )
        ]

    async def handle(self, tool_name: str, tool_input: dict) -> str:
        if tool_name != "nicole_work_open_items":
            return json.dumps({"error": f"Unknown Operator2 work tool: {tool_name}"})
        days = int((tool_input or {}).get("days") or 7)
        count = int((tool_input or {}).get("count") or 80)
        mail = _dedupe(_live_work_mail(days, count) + _cached_work_mail(days))
        calendar = _work_calendar(days)
        insights = _work_insights(days)
        littlebird = await _littlebird_actions(limit=10)
        items = filter_nicole_work_items(mail + calendar + insights) + littlebird
        payload = {
            "profile": "Operator2",
            "scope": "work",
            "lookback_days": days,
            "sources_checked": [
                "kipi_cnovate_mail",
                "nicole_work_calendar",
                "nicole_littlebird_synced_profile",
                "nicole_work_insights_cache",
            ],
            "counts": {
                "mail": len(mail),
                "calendar": len(calendar),
                "insights": len(insights),
                "littlebird": len(littlebird),
                "total": len(items),
            },
            "items": items[:30],
        }
        return json.dumps(payload, indent=2)
