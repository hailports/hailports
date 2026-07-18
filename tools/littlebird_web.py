"""Littlebird Sync — read meeting notes from synced Littlebird data. $0 cost.

Operator and Operator2 each run Littlebird on their own devices.
Their Littlebird data auto-syncs to the Mini via rsync (WatchPaths LaunchAgent).
We read the synced IndexedDB/LevelDB files directly — no web scraping, no API, $0.

Data paths on Mini:
  Operator:   ~/claude-stack/data/littlebird/Operator/IndexedDB/...
  Operator2: ~/claude-stack/data/littlebird/Operator2/IndexedDB/...

Setup per device:
  Install the sync script + LaunchAgent that watches Littlebird data dir
  and rsyncs to the Mini on every change.
"""

import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.expanduser("~/claude-stack"))
from tools.base import BaseTool, make_tool_def

log = logging.getLogger("littlebird-web")

CACHE_DIR = Path(os.path.expanduser("~/claude-stack/data/littlebird"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_TTL = 86400  # 24 hours — only scrape when someone actually asks

PROFILES = {
    "Operator": "littlebird-Operator",
    "Operator2": "littlebird-Operator2",
    "partner": "littlebird-Operator2",
}


def _cache_path(user: str) -> Path:
    return CACHE_DIR / f"{user}_meetings.json"


def _load_cache(user: str) -> dict:
    try:
        path = _cache_path(user)
        if path.exists():
            data = json.loads(path.read_text())
            if time.time() - data.get("fetched_at", 0) < CACHE_TTL:
                return data
    except Exception:
        pass
    return {}


def _save_cache(user: str, data: dict):
    data["fetched_at"] = time.time()
    data["user"] = user
    _cache_path(user).write_text(json.dumps(data, indent=2, default=str))


async def _scrape_meetings(user: str) -> list[dict]:
    """Use browser-use to pull meetings from app.littlebird.ai for a specific user profile."""
    profile = PROFILES.get(user, f"littlebird-{user}")
    try:
        from browser_use import Agent, ChatAnthropic
        from dotenv import load_dotenv
        load_dotenv(os.path.expanduser("~/claude-stack/.env"))

        llm = ChatAnthropic(
            model="claude-sonnet-4-20250514",
            api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        )
        agent = Agent(
            task=f"""Go to https://app.littlebird.ai
If logged in, navigate to the meetings or notes section.
Extract the last 10 meetings with:
- Meeting title/subject
- Date and time
- Participants (if shown)
- Summary/notes text (if available)
- Any action items

Return the data as structured text.
Do not click any delete or modify buttons.""",
            llm=llm,
            use_vision=False,
        )
        result = await agent.run()

        extracted = []
        for item in getattr(result, "all_results", []):
            if hasattr(item, "extracted_content") and item.extracted_content and len(item.extracted_content) > 50:
                extracted.append(item.extracted_content)

        meetings = [{
            "source": "littlebird_web",
            "user": user,
            "profile": profile,
            "raw": "\n".join(extracted),
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }]

        _save_cache(user, {"meetings": meetings})
        return meetings

    except Exception as e:
        log.error("Failed to scrape %s's Littlebird: %s", user, e)
        return []


class LittlebirdWebTool(BaseTool):
    name = "littlebird_web"
    description = "Access meeting notes from Littlebird web app for Operator or Operator2"

    def get_definitions(self):
        return [
            make_tool_def(
                "littlebird_web_meetings",
                "Get recent meeting notes from Littlebird for a specific user (Operator or Operator2)",
                {
                    "user": {"type": "string", "description": "User: 'Operator' or 'Operator2'"},
                    "refresh": {"type": "boolean", "description": "Force refresh from web (default: use 24hr cache)"},
                },
                ["user"],
            ),
            make_tool_def(
                "littlebird_web_search",
                "Search Littlebird meetings by keyword for a specific user",
                {
                    "user": {"type": "string", "description": "User: 'Operator' or 'Operator2'"},
                    "query": {"type": "string"},
                },
                ["user", "query"],
            ),
        ]

    async def handle(self, tool_name: str, tool_input: dict) -> str:
        try:
            user = tool_input.get("user", "Operator").lower()
            if user not in PROFILES:
                return json.dumps({"error": f"Unknown user: {user}. Use 'Operator' or 'Operator2'."})

            if tool_name == "littlebird_web_meetings":
                refresh = tool_input.get("refresh", False)
                cache = _load_cache(user)
                if cache and not refresh:
                    return json.dumps(cache)
                meetings = await _scrape_meetings(user)
                return json.dumps({"meetings": meetings, "count": len(meetings), "user": user})

            elif tool_name == "littlebird_web_search":
                query = tool_input.get("query", "").lower()
                cache = _load_cache(user)
                if not cache:
                    meetings = await _scrape_meetings(user)
                    cache = {"meetings": meetings}
                matches = [m for m in cache.get("meetings", []) if query in json.dumps(m).lower()]
                return json.dumps({"matches": matches, "query": query, "user": user})

        except Exception as e:
            return json.dumps({"error": str(e)})
        return json.dumps({"error": f"Unknown tool: {tool_name}"})
