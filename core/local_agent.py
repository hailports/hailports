"""Generic local tool-using agent — LOCAL_MODEL native tool-calling against
a curated list of READ + safe-WRITE tools. This is the savings-recoupment layer:
when the regex-based local_tools misses, we try tool-calling here before
paying Sonnet rates.

Design choices:
- READ tools + low-risk WRITE tools (create draft, flag email, save doc).
  High-risk writes (send email, modify CRM, delete) always go to Sonnet — higher stakes, Sonnet's tool-calling is more reliable,
  and a mistake here is hard to undo. The $/write is tiny relative to the
  risk of a wrong write.
- Context-tight: 15-20 tool defs fit in qwen3's 8K context with room for
  tool output. Passing all 140 tools blows context and ruins latency.
- Single tool-call iteration. Most inbound messages need 1 tool. Multi-
  tool workflows fall through to Sonnet (which handles them natively).
- Savings logged every success so the dashboard reflects actual displaced
  cost.
"""

from __future__ import annotations

from core.constants import LOCAL_MODEL
import asyncio
import fcntl
import json
import logging
import time
from datetime import datetime
from pathlib import Path

import httpx

from core import BASE_DIR, local_client, mountain_now
from core.local_client import OLLAMA_URL

log = logging.getLogger(__name__)
MODEL = LOCAL_MODEL
SAVINGS_LOG = BASE_DIR / "data" / "logs" / "savings.jsonl"

# Curated list of READ tools the local agent can pick from. Keep this short
# so qwen3's context isn't blown out. If a query legitimately needs a tool
# not in this list (write actions, exotic integrations), the agent returns
# None and the caller falls through to Sonnet.
ALLOWED_TOOLS = {
    # ── READS (zero risk) ──
    # Outlook calendar + inbox
    "outlook_today_agenda", "outlook_upcoming_events",
    "outlook_get_inbox", "outlook_get_sent", "outlook_search_emails",
    "outlook_search_events", "outlook_unread_count", "outlook_get_drafts",
    "outlook_read_email", "outlook_search_contacts", "outlook_find_email_address",
    "outlook_inbox_delta",
    # Apple Calendar
    "calendar_today_agenda", "calendar_upcoming", "calendar_search",
    # Salesforce reads
    "salesforce_soql_query", "salesforce_find_accounts",
    "salesforce_find_cases", "salesforce_find_contacts",
    "salesforce_find_opportunities", "salesforce_get_record",
    "salesforce_describe_object", "salesforce_search",
    # Monday reads
    "monday_list_boards", "monday_get_board_items", "monday_search_items",
    "monday_get_updates",
    # Littlebird
    "littlebird_my_meetings", "littlebird_recent_notes", "littlebird_search",
    "littlebird_action_items", "littlebird_daily_summary", "littlebird_transcripts",
    # Zoom reads
    "zoom_list_meetings", "zoom_today_meetings", "zoom_read_all",
    "zoom_unread_chats", "zoom_list_chats", "zoom_read_chat",
    "zoom_scan_all_chats", "zoom_calendar_quick_recaps", "zoom_status",
    # Slack reads
    "slack_list_channels", "slack_read_channel", "slack_search_messages",
    # Apple personal context reads
    "apple_contacts_search", "apple_contacts_get",
    "apple_notes_search", "apple_notes_list", "apple_notes_read",
    "apple_reminders_lists", "apple_reminders_list",
    "apple_shortcuts_list",
    # Travel search reads / user-completed booking links
    "travel_flight_search", "travel_flight_calendar", "travel_hotel_search",
    # Web
    "web_search", "web_fetch",

    # ── SAFE WRITES (low blast radius — user reviews before action) ──
    "outlook_flag_email",       # flags email — harmless
    "outlook_mark_as_read",     # marks read — harmless
    "outlook_mark_as_unread",   # marks unread — harmless
    "outlook_edit_draft",       # edits Operator's own Drafts only, never sends
    "monday_add_update",        # adds comment to Monday item — low risk
    "save_document",            # saves to OneDrive Claude Outputs — user reviews
}


