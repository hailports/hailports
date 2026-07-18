"""Apple Notes — read, search, create notes via AppleScript."""

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


class AppleNotesTool(BaseTool):
    name = "apple_notes"
    description = "Read, search, and create Apple Notes"

    def get_definitions(self):
        return [
            make_tool_def("apple_notes_search", "Search notes by text content", {"query": {"type": "string"}}, ["query"]),
            make_tool_def("apple_notes_list", "List recent notes", {"folder": {"type": "string", "description": "Folder name (default: Notes)"}, "count": {"type": "integer"}}, []),
            make_tool_def("apple_notes_read", "Read full content of a note by name", {"name": {"type": "string"}}, ["name"]),
            make_tool_def("apple_notes_create", "Create a new note", {"title": {"type": "string"}, "body": {"type": "string"}, "folder": {"type": "string"}}, ["title", "body"]),
        ]

    async def handle(self, tool_name: str, tool_input: dict) -> str:
        try:
            if tool_name == "apple_notes_search":
                query = _esc(tool_input["query"])
                raw = _osa(f'''
tell application "Notes"
    set output to ""
    set matchedNotes to every note whose name contains "{query}" or plaintext contains "{query}"
    repeat with n in (items 1 thru (min of 10 and (count of matchedNotes)) of matchedNotes)
        set output to output & name of n & "|||" & modification date of n as string & linefeed
    end repeat
    return output
end tell''')
                notes = [{"name": p.split("|||")[0], "modified": p.split("|||")[1] if "|||" in p else ""} for p in raw.split("\n") if p.strip()]
                return json.dumps({"notes": notes, "count": len(notes)})

            elif tool_name == "apple_notes_list":
                folder = _esc(tool_input.get("folder", "Notes"))
                count = tool_input.get("count", 20)
                raw = _osa(f'''
tell application "Notes"
    set output to ""
    set noteList to every note of folder "{folder}"
    repeat with n in (items 1 thru (min of {count} and (count of noteList)) of noteList)
        set output to output & name of n & "|||" & modification date of n as string & linefeed
    end repeat
    return output
end tell''')
                notes = [{"name": p.split("|||")[0], "modified": p.split("|||")[1] if "|||" in p else ""} for p in raw.split("\n") if p.strip()]
                return json.dumps({"notes": notes, "count": len(notes)})

            elif tool_name == "apple_notes_read":
                name = _esc(tool_input["name"])
                raw = _osa(f'''
tell application "Notes"
    set matchedNotes to every note whose name is "{name}"
    if (count of matchedNotes) > 0 then
        return plaintext of item 1 of matchedNotes
    else
        return "NOT_FOUND"
    end if
end tell''')
                if raw == "NOT_FOUND":
                    return json.dumps({"error": f"Note '{tool_input['name']}' not found"})
                return json.dumps({"name": tool_input["name"], "content": raw})

            elif tool_name == "apple_notes_create":
                title = _esc(tool_input["title"])
                body = _esc(tool_input["body"])
                folder = _esc(tool_input.get("folder", "Notes"))
                _osa(f'''
tell application "Notes"
    tell folder "{folder}"
        make new note with properties {{name:"{title}", body:"{body}"}}
    end tell
end tell''')
                return json.dumps({"ok": True, "title": tool_input["title"], "folder": folder})

        except Exception as e:
            return json.dumps({"error": str(e)})
        return json.dumps({"error": f"Unknown tool: {tool_name}"})
