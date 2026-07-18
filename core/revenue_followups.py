"""Deterministic revenue-chat follow-up actions."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from core import BASE_DIR, mountain_now


YES_RE = re.compile(r"^\s*(y|yes|yeah|yep|do it|run it|send it|go|go ahead|please do|execute)\s*[.!]*\s*$", re.I)


def is_yes_followup(text: str) -> bool:
    return bool(YES_RE.match(str(text or "")))


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content") if isinstance(message, dict) else ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
        return "\n".join(parts)
    return str(content or "")


def _last_assistant_text(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages or []):
        if isinstance(message, dict) and message.get("role") == "assistant":
            return _message_text(message)
    return ""


def maybe_handle_revenue_yes(text: str, messages: list[dict[str, Any]], base_dir: Path | str = BASE_DIR) -> str | None:
    if not is_yes_followup(text):
        return None
    previous = _last_assistant_text(messages).lower()
    if not previous:
        return None
    if "tiktok" in previous and ("draft" in previous or "queue" in previous or "post" in previous):
        return build_tiktok_queue_response(base_dir)
    if any(token in previous for token in ("safe hustle path", "strategy directive", "revenue machine", "next move", "want me")):
        from core.revenue_rundown import build_revenue_action_text

        return build_revenue_action_text(base_dir=base_dir, prompt="run the next revenue action", dispatch=True)
    return None


def build_tiktok_queue_response(base_dir: Path | str = BASE_DIR) -> str:
    base = Path(base_dir)
    now = mountain_now()
    stamp = now.strftime("%Y%m%d_%H%M%S")
    drafts_dir = base / "data" / "hustle" / "tiktok_drafts"
    drafts_dir.mkdir(parents=True, exist_ok=True)

    posts = [
        {
            "hook": "Your business does not need more tabs. It needs one clean operating rhythm.",
            "body": "I keep seeing smart operators lose money because the work lives in DMs, screenshots, notes, and vibes. The fix is boring: one intake, one tracker, one follow-up loop, one weekly review. That is what turns chaos into revenue.",
            "cta": "Comment SYSTEMS and I will send the checklist.",
        },
        {
            "hook": "If you are still manually chasing follow-ups, that is the leak.",
            "body": "Most small businesses do not have a lead problem. They have a memory problem. Leads get warm, then disappear because no one has a clean next-step system. Build the follow-up machine before you buy more attention.",
            "cta": "DM me FLOW if you want the template.",
        },
        {
            "hook": "The fastest product is usually the process you already repeat.",
            "body": "If people ask how you do something more than twice, document it. Turn it into a checklist, template, mini-guide, or audit. Sell the shortcut before you spend months building a huge thing nobody asked for.",
            "cta": "Save this and audit your repeatable workflows today.",
        },
    ]
    payload = {
        "id": f"tiktok_queue_{stamp}",
        "created_at": now.isoformat(),
        "brand": "persona1",
        "channel": "tiktok",
        "status": "queued_draft_ready",
        "posting_mode": "draft_only",
        "identity_note": "Use persona1/docsapp positioning. Do not tie to Operator, BrandA Standard, or CompanyA.",
        "posts": posts,
    }
    path = drafts_dir / f"{payload['id']}.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))

    directive_text = (
        "Queue persona1 TikTok content around operator systems, revenue routines, digital products, "
        "and follow-up loops. Use the draft packet as ready-to-edit source material."
    )
    try:
        from tools.strategy_ops import _update

        strategy_result = _update({
            "brand": "persona1",
            "channel": "tiktok",
            "priority": "high",
            "request": directive_text,
            "target_hashtags": ["operatormindset", "systemsbuilder", "digitalproducts", "revenueroutines", "smallbusinesssystems"],
            "target_keywords": ["operator systems", "follow up loop", "digital products", "revenue routines", "business systems"],
            "fallback_comments": [
                "the backend system is what makes the attention worth anything",
                "this is exactly where a clean follow-up loop pays for itself",
                "document the repeatable part and it becomes the product",
            ],
            "notes": f"Draft packet: {path}",
        })
    except Exception as exc:
        strategy_result = f"Strategy directive write failed: {exc}"

    lines = [
        "Done. I queued the TikTok draft packet and updated persona1's TikTok strategy override.",
        "",
        "Drafts ready:",
    ]
    for idx, post in enumerate(posts, 1):
        lines.append(f"{idx}. {post['hook']}")
    lines.extend([
        "",
        f"Path: {path}",
        "",
        strategy_result,
        "",
        "External posts/sends: none from this step. It created ready-to-use drafts and a runtime strategy override.",
        "Fast/cost note: deterministic local action only; no paid LLM call was needed.",
    ])
    return "\n".join(lines).strip()

