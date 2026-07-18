"""Slack Web API integration."""

from __future__ import annotations

import os
from datetime import datetime

import httpx

from core.api_client import ensure_external_api_allowed
from tools.base import BaseTool, make_tool_def

API_BASE = "https://slack.com/api"
CHANNEL_TYPES = "public_channel,private_channel,mpim,im"


def _normalize_channel(channel: str) -> str:
    return str(channel or "").strip().lstrip("#")


def _display_channel(channel: str) -> str:
    token = str(channel or "").strip()
    if not token:
        return "(unknown)"
    if token.startswith(("#", "@")):
        return token
    if token[:1] in {"C", "G", "D"} and token[1:].isalnum():
        return token
    return f"#{token}"


def _format_ts(ts: str) -> str:
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(ts or "")


async def send_message(get_token, channel: str, text: str, thread_ts: str = "") -> dict:
    """Post a Slack message to a named channel or channel ID."""
    tool = SlackTool(get_token)
    return await tool.post_message(channel=channel, text=text, thread_ts=thread_ts)


class SlackTool(BaseTool):
    name = "slack"
    description = "Slack channels and messages"

    def __init__(self, get_token):
        self._get_token = get_token

    async def _headers(self) -> dict:
        ensure_external_api_allowed("Slack API")
        token = await self._get_token("slack")
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"}

    async def _api_get(self, method: str, params: dict | None = None) -> dict:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(f"{API_BASE}/{method}", headers=await self._headers(), params=params)
            response.raise_for_status()
            payload = response.json()
        if not payload.get("ok", False):
            raise RuntimeError(payload.get("error") or f"Slack API {method} failed")
        return payload

    async def _api_post(self, method: str, payload: dict) -> dict:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(f"{API_BASE}/{method}", headers=await self._headers(), json=payload)
            response.raise_for_status()
            data = response.json()
        if not data.get("ok", False):
            raise RuntimeError(data.get("error") or f"Slack API {method} failed")
        return data

    async def _resolve_channel_id(self, channel: str) -> tuple[str, str]:
        raw = str(channel or "").strip()
        if not raw:
            raise RuntimeError("Slack channel is required")
        if raw[:1] in {"C", "G", "D"} and raw[1:].isalnum():
            return raw, raw

        want = _normalize_channel(raw).lower()
        cursor = ""
        while True:
            params = {
                "exclude_archived": "true",
                "limit": 200,
                "types": CHANNEL_TYPES,
            }
            if cursor:
                params["cursor"] = cursor
            payload = await self._api_get("conversations.list", params=params)
            for row in payload.get("channels", []):
                candidates = {
                    str(row.get("name") or "").lower(),
                    str(row.get("name_normalized") or "").lower(),
                }
                if want in candidates and row.get("id"):
                    return str(row["id"]), str(row.get("name") or want)
            cursor = str((payload.get("response_metadata") or {}).get("next_cursor") or "").strip()
            if not cursor:
                break
        raise RuntimeError(f"Slack channel not found: {raw}")

    async def post_message(self, channel: str, text: str, thread_ts: str = "") -> dict:
        channel_id, channel_name = await self._resolve_channel_id(channel)
        payload = {"channel": channel_id, "text": str(text or "")}
        if thread_ts:
            payload["thread_ts"] = str(thread_ts)
        data = await self._api_post("chat.postMessage", payload)
        data["_channel_name"] = channel_name
        return data

    def get_definitions(self) -> list[dict]:
        return [
            make_tool_def(
                "slack_list_channels",
                "List Slack channels the bot can access.",
                {"limit": {"type": "integer", "description": "Number of channels to list (default 25)"}},
                [],
            ),
            make_tool_def(
                "slack_read_channel",
                "Read recent messages from a Slack channel.",
                {
                    "channel": {"type": "string", "description": "Channel name like #ops or channel ID"},
                    "limit": {"type": "integer", "description": "Messages to return (default 10)"},
                },
                ["channel"],
            ),
            make_tool_def(
                "slack_search_messages",
                "Search Slack messages by keyword.",
                {
                    "query": {"type": "string", "description": "Slack search query"},
                    "count": {"type": "integer", "description": "Matches to return (default 10)"},
                },
                ["query"],
            ),
            make_tool_def(
                "slack_send_message",
                "Send a Slack message to a channel.",
                {
                    "channel": {"type": "string", "description": "Channel name like #ops or channel ID"},
                    "text": {"type": "string", "description": "Message text"},
                    "thread_ts": {"type": "string", "description": "Optional thread timestamp to reply in thread"},
                },
                ["channel", "text"],
            ),
        ]

    async def handle(self, tool_name: str, tool_input: dict) -> str:
        if tool_name == "slack_list_channels":
            limit = max(1, min(int(tool_input.get("limit", 25) or 25), 100))
            payload = await self._api_get(
                "conversations.list",
                {"exclude_archived": "true", "limit": limit, "types": CHANNEL_TYPES},
            )
            channels = payload.get("channels", [])
            if not channels:
                return "No Slack channels available."
            return "\n".join(
                f"{_display_channel(row.get('name') or row.get('id') or '')} ({row.get('id', '?')})"
                for row in channels[:limit]
            )

        if tool_name == "slack_read_channel":
            limit = max(1, min(int(tool_input.get("limit", 10) or 10), 50))
            channel_id, channel_name = await self._resolve_channel_id(tool_input["channel"])
            payload = await self._api_get(
                "conversations.history",
                {"channel": channel_id, "limit": limit},
            )
            messages = payload.get("messages", [])
            if not messages:
                return f"No recent messages in {_display_channel(channel_name)}."
            lines = []
            for msg in messages[:limit]:
                sender = msg.get("user") or msg.get("username") or msg.get("bot_id") or msg.get("subtype") or "unknown"
                text = " ".join(str(msg.get("text") or "").split()) or "(no text)"
                lines.append(f"{_format_ts(msg.get('ts', ''))} | {sender} | {text}")
            return "\n".join(lines)

        if tool_name == "slack_search_messages":
            count = max(1, min(int(tool_input.get("count", 10) or 10), 50))
            payload = await self._api_get(
                "search.messages",
                {"query": tool_input["query"], "count": count},
            )
            matches = ((payload.get("messages") or {}).get("matches") or [])
            if not matches:
                return "No matching Slack messages found."
            lines = []
            for match in matches[:count]:
                channel = (match.get("channel") or {}).get("name") or (match.get("channel") or {}).get("id") or "unknown"
                sender = match.get("username") or match.get("user") or "unknown"
                text = " ".join(str(match.get("text") or "").split()) or "(no text)"
                permalink = str(match.get("permalink") or "").strip()
                suffix = f"\n  {permalink}" if permalink else ""
                lines.append(f"{_display_channel(channel)} | {sender} | {text}{suffix}")
            return "\n".join(lines)

        if tool_name == "slack_send_message":
            data = await self.post_message(
                channel=tool_input["channel"],
                text=tool_input["text"],
                thread_ts=str(tool_input.get("thread_ts") or ""),
            )
            channel_name = _display_channel(data.get("_channel_name") or tool_input["channel"])
            return f"Slack message sent to {channel_name}."

        return f"Unknown slack tool: {tool_name}"
