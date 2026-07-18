"""Response verifier — catches hallucinated tool use and unverified claims.

Runs AFTER the engine generates a response but BEFORE sending to user.
Uses local LOCAL_MODEL (free) to check if the response matches the tool results.

Catches:
- "I created a draft" but outlook_create_draft was never called
- "You have 0 tickets" when the tool returned data
- Claims about actions taken that don't match tool call logs
- Confident answers about data that was never queried
"""

from core.constants import LOCAL_MODEL
import logging
import re
from core import local_client
from core.constants import WRITE_TOOLS

log = logging.getLogger(__name__)

# Phrases that claim an action was taken
ACTION_CLAIMS = re.compile(
    r"(draft\s+(?:is\s+)?ready|draft.*(created|saved|updated|corrected|fixed)|"
    r"(created|saved|updated|corrected|fixed).{0,80}\bdraft\b|email.*(sent|delivered)|"
    r"event.*(created|scheduled)|record.*(created|updated)|"
    r"meeting.*(scheduled|joined)|message.*(sent|delivered)|"
    r"moved to|marked as|flagged|deleted|accepted|declined)",
    re.IGNORECASE,
)

DRAFT_TOOLS = {
    "outlook_create_draft",
    "outlook_reply_draft",
    "outlook_forward_draft",
    "outlook_edit_draft",
    "nicole_create_draft",
}
EMAIL_SEND_TOOLS = {
    "outlook_send_email",
    "outlook_reply_to_email",
    "outlook_forward_email",
    "apple_mail_send",
}
EVENT_CREATE_TOOLS = {
    "outlook_create_event",
    "outlook_create_calendar_event",
    "calendar_create_event",
    "zoom_schedule_meeting",
}
SF_WRITE_TOOLS = {
    "salesforce_create_record",
    "salesforce_update_record",
    "salesforce_delete_record",
}
MESSAGE_SEND_TOOLS = {
    "zoom_send_meeting_chat",
    "zoom_send_direct_message",
    "zoom_reply_to_message",
    "slack_send_message",
    "salesforce_create_chatter",
}
TOOL_NAME_RE = re.compile(
    r"\b("
    r"salesforce_[a-z0-9_]+|sf_[a-z0-9_]+|outlook_[a-z0-9_]+|"
    r"zoom_[a-z0-9_]+|slack_[a-z0-9_]+|"
    r"web_[a-z0-9_]+|hands_[a-z0-9_]+|monday_[a-z0-9_]+|"
    r"gdrive_[a-z0-9_]+|sp_[a-z0-9_]+|calendar_[a-z0-9_]+|"
    r"apple_mail_[a-z0-9_]+|local_llm_[a-z0-9_]+"
    r")\b",
    re.IGNORECASE,
)
TOOL_EXECUTION_CLAIM_RE = re.compile(
    r"(\b(called|used|executed|ran|queried|checked|looked up)\b.{0,80}"
    r"\b([a-z]+_[a-z0-9_]+)\b|"
    r"\b([a-z]+_[a-z0-9_]+)\b.{0,80}\b(returned|confirmed|found|showed)\b)",
    re.IGNORECASE,
)
UNVERIFIED_EVIDENCE_CLAIM_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(?:"
    r"(?:confirmed|verified|found|checked|looked up)\s*:|"
    r"no issues detected\b|"
    r".*\b(?:is active|showing live data|live data|current status|dashboard layout)\b"
    r")",
    re.IGNORECASE,
)
ACTION_SECTION_RE = re.compile(r"^\s*(?:\*\*)?action taken(?:\*\*)?\s*:?\s*$", re.IGNORECASE)
MORALIZING_REFUSAL_RE = re.compile(
    r"^\s*I\s+(?:cannot|can(?:no|')t|will not|won(?:'|’)t)\s+"
    r".{0,240}?"
    r"(?:abusive language|false ownership claims|own(?:ership)?|morality|ethical|boundaries)"
    r".*?(?:\n\s*\n|$)",
    re.IGNORECASE | re.DOTALL,
)
TOOL_MARKUP_RE = re.compile(
    r"(\[tool_call\].*?\[/tool_call\]|"
    r"<tool_use\b[^>]*>.*?</tool_use>|"
    r"<function_calls\b[^>]*>.*?</function_calls>|"
    r"<invoke\b[^>]*>.*?</invoke>|"
    r"<think\b[^>]*>.*?</think>)",
    re.IGNORECASE | re.DOTALL,
)


