"""Apple Shortcuts — list and run Shortcuts via CLI."""

import json
import subprocess
from tools.base import BaseTool, make_tool_def


class AppleShortcutsTool(BaseTool):
    name = "apple_shortcuts"
    description = "List and run Apple Shortcuts"

    def get_definitions(self):
        return [
            make_tool_def("apple_shortcuts_list", "List all available Shortcuts", {}, []),
            make_tool_def("apple_shortcuts_run", "Run a Shortcut by name", {"name": {"type": "string"}, "input": {"type": "string", "description": "Optional input text to pass to the Shortcut"}}, ["name"]),
        ]

    async def handle(self, tool_name: str, tool_input: dict) -> str:
        try:
            if tool_name == "apple_shortcuts_list":
                r = subprocess.run(["shortcuts", "list"], capture_output=True, text=True, timeout=15)
                shortcuts = [s.strip() for s in r.stdout.strip().split("\n") if s.strip()]
                return json.dumps({"shortcuts": shortcuts, "count": len(shortcuts)})

            elif tool_name == "apple_shortcuts_run":
                name = tool_input["name"]
                input_text = tool_input.get("input", "")
                cmd = ["shortcuts", "run", name]
                if input_text:
                    cmd.extend(["--input-type", "text", "--input", input_text])
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                return json.dumps({"ok": r.returncode == 0, "output": r.stdout.strip()[:1000], "error": r.stderr.strip()[:200] if r.returncode != 0 else ""})

        except Exception as e:
            return json.dumps({"error": str(e)})
        return json.dumps({"error": f"Unknown tool: {tool_name}"})
