"""macOS Notifications — send system notifications via osascript."""

import json
import subprocess
from tools.base import BaseTool, make_tool_def


def _esc(s: str) -> str:
    return str(s or "").replace("\\", "\\\\").replace('"', '\\"')


class NotificationsTool(BaseTool):
    name = "notifications"
    description = "Send macOS system notifications"

    def get_definitions(self):
        return [
            make_tool_def("notify", "Send a macOS notification", {"title": {"type": "string"}, "message": {"type": "string"}, "sound": {"type": "boolean", "description": "Play notification sound (default true)"}}, ["title", "message"]),
            make_tool_def("notify_critical", "Send a critical notification that stays visible", {"title": {"type": "string"}, "message": {"type": "string"}}, ["title", "message"]),
        ]

    async def handle(self, tool_name: str, tool_input: dict) -> str:
        try:
            title = _esc(tool_input["title"])
            message = _esc(tool_input["message"])

            if tool_name == "notify":
                sound = tool_input.get("sound", True)
                sound_clause = ' sound name "Glass"' if sound else ""
                subprocess.run(["osascript", "-e",
                    f'display notification "{message}" with title "{title}"{sound_clause}'],
                    capture_output=True, text=True, timeout=10)
                return json.dumps({"ok": True, "title": tool_input["title"]})

            elif tool_name == "notify_critical":
                subprocess.run(["osascript", "-e",
                    f'display alert "{title}" message "{message}" as critical'],
                    capture_output=True, text=True, timeout=10)
                return json.dumps({"ok": True, "title": tool_input["title"], "critical": True})

        except Exception as e:
            return json.dumps({"error": str(e)})
        return json.dumps({"error": f"Unknown tool: {tool_name}"})