def _webui_chat_source(source: str) -> bool:
    token = str(source or "").strip().lower()
    return token == "webui" or token.startswith("webui:chat")


def _strip_leaked_tool_markup(response_text: str) -> str:
    """Remove model-written tool syntax that did not execute."""
    text = str(response_text or "")
    if not text:
        return text

    text = TOOL_MARKUP_RE.sub("", text)
    text = re.sub(r"\[/?tool_use\]", "", text, flags=re.IGNORECASE)
    text = re.sub(r"</?think\b[^>]*>", "", text, flags=re.IGNORECASE)

    cleaned_lines = []
    dropping_fence = False
    fence_buffer = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            if dropping_fence:
                block = "\n".join(fence_buffer)
                if not _looks_like_tool_markup(block):
                    cleaned_lines.extend(fence_buffer)
                fence_buffer = []
                dropping_fence = False
            else:
                dropping_fence = True
                fence_buffer = [line]
            continue
        if dropping_fence:
            fence_buffer.append(line)
            continue
        if _looks_like_tool_markup(stripped):
            continue
        cleaned_lines.append(line)

    if fence_buffer:
        block = "\n".join(fence_buffer)
        if not _looks_like_tool_markup(block):
            cleaned_lines.extend(fence_buffer)

    text = "\n".join(cleaned_lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _strip_moralizing_preamble(response_text: str) -> str:
    """Remove scolding/refusal boilerplate while preserving the useful answer."""
    text = str(response_text or "").strip()
    if not text:
        return text
    cleaned = MORALIZING_REFUSAL_RE.sub("", text, count=1).strip()
    if cleaned != text:
        cleaned = re.sub(r"^\s*(?:Regarding|As for)\s+your\s+[^:\n]{1,80}:\s*", "", cleaned, flags=re.IGNORECASE)
    return cleaned or text


def _extract_claimed_tools(response_text: str) -> set[str]:
    return {m.group(1).lower() for m in TOOL_NAME_RE.finditer(response_text or "")}


def _unsupported_tool_claims(response_text: str, tool_calls_made) -> list[str]:
    if not response_text:
        return []
    actual = {str(name or "").strip().lower() for name in (tool_calls_made or [])}
    claimed = _extract_claimed_tools(response_text)
    unsupported = sorted(name for name in claimed if name not in actual)
    if not unsupported:
        return []
    if TOOL_EXECUTION_CLAIM_RE.search(response_text) or ACTION_SECTION_RE.search(response_text):
        return [f"Claimed tool execution without tool call: {', '.join(unsupported)}"]
    return []


def _strip_unverified_tool_claim_text(response_text: str, tool_calls_made) -> str:
    """Remove fake action/evidence sections for tools that did not execute."""
    actual = {str(name or "").strip().lower() for name in (tool_calls_made or [])}
    lines = str(response_text or "").splitlines()
    cleaned: list[str] = []
    dropping_action_block = False

    for line in lines:
        stripped = line.strip()
        if ACTION_SECTION_RE.match(stripped):
            dropping_action_block = True
            continue

        names = _extract_claimed_tools(line)
        unsupported_names = names - actual
        claims_tool = bool(unsupported_names and TOOL_EXECUTION_CLAIM_RE.search(line))
        claims_evidence = not actual and bool(UNVERIFIED_EVIDENCE_CLAIM_RE.match(line))

        if dropping_action_block:
            if not stripped:
                dropping_action_block = False
                continue
            if claims_tool or claims_evidence or stripped.startswith(("-", "*")):
                continue
            dropping_action_block = False

        if claims_tool or claims_evidence:
            continue
        cleaned.append(line)

    text = "\n".join(cleaned)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _looks_like_tool_markup(text: str) -> bool:
    if not text:
        return False
    token = text.lower()
    if re.match(r"^(tool_use|tool_call|function_call)\s*:", token):
        return True
    if token.startswith("[tool response:"):
        return True
    if ("tool_name" in token or '"name"' in token) and (
        "arguments" in token
        or "tool_input" in token
        or "outlook_" in token
        or "salesforce_" in token
        or "zoom_" in token
        or "slack_" in token
    ):
        return True
    return False


def _remove_unsupported_claim_sentences(response_text: str) -> str:
    lines = response_text.splitlines()
    if len(lines) > 1:
        kept_lines = [line for line in lines if not ACTION_CLAIMS.search(line)]
        if len(kept_lines) != len(lines):
            return "\n".join(kept_lines).strip()

    pieces = re.split(r"(?<=[.!?])(\s+)", response_text)
    kept = []
    for idx in range(0, len(pieces), 2):
        sentence = pieces[idx]
        spacer = pieces[idx + 1] if idx + 1 < len(pieces) else ""
        if ACTION_CLAIMS.search(sentence):
            continue
        kept.append(sentence + spacer)
    return "".join(kept).strip()


def _webui_correction(issues: list[str], response_text: str) -> str:
    details = []
    if any("tool execution" in issue for issue in issues):
        details.append("I don't have live source data from that request.")
    if any("draft" in issue for issue in issues):
        details.append("No draft was created or saved from WebUI chat.")
    if any("email" in issue or "message" in issue or "sent" in issue for issue in issues):
        details.append("No email, chat message, or post was sent from WebUI chat.")
    if any("event" in issue or "meeting" in issue for issue in issues):
        details.append("No event or meeting was created from WebUI chat.")
    if any("record" in issue for issue in issues):
        details.append("No Salesforce record was changed from WebUI chat.")
    if not details:
        details.append("No write action was completed from WebUI chat.")

    body = _strip_unverified_tool_claim_text(response_text, [])
    body = _remove_unsupported_claim_sentences(body)
    prefix = " ".join(dict.fromkeys(details))
    if any("tool execution" in issue for issue in issues):
        return (
            f"{prefix} I need to run the actual status check before I can report "
            "dashboard names, Salesforce details, IDs, or health conclusions."
        )
    if body:
        return f"{prefix}\n\n{body}"
    return f"{prefix} I can prepare the copy or queue the action for explicit confirmation."


def sanitize_response_text(response_text: str, tool_calls_made=None, *, source: str = "") -> str:
    """Sanitize display text without changing legacy verifier validity behavior."""
    cleaned = _strip_leaked_tool_markup(response_text)
    cleaned = _strip_moralizing_preamble(cleaned)
    if not _webui_chat_source(source):
        return cleaned
    _, corrected = verify_response(cleaned, tool_calls_made or [], source=source)
    return corrected


def sanitize_stored_response_text(response_text: str, *, source: str = "") -> str:
    """Sanitize historical assistant text before WebUI display.

    Stored messages do not carry the request's tool-call ledger, so this stays
    narrower than live verification. It removes leaked internal markup and
    rewrites the known bad class of fake action/live-data claims instead of
    showing them forever in chat history.
    """
    cleaned = _strip_moralizing_preamble(_strip_leaked_tool_markup(response_text))
    if not _webui_chat_source(source):
        return cleaned

    tool_issues = _unsupported_tool_claims(cleaned, [])
    if tool_issues and (
        ACTION_SECTION_RE.search(cleaned)
        or "salesforce_soql_query" in cleaned.lower()
        or "live data" in cleaned.lower()
    ):
        return _webui_correction(tool_issues, cleaned)

    return cleaned


def verify_response(response_text, tool_calls_made, *, source: str = ""):
    """Check if the response claims actions that weren't actually performed.

    Args:
        response_text: The final text response about to be sent to user
        tool_calls_made: List of tool names that were actually called during this request

    Returns:
        (is_valid, corrected_text) — if invalid, corrected_text has a warning appended
    """
    response_text = _strip_moralizing_preamble(_strip_leaked_tool_markup(response_text))
    if not response_text or not ACTION_CLAIMS.search(response_text):
        tool_issues = _unsupported_tool_claims(response_text, tool_calls_made)
        if tool_issues:
            log.warning(f"Verifier caught hallucinated tool claims: {tool_issues}")
            if _webui_chat_source(source):
                return False, _webui_correction(tool_issues, response_text)
            return False, response_text + "\n\nWarning: I may have claimed to use a tool that was not actually called. Please verify and retry if needed."
        return True, response_text

    # Check if claimed actions match actual tool calls
    tools_set = set(tool_calls_made)
    issues = []

    # "draft created/updated/corrected/fixed" but outlook_create_draft not called
    if re.search(r"(draft\s+(?:is\s+)?ready|draft.*(created|saved|updated|corrected|fixed)|(created|saved|updated|corrected|fixed).{0,80}\bdraft\b)", response_text, re.I):
        if not tools_set.intersection(DRAFT_TOOLS):
            issues.append("Claimed draft was created/updated but no draft tool was called")

    # "email sent" but outlook_send_email not called
    if re.search(r"email.*(sent|delivered)", response_text, re.I):
        if not tools_set.intersection(EMAIL_SEND_TOOLS):
            issues.append("Claimed email was sent but no send tool was called")

    # "event created/scheduled" but no create tool called
    if re.search(r"(event|meeting).*(created|scheduled)", response_text, re.I):
        if not tools_set.intersection(EVENT_CREATE_TOOLS):
            issues.append("Claimed event was created but no create tool was called")

    # "record created/updated/deleted" but no SF tool called
    if re.search(r"record.*(created|updated|deleted)", response_text, re.I):
        if not tools_set.intersection(SF_WRITE_TOOLS):
            issues.append("Claimed record was modified but no Salesforce write tool was called")

    # "message sent" (zoom)
    if re.search(r"message.*(sent|delivered)", response_text, re.I):
        if not tools_set.intersection(MESSAGE_SEND_TOOLS):
            if not tools_set.intersection(EMAIL_SEND_TOOLS):
                issues.append("Claimed message was sent but no messaging tool was called")

    issues.extend(_unsupported_tool_claims(response_text, tool_calls_made))

    if issues:
        log.warning(f"Verifier caught hallucinated actions: {issues}")
        if _webui_chat_source(source):
            return False, _webui_correction(issues, response_text)

        warning = "\n\nWarning: I may have claimed to do something I didn't actually do. "
        for issue in issues:
            if "draft" in issue:
                warning += "The draft may NOT have been created — please check your Drafts folder. "
            elif "sent" in issue:
                warning += "The email/message may NOT have been sent. "
            elif "event" in issue or "meeting" in issue:
                warning += "The event may NOT have been created. "
            else:
                warning += f"{issue}. "
        warning += "Please verify and retry if needed."
        return False, response_text + warning

    return True, response_text

async def verify_with_local(response_text, tool_calls_made, tool_results):
    """Deeper verification using local LOCAL_MODEL — for complex responses.
    Only called when basic verification flags something suspicious."""
    if not tool_results:
        return True, response_text

    # Build a summary of what tools returned
    results_summary = "\n".join(
        f"Tool {name}: {str(result)[:200]}"
        for name, result in tool_results[:5]
    )

    prompt = f"""Does this response accurately reflect the tool results? Reply YES or NO with a brief reason.

Tool results:
{results_summary}

Response:
{response_text[:500]}

Accurate (YES/NO):"""

    result = await local_client.generate(prompt, model=LOCAL_MODEL, max_tokens=1024)
    if result and "NO" in result.upper():
        log.warning(f"Local verifier flagged inaccuracy: {result}")
        return False, response_text + "\n\nWarning: This response may contain inaccuracies. Please verify the data."

    return True, response_text
