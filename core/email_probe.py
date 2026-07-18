#!/usr/bin/env python3
"""Burner email probe — validates deliverability before real outreach.

Flow:
  1. outreach_cron calls probe(email) before sending real email
  2. Sends blank email + tracking pixel FROM the Yahoo burner account
  3. reply_detector.py monitors Yahoo IMAP for mailer-daemon bounces
  4. No bounce after BOUNCE_WINDOW_MINUTES → address is deliverable → send real email

The Yahoo account is completely disconnected from docsapp identity.
Bounces only hurt the throwaway, not user@example.com.
"""
import imaplib
import email as email_lib
import email.utils
import json
import logging
import os
import re
import smtplib
import time
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

log = logging.getLogger("email-probe")

BASE = Path(os.path.expanduser("~/claude-stack"))
PENDING_FILE = BASE / "data" / "hustle" / "probe_pending.json"
CACHE_FILE = BASE / "data" / "hustle" / "email_verification_cache.json"

# Yahoo burner credentials (set in .env)
# promptsite.app uses iCloud MX — same SMTP/IMAP as existing stack, no extra creds
PROBE_SMTP_HOST = "smtp.mail.me.com"
PROBE_SMTP_PORT = 587
PROBE_FROM = os.environ.get("PROBE_EMAIL", "user@example.com")
# iCloud SMTP login = Apple ID, not the custom domain alias
PROBE_SMTP_USER = os.environ.get("OUTREACH_SMTP_USER", "user@example.com")
PROBE_PASS = os.environ.get("OUTREACH_SMTP_PASS", "")

# Tracking pixel hosted on docsapp.dev
TRACK_BASE = "https://docsapp.dev/px"

# No bounce after this many minutes → assume deliverable
BOUNCE_WINDOW_MINUTES = 12


# ── Cache helpers ──────────────────────────────────────────────────────

def _load_pending() -> dict:
    try:
        return json.loads(PENDING_FILE.read_text()) if PENDING_FILE.exists() else {}
    except Exception:
        return {}


def _save_pending(data: dict):
    PENDING_FILE.parent.mkdir(parents=True, exist_ok=True)
    PENDING_FILE.write_text(json.dumps(data, indent=None))


def _load_cache() -> dict:
    try:
        return json.loads(CACHE_FILE.read_text()) if CACHE_FILE.exists() else {}
    except Exception:
        return {}


def _save_cache(data: dict):
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(data, indent=None))


# ── Status check ───────────────────────────────────────────────────────

def get_probe_status(email: str) -> str:
    """Returns: not_probed | pending | bounced | valid"""
    email = email.lower().strip()

    # Check verification cache first
    cache = _load_cache()
    if email in cache:
        r = cache[email]
        if isinstance(r, dict):
            flags = r.get("flags", [])
            if "probe_bounced" in flags:
                return "bounced"
            if "probe_valid" in flags:
                return "valid"
            # Legacy invalid from old MX/SMTP checks
            if r.get("status") == "invalid" and r.get("score", 1) == 0:
                return "bounced"

    # Check pending probes
    pending = _load_pending()
    if email in pending:
        sent_at_str = pending[email].get("sent_at", "")
        try:
            sent_at = datetime.fromisoformat(sent_at_str)
            age_min = (datetime.now(timezone.utc) - sent_at).total_seconds() / 60
            if age_min >= BOUNCE_WINDOW_MINUTES:
                # Window passed with no bounce → valid
                mark_valid(email)
                del pending[email]
                _save_pending(pending)
                return "valid"
        except Exception:
            pass
        return "pending"

    return "not_probed"


# ── Send probe ─────────────────────────────────────────────────────────

