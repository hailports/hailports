"""Reply visibility chat tool."""

from __future__ import annotations

import json

from tools.base import BaseTool, make_tool_def


class ReplyVisibilityTool(BaseTool):
    name = "reply_visibility"
    description = "Inbound outreach reply visibility and review queue"

    def get_definitions(self) -> list[dict]:
        return [
            make_tool_def(
                "reply_visibility_report",
                "Show detected inbound outreach replies, hot leads, neutral replies needing review, bounces, "
                "auto-responses, auto-reply status, and detector health. Use when asked to show, explain, "
                "or action replies reported by the revenue machine.",
                {
                    "format": {
                        "type": "string",
                        "enum": ["summary", "text", "json", "telegram"],
                        "description": "Output format. summary/text are human-readable; json is structured.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum recent replies to include.",
                    },
                },
                [],
            )
        ]

    async def handle(self, tool_name: str, tool_input: dict) -> str:
        from core import reply_visibility

        fmt = str((tool_input or {}).get("format") or "summary").strip().lower()
        limit = int((tool_input or {}).get("limit") or 12)
        summary = reply_visibility.summarize(recent_limit=max(1, limit))
        if fmt == "json":
            return json.dumps(summary, indent=2, default=str)
        if fmt == "telegram":
            return reply_visibility.format_telegram_digest(summary)
        return reply_visibility.format_text_digest(summary, recent_limit=max(1, limit))
