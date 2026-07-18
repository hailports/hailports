"""Claude MCP bridge status/config tools."""

from __future__ import annotations

import json

from mcp_gateway.bridge_config import bridge_manifest, write_runtime_configs
from tools.base import BaseTool, make_tool_def


class ClaudeMCPBridgeTool(BaseTool):
    name = "claude_mcp_bridge"
    description = "Expose claude-stack MCP bridge config for Anthropic API, Claude Code, and Claude.ai connectors."

    def get_definitions(self) -> list[dict]:
        return [
            make_tool_def(
                "claude_mcp_bridge_status",
                "Show redacted MCP bridge status for API, Claude Code, and Claude.ai connector setup.",
                {},
                [],
            ),
            make_tool_def(
                "claude_mcp_bridge_export",
                "Write local runtime MCP bridge config artifacts for Claude API and Claude Code.",
                {
                    "include_token": {
                        "type": "boolean",
                        "description": "Include the local gateway bearer token in chmod-0600 runtime files. Default true.",
                    }
                },
                [],
            ),
        ]

    async def handle(self, tool_name: str, tool_input: dict) -> str:
        if tool_name == "claude_mcp_bridge_status":
            return json.dumps(bridge_manifest(include_token=False), indent=2)
        if tool_name == "claude_mcp_bridge_export":
            include_token = bool(tool_input.get("include_token", True))
            return json.dumps({"ok": True, "paths": write_runtime_configs(include_token=include_token)}, indent=2)
        return f"Unknown Claude MCP bridge tool: {tool_name}"
