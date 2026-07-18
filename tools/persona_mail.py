from __future__ import annotations

import html
import json
import os
import smtplib
import ssl
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from products.outreach.compliance_guard import (
    audit_contact_decision,
    contact_allowed,
    evaluate_contact,
    is_masked_email,
    outbound_sends_enabled,
)
from tools.base import BaseTool, make_tool_def
from core.autonomous_outbound import guard_brand_sender_identity
from core.maroon_branding import render_maroon_email_html


ROOT = Path(os.environ.get("CLAUDE_STACK_DIR", Path(__file__).resolve().parents[1]))


def _load_dotenv() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        os.environ[key] = value.strip().strip("'\"")


_load_dotenv()
AUDIT_PATH = ROOT / "data" / "logs" / "persona_mail.jsonl"
SENT_LOG = ROOT / "data" / "hustle" / "outreach_sent.jsonl"
MAROON_SENT_LOG = ROOT / "products" / "outreach" / "sent.jsonl"

FREE_EMAIL_DOMAINS = {
    "gmail.com",
    "yahoo.com",
    "outlook.com",
    "hotmail.com",
    "icloud.com",
    "aol.com",
    "proton.me",
    "protonmail.com",
}
BLOCKED_TLDS = (".edu", ".gov", ".mil")
IDENTITY_TIED_SENDER_TOKENS = ("Operator", "branda", "CompanyA")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _truthy_env(name: str) -> bool:
    return str(os.environ.get(name, "")).strip().lower() in {"1", "true", "yes", "y", "on", "enabled"}


def _resend_key_for_pipeline(pipeline: str) -> str:
    pipeline = str(pipeline or "").strip().lower()
    if pipeline == "branda":
        return os.environ.get("MAROON_RESEND_API_KEY", "").strip() or os.environ.get("RESEND_API_KEY", "").strip()
    if pipeline == "docsapp":
        return (
            os.environ.get("docsapp_RESEND_API_KEY", "").strip()
            or os.environ.get("OUTREACH_RESEND_API_KEY", "").strip()
            or os.environ.get("RESEND_API_KEY", "").strip()
        )
    if pipeline == "persona1":
        return os.environ.get("IMA_RESEND_API_KEY", "").strip() or os.environ.get("RESEND_API_KEY", "").strip()
    return os.environ.get("RESEND_API_KEY", "").strip()


def _domain(email: str) -> str:
    return email.rsplit("@", 1)[-1].lower() if "@" in email else ""


def _redact(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    for key in list(out):
        if any(token in key.lower() for token in ("key", "token", "secret", "pass")):
            out[key] = "[redacted]"
    return out


def _audit(event: str, row: dict[str, Any]) -> None:
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"ts": _now(), "event": event, **_redact(row)}
    with AUDIT_PATH.open("a") as handle:
        handle.write(json.dumps(payload, sort_keys=True, default=str) + "\n")


def _sent_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for log_path in (SENT_LOG, MAROON_SENT_LOG):
        if not log_path.exists():
            continue
        for line in log_path.read_text(errors="ignore").splitlines():
            try:
                row = json.loads(line)
            except Exception:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def _already_sent(email: str, idempotency_key: str = "", *, allow_recipient_repeat: bool = False) -> bool:
    email = email.lower().strip()
    for row in _sent_rows():
        if idempotency_key and str(row.get("idempotency_key") or "") == idempotency_key:
            return True
        if (
            not allow_recipient_repeat
            and email
            and str(row.get("email") or "").lower().strip() == email
            and str(row.get("source") or "").startswith("persona_mail")
        ):
            return True
    return False


def _log_sent(email: str, subject: str, provider: str, *, idempotency_key: str = "", pipeline: str = "docsapp") -> None:
    SENT_LOG.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "ts": _now(),
        "email": email,
        "subject": subject,
        "provider": provider,
        "source": "persona_mail",
        "pipeline": pipeline,
        "idempotency_key": idempotency_key,
    }
    with SENT_LOG.open("a") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")


