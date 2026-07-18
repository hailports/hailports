"""Strategy operations tool for revenue/content channel directives."""

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from tools.base import BaseTool, make_tool_def


ROOT = Path(os.environ.get("CLAUDE_STACK_ROOT", Path.home() / "claude-stack"))
HUSTLE_DIR = ROOT / "data" / "hustle"
DIRECTIVES_PATH = HUSTLE_DIR / "strategy_directives.jsonl"
OVERRIDES_PATH = HUSTLE_DIR / "strategy_overrides.json"
REQUESTS_DIR = HUSTLE_DIR / "strategy_requests"
STRATEGY_PATH = HUSTLE_DIR / "strategy.json"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def _tail_jsonl(path: Path, limit: int = 5) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text().splitlines()[-max(1, limit * 3):]:
        try:
            item = json.loads(line)
        except Exception:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows[-limit:]


def _list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = re.split(r"[,;\n]", value)
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = str(item or "").strip().strip("#")
        if text:
            out.append(text)
    return out


def _merge_unique(primary: list[str], existing: list[str], limit: int = 80) -> list[str]:
    seen: set[str] = set()
    merged: list[str] = []
    for item in primary + existing:
        text = str(item or "").strip()
        key = text.lower()
        if text and key not in seen:
            seen.add(key)
            merged.append(text)
    return merged[:limit]


def _infer_brand(text: str, brand: str = "") -> str:
    value = str(brand or "").strip()
    if value:
        return value
    lower = text.lower()
    if "persona1" in lower or "furad" in lower:
        return "persona1"
    if "docsapp" in lower:
        return "docsapp"
    if "BrandA" in lower or "Operator" in lower:
        return "maroon_standard"
    return "revenue"


def _infer_channel(text: str, channel: str = "") -> str:
    value = str(channel or "").strip()
    if value:
        return value
    lower = text.lower()
    if "tiktok" in lower or "tik tok" in lower:
        return "tiktok"
    if "dm" in lower:
        return "dm"
    if "gumroad" in lower:
        return "gumroad"
    if "email" in lower or "outreach" in lower:
        return "email"
    if "video" in lower:
        return "video"
    return "general"


def _infer_tiktok_hashtags(text: str) -> list[str]:
    lower = text.lower()
    tags: list[str] = []
    if re.search(r"\b(dubai|luxury|lifestyle|freedom)\b", lower):
        tags += ["dubai", "dubailife", "freedomlifestyle", "locationindependent"]
    if re.search(r"\b(boss\s*babe|female|women|woman|fempreneur|founder)\b", lower):
        tags += ["bossbabe", "femaleentrepreneur", "womeninbusiness", "fempreneur", "womenwhohustle"]
    if re.search(r"\b(income|money|wealth|invest|business|entrepreneur|side\s*hustle)\b", lower):
        tags += ["onlinebusiness", "passiveincome", "sidehustle", "financialfreedom"]
    if not tags and ("tiktok" in lower or "persona1" in lower):
        tags = ["bossbabe", "femaleentrepreneur", "womeninbusiness", "onlinebusiness", "digitalproducts"]
    return _merge_unique(tags, [], limit=24)


def _infer_tiktok_keywords(text: str) -> list[str]:
    tags = _infer_tiktok_hashtags(text)
    keywords = [tag.replace("female", "female ").replace("online", "online ") for tag in tags]
    defaults = ["boss babe", "female entrepreneur", "women in business", "online business", "financial freedom"]
    return _merge_unique(keywords, defaults, limit=32)


def _default_comments(text: str) -> list[str]:
    lower = text.lower()
    if not re.search(r"\b(persona1|tiktok|boss|dubai|women|female|entrepreneur|income|freedom)\b", lower):
        return []
    return [
        "this is the energy",
        "women who build different hit different",
        "the clarity in this",
        "this is what financial freedom actually looks like",
        "she is not waiting for permission",
        "love seeing women build without apology",
    ]


def _default_dm_templates(text: str) -> list[str]:
    lower = text.lower()
    if not re.search(r"\b(persona1|tiktok|dm|boss|dubai|women|female|entrepreneur|income|freedom)\b", lower):
        return []
    return [
        "your content already has the hard part: attention. the next step is a repeatable content and DM follow-up system.",
        "the energy on your page is real. the missing leverage is usually hooks, offers, and follow-up in one simple loop.",
        "building online gets easier when the system is reusable: content ideas, buyer notes, and one clear next step.",
    ]


def _clamp_float(value: Any, default: float, lo: float = 0.0, hi: float = 1.0) -> float:
    try:
        return max(lo, min(hi, float(value)))
    except Exception:
        return default


