"""Apple Reminders — list, create, complete reminders via AppleScript."""

import json
import subprocess
from tools.base import BaseTool, make_tool_def


def _osa(script: str, timeout: int = 30) -> str:
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[:200])
    return r.stdout.strip()


def _esc(s: str) -> str:
    return str(s or "").replace("\\", "\\\\").replace('"', '\\"')


class AppleRemindersTool(BaseTool):
    name = "apple_reminders"
    description = "List, create, and complete Apple Reminders"

    def get_definitions(self):
        return [
            make_tool_def("apple_reminders_lists", "List all reminder lists", {}, []),
            make_tool_def("apple_reminders_list", "List reminders in a list", {"list_name": {"type": "string"}, "show_completed": {"type": "boolean"}}, []),
            make_tool_def("apple_reminders_create", "Create a new reminder", {"title": {"type": "string"}, "list_name": {"type": "string"}, "notes": {"type": "string"}, "due_date": {"type": "string", "description": "Date string like 'April 30, 2026'"}}, ["title"]),
            make_tool_def("apple_reminders_complete", "Mark a reminder as complete", {"title": {"type": "string"}, "list_name": {"type": "string"}}, ["title"]),
        ]

    async def handle(self, tool_name: str, tool_input: dict) -> str:
        try:
            if tool_name == "apple_reminders_lists":
                raw = _osa('tell application "Reminders" to return name of every list')
                lists = [x.strip() for x in raw.split(",") if x.strip()]
                return json.dumps({"lists": lists})

            elif tool_name == "apple_reminders_list":
                list_name = _esc(tool_input.get("list_name", "Reminders"))
                show_completed = tool_input.get("show_completed", False)
                filter_clause = "" if show_completed else "whose completed is false"
                raw = _osa(f'''
tell application "Reminders"
    set output to ""
    set rems to every reminder of list "{list_name}" {filter_clause}
    repeat with r in rems
        set dd to ""
        try
            set dd to due date of r as string
        end try
        set output to output & name of r & "|||" & completed of r & "|||" & dd & linefeed
    end repeat
    return output
end tell''')
                reminders = []
                for line in raw.split("\n"):
                    if not line.strip():
                        continue
                    parts = line.split("|||")
                    reminders.append({"title": parts[0].strip(), "completed": parts[1].strip() == "true" if len(parts) > 1 else False, "due": parts[2].strip() if len(parts) > 2 else ""})
                return json.dumps({"reminders": reminders, "count": len(reminders)})

            elif tool_name == "apple_reminders_create":
                title = _esc(tool_input["title"])
                list_name = _esc(tool_input.get("list_name", "Reminders"))
                notes = _esc(tool_input.get("notes", ""))
                due = tool_input.get("due_date", "")
                due_clause = f', due date:date "{_esc(due)}"' if due else ""
                notes_clause = f', body:"{notes}"' if notes else ""
                _osa(f'''
tell application "Reminders"
    tell list "{list_name}"
        make new reminder with properties {{name:"{title}"{notes_clause}{due_clause}}}
    end tell
end tell''')
                return json.dumps({"ok": True, "title": tool_input["title"], "list": list_name})

            elif tool_name == "apple_reminders_complete":
                title = _esc(tool_input["title"])
                list_name = _esc(tool_input.get("list_name", "Reminders"))
                _osa(f'''
tell application "Reminders"
    set rems to every reminder of list "{list_name}" whose name is "{title}" and completed is false
    if (count of rems) > 0 then
        set completed of item 1 of rems to true
    end if
end tell''')
                return json.dumps({"ok": True, "completed": tool_input["title"]})

        except Exception as e:
            return json.dumps({"error": str(e)})
        return json.dumps({"error": f"Unknown tool: {tool_name}"})
