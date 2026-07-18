"""Final response contract for chat surfaces.

Every channel should return one of these shapes:
- useful answer / action result
- concise clarification request
- clear blocked/error explanation with next step

This module is intentionally deterministic and cheap. It is the final guard
before text reaches WebUI, iMessage, Telegram, or OpenClaw chat.
"""

from __future__ import annotations

import re
from typing import Any


RAW_ERROR_RE = re.compile(
    r"("
    r"^error:\s*|"
    r"load failed|failed to fetch|networkerror|"
    r"string did not match the expected pattern|expected pattern|"
    r"traceback \(most recent call last\)|"
    r"\bhttp\s*5\d\d\b|internal server error|"
    r"chat request failed|something went wrong processing your request|"
    r"backend returned an empty answer|no response\b|"
    r"route failed before completion|failed before completion"
    r")",
    re.IGNORECASE,
)

LOW_VALUE_RE = re.compile(
    r"^\s*(?:i\s+)?(?:don'?t know|not sure|cannot determine|can'?t determine|unknown|n/?a)\.?\s*$",
    re.IGNORECASE,
)

AMBIGUOUS_SHORT_ACTION_RE = re.compile(
    r"^\s*(yes|yep|yeah|ok|okay|do it|run it|send it|execute|go|continue|finish|promote it|approve)\s*[.!]?\s*$",
    re.IGNORECASE,
)

ACTION_VERBS_RE = re.compile(
    r"\b("
    r"do|run|execute|fix|build|make|create|send|reply|email|publish|post|"
    r"promote|approve|queue|find|pull|export|download|save|copy|clone|"
    r"duplicate|deploy|update|add|remove|diagnose|check|look|search|wire|"
    r"turn on|start|stop|kill|restart"
    r")\b",
    re.IGNORECASE,
)

QUESTION_RE = re.compile(r"\?\s*$|\b(what|why|how|when|where|who|which|can|could|should|does|did|is|are)\b", re.I)


def wants_action(prompt: str) -> bool:
    return bool(ACTION_VERBS_RE.search(prompt or ""))


def wants_answer(prompt: str) -> bool:
    return bool(QUESTION_RE.search(prompt or ""))


def is_raw_or_empty(reply: Any) -> bool:
    text = "" if reply is None else str(reply).strip()
    if not text:
        return True
    if RAW_ERROR_RE.search(text):
        return True
    if LOW_VALUE_RE.match(text):
        return True
    return False


def _clean_prompt(prompt: str, limit: int = 180) -> str:
    text = " ".join(str(prompt or "").split())
    return text[:limit]


def _source_label(frontend: str = "") -> str:
    token = str(frontend or "").strip().lower()
    if "revenue" in token:
        return "revenue chat"
    if "telegram" in token:
        return "Telegram"
    if "imessage" in token:
        return "iMessage"
    if "openclaw" in token:
        return "OpenClaw"
    if "webui" in token:
        return "web chat"
    return "chat"


def clarification_reply(prompt: str, *, frontend: str = "") -> str:
    preview = _clean_prompt(prompt)
    if AMBIGUOUS_SHORT_ACTION_RE.match(prompt or ""):
        return (
            "I need one missing detail before I act: which prior item or target should I use?\n\n"
            "Reply with the ticket, account, experiment, message, or job name and I’ll continue from there."
        )
    if wants_action(prompt):
        return (
            "I understand this as an action request, but I need one concrete target before executing.\n\n"
            f"Request: {preview or '(blank)'}\n\n"
            "Send the target record, file, account, channel, or experiment ID and I’ll run the appropriate route."
        )
    return (
        "I need one clarification to answer this cleanly.\n\n"
        f"Request: {preview or '(blank)'}\n\n"
        "Tell me the system or scope you mean, and I’ll answer from the right data source."
    )


def blocked_reply(prompt: str, raw: str = "", *, frontend: str = "") -> str:
    source = _source_label(frontend)
    raw_text = " ".join(str(raw or "").split())
    if len(raw_text) > 240:
        raw_text = raw_text[:240] + "..."

    if wants_action(prompt):
        base = (
            "I tried to route this as an action, but the executor did not produce a usable result.\n\n"
            "Nothing was sent, posted, funded, traded, deployed, or changed from this failed attempt."
        )
    elif wants_answer(prompt):
        base = "I tried to answer this, but the data route failed before returning usable evidence."
    else:
        base = f"The {source} route failed before it produced a usable response."

    detail = f"\n\nTechnical note: {raw_text}" if raw_text and not LOW_VALUE_RE.match(raw_text) else ""
    return (
        f"{base}{detail}\n\n"
        "Next step: I can retry through OpenClaw/local deterministic routing, or ask for the one missing detail if the request is underspecified."
    )


def finalize_chat_reply(
    *,
    prompt: str,
    reply: Any,
    frontend: str = "",
    thread_id: str | None = None,
    route: str = "",
) -> str:
    """Return a user-safe final response for every chat path."""
    text = "" if reply is None else str(reply).strip()
    clean_prompt = str(prompt or "").strip()

    if text and not RAW_ERROR_RE.search(text) and not LOW_VALUE_RE.match(text):
        return text

    if AMBIGUOUS_SHORT_ACTION_RE.match(clean_prompt or ""):
        return clarification_reply(clean_prompt, frontend=frontend)

    if is_raw_or_empty(text):
        return blocked_reply(clean_prompt, text, frontend=frontend)

    return text or clarification_reply(clean_prompt, frontend=frontend)
