"""Apple Contacts — search and read contacts via AppleScript."""

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


class AppleContactsTool(BaseTool):
    name = "apple_contacts"
    description = "Search and read Apple Contacts"

    def get_definitions(self):
        return [
            make_tool_def("apple_contacts_search", "Search contacts by name, email, or phone", {"query": {"type": "string"}}, ["query"]),
            make_tool_def("apple_contacts_get", "Get full details of a contact by name", {"name": {"type": "string"}}, ["name"]),
        ]

    async def handle(self, tool_name: str, tool_input: dict) -> str:
        try:
            if tool_name == "apple_contacts_search":
                query = _esc(tool_input["query"])
                raw = _osa(f'''
tell application "Contacts"
    set output to ""
    set matches to every person whose name contains "{query}"
    repeat with p in (items 1 thru (min of 15 and (count of matches)) of matches)
        set em to ""
        try
            set em to value of first email of p
        end try
        set ph to ""
        try
            set ph to value of first phone of p
        end try
        set output to output & name of p & "|||" & em & "|||" & ph & linefeed
    end repeat
    return output
end tell''')
                contacts = []
                for line in raw.split("\n"):
                    if not line.strip():
                        continue
                    parts = line.split("|||")
                    contacts.append({"name": parts[0].strip(), "email": parts[1].strip() if len(parts) > 1 else "", "phone": parts[2].strip() if len(parts) > 2 else ""})
                return json.dumps({"contacts": contacts, "count": len(contacts)})

            elif tool_name == "apple_contacts_get":
                name = _esc(tool_input["name"])
                raw = _osa(f'''
tell application "Contacts"
    set matches to every person whose name is "{name}"
    if (count of matches) = 0 then return "NOT_FOUND"
    set p to item 1 of matches
    set output to name of p & linefeed
    try
        set output to output & "Title: " & job title of p & linefeed
    end try
    try
        set output to output & "Company: " & organization of p & linefeed
    end try
    repeat with e in emails of p
        set output to output & "Email: " & value of e & " (" & label of e & ")" & linefeed
    end repeat
    repeat with ph in phones of p
        set output to output & "Phone: " & value of ph & " (" & label of ph & ")" & linefeed
    end repeat
    repeat with a in addresses of p
        set output to output & "Address: " & formatted address of a & linefeed
    end repeat
    try
        set output to output & "Birthday: " & birth date of p as string & linefeed
    end try
    try
        set output to output & "Notes: " & note of p & linefeed
    end try
    return output
end tell''')
                if raw == "NOT_FOUND":
                    return json.dumps({"error": f"Contact '{tool_input['name']}' not found"})
                return json.dumps({"name": tool_input["name"], "details": raw})

        except Exception as e:
            return json.dumps({"error": str(e)})
        return json.dumps({"error": f"Unknown tool: {tool_name}"})