def _build_directive(tool_input: dict[str, Any]) -> dict[str, Any]:
    request = str(tool_input.get("request") or tool_input.get("objective") or "").strip()
    brand = _infer_brand(request, str(tool_input.get("brand") or ""))
    channel = _infer_channel(request, str(tool_input.get("channel") or ""))
    priority = str(tool_input.get("priority") or "normal").strip().lower()
    if priority not in {"low", "normal", "high", "urgent"}:
        priority = "normal"

    target_hashtags = _list(tool_input.get("target_hashtags"))
    target_keywords = _list(tool_input.get("target_keywords"))
    fallback_comments = _list(tool_input.get("fallback_comments"))
    dm_templates = _list(tool_input.get("dm_templates"))

    if brand == "persona1" and channel == "tiktok":
        target_hashtags = _merge_unique(target_hashtags, _infer_tiktok_hashtags(request), limit=32)
        target_keywords = _merge_unique(target_keywords, _infer_tiktok_keywords(request), limit=40)
        fallback_comments = _merge_unique(fallback_comments, _default_comments(request), limit=30)
        dm_templates = _merge_unique(dm_templates, _default_dm_templates(request), limit=24)

    lower = request.lower()
    aggressive = bool(re.search(r"\b(aggressive|more calls|push harder|increase|turn up|faster)\b", lower))

    directive = {
        "id": f"strategy_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}",
        "created_at": _now(),
        "brand": brand,
        "channel": channel,
        "priority": priority,
        "request": request or f"Update {brand} {channel} strategy",
        "status": "applied_override" if brand == "persona1" and channel == "tiktok" else "queued_for_strategist",
        "target_hashtags": target_hashtags,
        "target_keywords": target_keywords,
        "fallback_comments": fallback_comments,
        "dm_templates": dm_templates,
        "comment_probability": _clamp_float(tool_input.get("comment_probability"), 0.7 if aggressive else 0.4),
        "follow_probability": _clamp_float(tool_input.get("follow_probability"), 0.5 if aggressive else 0.3),
        "notes": str(tool_input.get("notes") or "").strip(),
        "requested_by": str(tool_input.get("requested_by") or "").strip(),
    }
    return directive


def _apply_tiktok_override(directive: dict[str, Any]) -> dict[str, Any]:
    overrides = _load_json(OVERRIDES_PATH, {})
    if not isinstance(overrides, dict):
        overrides = {}
    persona1 = overrides.setdefault("persona1", {})
    tiktok = persona1.setdefault("tiktok", {})
    tiktok["updated_at"] = _now()
    tiktok["last_directive_id"] = directive["id"]
    tiktok["objective"] = directive["request"]

    if directive.get("target_hashtags"):
        tiktok["target_hashtags"] = _merge_unique(directive["target_hashtags"], _list(tiktok.get("target_hashtags")))
    if directive.get("target_keywords"):
        tiktok["target_keywords"] = _merge_unique(directive["target_keywords"], _list(tiktok.get("target_keywords")))
    if directive.get("fallback_comments"):
        tiktok["fallback_comments"] = _merge_unique(directive["fallback_comments"], _list(tiktok.get("fallback_comments")))
    if directive.get("dm_templates"):
        tiktok["dm_templates_general"] = _merge_unique(directive["dm_templates"], _list(tiktok.get("dm_templates_general")))

    tiktok["comment_probability"] = directive["comment_probability"]
    tiktok["follow_probability"] = directive["follow_probability"]
    overrides["updated_at"] = _now()
    _write_json(OVERRIDES_PATH, overrides)
    return overrides


def _append_directive(directive: dict[str, Any]) -> Path:
    HUSTLE_DIR.mkdir(parents=True, exist_ok=True)
    with open(DIRECTIVES_PATH, "a") as handle:
        handle.write(json.dumps(directive, ensure_ascii=False) + "\n")
    REQUESTS_DIR.mkdir(parents=True, exist_ok=True)
    request_path = REQUESTS_DIR / f"{directive['id']}.json"
    _write_json(request_path, directive)
    return request_path


def _update(tool_input: dict[str, Any]) -> str:
    directive = _build_directive(tool_input)
    request_path = _append_directive(directive)
    applied = False
    if directive["brand"] == "persona1" and directive["channel"] == "tiktok":
        _apply_tiktok_override(directive)
        applied = True

    lines = [
        "Strategy directive recorded.",
        f"Directive: {directive['id']}",
        f"Scope: {directive['brand']} / {directive['channel']}",
        f"Status: {directive['status']}",
        f"Request: {directive['request']}",
        f"Path: {request_path}",
    ]
    if applied:
        lines.append(f"Runtime override: {OVERRIDES_PATH}")
        if directive.get("target_hashtags"):
            lines.append("TikTok hashtags: " + ", ".join("#" + tag for tag in directive["target_hashtags"][:10]))
    else:
        lines.append(f"Queued for strategist input: {DIRECTIVES_PATH}")
    return "\n".join(lines)


