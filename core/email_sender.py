"""Shared Apple Mail / AppleScript utilities.

Consolidates send_apple_mail() and run_applescript() previously
duplicated across multiple agent files.
"""

import logging
import os
import smtplib
import subprocess
import tempfile
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from core.autonomous_outbound import guard_autonomous_work_email

log = logging.getLogger(__name__)

SENDER_EMAIL = "user@example.com"
RECIPIENT_EMAIL = "user@example.com"


def _smtp_config() -> dict:
    return {
        "host": os.environ.get("scannerapp_SMTP_HOST") or os.environ.get("SMTP_HOST", ""),
        "port": int(os.environ.get("scannerapp_SMTP_PORT") or os.environ.get("SMTP_PORT", "587")),
        "username": os.environ.get("scannerapp_SMTP_USER") or os.environ.get("SMTP_USER", ""),
        "password": os.environ.get("scannerapp_SMTP_PASSWORD") or os.environ.get("SMTP_PASSWORD", ""),
        "from_email": os.environ.get("scannerapp_FROM_EMAIL") or os.environ.get("SMTP_FROM") or SENDER_EMAIL,
    }


def send_html_email(to_email: str, subject: str, html_body: str, text_body: str = "") -> bool:
    """Send a customer-facing HTML email through configured SMTP.

    Returns False instead of raising when SMTP is not configured so callers can
    keep user workflows moving while recording delivery status.
    """
    to_email = (to_email or "").strip()
    if "@" not in to_email:
        return False

    cfg = _smtp_config()
    if not cfg["host"] or not cfg["username"] or not cfg["password"]:
        log.warning("SMTP not configured; skipped email to %s", to_email)
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = cfg["from_email"]
    msg["To"] = to_email
    if text_body:
        msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=30) as server:
            server.starttls()
            server.login(cfg["username"], cfg["password"])
            server.sendmail(cfg["from_email"], [to_email], msg.as_string())
        log.info("HTML email sent to %s: %s", to_email, subject)
        return True
    except Exception as exc:
        log.warning("HTML email failed to %s: %s", to_email, exc)
        return False


def send_scannerapp_report_email(to_email: str, report: dict, report_url: str, pdf_url: str) -> bool:
    """Email scannerapp report links to the requester."""
    score = report.get("org_health_score", 0)
    label = report.get("org_health_label", "Unknown")
    findings = report.get("finding_count", 0)
    base_url = os.environ.get("scannerapp_PUBLIC_BASE_URL", "https://scannerapp.dev").rstrip("/")

    def absolute(url: str) -> str:
        return base_url + url if str(url).startswith("/") else str(url)

    html = f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#0f172a;line-height:1.6">
      <h2 style="margin:0 0 12px">Your scannerapp report is ready</h2>
      <p>Your Salesforce org health score is <strong>{score}/100 ({label})</strong> with <strong>{findings}</strong> findings.</p>
      <p>
        <a href="{absolute(report_url)}" style="display:inline-block;background:#0f172a;color:#fff;padding:10px 16px;border-radius:6px;text-decoration:none">View HTML Report</a>
        <a href="{absolute(pdf_url)}" style="display:inline-block;background:#0f766e;color:#fff;padding:10px 16px;border-radius:6px;text-decoration:none;margin-left:8px">Download PDF</a>
      </p>
      <p style="font-size:12px;color:#64748b">Credentials were used once for the scan and were not stored.</p>
    </div>
    """
    text = f"Your scannerapp report is ready. Score: {score}/100 ({label}). Findings: {findings}. Report: {absolute(report_url)} PDF: {absolute(pdf_url)}"
    return send_html_email(
        to_email,
        f"Your scannerapp report: {score}/100 ({label})",
        html,
        text,
    )


_FOREGROUND_APPS = ("Microsoft Outlook", "Mail")


def _app_running(name: str) -> bool:
    # `is running` never launches the app, so this can't foreground a closed app.
    try:
        r = subprocess.run(["osascript", "-e", f'application "{name}" is running'],
                           capture_output=True, text=True, timeout=8)
        return r.stdout.strip() == "true"
    except Exception:
        return False


def run_applescript(script: str, timeout: int = 60, allow_launch: bool = False) -> str:
    """Execute an AppleScript and return its stdout.

    Default-safe: if the script targets Outlook/Mail and that app isn't already
    running, skip it (return "") instead of letting `tell application` launch the
    app to the foreground. Background scan jobs (exec_assistant, mail_cleaner,
    email_triage) read live state but must never pop a closed app. Send paths that
    genuinely need to start the app pass allow_launch=True.
    """
    if not allow_launch:
        for app in _FOREGROUND_APPS:
            if f'application "{app}"' in script and not _app_running(app):
                log.info("Skipped AppleScript: %s not running (no foreground launch).", app)
                return ""
    try:
        r = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=timeout,
        )
        if r.returncode != 0:
            log.warning(f"AppleScript error: {r.stderr[:300]}")
            return ""
        return r.stdout
    except subprocess.TimeoutExpired:
        log.warning("AppleScript timed out")
        return ""
    except Exception as e:
        log.warning(f"AppleScript exception: {e}")
        return ""


def send_apple_mail(subject: str, html_body: str) -> bool:
    """Send HTML email via Apple Mail from iCloud account.

    This path is used by background agents; it is fail-closed unless
    ALLOW_AUTONOMOUS_WORK_EMAIL_SEND explicitly enables it.
    """
    if not guard_autonomous_work_email(f"Apple Mail subject={subject[:80]!r}"):
        return False
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".html", delete=False, dir="/tmp") as f:
            f.write(html_body)
            html_path = f.name

        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, dir="/tmp") as f:
            f.write(subject)
            subj_path = f.name

        script = f'''
            set htmlPath to POSIX file "{html_path}"
            set subjPath to POSIX file "{subj_path}"
            set htmlContent to read htmlPath as text
            set emailSubject to read subjPath as text

            tell application "Mail"
                set newMsg to make new outgoing message with properties {{subject:emailSubject, visible:false}}
                set html content of newMsg to htmlContent
                tell newMsg
                    make new to recipient at end of to recipients with properties {{address:"{RECIPIENT_EMAIL}"}}
                    set sender of newMsg to "{SENDER_EMAIL}"
                end tell
                send newMsg
            end tell
            return "sent"
        '''

        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=30,
        )

        os.unlink(html_path)
        os.unlink(subj_path)

        if result.returncode == 0:
            log.info(f"Email sent: {subject}")
            return True
        else:
            log.error(f"Email failed: {result.stderr}")
            return False
    except Exception as e:
        log.error(f"Email error: {e}")
        return False
