"""Fully-local travel agent — LOCAL_MODEL native tool-calling, zero Claude API.

Flow: user text → intent detected → qwen3 picks which travel_* tool + extracts
args → tools/travel.py executes → formatted result returned (already contains
affiliate deep-link).

Returns None if no clean tool call could be extracted — caller falls through
to the normal engine path.
"""

from __future__ import annotations

from core.constants import LOCAL_MODEL
import asyncio
import logging
import os
import re
import time
from datetime import datetime

import httpx

from tools.travel import TravelTool
from core.local_client import OLLAMA_URL

log = logging.getLogger(__name__)
MODEL = LOCAL_MODEL

# Defaults — override with env var or per-user profile
DEFAULT_HOME_AIRPORT = os.environ.get("TRAVEL_HOME_AIRPORT", "DEN")

# Travel-intent gate — kept precise so we don't steal metaphorical "fly" queries.
# Hits words that are strongly travel-scoped in practice.
_TRAVEL_INTENT = re.compile(
    r"\b(flight|flights|airfare|airline|airport code|"
    r"fly (to|from|between)|flying (to|from)|"
    r"trip to|travel to|"
    r"hotel|airbnb|lodging|accommodation|"
    r"cheapest day to fly|cheapest way to fly|"
    r"book (a )?(flight|hotel|room|stay)|"
    r"itinerary|boarding pass)\b",
    re.IGNORECASE,
)


def is_travel_intent(text: str) -> bool:
    return bool(text and _TRAVEL_INTENT.search(text))


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


def _profile_hint(user_profile):
    if user_profile:
        home = user_profile.get("home_airport") or DEFAULT_HOME_AIRPORT
    else:
        home = DEFAULT_HOME_AIRPORT
    return f"User's home airport: {home}."


async def _plan_tool_call(user_text: str, user_profile=None, today_iso=None):
    """Use LOCAL_MODEL tool-calling to pick + parameterize a travel_* tool."""
    from core.local_client import ollama_keep_alive_value

    today = today_iso or datetime.now().strftime("%Y-%m-%d")
    tool = TravelTool()
    ollama_tools = _anthropic_to_ollama_tools(tool.get_definitions())

    # Short system prompt — fewer prompt-eval tokens, faster planning.
    system = (
        f"Travel-search agent. Today: {today}. {_profile_hint(user_profile)} "
        f"Emit 3-letter IATA codes. Resolve relative dates. One tool call."
    )

    async with httpx.AsyncClient(timeout=240) as c:
        r = await c.post(
            f"{OLLAMA_URL}/api/chat",
            json={
                "model": MODEL,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_text},
                ],
                "stream": False,
                "tools": ollama_tools,
                "options": {"num_predict": 1200},
                "keep_alive": ollama_keep_alive_value(),
            },
        )
        r.raise_for_status()
        data = r.json()

    calls = data.get("message", {}).get("tool_calls") or []
    if not calls:
        return None, None, data
    fn = calls[0].get("function", {})
    return fn.get("name"), fn.get("arguments") or {}, data


async def handle(user_text: str, user_profile=None, today_iso=None) -> str | None:
    """Local travel agent entry point.

    Returns the formatted tool result (includes affiliate deep-link) on success,
    or None if the LLM couldn't produce a clean tool call — caller may fall back
    to the normal API-based engine path.
    """
    t0 = time.time()
    try:
        name, args, raw = await _plan_tool_call(user_text, user_profile, today_iso)
    except Exception as e:
        log.error(f"travel_local planning failed: {e}")
        return None

    if not name:
        log.warning("travel_local: no tool_call emitted; user_text=%r", user_text[:120])
        return None

    log.info(f"travel_local: {name}({args}) planning took {time.time()-t0:.1f}s")

    try:
        result = await TravelTool().handle(name, args)
    except Exception as e:
        log.exception("travel_local: tool execution failed")
        return f"Travel search failed mid-execution: {e}"

    dt = time.time() - t0
    log.info(f"travel_local: total {dt:.1f}s · {name}")
    return result
