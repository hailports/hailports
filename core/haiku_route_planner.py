from __future__ import annotations

"""Free front-door router for ambiguous chat prompts.

This module is deliberately narrower than the main API client. It sends only a
redacted prompt shape to the free LLM pool, asks for strict JSON, and times out
quickly. If free providers are unavailable or uncertain, callers fall back to
the deterministic regex router. It never performs paid front-side routing.
"""

import asyncio
import json
import logging
import os
import re
import time
from typing import Any

from core import SETTINGS
from core.free_llm_pool import try_free_providers
from core.cost_guard import check_api_spike

log = logging.getLogger(__name__)

_TRUE_VALUES = {"1", "true", "yes", "on"}
_ALLOWED_ROUTES = {"local", "haiku", "sonnet", "opus"}
_DEFAULT_TIMEOUT_S = 3.5
_MAX_PROMPT_CHARS = 1400
_LAST_FAILURE_TS = 0.0
_FAILURE_BACKOFF_S = 45.0


def _enabled() -> bool:
    token = os.environ.get("CLAUDE_STACK_HAIKU_ROUTER", "0").strip().lower()
    return token in _TRUE_VALUES


def _redact_for_routing(text: str) -> str:
    value = str(text or "")
    value = re.sub(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", "[email]", value)
    value = re.sub(r"\b(?:\+?1[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)\d{3}[-.\s]?\d{4}\b", "[phone]", value)
    value = re.sub(r"https?://[^\s]+", "[url]", value)
    value = re.sub(r"\b[a-zA-Z0-9_-]{16,}\b", "[id]", value)
    value = re.sub(r"\s+", " ", value).strip()
    if len(value) > _MAX_PROMPT_CHARS:
        value = value[:_MAX_PROMPT_CHARS] + "..."
    return value


def _content_text(response: Any) -> str:
    parts: list[str] = []
    for block in getattr(response, "content", []) or []:
        text = getattr(block, "text", None)
        if text is not None:
            parts.append(str(text))
            continue
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
    return "\n".join(parts).strip()


def _extract_json(text: str) -> dict[str, Any] | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        pass
    match = re.search(r"\{.*\}", raw, re.S)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


async def route_prompt(
    text: str,
    *,
    user_default: str = "sonnet",
    has_tool_context: bool = False,
    source: str = "router",
    timeout_s: float = _DEFAULT_TIMEOUT_S,
) -> str | None:
    """Return one of local/haiku/sonnet/opus, or None for deterministic fallback."""
    global _LAST_FAILURE_TS

    if not _enabled():
        return None
    if has_tool_context:
        return "sonnet"
    if time.time() - _LAST_FAILURE_TS < _FAILURE_BACKOFF_S:
        return None
    if check_api_spike(window_s=120):
        log.info("Haiku router skipped because cost guard detected a recent API spike")
        return None

    prompt = {
        "prompt": _redact_for_routing(text),
        "user_default": str(user_default or "sonnet"),
        "routes": {
            "local": "Use only for direct local system lookups or deterministic local tools: calendar, inbox, Salesforce/work artifacts, travel/expense, revenue stack, files, app status, or image/document tooling.",
            "haiku": "Use for tiny summarization/classification/formatting that does not need tools and does not need a conversational answer.",
            "sonnet": "Use for ordinary Claude/ChatGPT-style conversation, general knowledge, advice, brainstorming, creative writing, reasoning, drafting, coordination, unclear requests, or any non-operational prompt.",
            "opus": "Use only for complex architecture, deep debugging, multi-step strategy, high-stakes analysis, or large code/design work.",
        },
        "return": {"route": "local|haiku|sonnet|opus", "confidence": "0..1", "reason": "short"},
    }
    system = (
        "You are a routing classifier for a personal WorkOS assistant. "
        "Return only compact JSON. Choose the fastest competent route. "
        "Prefer sonnet for ordinary conversation, general questions, advice, brainstorming, "
        "creative writing, or ambiguous prompts. Prefer local only when the request directly "
        "names the user's local/work systems, schedule, travel/expenses, revenue stack, files, "
        "or deterministic tool actions. Do not answer the user."
    )

    try:
        response = await asyncio.wait_for(
            try_free_providers(
                json.dumps(prompt, separators=(",", ":")),
                system=system,
                max_tokens=120,
                explicit=True,
                tier="strong",
            ),
            timeout=max(1.0, float(timeout_s)) + 0.5,
        )
        text_response, provider = response
        if not text_response:
            return None
        data = _extract_json(str(text_response))
        if not data:
            return None
        route = str(data.get("route") or "").strip().lower()
        try:
            confidence = float(data.get("confidence") or 0)
        except Exception:
            confidence = 0.0
        if route not in _ALLOWED_ROUTES or confidence < 0.55:
            return None
        log.info("Free front router selected %s via %s (confidence %.2f)", route, provider, confidence)
        return route
    except Exception as exc:
        _LAST_FAILURE_TS = time.time()
        log.info("Free front router unavailable: %s", exc)
        return None
