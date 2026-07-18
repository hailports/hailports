"""Apple Mail via AppleScript — runs locally on Mac Mini."""

import asyncio
import subprocess
from tools.base import BaseTool, make_tool_def


def _osascript(script: str) -> str:
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"AppleScript error: {result.stderr.strip()}")
    return result.stdout.strip()


def _mail_running() -> bool:
    # `is running` never launches the app, so this can't foreground a closed Mail.
    try:
        r = subprocess.run(["osascript", "-e", 'application "Mail" is running'],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() == "true"
    except Exception:
        return False


class AppleMailTool(BaseTool):
    name = "apple_mail"
    description = "Apple Mail — read, send, search via AppleScript"

    def get_definitions(self) -> list:
        return [
            make_tool_def("apple_mail_get_inbox", "Get recent inbox messages from Apple Mail.",
                          {"count": {"type": "integer", "description": "Number of messages (default 10)"}},
                          []),
            make_tool_def("apple_mail_read_message", "Read a specific message by index.",
                          {"index": {"type": "integer", "description": "1-based message index in inbox"}},
                          ["index"]),
            make_tool_def("apple_mail_send", "Send an email via Apple Mail.",
                          {"to": {"type": "string"}, "subject": {"type": "string"}, "body": {"type": "string"}},
                          ["to", "subject", "body"]),
            make_tool_def("apple_mail_search", "Search Apple Mail.",
                          {"query": {"type": "string"}},
                          ["query"]),
            make_tool_def("apple_mail_check", "Check for new mail.", {}, []),
        ]

    async def handle(self, tool_name: str, tool_input: dict) -> str:
        loop = asyncio.get_event_loop()

        # Reads/checks must never launch Mail (that foregrounds a closed app and steals focus).
        # Only apple_mail_send is allowed to start it, since sending genuinely requires the app.
        if tool_name != "apple_mail_send" and not _mail_running():
            return "Apple Mail is not running; skipped to avoid launching it. Use the Outlook tools, or open Mail first."

        if tool_name == "apple_mail_get_inbox":
            count = tool_input.get("count", 10)
            script = f'''
                tell application "Mail"
                    set msgs to messages 1 thru {count} of inbox
                    set output to ""
                    repeat with m in msgs
                        set output to output & (read status of m as string) & " | " & (sender of m) & " | " & (subject of m) & " | " & (date received of m as string) & linefeed
                    end repeat
                    return output
                end tell'''
            return await loop.run_in_executor(None, _osascript, script)

        elif tool_name == "apple_mail_read_message":
            idx = tool_input["index"]
            script = f'''
                tell application "Mail"
                    set m to message {idx} of inbox
                    return "From: " & (sender of m) & linefeed & "Subject: " & (subject of m) & linefeed & "Date: " & (date received of m as string) & linefeed & linefeed & (content of m)
                end tell'''
            result = await loop.run_in_executor(None, _osascript, script)
            return result[:3000]

        elif tool_name == "apple_mail_send":
            to_addr = tool_input["to"].replace('"', '\\"')
            subject = tool_input["subject"].replace('"', '\\"')
            body = tool_input["body"].replace('"', '\\"')
            script = f'''
                tell application "Mail"
                    set newMsg to make new outgoing message with properties {{subject:"{subject}", content:"{body}", visible:false}}
                    tell newMsg
                        make new to recipient at end of to recipients with properties {{address:"{to_addr}"}}
                    end tell
                    send newMsg
                end tell
                return "sent"'''
            await loop.run_in_executor(None, _osascript, script)
            return f"Email sent to {tool_input['to']}"

        elif tool_name == "apple_mail_search":
            query = tool_input["query"].replace('"', '\\"')
            script = f'''
                tell application "Mail"
                    set found to (messages of inbox whose subject contains "{query}" or sender contains "{query}")
                    set output to ""
                    set maxCount to 10
                    if (count of found) < maxCount then set maxCount to (count of found)
                    repeat with i from 1 to maxCount
                        set m to item i of found
                        set output to output & (read status of m as string) & " | " & (sender of m) & " | " & (subject of m) & " | " & (date received of m as string) & linefeed
                    end repeat
                    return output
                end tell'''
            return await loop.run_in_executor(None, _osascript, script)

        elif tool_name == "apple_mail_check":
            await loop.run_in_executor(None, _osascript, 'tell application "Mail" to check for new mail')
            return "Mail check triggered."

        else:
            return f"Unknown apple_mail tool: {tool_name}"