def _docsapp_config() -> dict[str, Any]:
    from_email = os.environ.get("docsapp_FROM_EMAIL", "").strip()
    from_name = os.environ.get("docsapp_FROM_NAME", "docsapp Team").strip() or "docsapp Team"
    sender_identity_tied = not guard_brand_sender_identity("docsapp", from_email, from_name, "docsapp sender config")
    return {
        "pipeline": "docsapp",
        "enabled": _truthy_env("docsapp_SEND_ENABLED"),
        "outreach_enabled": outbound_sends_enabled("docsapp")[0],
        "from_email_set": bool(from_email),
        "from_name": from_name,
        "from_email": from_email,
        "sender_identity_tied": sender_identity_tied,
        "resend_configured": bool(_resend_key_for_pipeline("docsapp")),
        "brevo_configured": bool(os.environ.get("BREVO_API_KEY", "").strip()),
        "smtp_configured": all(
            os.environ.get(key, "").strip()
            for key in ("OUTREACH_SMTP_HOST", "OUTREACH_SMTP_USER", "OUTREACH_SMTP_PASS")
        ),
    }


def _maroon_config() -> dict[str, Any]:
    # HARD IDENTITY RAIL: BrandA fronts ONLY as user@example.com / "Operator2".
    # Single source of truth = core.maroon_send_guard (the same seam agents/maroon_outreach.py uses).
    # This _maroon_config previously defaulted from_email to user@example.com — a SECOND seam
    # the 2026-07-11 hardening missed, which would front the operator's real first name if any agent
    # sent BrandA via persona_mail. NOT env-overridable.
    from core.maroon_send_guard import resolve_sender
    from_name, from_email = resolve_sender()  # ("Operator2", "user@example.com")
    return {
        "pipeline": "branda",
        "enabled": _truthy_env("MAROON_SEND_ENABLED") or _truthy_env("OUTREACH_SEND_ENABLED"),
        "outreach_enabled": outbound_sends_enabled("maroon_sender")[0],
        "from_email_set": bool(from_email),
        "from_name": from_name,
        "from_email": from_email,
        "sender_identity_tied": True,
        "resend_configured": bool(_resend_key_for_pipeline("branda")),
        "brevo_configured": bool(os.environ.get("BREVO_API_KEY", "").strip()),
        "smtp_configured": all(
            os.environ.get(key, "").strip()
            for key in ("OUTREACH_SMTP_HOST", "OUTREACH_SMTP_USER", "OUTREACH_SMTP_PASS")
        ),
    }


def _preflight_maroon(
    to_email: str,
    subject: str,
    body: str,
    *,
    idempotency_key: str = "",
) -> tuple[bool, str, dict[str, Any]]:
    cfg = _maroon_config()
    email = str(to_email or "").strip().lower()
    reasons: list[str] = []
    if not cfg["enabled"]:
        reasons.append("BrandA/OUTREACH send switch is not enabled")
    enabled, enabled_reason = outbound_sends_enabled("maroon_sender", recipient_email=email, base=ROOT)
    if not enabled:
        reasons.append(enabled_reason)
    if not cfg["from_email_set"] or not cfg["from_email"].endswith("@branda.com"):
        reasons.append("BrandA sender must be a branda.com address")
    if not email or "@" not in email:
        reasons.append("missing valid recipient")
    if is_masked_email(email):
        reasons.append("masked recipient")
    domain = _domain(email)
    if domain in FREE_EMAIL_DOMAINS:
        reasons.append("free email domain blocked for cold outbound")
    if domain.endswith(BLOCKED_TLDS):
        reasons.append("blocked TLD")
    if not subject.strip():
        reasons.append("missing subject")
    if not body.strip():
        reasons.append("missing body")
    if _already_sent(email, idempotency_key=idempotency_key):
        reasons.append("duplicate send blocked")
    allowed, contact_reason = contact_allowed({"email": email, "brand": "maroon_standard"}, email=email, action="maroon_sender")
    if not allowed:
        reasons.append(contact_reason)
    if not cfg["resend_configured"] and not cfg["brevo_configured"] and not cfg["smtp_configured"]:
        reasons.append("no BrandA mail provider configured")
    return not reasons, "; ".join(reasons) or "ok", {"config": cfg}


