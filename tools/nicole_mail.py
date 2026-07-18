"""Operator2's Apple Mail reader — queries only her 4 accounts via AppleScript."""

import os
import subprocess
import json
from datetime import datetime
from pathlib import Path

NICOLE_ACCOUNTS = [
    "user@example.com",
    "user@example.com",
    "user@example.com",
    "user@example.com",
]

APPLE_MAIL_DRAFTS_ENABLED = False

# SEND FENCE — mirrors the work-lane rail (apps.redacted_brain.safety.is_send): a send-capable path is
# drafts-only and NEVER transmits unless explicitly armed, and the MAROON_OFF killswitch hard-halts it.
try:
    from apps.redacted_brain.safety import is_send as _vb_is_send
except Exception:
    _vb_is_send = None


def _maroon_off() -> bool:
    """True when the hustle-lane MAROON_OFF killswitch file is present."""
    return (Path(os.path.expanduser("~/claude-stack")) / "data" / "hustle" / "MAROON_OFF").exists()


def _send_fence(tool_name: str) -> dict | None:
    """Drafts-only fence for Operator2's LIVE Apple-Mail send/reply paths. Returns an error dict to
    return early, or None when the send may proceed. Both send_nicole_email and reply_nicole_email
    are is_send()-class transmit paths, so they stay fenced unless NICOLE_MAIL_SEND_ARMED=1; the
    MAROON_OFF killswitch hard-halts regardless of arming. Does NOT replace the account-allowlist /
    test-recipient guards — it stacks on top of them."""
    if _maroon_off():
        return {"ok": False, "error": "MAROON_OFF killswitch present — Operator2 send halted"}
    if _vb_is_send is not None:
        try:
            _vb_is_send(tool_name)  # shared classifier (send paths are fenced by default)
        except Exception:
            pass
    if os.environ.get("NICOLE_MAIL_SEND_ARMED") != "1":
        return {"ok": False, "deferred": True,
                "error": "Operator2 send is fenced (drafts-only) — set NICOLE_MAIL_SEND_ARMED=1 to arm"}
    return None


def _osascript(script: str) -> str:
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=45)
    if result.returncode != 0:
        raise RuntimeError(f"AppleScript error: {result.stderr.strip()}")
    return result.stdout.strip()


def _escape_applescript_string(value: str) -> str:
    return str(value or "").replace("\\", "\\\\").replace('"', '\\"')


def _message_lookup_applescript(message_subject: str, message_id: str = "") -> str:
    """Return AppleScript that sets origMsg by stable Mail id, falling back to subject search."""
    msg_id_esc = _escape_applescript_string(message_id)
    subj_esc = _escape_applescript_string(message_subject)
    if msg_id_esc:
        return f'''
        set origMsg to missing value
        repeat with candidateMsg in messages of mbox
            try
                if (id of candidateMsg as string) is "{msg_id_esc}" then
                    set origMsg to candidateMsg
                    exit repeat
                end if
            end try
        end repeat
        if origMsg is missing value then return "NOT_FOUND_ID"'''
    return f'''
        set targetMsgs to (messages of mbox whose subject contains "{subj_esc}")
        if (count of targetMsgs) > 0 then
            set origMsg to item 1 of targetMsgs
        else
            return "NOT_FOUND"
        end if'''


def get_nicole_inbox(count: int = 20) -> list[dict]:
    """Get recent messages from all of Operator2's accounts, merged and sorted by date."""
    all_messages = []
    for acct in NICOLE_ACCOUNTS:
        try:
            # Some accounts use "INBOX", some "Inbox" — try both
            mbox = "Inbox" if "cnovate" in acct else "INBOX"
            script = f'''
tell application "Mail"
    try
        set acctRef to account "{acct}"
        set mbox to mailbox "{mbox}" of acctRef
        set msgs to messages 1 thru {count} of mbox
        set output to ""
        repeat with m in msgs
            set subj to subject of m
            set sndr to sender of m
            set dt to date received of m as string
            set rd to read status of m
            set localId to id of m as string
            set headerId to ""
            try
                set headerId to message id of m as string
            end try
            set prev to extract name from m
            try
                set prev to content of m
                if (length of prev) > 100 then set prev to text 1 thru 100 of prev
            on error
                set prev to ""
            end try
            set output to output & rd & "|||" & sndr & "|||" & subj & "|||" & dt & "|||" & prev & "|||" & localId & "|||" & headerId & linefeed
        end repeat
        return output
    on error
        return ""
    end try
end tell'''
            raw = _osascript(script)
            if raw:
                for line in raw.strip().split("\n"):
                    parts = line.split("|||")
                    if len(parts) >= 4:
                        all_messages.append({
                            "account": acct,
                            "read": parts[0].strip() == "true",
                            "from": parts[1].strip(),
                            "subject": parts[2].strip(),
                            "date": parts[3].strip(),
                            "preview": parts[4].strip()[:100] if len(parts) > 4 else "",
                            "message_id": parts[5].strip() if len(parts) > 5 else "",
                            "rfc822_message_id": parts[6].strip() if len(parts) > 6 else "",
                        })
        except Exception:
            continue

    # Sort by date descending (best effort)
    all_messages.sort(key=lambda m: m.get("date", ""), reverse=True)
    return all_messages[:count]


