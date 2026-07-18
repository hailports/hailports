from __future__ import annotations

"""Shared chat routing helpers for WebUI, Telegram, and iMessage.

The routing functions in this module are intentionally cheap: regex checks and
recent message previews only. They must never call an LLM or scan slow external
systems. Rich local context is assembled separately after the route is chosen.
"""

import re
import json
from pathlib import Path
from typing import Any

from core import SETTINGS, mountain_now

HAIKU = SETTINGS["routing"]["haiku_model"]
SONNET = SETTINGS["routing"]["sonnet_model"]
OPUS = SETTINGS["routing"]["opus_model"]

_EXPLICIT_OPUS = ("!opus", "!deep", "!think")
_EXPLICIT_HAIKU = ("!fast", "!quick", "!haiku")
_EXPLICIT_SONNET = ("!sonnet", "!mid")

_FOLLOWUP_MARKERS = re.compile(
    r"^\s*(yes|yeah|yep|yup|ok|okay|sure|do it|go ahead|please do|"
    r"send it|draft it|run it|make it|fix it|continue|keep going|"
    r"that|this|same|also|and then|what about)\b",
    re.IGNORECASE,
)

_OPUS_MARKERS = re.compile(
    r"\b("
    r"architect|architecture|system design|end[- ]to[- ]end|complete replacement|"
    r"replace claude desktop|multi[- ]system|cross[- ]system|migration|"
    r"deep dive|strategy|strategic|root cause|postmortem|incident|"
    r"refactor|security review|threat model|data migration|bulk update|"
    r"irreversible|destructive|high[- ]stakes"
    r")\b",
    re.IGNORECASE,
)

_WRITE_OR_EXTERNAL_ACTION = re.compile(
    r"\b("
    r"send|forward|reply|create|schedule|update|delete|move|assign|"
    r"approve|reject|close|merge|post|publish|submit|buy|purchase|"
    r"draft|compose|email|message|zoom|teams|slack|salesforce"
    r")\b",
    re.IGNORECASE,
)

_HAIKU_MARKERS = re.compile(
    r"\b("
    r"classify|label|extract|format|rewrite|summarize|summary|status|"
    r"count|list|short reply|one sentence|quick answer|syntax"
    r")\b",
    re.IGNORECASE,
)

_VAGUE_ACTION = re.compile(
    r"^\s*(fix|handle|do|make|build|write|draft|review|analyze|"
    r"look into|check|take care of|deal with)\s*(it|this|that)?\s*[\.\!]*\s*$",
    re.IGNORECASE,
)

_CAPABILITY_QUERY = re.compile(
    r"^\s*("
    r"what\s+can\s+you\s+do|"
    r"what(?:\s+are|\s+r|'re|’re)\s+you\s+capable\s+of|"
    r"how\s+capable\s+are\s+you|"
    r"what\s+are\s+your\s+capabilities|"
    r"what\s+can\s+you\s+help\s+with|"
    r"what\s+are\s+you\s+able\s+to\s+do|"
    r"capabilities"
    r")[\?>!\.\s]*$",
    re.IGNORECASE,
)

_SELF_CONTAINED_SHORT_QUERY = re.compile(
    r"\b(?:calendar|agenda|schedule|events?|meetings?|calls?|day|today|tomorrow|"
    r"morning|afternoon|week|priorities|priority|inbox|email|mail|revenue|funnel)\b"
    r"|\b(?:what|whats|what's|what’s|how|hows|how's|how’s)\b.{0,50}\blook(?:ing|\s+like)?\b",
    re.IGNORECASE,
)

_CONTEXT_STOPWORDS = {
    "about",
    "after",
    "again",
    "also",
    "because",
    "before",
    "being",
    "could",
    "exact",
    "from",
    "have",
    "into",
    "just",
    "latest",
    "like",
    "message",
    "need",
    "please",
    "should",
    "that",
    "their",
    "there",
    "these",
    "thing",
    "this",
    "thread",
    "what",
    "when",
    "where",
    "which",
    "with",
    "would",
    "your",
}

_BASE_DIR = Path(__file__).resolve().parent.parent
_HUSTLE_DIR = _BASE_DIR / "data" / "hustle"