def _status(limit: int = 5) -> str:
    strategy = _load_json(STRATEGY_PATH, {})
    overrides = _load_json(OVERRIDES_PATH, {})
    directives = _tail_jsonl(DIRECTIVES_PATH, limit=limit)
    lines = ["Strategy status"]
    if isinstance(strategy, dict) and strategy:
        lines.append(f"Generated: {strategy.get('generated_at', 'unknown')}")
        lines.append(f"Pipeline health: {strategy.get('pipeline_health', 'unknown')}")
        summary = strategy.get("summary") if isinstance(strategy.get("summary"), dict) else {}
        if summary:
            lines.append(
                "Sends/replies: "
                f"{summary.get('total_sends', 0)} / {summary.get('total_replies', 0)}"
            )
            if summary.get("detected_replies") is not None:
                lines.append(
                    "Detected replies: "
                    f"{summary.get('detected_replies', 0)} "
                    f"({summary.get('auto_responses', 0)} auto, {summary.get('bounces', 0)} bounce)"
                )
        strategy_directives = strategy.get("directives") if isinstance(strategy.get("directives"), dict) else {}
        if strategy_directives.get("outbound_hold"):
            lines.append("Outbound hold: active")
            if strategy_directives.get("outbound_hold_reason"):
                lines.append(f"Hold reason: {strategy_directives.get('outbound_hold_reason')}")
        elif strategy_directives:
            lines.append("Outbound hold: inactive")
    ima_overrides = overrides.get("persona1") if isinstance(overrides, dict) else {}
    if not isinstance(ima_overrides, dict):
        ima_overrides = {}
    tiktok = ima_overrides.get("tiktok") or {}
    if not isinstance(tiktok, dict):
        tiktok = {}
    if tiktok:
        lines.append("")
        lines.append("persona1 TikTok override:")
        lines.append(f"- updated: {tiktok.get('updated_at', 'unknown')}")
        tags = _list(tiktok.get("target_hashtags"))
        if tags:
            lines.append("- hashtags: " + ", ".join("#" + tag for tag in tags[:12]))
        lines.append(f"- comment probability: {tiktok.get('comment_probability', 'default')}")
        lines.append(f"- follow probability: {tiktok.get('follow_probability', 'default')}")
    if directives:
        lines.append("")
        lines.append("Recent directives:")
        for item in directives:
            lines.append(f"- {item.get('status', 'queued')} | {item.get('brand')}/{item.get('channel')} | {item.get('request', '')[:120]}")
    else:
        lines.append("")
        lines.append("No operator strategy directives recorded yet.")
    return "\n".join(lines)


class StrategyOpsTool(BaseTool):
    name = "strategy_ops"
    description = "Record and apply revenue/content strategy directives for local automation lanes."

    def get_definitions(self) -> list[dict]:
        return [
            make_tool_def(
                "strategy_update_directive",
                (
                    "Record a strategy update request and apply safe runtime overrides where supported. "
                    "Use for requests like updating persona1's TikTok strategy, changing content direction, "
                    "or adding operator guidance to the revenue strategist."
                ),
                {
                    "brand": {"type": "string", "description": "Brand/lane, e.g. persona1, docsapp, maroon_standard."},
                    "channel": {"type": "string", "description": "Channel, e.g. tiktok, dm, email, video, gumroad."},
                    "request": {"type": "string", "description": "The operator's strategy request."},
                    "priority": {"type": "string", "description": "low, normal, high, or urgent."},
                    "target_hashtags": {"type": "array", "items": {"type": "string"}},
                    "target_keywords": {"type": "array", "items": {"type": "string"}},
                    "fallback_comments": {"type": "array", "items": {"type": "string"}},
                    "dm_templates": {"type": "array", "items": {"type": "string"}},
                    "comment_probability": {"type": "number", "description": "TikTok comment probability from 0 to 1."},
                    "follow_probability": {"type": "number", "description": "TikTok follow probability from 0 to 1."},
                    "notes": {"type": "string", "description": "Optional implementation notes."},
                },
                ["request"],
            ),
            make_tool_def(
                "strategy_status",
                "Show current strategy snapshot, overrides, and recent directives.",
                {"limit": {"type": "integer", "description": "Recent directives to show."}},
            ),
        ]

    async def handle(self, tool_name: str, tool_input: dict) -> str:
        loop = asyncio.get_event_loop()
        if tool_name == "strategy_update_directive":
            return await loop.run_in_executor(None, _update, dict(tool_input or {}))
        if tool_name == "strategy_status":
            limit = int((tool_input or {}).get("limit") or 5)
            return await loop.run_in_executor(None, _status, limit)
        return f"Unknown tool: {tool_name}"