def get_nicole_unread_counts() -> dict[str, int]:
    """Get unread count per account."""
    counts = {}
    for acct in NICOLE_ACCOUNTS:
        try:
            script = f'''
tell application "Mail"
    try
        set acctRef to account "{acct}"
        set mboxName to "INBOX"
        if "{acct}" contains "cnovate" then set mboxName to "Inbox"
        set unreadMsgs to (messages of mailbox mboxName of acctRef whose read status is false)
        return (count of unreadMsgs) as string
    on error
        return "0"
    end try
end tell'''
            raw = _osascript(script)
            counts[acct] = int(raw) if raw.isdigit() else 0
        except Exception:
            counts[acct] = 0
    return counts


def get_nicole_calendar_events(days: int = 1) -> list[dict]:
    """Get Operator2's Apple Calendar events for today (or N days)."""
    import re
    script = f'''
tell application "Calendar"
    set today to current date
    set todayStart to today - (time of today)
    set futureEnd to todayStart + ({days} * days)
    set output to ""
    repeat with c in calendars
        try
            set evts to (every event of c whose start date >= todayStart and start date < futureEnd)
            repeat with e in evts
                set output to output & (start date of e as string) & "|||" & (end date of e as string) & "|||" & (summary of e) & "|||" & (name of c) & linefeed
            end repeat
        end try
    end repeat
    if output = "" then return "NONE"
    return output
end tell'''
    try:
        raw = _osascript(script)
        if raw == "NONE" or not raw:
            return []
        events = []
        for line in raw.strip().split("\n"):
            parts = line.split("|||")
            if len(parts) >= 3:
                start_str = parts[0].strip()
                end_str = parts[1].strip()
                # Extract time display
                st_match = re.search(r'(\d{1,2}):(\d{2}):\d{2}\s*(AM|PM)', start_str)
                et_match = re.search(r'(\d{1,2}):(\d{2}):\d{2}\s*(AM|PM)', end_str)
                if st_match:
                    st = f"{st_match.group(1)}:{st_match.group(2)} {st_match.group(3)}"
                    et = f"{et_match.group(1)}:{et_match.group(2)} {et_match.group(3)}" if et_match else ""
                    time_display = f"{st} - {et}" if et else st
                else:
                    time_display = start_str
                events.append({
                    "subject": parts[2].strip(),
                    "time_display": time_display,
                    "calendar": parts[3].strip() if len(parts) > 3 else "",
                    "start_raw": start_str,
                })
        # Sort by time
        def _sort_time(e):
            m = re.match(r'(\d+):(\d+)\s*(AM|PM)', e.get('time_display', '') or '')
            if not m:
                return 9999
            h, mn = int(m.group(1)), int(m.group(2))
            if m.group(3).upper() == 'PM' and h != 12:
                h += 12
            if m.group(3).upper() == 'AM' and h == 12:
                h = 0
            return h * 60 + mn
        events.sort(key=_sort_time)
        return events
    except Exception:
        return []