def _anthropic_to_ollama_tools(defs):
    """Convert tools/base.py make_tool_def format to Ollama function format."""
    return [
        {
            "type": "function",
            "function": {
                "name": d["name"],
                "description": d["description"],
                "parameters": d["input_schema"],
            },
        }
        for d in defs
    ]


def _log_savings(user_text, response, displaced_tier, tool_name):
    """Log a successful local resolution to savings.jsonl so the dashboard
    reflects actual displaced cost. Schema matches llm_router.log_savings
    so existing readers work unchanged."""
    try:
        # Rate estimates per-1M-tokens for the displaced tier
        rates = {
            "haiku":  {"input": 0.25,  "output": 1.25},
            "sonnet": {"input": 3.0,   "output": 15.0},
            "opus":   {"input": 15.0,  "output": 75.0},
        }.get(displaced_tier, {"input": 3.0, "output": 15.0})
        in_tok = len(user_text) / 4
        out_tok = len(response) / 4
        saved = (in_tok * rates["input"] + out_tok * rates["output"]) / 1_000_000

        now = mountain_now()
        entry = {
            "ts": time.time(),
            "date": now.strftime("%Y-%m-%d"),
            "source": f"local_agent:{tool_name or 'bare'}",
            "displaced_tier": displaced_tier,
            "local_model": MODEL,
            "tier": "agent",
            "in_chars": len(user_text),
            "out_chars": len(response),
            "saved": round(saved, 6),
        }
        SAVINGS_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(SAVINGS_LOG, "a") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


async def _qwen_tool_call(messages, tools, timeout=30):
    """Single qwen3 /api/chat call with tool schemas. Returns parsed response."""
    from core.local_client import ollama_keep_alive_value

    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.post(
            f"{OLLAMA_URL}/api/chat",
            json={
                "model": MODEL,
                "messages": messages,
                "stream": False,
                "tools": tools,
                "options": {"num_predict": 6144, "num_ctx": 12288},
                "keep_alive": ollama_keep_alive_value(),
            },
        )
        r.raise_for_status()
        return r.json()


