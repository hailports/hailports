"""Bridge legacy chat frontends into claude-stack-v2.

The old repo still owns many tools. v2 owns the shared chat surface, policy, and
job queue. This module lets Engine route ordinary chat through v2 without
breaking legacy commands, attachments, and tool-heavy workflows.
"""

from __future__ import annotations

from core.constants import LOCAL_MODEL
import asyncio
import json
import os
import re
import urllib.error
import urllib.request
from typing import Any


V2_URL = os.environ.get("CLAUDE_STACK_V2_URL", "http://127.0.0.1:8765").rstrip("/")
V2_ENABLED = os.environ.get("CLAUDE_STACK_V2_CHAT", "1").strip().lower() not in {"0", "false", "no", "off"}
V2_TIMEOUT_S = float(os.environ.get("CLAUDE_STACK_V2_CHAT_TIMEOUT_S", "25"))
ACCEPT_DEGRADED = os.environ.get("CLAUDE_STACK_ACCEPT_V2_FALLBACK", "0").strip().lower() in {"1", "true", "yes", "on"}

_TOOL_HEAVY_RE = re.compile(
    r"\b("
    r"salesforce|soql|outlook|email|mailbox|inbox|gmail|icloud|apple mail|calendar|meeting|call|appointment|invite|zoom|teams|slack|monday|revenue|hustle|income|strategy|pipeline|stream|streams|"
    r"create|setup|set up|schedule|arrange|book|reschedule|update|delete|deploy|send|post|publish|draft|document|docx|pdf|"
    r"file|folder|terminal|run|script|browser|screenshot|download|upload|"
    r"image|video|comfy|voice|transcribe|travel|expense|expenses|receipt|receipts|hotel|flight|rideshare|ride share|uber|lyft"
    r")\b",
    re.I,
)

# Day-rundown and multi-source queries must go through the engine's compound
# handler (real calendar/ticket data) — not the v2/ChatGPT path.
_COMPOUND_BYPASS_RE = re.compile(
    r"(?:"
    r"how.{0,15}(?:my\s+)?(?:day|today|tomorrow|week).{0,15}look"
    r"|what.{0,15}(?:my\s+)?(?:day|today|tomorrow).{0,15}look"
    r"|what.{0,15}(?:my\s+)?(?:week|this\s+week|next\s+week).{0,15}look"
    r"|(?:my\s+week|this\s+week|next\s+week|the\s+week)"
    r"|(?:my\s+day|today'?s?\s+(?:agenda|schedule|calendar))"
    r"|(?:morning|daily|eod)\s*(?:brief|briefing|rundown|update|summary)"
    r"|rundown|briefing"
    r"|(?:give|show|get)\s*me.{0,20}(?:rundown|summary|overview|status|update|recap)"
    r"|priorities|what.{0,20}(?:pressing|urgent|on\s+my\s+plate)"
    r"|catch\s+me\s+up|bring\s+me\s+up\s+to\s+speed"
    r")",
    re.I,
)

_WORK_ITEMS_BYPASS_RE = re.compile(
    r"(?:"
    r"\bwork\s*items?\b"
    r"|\bwork\s+queue\b"
    r"|\baction\s+queue\b"
    r"|\bworkos\b"
    r"|\bwhat.{0,20}(?:are|is|'?s).{0,12}(?:my\s+)?(?:tasks?|to.?dos?|priorities)\b"
    r"|\bwhat\s+should\s+i\s+(?:focus|work|tackle|start)\s+on\b"
    r"|\bon\s+my\s+plate\b"
    r")",
    re.I,
)

_CATCH_UP_RE = re.compile(
    r"\b(?:what(?:'d| did)? i miss(?: at work)?(?: this week)?|what have i missed(?: at work)?|catch me up(?: on work)?|bring me up to speed(?: on work)?|what(?:'s| is)?\s+happening\s+this\s+week|what(?:'s| is)?\s+on\s+my\s+week)\b",
    re.I,
)


def should_try_v2(text: str, *, attachments=None, fallback_model: str | None = None) -> bool:
    if not V2_ENABLED:
        return False
    if attachments:
        return False
    clean = str(text or "").strip()
    if not clean:
        return False
    lower = clean.lower()
    if lower.startswith(("/", "!", "#")):
        return False
    if not _is_local_fallback_model(fallback_model):
        return False
    if _TOOL_HEAVY_RE.search(clean):
        return False
    # Compound/day-rundown queries need real calendar+ticket data from the
    # local compound handler — skip v2 so they don't get a generic LLM reply.
    if _COMPOUND_BYPASS_RE.search(clean):
        return False
    # Work-item questions must use the local WorkOS/Salesforce artifacts. The
    # generic v2 chat path has no reliable access to those per-user queues.
    if _WORK_ITEMS_BYPASS_RE.search(clean):
        return False
    if _CATCH_UP_RE.search(clean):
        return False
    return True


def _is_local_fallback_model(value: str | None) -> bool:
    token = str(value or "").strip().lower()
    if token in {"", "auto", "local"}:
        return True
    if token.startswith(("local:", "ollama:")):
        return True
    return token == str(LOCAL_MODEL).strip().lower()


async def maybe_answer(
    *,
    user_id: str,
    text: str,
    frontend: str = "",
    thread_id: str | None = None,
    attachments=None,
    fallback_model: str | None = None,
) -> str | None:
    if not should_try_v2(text, attachments=attachments, fallback_model=fallback_model):
        return None
    payload = {
        "text": text,
        "channel": _channel(frontend),
        "user_id": user_id,
        "lane": _lane(frontend, thread_id),
        "thread_id": thread_id or "",
        "metadata": {"legacy_frontend": frontend or ""},
    }
    try:
        return await asyncio.wait_for(asyncio.to_thread(_post_chat, payload), timeout=V2_TIMEOUT_S + 2)
    except Exception:
        return None


def _post_chat(payload: dict[str, Any]) -> str | None:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(f"{V2_URL}/v1/chat", data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=V2_TIMEOUT_S) as response:
            parsed = json.loads(response.read().decode("utf-8", errors="replace"))
    except (OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError):
        return None
    text = str(parsed.get("text") or "").strip() if isinstance(parsed, dict) else ""
    if isinstance(parsed, dict) and parsed.get("model") == "local-fallback" and not ACCEPT_DEGRADED:
        return None
    return text or None


def _channel(frontend: str) -> str:
    token = str(frontend or "").lower()
    if "telegram" in token:
        return "telegram"
    if "imessage" in token:
        return "imessage"
    if "webui" in token or "web" in token:
        return "webchat"
    return "api"


def _lane(frontend: str, thread_id: str | None) -> str:
    token = f"{frontend or ''} {thread_id or ''}".lower()
    if "persona1" in token:
        return "ima_furad"
    if "BrandA" in token:
        return "maroon_standard"
    if "docsapp" in token:
        return "docsapp"
    if "CompanyA" in token or "work" in token:
        return "redacted_exec"
    return "operator"
