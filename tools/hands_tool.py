"""Local-first browser + desktop control surface for conversational agents."""

from __future__ import annotations

from tools.base import BaseTool, make_tool_def
from core import hands


class HandsTool(BaseTool):
    name = "hands"
    description = (
        "Local-first browser and desktop control using browser-use, macOS app activation, "
        "UI clicks, Shortcuts, and a confirmation queue for risky final actions."
    )

    def get_definitions(self) -> list[dict]:
        return [
            make_tool_def(
                "hands_status",
                "Show local hands capability status, pending confirmation queue, and UI-guard state.",
                {
                    "limit": {
                        "type": "integer",
                        "description": "How many recent queued actions to include.",
                    },
                },
                [],
            ),
            make_tool_def(
                "hands_list_pending_actions",
                "List pending queued actions that require explicit confirmation before execution.",
                {
                    "limit": {
                        "type": "integer",
                        "description": "How many pending actions to include.",
                    },
                },
                [],
            ),
            make_tool_def(
                "hands_find_in_onedrive",
                "Search local synced OneDrive roots for a file or folder. Can optionally reveal or open the best match in Finder.",
                {
                    "query": {"type": "string", "description": "Name or partial name to find, e.g. 'SP portal project'."},
                    "account_hint": {
                        "type": "string",
                        "description": "Optional tenant/account hint such as 'CompanyA' to prioritize the right OneDrive root.",
                    },
                    "limit": {"type": "integer", "description": "Maximum number of matches to return."},
                    "reveal_best_match": {
                        "type": "boolean",
                        "description": "Reveal the top match in Finder after search completes.",
                    },
                    "open_best_match": {
                        "type": "boolean",
                        "description": "Open the top match after search completes.",
                    },
                },
                ["query"],
            ),
            make_tool_def(
                "hands_search_path",
                "Search local file paths using macOS Spotlight with filesystem fallback.",
                {
                    "query": {"type": "string", "description": "Filename or project name to search for."},
                    "roots": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional absolute root paths to constrain the search.",
                    },
                    "account_hint": {
                        "type": "string",
                        "description": "Optional OneDrive account hint used when roots are omitted.",
                    },
                    "limit": {"type": "integer", "description": "Maximum number of matches to return."},
                    "include_files": {"type": "boolean", "description": "Include files in results."},
                    "include_dirs": {"type": "boolean", "description": "Include folders in results."},
                },
                ["query"],
            ),
            make_tool_def(
                "hands_list_folder",
                "List the contents of a local folder with folders first and basic metadata.",
                {
                    "path": {"type": "string", "description": "Absolute or home-relative folder path."},
                    "limit": {"type": "integer", "description": "Maximum number of children to include."},
                    "include_hidden": {"type": "boolean", "description": "Include hidden dotfiles and dotfolders."},
                },
                ["path"],
            ),
            make_tool_def(
                "hands_reveal_in_finder",
                "Reveal a local file or folder in Finder using the native macOS reveal action.",
                {
                    "path": {"type": "string", "description": "Absolute or home-relative path to reveal."},
                },
                ["path"],
            ),
            make_tool_def(
                "hands_open_path",
                "Open a local file or folder using the default macOS app or Finder.",
                {
                    "path": {"type": "string", "description": "Absolute or home-relative path to open."},
                },
                ["path"],
            ),
            make_tool_def(
                "hands_confirm_action",
                "Execute a queued action after the user explicitly approves it.",
                {
                    "action_id": {
                        "type": "string",
                        "description": "Queued action ID from hands_list_pending_actions.",
                    },
                    "explicit_approval": {
                        "type": "boolean",
                        "description": "Must be true when the user has explicitly approved execution.",
                    },
                },
                ["action_id"],
            ),
            make_tool_def(
                "hands_upwork_search",
                "Queue safe Upwork opportunity review work. This never applies, submits, messages, or clicks final actions.",
                {
                    "query": {"type": "string", "description": "Opportunity search phrase."},
                    "limit": {"type": "integer", "description": "Maximum review tasks to queue."},
                    "source": {"type": "string", "description": "Optional source label for the queued review work."},
                },
                [],
            ),
            make_tool_def(
                "hands_reddit_engage",
                "Queue safe Reddit engagement review work. This never posts, votes, follows, or sends messages.",
                {
                    "topic": {"type": "string", "description": "Topic or niche to find/draft engagement for."},
                    "limit": {"type": "integer", "description": "Maximum review tasks to queue."},
                },
                [],
            ),
            make_tool_def(
                "hands_monitor_fiverr",
                "Queue safe Fiverr listing and inbox monitor reviews. This never logs in, messages, publishes, or edits gigs.",
                {
                    "limit": {"type": "integer", "description": "Maximum review tasks to queue."},
                },
                [],
            ),
            make_tool_def(
                "hands_browser_open",
                "Open a URL in a browser-use session using local browser control.",
                {
                    "url": {"type": "string", "description": "URL to open."},
                    "session": {
                        "type": "string",
                        "description": "Browser session name. Reuse it across follow-up actions.",
                    },
                },
                ["url"],
            ),
            make_tool_def(
                "hands_browser_state",
                "Inspect the current browser page and return indexed clickable elements for follow-up actions.",
                {
                    "session": {
                        "type": "string",
                        "description": "Browser session name.",
                    },
                },
                [],
            ),
            make_tool_def(
                "hands_browser_click",
                "Click a browser element by index or coordinates. Risky final-action buttons are queued for confirmation unless explicit approval is set.",
                {
                    "session": {
                        "type": "string",
                        "description": "Browser session name.",
                    },
                    "index": {
                        "type": "integer",
                        "description": "Indexed element from hands_browser_state.",
                    },
                    "x": {
                        "type": "integer",
                        "description": "Screen x coordinate for coordinate-based click.",
                    },
                    "y": {
                        "type": "integer",
                        "description": "Screen y coordinate for coordinate-based click.",
                    },
                    "explicit_approval": {
                        "type": "boolean",
                        "description": "Set true only after the user explicitly approves a risky final action.",
                    },
                },
                [],
            ),
            make_tool_def(
                "hands_browser_input",
                "Type text into a browser field by element index.",
                {
                    "session": {"type": "string", "description": "Browser session name."},
                    "index": {"type": "integer", "description": "Indexed element from hands_browser_state."},
                    "text": {"type": "string", "description": "Text to type into the field."},
                },
                ["index", "text"],
            ),
            make_tool_def(
                "hands_browser_keys",
                "Send keyboard keys to the active browser session. Enter-like finalizing keys may queue for confirmation.",
                {
                    "session": {"type": "string", "description": "Browser session name."},
                    "keys": {"type": "string", "description": 'Keys string, e.g. "Tab", "Escape", "Enter".'},
                    "explicit_approval": {
                        "type": "boolean",
                        "description": "Set true only after the user explicitly approves a risky final action.",
                    },
                },
                ["keys"],
            ),
            make_tool_def(
                "hands_browser_wait_text",
                "Wait for text to appear in the current browser session.",
                {
                    "session": {"type": "string", "description": "Browser session name."},
                    "text": {"type": "string", "description": "Text that should appear."},
                    "timeout_s": {"type": "integer", "description": "Timeout in seconds."},
                },
                ["text"],
            ),
            make_tool_def(
                "hands_browser_screenshot",
                "Capture a screenshot from the current browser session and save it locally.",
                {
                    "session": {"type": "string", "description": "Browser session name."},
                    "full": {"type": "boolean", "description": "Capture full page screenshot."},
                    "path": {"type": "string", "description": "Optional output path."},
                },
                [],
            ),
            make_tool_def(
                "hands_browser_extract",
                "Extract targeted information from the current browser page using browser-use.",
                {
                    "session": {"type": "string", "description": "Browser session name."},
                    "query": {"type": "string", "description": "What to extract from the current page."},
                },
                ["query"],
            ),
            make_tool_def(
                "hands_activate_app",
                "Bring a macOS application to the foreground using the local system.",
                {
                    "app_name": {"type": "string", "description": "Application name, e.g. Safari or Messages."},
                },
                ["app_name"],
            ),
            make_tool_def(
                "hands_click_ui",
                "Click a named button in the frontmost window of a macOS app. Risky final-action labels queue for confirmation unless explicit approval is set.",
                {
                    "app_name": {"type": "string", "description": "Application name."},
                    "button_label": {"type": "string", "description": "Button text to click."},
                    "explicit_approval": {
                        "type": "boolean",
                        "description": "Set true only after the user explicitly approves a risky final action.",
                    },
                },
                ["app_name", "button_label"],
            ),
            make_tool_def(
                "hands_dismiss_dialog",
                "Dismiss the frontmost modal/dialog in the current app or a specified app.",
                {
                    "app_name": {
                        "type": "string",
                        "description": "Optional application name. If omitted, uses the frontmost app.",
                    },
                },
                [],
            ),
            make_tool_def(
                "hands_list_shortcuts",
                "List available macOS Shortcuts on this machine.",
                {},
                [],
            ),
            make_tool_def(
                "hands_run_shortcut",
                "Run a local macOS Shortcut by name using system resources only.",
                {
                    "shortcut_name": {"type": "string", "description": "Shortcut name or identifier."},
                    "input_path": {"type": "string", "description": "Optional input file path."},
                    "output_path": {"type": "string", "description": "Optional output file path."},
                    "output_type": {"type": "string", "description": "Optional UTI output type."},
                },
                ["shortcut_name"],
            ),
        ]

    async def handle(self, tool_name: str, tool_input: dict) -> str:
        payload = dict(tool_input or {})
        if tool_name == "hands_status":
            return str(hands.status(limit=int(payload.get("limit") or 10)))
        if tool_name == "hands_list_pending_actions":
            return str(hands.list_pending_actions(limit=int(payload.get("limit") or 25)))
        if tool_name == "hands_find_in_onedrive":
            return str(
                hands.find_in_onedrive(
                    str(payload.get("query") or ""),
                    account_hint=str(payload.get("account_hint") or ""),
                    limit=int(payload.get("limit") or 25),
                    reveal_best_match=bool(payload.get("reveal_best_match")),
                    open_best_match=bool(payload.get("open_best_match")),
                )
            )
        if tool_name == "hands_search_path":
            return str(
                hands.search_path(
                    str(payload.get("query") or ""),
                    roots=[str(item) for item in (payload.get("roots") or [])],
                    account_hint=str(payload.get("account_hint") or ""),
                    limit=int(payload.get("limit") or 25),
                    include_files=bool(payload.get("include_files", True)),
                    include_dirs=bool(payload.get("include_dirs", True)),
                )
            )
        if tool_name == "hands_list_folder":
            return str(
                hands.list_folder(
                    str(payload.get("path") or ""),
                    limit=int(payload.get("limit") or 200),
                    include_hidden=bool(payload.get("include_hidden")),
                )
            )
        if tool_name == "hands_reveal_in_finder":
            return str(hands.reveal_in_finder(str(payload.get("path") or "")))
        if tool_name == "hands_open_path":
            return str(hands.open_path(str(payload.get("path") or "")))
        if tool_name == "hands_confirm_action":
            return str(
                hands.confirm_action(
                    str(payload.get("action_id") or ""),
                    explicit_approval=bool(payload.get("explicit_approval")),
                )
            )
        if tool_name == "hands_upwork_search":
            return str(
                hands.upwork_search(
                    query=str(payload.get("query") or "Salesforce admin help"),
                    limit=int(payload.get("limit") or 10),
                    source=str(payload.get("source") or "hands_tool"),
                )
            )
        if tool_name == "hands_reddit_engage":
            return str(
                hands.reddit_engage(
                    topic=str(payload.get("topic") or "Salesforce"),
                    limit=int(payload.get("limit") or 5),
                )
            )
        if tool_name == "hands_monitor_fiverr":
            return str(hands.monitor_fiverr(limit=int(payload.get("limit") or 10)))
        if tool_name == "hands_browser_open":
            return str(hands.browser_open(str(payload.get("url") or ""), session=str(payload.get("session") or "default")))
        if tool_name == "hands_browser_state":
            return str(hands.browser_state(session=str(payload.get("session") or "default")))
        if tool_name == "hands_browser_click":
            return str(
                hands.browser_click(
                    session=str(payload.get("session") or "default"),
                    index=payload.get("index"),
                    x=payload.get("x"),
                    y=payload.get("y"),
                    explicit_approval=bool(payload.get("explicit_approval")),
                )
            )
        if tool_name == "hands_browser_input":
            return str(
                hands.browser_input(
                    session=str(payload.get("session") or "default"),
                    index=int(payload.get("index")),
                    text=str(payload.get("text") or ""),
                )
            )
        if tool_name == "hands_browser_keys":
            return str(
                hands.browser_keys(
                    session=str(payload.get("session") or "default"),
                    keys=str(payload.get("keys") or ""),
                    explicit_approval=bool(payload.get("explicit_approval")),
                )
            )
        if tool_name == "hands_browser_wait_text":
            return str(
                hands.browser_wait_for_text(
                    session=str(payload.get("session") or "default"),
                    text=str(payload.get("text") or ""),
                    timeout_s=int(payload.get("timeout_s") or 30),
                )
            )
        if tool_name == "hands_browser_screenshot":
            return str(
                hands.browser_screenshot(
                    session=str(payload.get("session") or "default"),
                    full=bool(payload.get("full")),
                    path=str(payload.get("path") or ""),
                )
            )
        if tool_name == "hands_browser_extract":
            return str(
                hands.browser_extract(
                    session=str(payload.get("session") or "default"),
                    query=str(payload.get("query") or ""),
                )
            )
        if tool_name == "hands_activate_app":
            return str(hands.activate_app(str(payload.get("app_name") or "")))
        if tool_name == "hands_click_ui":
            return str(
                hands.click_ui(
                    app_name=str(payload.get("app_name") or ""),
                    button_label=str(payload.get("button_label") or ""),
                    explicit_approval=bool(payload.get("explicit_approval")),
                )
            )
        if tool_name == "hands_dismiss_dialog":
            return str(hands.dismiss_dialog(str(payload.get("app_name") or "")))
        if tool_name == "hands_list_shortcuts":
            return str(hands.list_shortcuts())
        if tool_name == "hands_run_shortcut":
            return str(
                hands.run_shortcut(
                    shortcut_name=str(payload.get("shortcut_name") or ""),
                    input_path=str(payload.get("input_path") or ""),
                    output_path=str(payload.get("output_path") or ""),
                    output_type=str(payload.get("output_type") or ""),
                )
            )
        return f"Unknown hands tool: {tool_name}"
