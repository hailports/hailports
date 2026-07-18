"""Tool discovery, registration, and dispatch."""

from __future__ import annotations
import logging
from typing import Any

from core.constants import (
    APPROVAL_REQUIRED_TOOLS,
    COMMUNICATION_TOOLS,
    DESTRUCTIVE_TOOLS,
    DEPLOY_TOOLS,
    SALESFORCE_PROD_WRITE_TOOLS,
    WEBUI_CONFIRMATION_ONLY_TOOLS,
    WEBUI_DRAFT_EQUIVALENTS,
)

log = logging.getLogger(__name__)


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Any] = {}  # name -> tool instance

    def register(self, tool) -> None:
        """Register a tool instance. It must have get_definitions() and handle()."""
        for defn in tool.get_definitions():
            name = defn["name"]
            self._tools[name] = tool
            log.debug(f"Registered tool: {name}")  # was info: spammed 83MB on every heavy boot, slowing cold start

    def get_all_definitions(self, source: str = "") -> list[dict]:
        """Return all tool schemas for the Anthropic API tools parameter."""
        definitions = []
        for tool in set(self._tools.values()):
            for defn in tool.get_definitions():
                name = str(defn.get("name") or "").strip()
                if _webui_chat_source(source) and (
                    name in COMMUNICATION_TOOLS or name in WEBUI_CONFIRMATION_ONLY_TOOLS
                ):
                    continue
                definitions.append(defn)
        return definitions

    async def execute(self, tool_name: str, tool_input: dict, *, source: str = "") -> str:
        """Execute a tool call and return the result as a string."""
        tool_input = tool_input if isinstance(tool_input, dict) else {}
        explicit_approval = bool(tool_input.get("explicit_approval"))
        rewrite_from = ""

        if _webui_chat_source(source):
            if tool_name in COMMUNICATION_TOOLS:
                return (
                    f"BLOCKED: {tool_name} would send or post externally. "
                    "WebUI chat is drafts-only; tee up the copy and wait for a confirm-draft click."
                )
            if tool_name in WEBUI_CONFIRMATION_ONLY_TOOLS:
                return (
                    f"BLOCKED: {tool_name} is confirm-only. "
                    "WebUI chat can prepare the action, but only an explicit confirm action may execute it."
                )
        elif _webui_source(source) and tool_name in COMMUNICATION_TOOLS:
            replacement = WEBUI_DRAFT_EQUIVALENTS.get(tool_name, "")
            if replacement:
                rewrite_from = tool_name
                tool_name = replacement
            else:
                return (
                    f"BLOCKED: {tool_name} would send or post externally. "
                    "The WebUI may tee up the message, but it cannot send it."
                )

        tool = self._tools.get(tool_name)
        if not tool:
            return f"Error: Unknown tool '{tool_name}'"

        if tool_name in APPROVAL_REQUIRED_TOOLS and not explicit_approval:
            if tool_name in SALESFORCE_PROD_WRITE_TOOLS:
                return (
                    f"BLOCKED: {tool_name} is a Salesforce production write path and requires Operator's express approval "
                    "in the current request. Prepare diagnostics, a rollback plan, and the exact proposed change instead."
                )
            if tool_name in COMMUNICATION_TOOLS:
                return (
                    f"BLOCKED: {tool_name} is an outbound communication action and requires explicit user approval. "
                    "Have the user directly confirm the send/post/invite action before executing it."
                )
            if tool_name in DESTRUCTIVE_TOOLS:
                return (
                    f"BLOCKED: {tool_name} is destructive and requires explicit user approval. "
                    "Prepare the action and wait for direct consent before executing it."
                )
            return (
                f"BLOCKED: {tool_name} requires explicit user approval before execution."
            )

        if tool_name in DEPLOY_TOOLS and bool(tool_input.get("confirmed")) and not explicit_approval:
            return (
                f"BLOCKED: {tool_name} is a deployment action and requires explicit user approval at execution time. "
                "Prepare the change and mark it ready instead of deploying automatically."
            )

        try:
            result = await tool.handle(tool_name, tool_input)
            result_text = str(result)
            if rewrite_from and not result_text.startswith(("Error", "BLOCKED:")):
                return "Draft created instead of sending due to the WebUI drafts-only safeguard. " + result_text
            return result_text
        except Exception as e:
            log.exception(f"Tool {tool_name} failed")
            return f"Error executing {tool_name}: {e}"


def _webui_source(source: str) -> bool:
    return str(source or "").strip().lower().startswith("webui")


def _webui_chat_source(source: str) -> bool:
    token = str(source or "").strip().lower()
    return token == "webui" or token.startswith("webui:chat")


def _text_only_chat_source(source: str) -> bool:
    token = str(source or "").strip().lower()
    return token.startswith("webui:chat") and (
        "text" in token or "chat_only" in token or "chat-only" in token
    )


def _media_tool(name: str) -> bool:
    return str(name or "").startswith("image_production_")