def _safe_json(path: Path) -> dict[str, Any]:
    try:
        if path.exists():
            data = json.loads(path.read_text())
            return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


def _instant_ima_reply(text: str) -> str:
    lower = str(text or "").lower()
    # Strip bracket context annotations (e.g. "[Revenue chat context: ... persona1 ... TikTok ...]")
    # so injected metadata doesn't fire the persona1 fast-path for unrelated revenue questions.
    clean_lower = re.sub(r"\[[^\]]{20,}\]", " ", lower)
    if not re.search(r"\b(persona1|furad|farud)\b", clean_lower):
        return ""
    if not re.search(r"\b(post|content|reel|tiktok|short|grow|following|follower|idea|caption|image|photo|visual)\b", clean_lower):
        return ""
    asks_to_render = re.search(
        r"\b(render|generate the image|make the image|produce the image|create the actual image)\b",
        lower,
    )
    negates_render = re.search(r"\b(do not|don't|dont|without|no)\s+render", lower)
    if asks_to_render and not negates_render:
        return ""

    state = _safe_json(_HUSTLE_DIR / "auth_maintenance_state.json")
    sessions = state.get("browser_sessions") if isinstance(state.get("browser_sessions"), dict) else {}
    blocked = [
        name for name, row in sessions.items()
        if isinstance(row, dict) and row.get("configured") is not False and not row.get("ok")
    ]
    blocker_line = ""
    if blocked:
        blocker_line = "\n\nSession note: " + ", ".join(blocked[:4]) + " need reauth before those channels can post/engage automatically."

    return (
        "Use persona1 as the operator friend for creators turning attention into revenue, not as an art account.\n\n"
        "1. Hook: \"your followers are not the business yet\"\n"
        "   Format: 20-second talking-head or text-overlay reel. Point: followers only matter when there is a simple offer, DM follow-up, and repeatable content lane. CTA: follow @persona1 for the system.\n\n"
        "2. Hook: \"the DM pile is where money leaks\"\n"
        "   Format: screen-record style checklist. Show: reply buckets, saved responses, opt-out respect, and one soft product CTA. CTA: grab the mini system from the link.\n\n"
        "3. Hook: \"boss-babe energy needs boring backend\"\n"
        "   Format: POV / caption-first short. Contrast chaotic posting with a daily loop: post, reply, track, package, repeat. CTA: subscribe on TikTok because the useful stuff goes there first."
        f"{blocker_line}"
    )


def _ima_context_for_text(text: str) -> str:
    lower = str(text or "").lower()
    if not re.search(r"\b(persona1|furad|farud|tiktok|instagram|reel|shorts|creator)\b", lower):
        return ""
    strategy_blob = _safe_json(_HUSTLE_DIR / "strategy_overrides.json")
    persona1 = strategy_blob.get("persona1") if isinstance(strategy_blob.get("persona1"), dict) else {}
    loop = _safe_json(_HUSTLE_DIR / "ima_creator_loop_state.json")
    auth = _safe_json(_HUSTLE_DIR / "auth_maintenance_state.json")
    sessions = auth.get("browser_sessions") if isinstance(auth.get("browser_sessions"), dict) else {}
    blocked = [
        name for name, row in sessions.items()
        if isinstance(row, dict) and row.get("configured") is not False and not row.get("ok")
    ]
    facts = loop.get("facts") if isinstance(loop.get("facts"), dict) else {}
    positioning = persona1.get("positioning") or (
        "persona1 is the operator friend who turns chaotic hustle energy into clean systems, "
        "digital products, templates, and revenue routines."
    )
    product = persona1.get("product_positioning") or (
        "persona1's products help creators turn attention into followers, followers into an offer, "
        "and scattered DMs into a simple revenue loop."
    )
    return (
        "persona1 CONTEXT: persona1 Furad is a creator/business/revenue-systems persona, not an art account. "
        f"Positioning: {positioning} Product: {product} "
        f"Growth platforms: {((persona1.get('growth') or {}).get('platforms') if isinstance(persona1.get('growth'), dict) else None) or ['tiktok','instagram','x']}. "
        f"Ready content assets: {facts.get('ready_assets') or facts.get('ready_posts')}. "
        f"Auth blockers: {blocked[:5]}. "
        "For chat-mode content requests, answer with specific ideas/copy/plans. Render an image only when the user asks for an image/photo/visual."
    )


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(str(block.get("text") or ""))
                elif block.get("type") == "tool_result":
                    parts.append(str(block.get("content") or ""))
            else:
                parts.append(str(block))
        return " ".join(p for p in parts if p)
    return str(content or "")