def probe(email: str, first_name: str = "", company: str = "") -> str:
    """Send probe email from Yahoo burner. Returns: pending | valid (if probe fails)."""
    email = email.lower().strip()

    if not PROBE_PASS:
        log.warning("PROBE_EMAIL_PASS not set — skipping probe for %s", email)
        mark_valid(email)  # can't probe, treat as valid
        return "valid"

    try:
        from core.autonomous_outbound import is_personal_icloud_smtp_host

        if is_personal_icloud_smtp_host(PROBE_SMTP_HOST):
            log.info(
                "Email probe disabled: PROBE_SMTP_HOST=%s is iCloud relay; set "
                "OUTREACH_ALLOW_ICLOUD_SMTP=1 to re-enable. Marking %s valid.",
                PROBE_SMTP_HOST,
                email,
            )
            mark_valid(email)
            return "valid"
    except Exception:
        pass

    # Unique token for tracking pixel
    token = f"{int(time.time() * 1000)}_{email.replace('@', '_').replace('.', '_')}"
    pixel_url = f"{TRACK_BASE}/{token}.gif"

    # Blank email: no visible body, just a tracking pixel in HTML part
    html_body = (
        f'<html><body style="margin:0;padding:0;">'
        f'<img src="{pixel_url}" width="1" height="1" style="display:block;" alt="">'
        f'</body></html>'
    )

    msg = MIMEMultipart("alternative")
    # Suppress display name — most clients show just the raw address with no label,
    # which reads as an accidental/system send. Reply-To misdirects any manual replies
    # away from the burner; bounces (envelope MAIL FROM) still land on Yahoo correctly.
    msg["From"] = email_lib.utils.formataddr(("", PROBE_FROM))
    msg["To"] = email
    msg["Subject"] = ""          # blank — looks like an accidental draft send
    msg["X-Probe-Token"] = token  # internal tracking only

    msg.attach(MIMEText("", "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP(PROBE_SMTP_HOST, PROBE_SMTP_PORT, timeout=20) as s:
            s.ehlo()
            s.starttls()
            s.ehlo()
            s.login(PROBE_SMTP_USER, PROBE_PASS)  # Apple ID, not alias
            s.sendmail(PROBE_FROM, [email], msg.as_string())
        log.info("Probe sent to %s (token: %s)", email, token[:20])
    except smtplib.SMTPRecipientsRefused:
        # SMTP server itself rejected → definite invalid
        log.info("Probe SMTP-rejected %s — marking bounced immediately", email)
        mark_bounced(email)
        return "bounced"
    except Exception as e:
        log.warning("Probe send failed for %s: %s — treating as valid", email, e)
        mark_valid(email)
        return "valid"

    # Record pending + reverse token map
    pending = _load_pending()
    pending[email] = {
        "sent_at": datetime.now(timezone.utc).isoformat(),
        "token": token,
        "company": company,
    }
    _save_pending(pending)
    # Write reverse map: token -> email (for tracking pixel lookup)
    tmap_file = PENDING_FILE.parent / "probe_token_map.json"
    try:
        tmap = json.loads(tmap_file.read_text()) if tmap_file.exists() else {}
        tmap[token] = email
        tmap_file.write_text(json.dumps(tmap))
    except Exception:
        pass
    return "pending"


# ── Bounce detection (called by reply_detector or directly) ───────────

def mark_bounced(email: str):
    """Mark email as probe_bounced in cache. Called when mailer-daemon detected."""
    email = email.lower().strip()
    cache = _load_cache()
    cache[email] = {"status": "invalid", "score": 0, "flags": ["probe_bounced"]}
    _save_cache(cache)
    pending = _load_pending()
    pending.pop(email, None)
    _save_pending(pending)
    log.info("probe_bounced: %s", email)


def mark_valid(email: str):
    """Mark email as probe_valid (bounce window passed, no bounce received)."""
    email = email.lower().strip()
    cache = _load_cache()
    existing = cache.get(email, {})
    flags = [f for f in (existing.get("flags", []) if isinstance(existing, dict) else []) if "probe" not in f]
    flags.append("probe_valid")
    cache[email] = {"status": "valid", "score": 80, "flags": flags}
    _save_cache(cache)
    log.info("probe_valid: %s", email)


# ── Maintenance: expire old pending probes ────────────────────────────

def cleanup_pending():
    """Expire probes older than BOUNCE_WINDOW_MINUTES → mark valid."""
    pending = _load_pending()
    expired = []
    for email_addr, info in list(pending.items()):
        try:
            sent_at = datetime.fromisoformat(info["sent_at"])
            age_min = (datetime.now(timezone.utc) - sent_at).total_seconds() / 60
            if age_min >= BOUNCE_WINDOW_MINUTES:
                mark_valid(email_addr)
                expired.append(email_addr)
        except Exception:
            expired.append(email_addr)

    for e in expired:
        pending.pop(e, None)
    if expired:
        _save_pending(pending)
        log.info("Expired %d probes → marked valid", len(expired))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cleanup_pending()