def send_nicole_email(account: str, to: str, subject: str, body_html: str) -> dict:
    """Send an email from one of Operator2's accounts via Apple Mail."""
    if account not in NICOLE_ACCOUNTS:
        return {"ok": False, "error": f"Account {account} not in Operator2's accounts"}
    _fenced = _send_fence("nicole_send_email")
    if _fenced:
        return _fenced
    # Hard guard: never let a test/example recipient queue a REAL Apple Mail send. RFC-reserved
    # example.* recipients (e.g. the friend@example.com test fixture) once stuck in the Outbox and
    # Mail's send-retry loop kept popping compose windows + Gmail auth prompts. Refuse them outright.
    _to_dom = (to or "").rsplit("@", 1)[-1].strip().lower()
    if _to_dom in ("example.com", "example.net", "example.org") or not _to_dom or "@" not in (to or ""):
        return {"ok": False, "error": f"Refused: '{to}' is a test/invalid recipient, not sending."}
    # Escape for AppleScript
    subj_esc = subject.replace('"', '\\"').replace('\\', '\\\\')
    body_esc = body_html.replace('"', '\\"').replace('\\', '\\\\')
    to_esc = to.replace('"', '\\"')
    script = f'''
tell application "Mail"
    set newMsg to make new outgoing message with properties {{subject:"{subj_esc}", content:"{body_esc}", visible:false}}
    tell newMsg
        set sender to "{account}"
        make new to recipient at end of to recipients with properties {{address:"{to_esc}"}}
    end tell
    send newMsg
end tell'''
    try:
        _osascript(script)
        return {"ok": True, "sent_from": account, "to": to, "subject": subject}
    except RuntimeError as e:
        return {"ok": False, "error": "Mail operation failed"}


def _foreground_safe_to_drive_mail() -> bool:
    """Driving Apple Mail (reply/draft) can momentarily touch the GUI. Never do it while
    Operator is actively at the machine — honors the hard 'nothing jacks my foreground' rule."""
    try:
        from core.user_active import should_foreground
        return should_foreground()
    except Exception:
        return True


def reply_nicole_email(account: str, message_subject: str, body_html: str, message_id: str = "") -> dict:
    """Reply to an email in Operator2's Apple Mail, preferring a stable Mail message id."""
    if account not in NICOLE_ACCOUNTS:
        return {"ok": False, "error": f"Account {account} not in Operator2's accounts"}
    _fenced = _send_fence("nicole_reply_email")
    if _fenced:
        return _fenced
    if not _foreground_safe_to_drive_mail():
        return {"ok": False, "deferred": True, "error": "user active — deferring Mail reply to avoid foreground steal"}
    body_esc = _escape_applescript_string(body_html)
    lookup_script = _message_lookup_applescript(message_subject, message_id)
    script = f'''
tell application "Mail"
    try
        set acctRef to account "{account}"
        set mboxName to "INBOX"
        if "{account}" contains "cnovate" then set mboxName to "Inbox"
        set mbox to mailbox mboxName of acctRef
        {lookup_script}
        set replyMsg to reply origMsg without opening
        set originalContent to ""
        try
            set originalContent to content of replyMsg
        end try
        if originalContent is not "" then
            set content of replyMsg to "{body_esc}" & return & return & originalContent
        else
            set content of replyMsg to "{body_esc}"
        end if
        set visible of replyMsg to false
        send replyMsg
        return "OK"
    on error errMsg
        return "ERROR:" & errMsg
    end try
end tell'''
    try:
        result = _osascript(script)
        if result == "NOT_FOUND_ID":
            return {"ok": False, "error": f"No message with id '{message_id}' found"}
        if result == "NOT_FOUND":
            return {"ok": False, "error": f"No message with subject containing '{message_subject}' found"}
        if result.startswith("ERROR:"):
            return {"ok": False, "error": result[6:]}
        return {"ok": True, "replied_from": account, "subject": message_subject}
    except RuntimeError as e:
        return {"ok": False, "error": "Mail operation failed"}