def _single_line(text: str, limit: int) -> str:
    compact = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(compact) <= limit:
        return compact
    return compact[: max(0, limit - 3)] + "..."


def recent_message_previews(
    history: list[dict[str, Any]] | None,
    *,
    max_items: int = 4,
    max_chars: int = 180,
) -> list[str]:
    """Return compact recent chat previews for routing/prompt context."""
    if not history:
        return []
    previews: list[str] = []
    for item in history[-max_items:]:
        role = str(item.get("role") or "message")
        text = _single_line(_content_to_text(item.get("content")), max_chars)
        if not text:
            continue
        previews.append(f"{role}: {text}")
    return previews


def is_capability_query(text: str) -> bool:
    return bool(_CAPABILITY_QUERY.match(str(text or "").strip()))


def looks_like_followup(text: str) -> bool:
    stripped = str(text or "").strip()
    if not stripped:
        return False
    if is_capability_query(stripped):
        return False
    if _FOLLOWUP_MARKERS.search(stripped):
        return True
    # Short, self-contained local queries like "whats my day look like" must
    # still route to calendar/priority tools instead of contextual fallback.
    if _SELF_CONTAINED_SHORT_QUERY.search(stripped):
        return False
    # Very short fragments are often contextual follow-ups in chat clients.
    return len(stripped.split()) <= 5 and not stripped.endswith("?")


def build_routing_text(text: str, history: list[dict[str, Any]] | None) -> str:
    """Build the heartbeat-fast text used by router.select_model().

    Initial, self-contained requests route on their own text. Contextual
    follow-ups include a few recent previews so "yes, do it" inherits the
    previous request's tool/reasoning shape.
    """
    stripped = str(text or "").strip()
    if not stripped or not looks_like_followup(stripped):
        return stripped
    previews = recent_message_previews(history, max_items=4, max_chars=180)
    if not previews:
        return stripped
    return stripped + "\n\nRecent chat preview:\n" + "\n".join(f"- {p}" for p in previews)


def instant_local_reply(
    text: str,
    history: list[dict[str, Any]] | None,
    *,
    channel: str = "",
) -> str:
    """Return deterministic replies for tiny local intents that should not spend model time."""
    if is_capability_query(text):
        return (
            "I can help with Salesforce tickets, WorkOS triage, Outlook/calendar context, "
            "local file and repo work, Claude Code handoffs, revenue/backlog analysis, and guarded drafts. "
            "A useful starting point is: what's pressing today?"
        )
    return ""


def _simple_math_reply(text: str) -> str:
    expr = str(text or "").strip().lower()
    expr = re.sub(r"^(what(?:'s| is)?|calculate|solve)\s+", "", expr).strip(" ?")
    if not re.fullmatch(r"[0-9\s\+\-\*/\(\)\.]+", expr):
        return ""
    if not re.search(r"[0-9]", expr) or len(expr) > 80:
        return ""
    # Reject exponentiation / floor-div: `2**99999999` would hang/OOM the eval.
    if "**" in expr or "//" in expr:
        return ""
    try:
        value = eval(expr, {"__builtins__": {}}, {})
    except Exception:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value)


def availability_fallback_reply(
    text: str,
    history: list[dict[str, Any]] | None,
    *,
    channel: str = "",
) -> str:
    """Minimal failure note when the model path is unavailable."""
    return "I couldn’t generate a response just now."