def _preflight_docsapp(
    to_email: str,
    subject: str,
    body: str,
    *,
    idempotency_key: str = "",
    review_seed: bool = False,
) -> tuple[bool, str, dict[str, Any]]:
    cfg = _docsapp_config()
    email = str(to_email or "").strip().lower()
    reasons: list[str] = []
    if not cfg["enabled"]:
        reasons.append("docsapp_SEND_ENABLED is not enabled")
    enabled, enabled_reason = outbound_sends_enabled("docsapp", recipient_email=email, base=ROOT)
    if not enabled:
        reasons.append(enabled_reason)
    if not cfg["from_email_set"]:
        reasons.append("docsapp_FROM_EMAIL is not configured")
    if cfg["sender_identity_tied"]:
        reasons.append("docsapp sender appears identity-tied")
    if not email or "@" not in email:
        reasons.append("missing valid recipient")
    if is_masked_email(email):
        reasons.append("masked recipient")
    domain = _domain(email)
    if domain in FREE_EMAIL_DOMAINS:
        reasons.append("free email domain blocked for cold outbound")
    if domain.endswith(BLOCKED_TLDS):
        reasons.append("blocked TLD")
    if not subject.strip():
        reasons.append("missing subject")
    if not body.strip():
        reasons.append("missing body")
    if _already_sent(email, idempotency_key=idempotency_key, allow_recipient_repeat=review_seed):
        reasons.append("duplicate send blocked")
    compliance = evaluate_contact({"email": email}, email=email)
    audit_contact_decision(compliance, prospect={"email": email}, action="docsapp_send_preflight")
    if not compliance.get("allowed"):
        reasons.extend(str(reason) for reason in compliance.get("reasons") or [])
    if not cfg["resend_configured"] and not cfg["brevo_configured"] and not cfg["smtp_configured"]:
        reasons.append("no persona mail provider configured")
    return not reasons, "; ".join(reasons) or "ok", {"config": cfg, "compliance": compliance}


def _html_from_text(body: str, from_email: str) -> str:
    paragraphs = [p.strip() for p in str(body or "").split("\n\n") if p.strip()]
    body_html = "".join(f"<p>{html.escape(p).replace(chr(10), '<br>')}</p>" for p in paragraphs)
    return (
        '<!doctype html><html><head><meta charset="utf-8"></head>'
        '<body style="margin:0;padding:0;font-family:Arial,sans-serif;font-size:14px;line-height:1.6;color:#111;background:#fff;">'
        f'<div style="max-width:600px;padding:20px;">{body_html}'
        f'<p style="margin-top:24px;color:#111;">docsapp<br><span style="color:#555;font-size:13px;">{html.escape(from_email)}</span></p>'
        "</div></body></html>"
    )


def _html_part(body: str, subject: str, cfg: dict[str, Any]) -> str:
    """The themed HTML alternative for a send. BrandA keeps its own skin; every
    other brand resolves from the from_email domain (brand_registry -> brand_email)
    so the email matches its storefront palette. Any failure (unknown brand,
    import error) falls back to the generic docsapp skin — identical to the
    pre-theming behavior, so the text part is never affected."""
    if cfg.get("pipeline") == "branda":
        return render_maroon_email_html(body, preheader=subject)
    from_email = cfg.get("from_email", "")
    try:
        from core import brand_email as _brand_email
        from core import brand_registry as _brand_registry

        reg = _brand_registry.get_brand(_domain(from_email))
        rec = _brand_email.resolve_brand((reg or {}).get("key", "")) or _brand_email.resolve_brand(_domain(from_email))
        if rec is not None:
            # body already carries the canspam footer (postal address + opt-out);
            # render_html linkifies + themes it inline, footer block left empty.
            return _brand_email.render_html(rec, subject=subject, body=body, footer_text="")
    except Exception:
        pass
    return _html_from_text(body, from_email)