async def handle(
    user_text: str,
    tool_registry,
    user_addon: str = "",
    displaced_tier: str = "sonnet",
) -> tuple[str, str] | None:
    """Try to resolve user_text with qwen3 tool-calling.

    Returns (response_text, tool_name_used) on success, or None on any
    failure — caller should escalate to Sonnet.

    Guards:
      - Only tools in ALLOWED_TOOLS get exposed. Everything else → Sonnet.
      - If qwen3 picks a tool not in the registry, fall through.
      - If tool execution fails, fall through.
      - If synthesis produces junk (empty / fake tool name), fall through.
    """
    # Get all registered tool defs, filter to allowed set.
    try:
        all_defs = tool_registry.get_all_definitions() or []
    except Exception as e:
        log.warning(f"local_agent: failed to read tool registry: {e}")
        return None

    filtered_defs = [d for d in all_defs if d.get("name") in ALLOWED_TOOLS]
    if not filtered_defs:
        log.warning("local_agent: no allowed tools available in registry")
        return None

    ollama_tools = _anthropic_to_ollama_tools(filtered_defs)
    today = datetime.now().strftime("%A, %B %d %Y")

    # Detect document creation intent
    import re as _re_doc
    _is_doc_request = bool(_re_doc.search(
        r"(create|generate|build|make|write|draft|produce)\s+.{0,30}"
        r"(doc|document|guide|report|process|sop|presentation|template|brief|memo)",
        user_text, _re_doc.IGNORECASE
    ))

    if _is_doc_request:
        system = (
            f"You are a document generator. Today is {today}. "
            "You MUST call the save_document tool. "
            "Generate the FULL document as styled HTML with inline CSS. "
            "Include headings, tables, and professional styling. "
            "NEVER reply with plain text. ALWAYS call save_document with complete HTML content."
        )
    else:
        system = (
            f"You are Operator's personal assistant. Today is {today}. "
            f"{user_addon} "
            "If the user's question can be answered with a tool, call ONE tool with "
            "correct parameters. If no tool applies, reply in plain English directly. "
            "Never fabricate a tool name — only use the tools provided."
        )
        if re.search(r"\b(?:what(?:'d| did)? i miss(?: at work)?(?: this week)?|what have i missed(?: at work)?|catch me up(?: on work)?|bring me up to speed(?: on work)?|what(?:'s| is)?\s+happening\s+this\s+week|what(?:'s| is)?\s+on\s+my\s+week)\b", str(user_text or ""), re.I):
            system += (
                " For work catch-up requests, assume the live CompanyA integrations are already available "
                "(Outlook, Zoom Team Chat, Teams, Slack, Monday.com, SharePoint/OneDrive, Littlebird, WorkOS, revenue and infra signals). "
                "Do not ask the user which systems to check. Synthesize naturally from the available context."
            )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_text},
    ]

    # --- Step 1: ask qwen3 to pick a tool or answer directly ---
    t0 = time.time()
    try:
        _timeout = 600 if _is_doc_request else 30
        log.info(f"local_agent: calling with timeout={_timeout}s, doc_request={_is_doc_request}"); resp = await _qwen_tool_call(messages, ollama_tools, timeout=_timeout)
    except Exception as e:
        log.warning(f"local_agent: tool-call failed: {type(e).__name__}: {e}")
        return None

    msg = resp.get("message", {})
    tool_calls = msg.get("tool_calls") or []
    content = (msg.get("content") or "").strip()

    # Case A: qwen3 emitted a tool_call
    if tool_calls:
        tc = tool_calls[0]["function"]
        name = tc.get("name", "")
        args = tc.get("arguments") or {}
        if name not in ALLOWED_TOOLS:
            log.warning(f"local_agent: qwen3 picked disallowed tool {name!r}, falling through")
            return None
        log.info(f"local_agent: qwen3 picked {name}({args}) in {time.time()-t0:.1f}s")

        # Execute the tool
        try:
            tool_result = await asyncio.wait_for(
                tool_registry.execute(name, args),
                timeout=60,
            )
        except Exception as e:
            log.warning(f"local_agent: tool {name} failed: {e}")
            return None

        # --- Step 2: ask qwen3 to synthesize user-facing response from tool output ---
        synth_messages = [
            {"role": "system", "content": (
                f"You are writing the user-facing reply. Today is {today}. "
                "Format the tool output into a concise, helpful response. "
                "Use bullet points for lists. Do NOT invent information. "
                "Do NOT emit tool names or function-call syntax. "
                "For work catch-up requests, do not ask the user which systems to check; answer naturally from the available live context."
            )},
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": f"I called {name} and got:\n\n{str(tool_result)[:3500]}"},
            {"role": "user", "content": "Write the final reply to the user now, plain text only."},
        ]
        try:
            synth = await _qwen_tool_call(synth_messages, [], timeout=30)
        except Exception as e:
            log.warning(f"local_agent: synthesis call failed: {e}")
            return None

        synth_text = (synth.get("message", {}).get("content") or "").strip()
        # Reject fake tool-call outputs
        if not synth_text or len(synth_text) < 10:
            log.warning(f"local_agent: synthesis returned empty/tiny")
            return None
        import re as _re
        if _re.match(r"^[a-z][a-z0-9_]+\s*(\(|$)", synth_text, _re.I) and len(synth_text) < 100:
            log.warning(f"local_agent: synthesis looks like a fake tool call: {synth_text[:80]!r}")
            return None

        dt = time.time() - t0
        log.info(f"local_agent: {name} end-to-end in {dt:.1f}s, {len(synth_text)} chars")
        _log_savings(user_text, synth_text, displaced_tier, name)
        return synth_text, name

    # Case B: qwen3 answered directly (no tool needed)
    if content and len(content) > 10:
        # Same fake-tool-call guard
        import re as _re
        if _re.match(r"^[a-z][a-z0-9_]+\s*(\(|$)", content, _re.I) and len(content) < 100:
            log.warning(f"local_agent: direct reply looks like a fake tool call: {content[:80]!r}")
            return None
        log.info(f"local_agent: direct reply in {time.time()-t0:.1f}s, {len(content)} chars")
        _log_savings(user_text, content, displaced_tier, "")
        return content, ""

    # Case C: empty response — fall through
    log.warning(f"local_agent: no tool_calls and no content — falling through")
    return None