def choose_paid_model(
    text: str,
    *,
    user_default: str = "sonnet",
    has_tool_context: bool = False,
) -> str:
    """Choose Haiku/Sonnet/Opus after local-first routing says API is needed."""
    stripped = str(text or "").strip()
    lower = stripped.lower()
    default = str(user_default or "").lower()

    if lower.startswith(_EXPLICIT_OPUS):
        return OPUS
    if lower.startswith(_EXPLICIT_HAIKU):
        return HAIKU
    if lower.startswith(_EXPLICIT_SONNET):
        return SONNET

    if "opus" in default:
        return OPUS

    without_prefix = re.sub(r"^!(api|sonnet|mid|fast|quick|haiku|opus|deep|think)\s+", "", stripped, flags=re.I)
    scoring_text = without_prefix + ("\n" + stripped if without_prefix != stripped else "")

    if len(scoring_text) > 3500 or _OPUS_MARKERS.search(scoring_text):
        return OPUS

    if has_tool_context:
        return SONNET

    simple_enough = len(scoring_text) < 900
    if simple_enough and _HAIKU_MARKERS.search(scoring_text) and not _WRITE_OR_EXTERNAL_ACTION.search(scoring_text):
        return HAIKU

    if lower.startswith("!api") and simple_enough and not _WRITE_OR_EXTERNAL_ACTION.search(scoring_text):
        return HAIKU

    return SONNET


def needs_opus(text: str) -> bool:
    """Return True for requests whose shape needs the strongest paid tier."""
    scoring_text = str(text or "")
    return len(scoring_text) > 3500 or bool(_OPUS_MARKERS.search(scoring_text))


def _context_search_terms(text: str, *, limit: int = 4) -> list[str]:
    quoted = [m.strip() for m in re.findall(r"['\"]([^'\"]{3,80})['\"]", str(text or ""))]
    words = [
        w.lower()
        for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]{3,}", str(text or ""))
        if w.lower() not in _CONTEXT_STOPWORDS
    ]
    terms: list[str] = []
    for term in quoted + words:
        if term and term not in terms:
            terms.append(term)
        if len(terms) >= limit:
            break
    return terms


def build_context_addon(
    *,
    user_id: str,
    channel: str,
    thread_id: str | None,
    text: str,
    history: list[dict[str, Any]] | None,
    max_chars: int = 3500,
) -> str:
    """Build compact local context for the selected model's prompt."""
    parts: list[str] = []
    now = mountain_now()
    parts.append(
        "OMNICHANNEL CONTEXT: "
        f"channel={channel or 'unknown'}; "
        f"thread={thread_id or 'default'}; "
        f"local_time={now.strftime('%I:%M %p %Z, %A %B %d %Y')}"
    )

    previews = recent_message_previews(history, max_items=4, max_chars=220)
    if previews:
        parts.append("Recent message previews:\n" + "\n".join(f"- {p}" for p in previews))

    ima_context = _ima_context_for_text(text)
    if ima_context:
        parts.append(ima_context)

    if thread_id:
        try:
            from core import conversation

            total = conversation.get_thread_message_count(user_id, thread_id)
            recent_count = len(history or [])
            if total:
                parts.append(
                    f"Thread archive: {total} saved message(s) in this thread; "
                    f"{recent_count} recent message(s) are in the active prompt window."
                )

            seen: set[tuple[str, float]] = set()
            matches: list[dict[str, Any]] = []
            for term in _context_search_terms(text):
                for item in conversation.search_thread_history(user_id, thread_id, term, limit=3):
                    key = (str(item.get("text") or ""), float(item.get("created_at") or 0))
                    if key in seen:
                        continue
                    seen.add(key)
                    matches.append(item)
                    if len(matches) >= 5:
                        break
                if len(matches) >= 5:
                    break
            if matches:
                parts.append(
                    "Relevant saved thread matches:\n"
                    + "\n".join(
                        f"- {item.get('role', 'message')}: {_single_line(str(item.get('text') or ''), 220)}"
                        for item in matches
                    )
                )
        except Exception:
            pass

    try:
        from core.smart_context import build_chat_context

        local_context = build_chat_context(user_id, text)
        if local_context:
            parts.append(local_context)
    except Exception:
        pass

    lowered = str(text or "").lower()
    if any(token in lowered for token in ("miss at work", "what did i miss", "what'd i miss", "catch me up", "week", "what's on my week")):
        parts.append(
            "WORK CATCH-UP RULES: answer directly from the live CompanyA context. "
            "Do not ask what systems to check. Do not ask for workplace basics. "
            "If the context is incomplete, make the best synthesis from what is available and only mention a missing system in passing."
        )

    addon = "\n\n".join(parts)
    if len(addon) > max_chars:
        addon = addon[: max(0, max_chars - 3)] + "..."
    return addon
