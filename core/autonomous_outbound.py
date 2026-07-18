"""Fail-closed guardrails for background outbound communications.

Interactive/user-approved sends are gated elsewhere via tool execution.
This module is specifically for autonomous/background agent paths that
would otherwise send mail or work-chat messages on a schedule.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)


def _enabled(env_name: str) -> bool:
    value = str(os.environ.get(env_name, "")).strip().lower()
    return value in {"1", "true", "yes", "on"}


def autonomous_work_email_allowed() -> bool:
    """Whether background agents may send work email without an interactive approval step."""
    return _enabled("ALLOW_AUTONOMOUS_WORK_EMAIL_SEND")


def autonomous_work_chat_allowed() -> bool:
    """Whether background agents may send work chat messages without an approval step."""
    return _enabled("ALLOW_AUTONOMOUS_WORK_CHAT_SEND")


def guard_autonomous_work_email(context: str) -> bool:
    if autonomous_work_email_allowed():
        return True
    log.warning(
        "Blocked autonomous work email send for %s. Set ALLOW_AUTONOMOUS_WORK_EMAIL_SEND=1 to override.",
        context,
    )
    return False


def guard_autonomous_work_chat(context: str) -> bool:
    if autonomous_work_chat_allowed():
        return True
    log.warning(
        "Blocked autonomous work chat send for %s. Set ALLOW_AUTONOMOUS_WORK_CHAT_SEND=1 to override.",
        context,
    )
    return False


_BLOCKED_SMTP_HOST_SUBSTRINGS = (
    "smtp.mail.me.com",
    "mail.me.com",
    ".icloud.com",
    "icloud.com",
)


def is_personal_icloud_smtp_host(host: str) -> bool:
    """True if the SMTP host is the user's personal iCloud relay.

    Outreach paths must never fall back to iCloud SMTP — that would send mail
    from Operator's personal Apple ID instead of the configured Resend/Brevo
    transactional providers. Allow override via OUTREACH_ALLOW_ICLOUD_SMTP=1
    only for explicit one-off probes/diagnostics.
    """
    if _enabled("OUTREACH_ALLOW_ICLOUD_SMTP"):
        return False
    h = (host or "").strip().lower()
    if not h:
        return False
    return any(token in h for token in _BLOCKED_SMTP_HOST_SUBSTRINGS)


IDENTITY_TIED_SENDER_TOKENS = (
    "Operator",
    "user",
    "Operator",
    "CompanyA",
    "branda",
    "BrandA standard",
    "icloud",
    "me.com",
    "mac.com",
)

BRAND_SENDER_ALLOWLIST = {
    # docsapp family — each domain is its own micro-brand for the same wedge.
    # Rotating across them multiplies safe send volume. branda.com +
    # persona1.com stay OUT (real co + social persona — never cold-blast).
    "docsapp": ("docsapp.dev", "opsapp.app", "opsapp.app", "opsapp.us", "scannerapp.dev"),
    "persona1": ("docsapp.dev", "persona1.com"),
    "persona1": ("docsapp.dev", "persona1.com"),
    "branda": ("branda.com",),
    "maroon_standard": ("branda.com",),
}


def sender_identity_tied_to_user(sender: str, name: str = "") -> bool:
    """True when a brand/persona sender leaks Operator, CompanyA, or personal identity."""
    haystack = f"{sender or ''} {name or ''}".strip().lower()
    if not haystack:
        return False
    return any(token in haystack for token in IDENTITY_TIED_SENDER_TOKENS)


def guard_brand_sender_identity(
    pipeline: str,
    from_email: str,
    from_name: str = "",
    context: str = "",
) -> bool:
    """Fail closed if autonomous persona mail would expose the wrong identity."""
    pipeline_key = (pipeline or "").strip().lower().replace("-", "_")
    sender = (from_email or "").strip().lower()
    domain = sender.rsplit("@", 1)[-1] if "@" in sender else ""
    allowed_domains = BRAND_SENDER_ALLOWLIST.get(pipeline_key)
    if allowed_domains and domain not in allowed_domains:
        log.error(
            "Refusing %s sender %r for %s — domain is not in %s.",
            pipeline or "persona",
            from_email,
            context or "brand sender identity guard",
            ", ".join(allowed_domains),
        )
        return False
    if pipeline_key in {"docsapp", "persona1", "persona1"} and sender_identity_tied_to_user(sender, from_name):
        log.error(
            "Refusing %s sender %r/%r for %s — sender is identity-tied.",
            pipeline or "persona",
            from_email,
            from_name,
            context or "brand sender identity guard",
        )
        return False
    return True


def guard_outbound_smtp_host(host: str, context: str = "") -> bool:
    """Return True if the host is acceptable for outbound transactional mail.

    Logs and blocks when the host points at iCloud/Apple Mail relays so that
    `OUTREACH_SMTP_*` misconfiguration cannot silently route mail through the
    user's personal iCloud account.
    """
    if is_personal_icloud_smtp_host(host):
        log.error(
            "Refusing outbound SMTP host %r for %s — iCloud/Apple Mail relay is "
            "disabled for outreach. Configure Resend or Brevo, or set "
            "OUTREACH_ALLOW_ICLOUD_SMTP=1 to override.",
            host,
            context or "outbound mail",
        )
        return False
    return True