def _unsub_headers(cfg: dict[str, Any], headers: dict[str, Any] | None) -> dict[str, str]:
    """List-Unsubscribe one-click headers for an outbound. Defaults to the brand's own
    sending-address mailto + One-Click (today's behavior, unchanged when no headers passed).
    A caller MAY pass per-brand headers (e.g. a resolving https one-click URL) that override
    the defaults — but the two opt-out keys are always re-asserted so a send can NEVER drop
    below the baseline (no-downgrade)."""
    out: dict[str, str] = {
        "List-Unsubscribe": f"<mailto:{cfg['from_email']}?subject=unsubscribe>",
    }
    # Only the opt-out keys are honored from a caller — never inject From/Reply-To/etc into the
    # provider's custom-headers (Resend sets From from the top-level field; a duplicate would error).
    if headers:
        for k in ("List-Unsubscribe", "List-Unsubscribe-Post"):
            v = headers.get(k)
            if v:
                out[k] = str(v)
    # RFC 8058: One-Click is only valid when List-Unsubscribe carries an https URI (the
    # provider POSTs to it). A mailto-only target + One-Click-Post is malformed and gets
    # ignored/penalized by Gmail/Yahoo, so only assert it when an https endpoint exists.
    if "https://" in out.get("List-Unsubscribe", "") and "List-Unsubscribe-Post" not in out:
        out["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    elif "https://" not in out.get("List-Unsubscribe", ""):
        out.pop("List-Unsubscribe-Post", None)
    return out


def _send_resend(to_email: str, subject: str, body: str, cfg: dict[str, Any], html: str | None = None, headers: dict[str, Any] | None = None) -> dict[str, Any]:
    resend_key = _resend_key_for_pipeline(str(cfg.get("pipeline") or ""))
    if not resend_key:
        return {"ok": False, "provider": "resend", "error": "Resend key is not configured for this pipeline"}
    # DEAD BRANDS (PERMABLOCK) — every opsapp/opsdeck/front-counter variant is permanently
    # killed (PII hazard: the .us WHOIS publicly leaked the owner's real name + home address).
    # Nothing ever sends from them again, regardless of lane or config.
    try:
        from core.brand_email import is_dead_brand as _idb  # type: ignore
        _dead = _idb(cfg.get("from_email")) or _idb(cfg.get("brand")) or _idb(cfg.get("from_name"))
    except Exception:
        _dom = str(cfg.get("from_email") or "").rsplit("@", 1)[-1].lower()
        _dead = _dom in ("opsapp.us", "opsapp.app", "opsapp.app")
    if _dead:
        return {"ok": False, "provider": "resend", "error": "DEAD BRAND: opsapp/opsdeck/front-counter permanently disabled"}
    # Wire-level identity rail: a BrandA send is structurally forbidden from going out as anything
    # but Operator2@ (never the operator's real name), no matter what config/env drifted.
    if str(cfg.get("pipeline") or "") == "branda":
        from core.maroon_send_guard import assert_sender_is_nicole
        assert_sender_is_nicole(cfg.get("from_email"))
    _p = {
        "from": f"{cfg['from_name']} <{cfg['from_email']}>",
        "to": [to_email],
        "subject": subject,
        "text": body,
        "html": html or _html_part(body, subject, cfg),
        "headers": _unsub_headers(cfg, headers),
        # Cold-mail deliverability: tracking OFF by default. track_opens injects a 1x1 pixel
        # (defeats the pixel-free render -> spam signal); track_clicks rewrites every CTA through
        # Resend's redirect domain, which BREAKS sender==body-link AND routes all brands through
        # one shared tracking host (a cross-brand correlation tell) AND adds a penalized redirect
        # hop. A warm/analytics lane may re-enable per-cfg.
        "track_opens": bool(cfg.get("track_opens", False)),
        "track_clicks": bool(cfg.get("track_clicks", False)),
    }
    # Per-brand reply routing — CONTROLLED config only (never arbitrary caller From/Reply-To).
    #  reply_to: where replies go. Faceless lanes set this to the brand's OWN address so the
    #    operator's iCloud/name never appears on the wire; named lanes (BrandA) may use a real
    #    reply alias. bcc: a silent operator copy of every send (BrandA -> user@example.com).
    if cfg.get("reply_to"):
        _p["reply_to"] = cfg["reply_to"]
    # Operator visibility: BCC EVERY outbound to Operator's inbox so he sees everything the
    # machine sends. BCC is stripped by the mail server -> invisible to the recipient, so
    # faceless anonymity is fully preserved. Merge with any per-brand bcc; never duplicate.
    _bcc = cfg.get("bcc")
    _bccs = list(_bcc) if isinstance(_bcc, list) else ([_bcc] if _bcc else [])
    if "user@example.com" not in _bccs:
        _bccs.append("user@example.com")
    _p["bcc"] = _bccs
    payload = json.dumps(_p).encode("utf-8")
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=payload,
        headers={
            "Authorization": f"Bearer {resend_key}",
            "Content-Type": "application/json",
            # Browser UA: python-urllib default UA is CF-1010 blocked by Resend.
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/124.0.0.0 Safari/537.36",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            text = response.read().decode("utf-8", errors="replace")
            data = json.loads(text) if text else {}
            return {"ok": 200 <= int(response.status) < 300, "provider": "resend", "status": int(response.status), "message_id": data.get("id", "")}
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        return {"ok": False, "provider": "resend", "status": int(exc.code), "error": text[:1000]}
    except Exception as exc:
        return {"ok": False, "provider": "resend", "error": str(exc)}

def _send_brevo(to_email: str, subject: str, body: str, cfg: dict[str, Any], html: str | None = None, headers: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = json.dumps(
        {
            "sender": {"name": cfg["from_name"], "email": cfg["from_email"]},
            "to": [{"email": to_email}],
            "subject": subject,
            "textContent": body,
            "htmlContent": html or _html_part(body, subject, cfg),
            "headers": _unsub_headers(cfg, headers),
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        "https://api.brevo.com/v3/smtp/email",
        data=payload,
        headers={
            "accept": "application/json",
            "content-type": "application/json",
            "api-key": os.environ.get("BREVO_API_KEY", ""),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            text = response.read().decode("utf-8", errors="replace")
            data = json.loads(text) if text else {}
            return {"ok": True, "provider": "brevo", "status": int(response.status), "message_id": data.get("messageId", "")}
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        return {"ok": False, "provider": "brevo", "status": int(exc.code), "error": text[:1000]}
    except Exception as exc:
        return {"ok": False, "provider": "brevo", "error": str(exc)}


def _send_smtp(to_email: str, subject: str, body: str, cfg: dict[str, Any], html: str | None = None, headers: dict[str, Any] | None = None) -> dict[str, Any]:
    host = os.environ.get("OUTREACH_SMTP_HOST", "").strip()
    port = int(os.environ.get("OUTREACH_SMTP_PORT", "587") or "587")
    user = os.environ.get("OUTREACH_SMTP_USER", "").strip()
    password = os.environ.get("OUTREACH_SMTP_PASS", "")
    message = EmailMessage()
    message["From"] = f"{cfg['from_name']} <{cfg['from_email']}>"
    message["To"] = to_email
    message["Subject"] = subject
    for _hk, _hv in _unsub_headers(cfg, headers).items():
        message[_hk] = _hv
    message.set_content(body)
    message.add_alternative(html or _html_part(body, subject, cfg), subtype="html")
    try:
        from core.autonomous_outbound import guard_outbound_smtp_host

        if not guard_outbound_smtp_host(host, f"persona_mail.{cfg.get('pipeline', 'docsapp')}_send_email"):
            return {"ok": False, "provider": "smtp", "error": "identity-protected SMTP guard blocked host"}
        with smtplib.SMTP(host, port, timeout=25) as server:
            server.starttls(context=ssl.create_default_context())
            server.login(user, password)
            server.send_message(message)
        return {"ok": True, "provider": "smtp"}
    except Exception as exc:
        return {"ok": False, "provider": "smtp", "error": str(exc)}


class PersonaMailTool(BaseTool):
    name = "persona_mail"
    description = "Persona-owned outbound email tools for non-identity-tied revenue pipelines."

    def get_definitions(self) -> list[dict]:
        return [
            make_tool_def("docsapp_sender_status", "Show redacted docsapp persona sender readiness.", {}, []),
            make_tool_def("maroon_standard_sender_status", "Show redacted BrandA Standard sender readiness.", {}, []),
            make_tool_def(
                "docsapp_send_email",
                "Send one autonomous docsapp email through the persona-owned sender after compliance and idempotency checks.",
                {
                    "to": {"type": "string"},
                    "subject": {"type": "string"},
                    "body": {"type": "string"},
                    "body_html": {"type": "string"},
                    "headers": {"type": "object"},
                    "idempotency_key": {"type": "string"},
                    "dry_run": {"type": "boolean"},
                    "review_seed": {"type": "boolean"},
                    "intended_to": {"type": "string"},
                    "campaign_key": {"type": "string"},
                },
                ["to", "subject", "body", "idempotency_key"],
            ),
            make_tool_def(
                "maroon_standard_send_email",
                "Send one BrandA Standard email from the BrandA Standard sender after compliance and idempotency checks.",
                {
                    "to": {"type": "string"},
                    "subject": {"type": "string"},
                    "body": {"type": "string"},
                    "body_html": {"type": "string"},
                    "idempotency_key": {"type": "string"},
                    "dry_run": {"type": "boolean"},
                    "not_before": {"type": "string"},
                },
                ["to", "subject", "body", "idempotency_key"],
            ),
        ]

    async def handle(self, tool_name: str, tool_input: dict) -> str:
        if tool_name == "docsapp_sender_status":
            cfg = _docsapp_config()
            return json.dumps(
                {
                    "ok": bool(
                        cfg["enabled"]
                        and cfg["outreach_enabled"]
                        and cfg["from_email_set"]
                        and not cfg["sender_identity_tied"]
                        and (cfg["resend_configured"] or cfg["brevo_configured"] or cfg["smtp_configured"])
                    ),
                    "pipeline": "docsapp",
                    "from_email": cfg["from_email"],
                    "from_name": cfg["from_name"],
                    "resend_configured": cfg["resend_configured"],
                    "brevo_configured": cfg["brevo_configured"],
                    "smtp_configured": cfg["smtp_configured"],
                    "sender_identity_tied": cfg["sender_identity_tied"],
                },
                indent=2,
            )
        if tool_name == "maroon_standard_sender_status":
            cfg = _maroon_config()
            return json.dumps(
                {
                    "ok": bool(
                        cfg["enabled"]
                        and cfg["outreach_enabled"]
                        and cfg["from_email_set"]
                        and cfg["from_email"].endswith("@branda.com")
                        and (cfg["resend_configured"] or cfg["brevo_configured"] or cfg["smtp_configured"])
                    ),
                    "pipeline": "branda",
                    "from_email": cfg["from_email"],
                    "from_name": cfg["from_name"],
                    "resend_configured": cfg["resend_configured"],
                    "brevo_configured": cfg["brevo_configured"],
                    "smtp_configured": cfg["smtp_configured"],
                    "sender_identity_tied": cfg["sender_identity_tied"],
                },
                indent=2,
            )
        if tool_name == "maroon_standard_send_email":
            to_email = str(tool_input.get("to") or "").strip().lower()
            subject = str(tool_input.get("subject") or "").strip()
            body = str(tool_input.get("body") or "").strip()
            idempotency_key = str(tool_input.get("idempotency_key") or "").strip()
            allowed, reason, detail = _preflight_maroon(to_email, subject, body, idempotency_key=idempotency_key)
            if not allowed:
                _audit("blocked", {"pipeline": "branda", "to": to_email, "subject": subject, "reason": reason, "idempotency_key": idempotency_key})
                return f"BLOCKED: {reason}"
            cfg = detail["config"]
            provider = "resend" if cfg["resend_configured"] else ("brevo" if cfg["brevo_configured"] else "smtp")
            if bool(tool_input.get("dry_run")):
                _audit("dry_run", {"pipeline": "branda", "to": to_email, "subject": subject, "idempotency_key": idempotency_key})
                return json.dumps({"ok": True, "dry_run": True, "would_send": True, "pipeline": "branda", "to": to_email, "provider": provider}, indent=2)
            if cfg["resend_configured"]:
                result = _send_resend(to_email, subject, body, cfg)
                if not result.get("ok") and cfg["brevo_configured"]:
                    result = _send_brevo(to_email, subject, body, cfg)
                if not result.get("ok") and cfg["smtp_configured"]:
                    result = _send_smtp(to_email, subject, body, cfg)
            elif cfg["brevo_configured"]:
                result = _send_brevo(to_email, subject, body, cfg)
                if not result.get("ok") and cfg["smtp_configured"]:
                    result = _send_smtp(to_email, subject, body, cfg)
            else:
                result = _send_smtp(to_email, subject, body, cfg)
            _audit("sent" if result.get("ok") else "send_failed", {"pipeline": "branda", "to": to_email, "subject": subject, "provider": result.get("provider"), "result": result, "idempotency_key": idempotency_key})
            if not result.get("ok"):
                return f"Error: BrandA Standard send failed via {result.get('provider')}: {result.get('error') or result}"
            _log_sent(to_email, subject, str(result.get("provider") or "persona_mail"), idempotency_key=idempotency_key, pipeline="branda")
            return json.dumps({"ok": True, "sent": True, "pipeline": "branda", "to": to_email, "provider": result.get("provider")}, indent=2)

        if tool_name != "docsapp_send_email":
            return f"Error: unknown tool {tool_name}"

        to_email = str(tool_input.get("to") or "").strip().lower()
        subject = str(tool_input.get("subject") or "").strip()
        body = str(tool_input.get("body") or "").strip()
        # Optional purpose-built light-HTML alternative. None -> _html_part auto-renders from
        # text (unchanged legacy behavior). multipart/alternative (text + html) either way.
        body_html = str(tool_input.get("body_html") or "").strip() or None
        # Optional per-brand outbound headers (e.g. a resolving https List-Unsubscribe one-click
        # URL). None -> _unsub_headers keeps the brand mailto + One-Click default (unchanged).
        extra_headers = tool_input.get("headers") if isinstance(tool_input.get("headers"), dict) else None
        idempotency_key = str(tool_input.get("idempotency_key") or "").strip()
        review_seed = bool(tool_input.get("review_seed"))
        allowed, reason, detail = _preflight_docsapp(
            to_email,
            subject,
            body,
            idempotency_key=idempotency_key,
            review_seed=review_seed,
        )
        if not allowed:
            _audit("blocked", {"to": to_email, "subject": subject, "reason": reason, "idempotency_key": idempotency_key})
            return f"BLOCKED: {reason}"

        cfg = detail["config"]
        if bool(tool_input.get("dry_run")):
            _audit("dry_run", {"to": to_email, "subject": subject, "idempotency_key": idempotency_key})
            return json.dumps(
                {
                    "ok": True,
                    "dry_run": True,
                    "would_send": True,
                    "pipeline": "docsapp",
                    "to": to_email,
                    "provider": "resend" if cfg["resend_configured"] else ("brevo" if cfg["brevo_configured"] else "smtp"),
                    "review_seed": review_seed,
                },
                indent=2,
            )

        if cfg["resend_configured"]:
            result = _send_resend(to_email, subject, body, cfg, body_html, extra_headers)
            if not result.get("ok") and cfg["brevo_configured"]:
                result = _send_brevo(to_email, subject, body, cfg, body_html, extra_headers)
            if not result.get("ok") and cfg["smtp_configured"]:
                result = _send_smtp(to_email, subject, body, cfg, body_html, extra_headers)
        elif cfg["brevo_configured"]:
            result = _send_brevo(to_email, subject, body, cfg, body_html, extra_headers)
            if not result.get("ok") and cfg["smtp_configured"]:
                result = _send_smtp(to_email, subject, body, cfg, body_html, extra_headers)
        else:
            result = _send_smtp(to_email, subject, body, cfg, body_html, extra_headers)
        _audit(
            "sent" if result.get("ok") else "send_failed",
            {
                "to": to_email,
                "subject": subject,
                "provider": result.get("provider"),
                "result": result,
                "idempotency_key": idempotency_key,
                "review_seed": review_seed,
            },
        )
        if not result.get("ok"):
            return f"Error: docsapp send failed via {result.get('provider')}: {result.get('error') or result}"
        _log_sent(to_email, subject, str(result.get("provider") or "persona_mail"), idempotency_key=idempotency_key)
        return json.dumps(
            {
                "ok": True,
                "sent": True,
                "pipeline": "docsapp",
                "to": to_email,
                "provider": result.get("provider"),
                "review_seed": review_seed,
            },
            indent=2,
        )
