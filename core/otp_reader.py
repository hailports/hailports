#!/usr/bin/env python3
"""OTP reader — watches Mac Messages DB for SMS forwarded from Spectrum iPhone.

Requires: iPhone Settings → Messages → Text Message Forwarding → Mini enabled.

Usage:
    from core.otp_reader import wait_for_otp
    code = wait_for_otp(timeout=120)
"""
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.expanduser("~/claude-stack"))

OTP_PATTERN = re.compile(r'\b(\d{4,8})\b')
OTP_FILE = Path.home() / "claude-stack" / "data" / "hustle" / "otp_inbox.json"
TG_OFFSET_FILE = Path.home() / "claude-stack" / "data" / "hustle" / "otp_tg_offset.json"


def _latest_sms_codes(since_ts: float, max_age_s: int = 300) -> list[str]:
    """Query Messages DB for recent inbound SMS texts containing digit codes."""
    try:
        from core.imessage_db import query_rows
        # message.date is Apple epoch (seconds since 2001-01-01); convert unix ts
        apple_epoch_offset = 978307200
        apple_ts = int(since_ts - apple_epoch_offset) * 1_000_000_000  # nanoseconds
        sql = f"""
            SELECT m.text
            FROM message m
            WHERE m.is_from_me = 0
              AND m.date >= {apple_ts}
              AND m.text IS NOT NULL
            ORDER BY m.date DESC
            LIMIT 20
        """
        rows = query_rows(sql)
        codes = []
        for (text,) in rows:
            m = OTP_PATTERN.search(text or "")
            if m:
                codes.append(m.group(1))
        return codes
    except Exception:
        return []


def _poll_telegram() -> str:
    """Fallback: check if user texted the code to the Telegram bot."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        return ""
    try:
        import urllib.request
        offset = 0
        try:
            offset = json.loads(TG_OFFSET_FILE.read_text()).get("offset", 0)
        except Exception:
            pass
        url = f"https://api.telegram.org/bot{token}/getUpdates?offset={offset}&timeout=1&limit=10"
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read())
        for upd in data.get("result", []):
            TG_OFFSET_FILE.parent.mkdir(parents=True, exist_ok=True)
            TG_OFFSET_FILE.write_text(json.dumps({"offset": upd["update_id"] + 1}))
            text = upd.get("message", {}).get("text", "")
            m = OTP_PATTERN.search(text)
            if m:
                return m.group(1)
    except Exception:
        pass
    return ""


def _poll_inbox() -> str:
    """Check local webhook inbox (Burner/future)."""
    if not OTP_FILE.exists():
        return ""
    try:
        inbox = json.loads(OTP_FILE.read_text())
    except Exception:
        return ""
    for i, entry in enumerate(inbox):
        if entry.get("consumed"):
            continue
        otp = entry.get("otp", "")
        if otp:
            inbox[i]["consumed"] = True
            OTP_FILE.write_text(json.dumps(inbox, indent=2))
            return otp
    return ""


def wait_for_otp(timeout=120):
    """Block until OTP arrives via SMS (Messages DB), Telegram, or webhook.
    Returns the code string or None on timeout.
    """
    start = time.time()
    deadline = start + timeout
    while time.time() < deadline:
        # Primary: Messages DB (SMS forwarded from iPhone)
        codes = _latest_sms_codes(since_ts=start - 10)  # 10s grace for clock skew
        if codes:
            return codes[0]
        # Fallback 1: webhook inbox
        code = _poll_inbox()
        if code:
            return code
        # Fallback 2: Telegram (user manually pastes code)
        code = _poll_telegram()
        if code:
            return code
        time.sleep(4)
    return None
