"""core/alert.py — real interest alerts to Operator.

Alert email delivery is disabled by default to prevent inbox floods. Set
STACK_ALERT_EMAILS_ENABLED=1 to re-enable the Resend/Brevo email leg.
"""
import json
import logging
import os
import subprocess
import urllib.request
import urllib.error

log = logging.getLogger("alert")

ALEX_EMAIL = "user@example.com"
ALEX_PHONE = "XPHONEX"  # guaranteed SMS/iMessage leg for any live bite
_FROM = "user@example.com"
_FROM_NAME = "docsapp Alerts"
_EMAILS_ENABLED = os.environ.get("STACK_ALERT_EMAILS_ENABLED") == "1"

_SKIP_SENDERS = (
    "mailer-daemon", "postmaster", "user@example.com",
    "noreply@", "no-reply@", "donotreply@",
)


def _should_skip(sender_email: str) -> bool:
    return any(p in sender_email.lower() for p in _SKIP_SENDERS)


def _send_telegram(subject: str, body: str):
    try:
        from core.telegram import send_to_alex
        send_to_alex(f"🔔 *{subject}*\n\n{body[:800]}", parse_mode="Markdown")
        log.info("Telegram alert sent to Operator")
    except Exception as e:
        log.error("Telegram alert failed: %s", e)


_IMSG_SCRIPT = (
    'on run {targetPhone, targetMsg}\n'
    '  tell application "Messages"\n'
    '    set svc to 1st service whose service type = iMessage\n'
    '    send targetMsg to buddy targetPhone of svc\n'
    '  end tell\n'
    'end run'
)


def _imsg_rate_ok(subject: str, body: str) -> bool:
    """Flood guard for the guaranteed iMessage leg: dedup identical alerts within a
    window and cap total sends per hour, so a crash/bite loop can't storm Operator's
    phone. Bypass for genuinely critical alerts with ALERT_GW_PASSTHROUGH=1."""
    import os, time, json, hashlib, tempfile
    if os.environ.get("ALERT_GW_PASSTHROUGH") == "1":
        return True
    try:
        dedup_s = int(os.environ.get("ALERT_IMSG_DEDUP_S", "900"))
        max_hr = int(os.environ.get("ALERT_IMSG_MAX_PER_HOUR", "15"))
        state_path = os.path.join(tempfile.gettempdir(), "claude_stack_imsg_ratelimit.json")
        now = time.time()
        key = hashlib.sha1(f"{subject}\n{body}".encode("utf-8", "ignore")).hexdigest()[:16]
        try:
            with open(state_path) as fh:
                st = json.load(fh)
        except Exception:
            st = {"sent": {}, "recent": []}
        last = st.get("sent", {}).get(key, 0)
        if now - last < dedup_s:
            log.info("iMessage alert deduped (within %ss): %s", dedup_s, subject[:60])
            return False
        recent = [t for t in st.get("recent", []) if now - t < 3600]
        if len(recent) >= max_hr:
            log.warning("iMessage alert rate-capped (%s/hr) — suppressing: %s", max_hr, subject[:60])
            st["recent"] = recent
            try:
                with open(state_path, "w") as fh:
                    json.dump(st, fh)
            except Exception:
                pass
            return False
        recent.append(now)
        st["recent"] = recent
        st.setdefault("sent", {})[key] = now
        st["sent"] = {k: v for k, v in st["sent"].items() if now - v < 86400}
        try:
            with open(state_path, "w") as fh:
                json.dump(st, fh)
        except Exception:
            pass
        return True
    except Exception as e:
        log.warning("iMessage rate-limit check errored (allowing send): %s", e)
        return True


def _send_imessage_phone(subject: str, body: str) -> bool:
    """Guaranteed leg: text the alert straight to Operator's phone via iMessage.
    This bypasses Telegram/global-mute so a live bite always reaches him."""
    if not _imsg_rate_ok(subject, body):
        return False
    msg = f"🔔 {subject}\n{body}"[:600]
    try:
        r = subprocess.run(
            ["/usr/bin/osascript", "-e", _IMSG_SCRIPT, ALEX_PHONE, msg],
            capture_output=True, text=True, timeout=25,
        )
        if r.returncode == 0:
            log.info("iMessage alert sent to Operator phone")
            return True
        log.warning("iMessage alert failed rc=%s: %s", r.returncode, r.stderr[:200])
    except Exception as e:
        log.warning("iMessage alert error: %s", e)
    return False


def _post_json(url: str, payload: dict, headers: dict) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read() or b"{}")


def _send_via_resend(subject: str, body: str) -> bool:
    key = os.environ.get("RESEND_API_KEY", "").strip()
    if not key:
        return False
    try:
        result = _post_json(
            "https://api.resend.com/emails",
            {
                "from": f"{_FROM_NAME} <{_FROM}>",
                "to": [ALEX_EMAIL],
                "subject": subject,
                "text": body,
            },
            {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
                # Browser UA: python-urllib default UA is CF-1010 blocked by Resend.
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/124.0.0.0 Safari/537.36",
            },
        )
        log.info("Alert email via Resend to %s id=%s", ALEX_EMAIL, result.get("id", "?"))
        return True
    except urllib.error.HTTPError as e:
        log.warning("Resend alert HTTP %s: %s", e.code, e.read().decode(errors="replace")[:300])
    except Exception as e:
        log.warning("Resend alert error: %s", e)
    return False


def _send_via_brevo(subject: str, body: str) -> bool:
    key = os.environ.get("BREVO_API_KEY", "").strip()
    if not key:
        return False
    try:
        result = _post_json(
            "https://api.brevo.com/v3/smtp/email",
            {
                "sender": {"email": _FROM, "name": _FROM_NAME},
                "to": [{"email": ALEX_EMAIL}],
                "subject": subject,
                "textContent": body,
            },
            {
                "api-key": key,
                "Content-Type": "application/json",
                "accept": "application/json",
            },
        )
        log.info("Alert email via Brevo to %s id=%s", ALEX_EMAIL, result.get("messageId", "?"))
        return True
    except urllib.error.HTTPError as e:
        log.warning("Brevo alert HTTP %s: %s", e.code, e.read().decode(errors="replace")[:300])
    except Exception as e:
        log.warning("Brevo alert error: %s", e)
    return False


def _send_email(subject: str, body: str) -> bool:
    if not _EMAILS_ENABLED:
        log.debug("Alert email suppressed: STACK_ALERT_EMAILS_ENABLED is not 1")
        return False
    if _send_via_resend(subject, body):
        return True
    if _send_via_brevo(subject, body):
        return True
    log.error("Alert email skipped: no working transactional provider (Resend/Brevo)")
    return False


def alert_alex(subject: str, body: str, sender_email: str = ""):
    """Fire Telegram + email to Operator. Real interest only."""
    if sender_email and _should_skip(sender_email):
        log.debug("alert_alex suppressed for: %s", sender_email)
        return
    _send_imessage_phone(subject, body)  # guaranteed phone leg FIRST
    # Telegram leg PURGED 2026-06-12 — alerts go via iMessage + email only.
    _send_email(f"[docsapp] {subject}", body)
