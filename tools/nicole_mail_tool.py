"""Operator2's Mail as a BaseTool — wraps nicole_mail.py standalone functions for MCP exposure."""

import json
from tools.base import BaseTool, make_tool_def
from tools.nicole_mail import (
    NICOLE_ACCOUNTS,
    get_nicole_inbox,
    get_nicole_unread_counts,
    get_nicole_calendar_events,
    send_nicole_email,
    reply_nicole_email,
    forward_nicole_email,
)


class NicoleMailTool(BaseTool):
    name = "nicole_mail"
    description = "Operator2's email and calendar across 4 accounts (kipi.ai, Gmail, cnovate.io, iCloud)"

    def get_definitions(self):
        return [
            make_tool_def(
                "nicole_get_inbox",
                "Get recent messages from all of Operator2's email accounts, merged and sorted by date",
                {"count": {"type": "integer", "description": "Number of messages per account (default 20)"}},
                [],
            ),
            make_tool_def(
                "nicole_unread_counts",
                "Get unread email count per account",
                {},
                [],
            ),
            make_tool_def(
                "nicole_calendar_events",
                "Get Operator2's Apple Calendar events for today or upcoming days",
                {"days": {"type": "integer", "description": "Number of days to look ahead (default 1)"}},
                [],
            ),
            make_tool_def(
                "nicole_send_email",
                "Send an email from one of Operator2's accounts",
                {
                    "account": {"type": "string", "description": f"Sender account. One of: {', '.join(NICOLE_ACCOUNTS)}"},
                    "to": {"type": "string", "description": "Recipient email address"},
                    "subject": {"type": "string", "description": "Email subject"},
                    "body_html": {"type": "string", "description": "Email body (HTML or plain text)"},
                },
                ["account", "to", "subject", "body_html"],
            ),
            make_tool_def(
                "nicole_reply_email",
                "Reply to an email found by subject in Operator2's inbox",
                {
                    "account": {"type": "string", "description": f"Account to search. One of: {', '.join(NICOLE_ACCOUNTS)}"},
                    "message_subject": {"type": "string", "description": "Subject of the email to reply to (partial match)"},
                    "body_html": {"type": "string", "description": "Reply body"},
                },
                ["account", "message_subject", "body_html"],
            ),
            make_tool_def(
                "nicole_create_draft",
                "Create a draft email in Apple Mail for Operator2",
                {
                    "account": {"type": "string", "description": f"Drafting account. One of: {', '.join(NICOLE_ACCOUNTS)}"},
                    "to": {"type": "string", "description": "Recipient email"},
                    "subject": {"type": "string", "description": "Email subject"},
                    "body_html": {"type": "string", "description": "Draft body"},
                },
                ["account", "to", "subject", "body_html"],
            ),
            make_tool_def(
                "nicole_forward_email",
                "Forward an email from Operator2's inbox to a new recipient",
                {
                    "account": {"type": "string", "description": f"Source account. One of: {', '.join(NICOLE_ACCOUNTS)}"},
                    "message_subject": {"type": "string", "description": "Subject of email to forward (partial match)"},
                    "to": {"type": "string", "description": "Forward to this email address"},
                },
                ["account", "message_subject", "to"],
            ),
        ]

    async def handle(self, tool_name: str, tool_input: dict) -> str:
        if tool_name == "nicole_get_inbox":
            result = get_nicole_inbox(tool_input.get("count", 20))
            return json.dumps(result, default=str)
        elif tool_name == "nicole_unread_counts":
            result = get_nicole_unread_counts()
            return json.dumps(result)
        elif tool_name == "nicole_calendar_events":
            result = get_nicole_calendar_events(tool_input.get("days", 1))
            return json.dumps(result, default=str)
        elif tool_name == "nicole_send_email":
            result = send_nicole_email(
                tool_input["account"], tool_input["to"],
                tool_input["subject"], tool_input["body_html"],
            )
            return json.dumps(result)
        elif tool_name == "nicole_reply_email":
            result = reply_nicole_email(
                tool_input["account"], tool_input["message_subject"],
                tool_input["body_html"],
            )
            return json.dumps(result)
        elif tool_name == "nicole_create_draft":
            result = {"ok": False, "error": "Apple Mail draft creation is disabled"}
            return json.dumps(result)
        elif tool_name == "nicole_forward_email":
            result = forward_nicole_email(
                tool_input["account"], tool_input["message_subject"],
                tool_input["to"],
            )
            return json.dumps(result)
        return json.dumps({"error": f"Unknown tool: {tool_name}"})