def create_nicole_reply_draft(account: str, message_subject: str, body_html: str, message_id: str = "") -> dict:
    """Create a reply draft in Operator2's Apple Mail, preferring a stable Mail message id."""
    if account not in NICOLE_ACCOUNTS:
        return {"ok": False, "error": f"Account {account} not in Operator2's accounts"}
    if not _foreground_safe_to_drive_mail():
        return {"ok": False, "deferred": True, "error": "user active — deferring Mail draft to avoid foreground steal"}
    body_esc = _escape_applescript_string(body_html)
    lookup_script = _message_lookup_applescript(message_subject, message_id)
    script = f'''
tell application "Mail"
    try
        set acctRef to account "{account}"
        set mboxName to "INBOX"
        if "{account}" contains "cnovate" then set mboxName to "Inbox"
        set mbox to mailbox mboxName of acctRef
        {lookup_script}
        set replyMsg to reply origMsg without opening
        set originalContent to ""
        try
            set originalContent to content of replyMsg
        end try
        if originalContent is not "" then
            set content of replyMsg to "{body_esc}" & return & return & originalContent
        else
            set content of replyMsg to "{body_esc}"
        end if
        set visible of replyMsg to false
        save replyMsg
        return "OK"
    on error errMsg
        return "ERROR:" & errMsg
    end try
end tell'''
    try:
        result = _osascript(script)
        if result == "NOT_FOUND_ID":
            return {"ok": False, "error": f"No message with id '{message_id}' found"}
        if result == "NOT_FOUND":
            return {"ok": False, "error": f"No message with subject containing '{message_subject}' found"}
        if result.startswith("ERROR:"):
            return {"ok": False, "error": result[6:]}
        return {"ok": True, "draft_from": account, "subject": message_subject}
    except RuntimeError as e:
        return {"ok": False, "error": "Mail operation failed"}


def create_nicole_draft(account: str, to: str, subject: str, body_html: str) -> dict:
    """Create a draft in Apple Mail for one of Operator2's accounts."""
    if not APPLE_MAIL_DRAFTS_ENABLED:
        return {"ok": False, "error": "Apple Mail draft creation is disabled"}
    if account not in NICOLE_ACCOUNTS:
        return {"ok": False, "error": f"Account {account} not in Operator2's accounts"}
    if not _foreground_safe_to_drive_mail():
        return {"ok": False, "deferred": True, "error": "user active — deferring Mail draft to avoid foreground steal"}
    subj_esc = subject.replace('"', '\\"').replace('\\', '\\\\')
    body_esc = body_html.replace('"', '\\"').replace('\\', '\\\\')
    to_esc = to.replace('"', '\\"')
    # visible:false — the draft is saved to Drafts silently (review there); a visible window
    # was the rogue composer that grabbed Operator's foreground.
    script = f'''
tell application "Mail"
    set newMsg to make new outgoing message with properties {{subject:"{subj_esc}", content:"{body_esc}", visible:false}}
    tell newMsg
        set sender to "{account}"
        make new to recipient at end of to recipients with properties {{address:"{to_esc}"}}
    end tell
    save newMsg
end tell'''
    try:
        _osascript(script)
        return {"ok": True, "draft_from": account, "to": to, "subject": subject}
    except RuntimeError as e:
        return {"ok": False, "error": "Mail operation failed"}


def forward_nicole_email(account: str, message_subject: str, to: str) -> dict:
    """Forward an email found by subject to a new recipient via Apple Mail."""
    if account not in NICOLE_ACCOUNTS:
        return {"ok": False, "error": f"Account {account} not in Operator2's accounts"}
    if not _foreground_safe_to_drive_mail():
        return {"ok": False, "deferred": True, "error": "user active — deferring Mail forward to avoid foreground steal"}
    subj_esc = message_subject.replace('"', '\\"').replace('\\', '\\\\')
    to_esc = to.replace('"', '\\"')
    script = f'''
tell application "Mail"
    try
        set acctRef to account "{account}"
        set mboxName to "INBOX"
        if "{account}" contains "cnovate" then set mboxName to "Inbox"
        set mbox to mailbox mboxName of acctRef
        set targetMsgs to (messages of mbox whose subject contains "{subj_esc}")
        if (count of targetMsgs) > 0 then
            set origMsg to item 1 of targetMsgs
            set fwdMsg to forward origMsg without opening
            tell fwdMsg
                make new to recipient at end of to recipients with properties {{address:"{to_esc}"}}
                set visible to false
            end tell
            send fwdMsg
            return "OK"
        else
            return "NOT_FOUND"
        end if
    on error errMsg
        return "ERROR:" & errMsg
    end try
end tell'''
    try:
        result = _osascript(script)
        if result == "NOT_FOUND":
            return {"ok": False, "error": f"No message with subject containing '{message_subject}' found"}
        if result.startswith("ERROR:"):
            return {"ok": False, "error": result[6:]}
        return {"ok": True, "forwarded_from": account, "to": to, "subject": message_subject}
    except RuntimeError as e:
        return {"ok": False, "error": "Mail operation failed"}
