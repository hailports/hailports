from __future__ import annotations

"""Bounded autonomy policy for revenue agents.

This module keeps the revenue stack aggressive without making every caller
invent its own interpretation of spend, send-volume, and escalation limits.
It intentionally stores no secrets.
"""

import json
import os
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ROOT = Path(os.environ.get("CLAUDE_STACK_DIR", Path(__file__).resolve().parents[1]))
POLICY_PATH = ROOT / "data" / "hustle" / "revenue_operating_policy.json"
SPEND_LEDGER = ROOT / "data" / "hustle" / "revenue_spend_ledger.jsonl"
REVENUE_KILL_SWITCH_PATH = ROOT / "data" / "runtime" / "revenue_kill_switch.json"
OUTBOUND_PAUSE_PATH = ROOT / "data" / "runtime" / "outbound_sends_paused.json"
KILL_SWITCH_PHRASE = "BROKE."

TRUE_VALUES = {"1", "true", "yes", "y", "on", "enabled"}

DEFAULT_POLICY: dict[str, Any] = {
    "mode": "bounded_autonomous_revenue",
    "autonomy": {
        "enabled": False,
        "operator": "Operator",
        "stop_if_human_detection_risk": True,
    },
    "budget": {
        "rolling_24h_spend_cap_usd": 25.0,
        "weekly_spend_cap_usd": 175.0,
        "spend_ledger": "data/hustle/revenue_spend_ledger.jsonl",
        "paid_actions_require_budget_check": True,
    },
    "offer_policy": {
        "default_contact_email": "user@example.com",
        "website": "https://docsapp.dev",
        "approved_categories": [
            "managed_salesforce_administration",
            "salesforce_security_audit",
            "org_health_scan",
            "remediation_sprint",
            "continuous_monitoring",
            "digital_downloads",
            "consultation",
        ],
        "price_bounds_usd": {
            "free_audit_or_snapshot": {"min": 0, "max": 0},
            "one_dollar_teardown": {"min": 1, "max": 1},
            "digital_download": {"min": 5, "max": 99},
            "consultation": {"min": 99, "max": 399},
            "diagnostic_scan": {"min": 49, "max": 750},
            "remediation_sprint": {"min": 750, "max": 2500},
            "monthly_retainer": {"min": 150, "max": 2000},
        },
        "posture": "aggressive_direct_response",
        "copy_boundaries": [
            "optimize for first sale and fast commitment",
            "free, one-dollar, fixed-price, and promotional tests are allowed",
            "do not make false claims, impersonate Operator, or promise unsupported outcomes",
        ],
    },
    "reputation_caps": {
        "email": {
            "daily_send_cap": 180,
            "hourly_send_cap": 8,
            "weekly_send_cap": 900,
            "same_domain_daily_cap": 2,
            "bounce_rate_pause_threshold": 0.08,
            "complaint_pause_threshold": 1,
            "unsubscribe_pause_threshold": 3,
            "sent_logs": [
                "products/outreach/sent.jsonl",
                "data/hustle/outbound_sends.jsonl",
            ],
        },
        "social": {
            "linkedin_daily_connection_cap": 10,
            "linkedin_daily_message_cap": 15,
            "reddit_daily_comment_cap": 12,
            "tiktok_daily_post_cap": 2,
        },
    },
    "channel_caps": {
        "email_docsapp": {
            "daily_cap": "adaptive",
            "max_daily_cap": 500,
            "hourly_cap": 50,
            "weekly_cap": 2500,
            "same_domain_daily_cap": 2,
            "components": ["outreach_cron", "outreach_engine", "direct_sender", "sf_client_blitz", "hunter", "docsapp"],
            "sent_logs": ["data/hustle/outreach_sent.jsonl"],
        },
        "email_maroon_standard": {
            "daily_cap": "adaptive",
            "max_daily_cap": 300,
            "hourly_cap": 35,
            "weekly_cap": 1500,
            "same_domain_daily_cap": 2,
            "approval_required": True,
            "components": ["maroon_sender", "maroon_standard"],
            "sent_logs": ["products/outreach/sent.jsonl"],
            "match": {"sender": "user@example.com"},
        },
        "email_warm_followup": {
            "daily_cap": "adaptive",
            "max_daily_cap": 200,
            "hourly_cap": 25,
            "weekly_cap": 1000,
            "same_domain_daily_cap": 2,
            "components": ["warm_lead_closer", "nurture_sequence"],
            "sent_logs": ["data/hustle/outbound_sends.jsonl", "products/outreach/sent.jsonl"],
        },
        "social_linkedin": {
            "daily_connection_cap": 10,
            "daily_message_cap": 15,
            "components": ["linkedin_outreach", "linkedin_sender", "linkedin_cold_prospector"],
        },
        "social_reddit": {
            "daily_comment_cap": 12,
            "components": ["reddit_engagement", "reddit_poster"],
        },
        "social_tiktok": {
            "daily_post_cap": 2,
            "daily_reply_cap": 36,
            "components": ["tiktok_engagement", "ima_viral_reply", "ima_tiktok_commenter"],
        },
        "marketplace_proposals": {
            "daily_proposal_cap": 40,
            "components": ["proposal_submitter", "upwork_submitter", "bid_submitter", "multi_submitter"],
        },
    },
    "human_escalation": {
        "stop_and_ask_on": [
            "login_or_2fa_checkpoint",
            "platform_warning_or_restriction",
            "direct_accusation_of_automation",
            "legal_threat",
            "refund_or_chargeback",
            "contract_terms",
            "custom_scope_above_500_usd",
            "client_system_destructive_change",
            "client_requests_data_export_or_credentials",
        ]
    },
    "blocked": [
        "identity_deception",
        "ban_evasion",
        "captcha_or_verification_bypass",
        "payment_or_auth_abuse",
        "illegal_scraping",
        "unsolicited_credential_collection",
        "destructive_client_system_changes_without_written_approval",
    ],
}


def _base(base: Path | None = None) -> Path:
    return Path(base or ROOT)


def _policy_path(base: Path | None = None) -> Path:
    return _base(base) / "data" / "hustle" / "revenue_operating_policy.json"


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in TRUE_VALUES


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def _read_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text())
    except Exception:
        return default
    return default


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for line in path.read_text(errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def load_policy(base: Path | None = None) -> dict[str, Any]:
    """Return the current bounded-autonomy policy with env overrides applied."""
    policy = _deep_merge(DEFAULT_POLICY, {})
    raw = _read_json(_policy_path(base), {})
    if isinstance(raw, dict):
        policy = _deep_merge(policy, raw)

    if "REVENUE_AUTONOMY_ENABLED" in os.environ:
        policy.setdefault("autonomy", {})["enabled"] = _truthy(os.environ.get("REVENUE_AUTONOMY_ENABLED"))
    if os.environ.get("REVENUE_WEEKLY_SPEND_CAP_USD"):
        try:
            policy.setdefault("budget", {})["weekly_spend_cap_usd"] = float(os.environ["REVENUE_WEEKLY_SPEND_CAP_USD"])
        except ValueError:
            pass
    if os.environ.get("REVENUE_24H_SPEND_CAP_USD"):
        try:
            policy.setdefault("budget", {})["rolling_24h_spend_cap_usd"] = float(os.environ["REVENUE_24H_SPEND_CAP_USD"])
        except ValueError:
            pass
    if os.environ.get("REVENUE_EMAIL_DAILY_CAP"):
        try:
            policy.setdefault("reputation_caps", {}).setdefault("email", {})["daily_send_cap"] = int(os.environ["REVENUE_EMAIL_DAILY_CAP"])
        except ValueError:
            pass
    if os.environ.get("REVENUE_EMAIL_HOURLY_CAP"):
        try:
            policy.setdefault("reputation_caps", {}).setdefault("email", {})["hourly_send_cap"] = int(os.environ["REVENUE_EMAIL_HOURLY_CAP"])
        except ValueError:
            pass

    return policy


def autonomous_revenue_enabled(base: Path | None = None) -> bool:
    return bool(load_policy(base).get("autonomy", {}).get("enabled"))


def is_revenue_kill_switch_command(text: str) -> bool:
    return str(text or "").strip().upper() == KILL_SWITCH_PHRASE


def _runtime_path(base: Path | None, name: str) -> Path:
    return _base(base) / "data" / "runtime" / name


def revenue_kill_switch_active(base: Path | None = None) -> tuple[bool, str]:
    path = _runtime_path(base, "revenue_kill_switch.json")
    payload = _read_json(path, {})
    if isinstance(payload, dict) and bool(payload.get("active")):
        return True, str(payload.get("reason") or "BROKE kill switch active")
    return False, ""


def activate_revenue_kill_switch(*, triggered_by: str = "", frontend: str = "", base: Path | None = None) -> dict[str, Any]:
    """Pause autonomous revenue/outbound activity without shutting down chat."""
    root = _base(base)
    now = datetime.now(timezone.utc).isoformat()
    state = {
        "active": True,
        "phrase": KILL_SWITCH_PHRASE,
        "activated_at": now,
        "triggered_by": triggered_by,
        "frontend": frontend,
        "reason": "BROKE kill switch active: autonomous revenue/outbound sends are paused until manually cleared.",
    }
    kill_path = root / "data" / "runtime" / "revenue_kill_switch.json"
    pause_path = root / "data" / "runtime" / "outbound_sends_paused.json"
    kill_path.parent.mkdir(parents=True, exist_ok=True)
    kill_path.write_text(json.dumps(state, indent=2, sort_keys=True))
    pause_path.write_text(json.dumps({
        "paused": True,
        "reason": state["reason"],
        "source": "revenue_kill_switch",
        "phrase": KILL_SWITCH_PHRASE,
        "activated_at": now,
        "triggered_by": triggered_by,
        "frontend": frontend,
    }, indent=2, sort_keys=True))
    return {"ok": True, "active": True, "message": state["reason"], "kill_path": str(kill_path), "pause_path": str(pause_path)}


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        if len(text) == 10 and text[4] == "-" and text[7] == "-":
            return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def _row_time(row: dict[str, Any]) -> datetime | None:
    for key in ("ts", "timestamp", "sent_at", "created_at", "date"):
        parsed = _parse_time(row.get(key))
        if parsed:
            return parsed
    return None


def _send_log_paths(policy: dict[str, Any], base: Path | None = None) -> list[Path]:
    email_caps = policy.get("reputation_caps", {}).get("email", {})
    raw_paths = email_caps.get("sent_logs") if isinstance(email_caps, dict) else None
    paths = raw_paths if isinstance(raw_paths, list) and raw_paths else DEFAULT_POLICY["reputation_caps"]["email"]["sent_logs"]
    return [(_base(base) / str(path)) for path in paths]


def _component_key(value: str | None) -> str:
    return str(value or "").strip().lower().replace("-", "_")


def _channel_caps(policy: dict[str, Any]) -> dict[str, Any]:
    caps = policy.get("channel_caps")
    return caps if isinstance(caps, dict) else {}


def channel_for_component(component: str | None, policy: dict[str, Any] | None = None) -> str:
    """Return the configured outbound channel for a component.

    Caps are intentionally per channel. For example, docsapp email and BrandA
    Standard email can each have their own daily target instead of consuming a
    single shared "email" counter.
    """
    policy = policy or load_policy()
    component_key = _component_key(component)
    if not component_key:
        return ""
    for channel, cfg in _channel_caps(policy).items():
        if not isinstance(cfg, dict):
            continue
        components = cfg.get("components")
        if not isinstance(components, list):
            continue
        if component_key in {_component_key(str(item)) for item in components}:
            return str(channel)
    if "BrandA" in component_key:
        return "email_maroon_standard"
    if any(token in component_key for token in ("outreach", "docsapp", "direct_sender", "hunter")):
        return "email_docsapp"
    if any(token in component_key for token in ("warm", "nurture", "followup")):
        return "email_warm_followup"
    return ""


def _channel_email_caps(policy: dict[str, Any], channel: str) -> dict[str, Any]:
    cfg = _channel_caps(policy).get(channel)
    if not isinstance(cfg, dict):
        return {}
    if not channel.startswith("email_"):
        return {}
    return cfg


def _channel_send_log_paths(policy: dict[str, Any], channel: str, base: Path | None = None) -> list[Path]:
    cfg = _channel_email_caps(policy, channel)
    raw_paths = cfg.get("sent_logs") if isinstance(cfg, dict) else None
    if isinstance(raw_paths, list) and raw_paths:
        return [(_base(base) / str(path)) for path in raw_paths]
    return _send_log_paths(policy, base)


def _row_matches_channel(row: dict[str, Any], cfg: dict[str, Any]) -> bool:
    match = cfg.get("match")
    if not isinstance(match, dict) or not match:
        return True
    for key, expected in match.items():
        if str(row.get(str(key)) or "").strip().lower() != str(expected or "").strip().lower():
            return False
    return True


def _cap_value(caps: dict[str, Any], *keys: str) -> int:
    for key in keys:
        try:
            value = int(caps.get(key, 0) or 0)
        except Exception:
            value = 0
        if value > 0:
            return value
    return 0


def _str_token(value: Any) -> str:
    return str(value or "").strip().lower()


def _is_adaptive_cap(value: Any) -> bool:
    return _str_token(value) in {"adaptive", "max_without_flags", "as_much_as_safe", "as_much_as_possible"}


def _reply_class_counts(base: Path | None = None, now: datetime | None = None, days: int = 7) -> dict[str, int]:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cutoff = now - timedelta(days=days)
    rows = _read_json(_base(base) / "products" / "outreach" / "replies.json", [])
    counts: dict[str, int] = {}
    if not isinstance(rows, list):
        return counts
    for row in rows:
        if not isinstance(row, dict):
            continue
        when = _row_time(row)
        if when and when < cutoff:
            continue
        cls = _str_token(row.get("classification") or "unknown") or "unknown"
        counts[cls] = counts.get(cls, 0) + 1
    return counts


def deliverability_risk(base: Path | None = None, now: datetime | None = None) -> dict[str, Any]:
    """Return coarse send-risk signals from local reply and send ledgers.

    Opens are intentionally not a hard gate because tracking pixels can be
    blocked. Bounces, complaints, and unsubscribes are hard reputation signals.
    """
    policy = load_policy(base)
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    sent_week = sent_counts(base=base, now=now).get("week", 0)
    reply_counts = _reply_class_counts(base=base, now=now, days=7)
    bounces = int(reply_counts.get("bounce", 0) or 0)
    unsubscribes = int(reply_counts.get("unsubscribe", 0) or 0)
    complaints = int(reply_counts.get("complaint", 0) or 0)
    email_caps = policy.get("reputation_caps", {}).get("email", {})
    bounce_rate = (bounces / sent_week) if sent_week > 0 else 0.0
    bounce_threshold = float(email_caps.get("bounce_rate_pause_threshold") or 0.08)
    complaint_threshold = int(email_caps.get("complaint_pause_threshold") or 1)
    unsubscribe_threshold = int(email_caps.get("unsubscribe_pause_threshold") or 3)
    if complaints >= complaint_threshold:
        level = "pause"
        reason = f"complaints {complaints}/{complaint_threshold}"
    elif unsubscribes >= unsubscribe_threshold:
        level = "pause"
        reason = f"unsubscribes {unsubscribes}/{unsubscribe_threshold}"
    elif sent_week > 0 and bounce_rate >= bounce_threshold:
        level = "pause"
        reason = f"bounce rate {bounce_rate:.1%} >= {bounce_threshold:.1%}"
    elif sent_week > 0 and bounce_rate >= max(0.03, bounce_threshold / 2):
        level = "caution"
        reason = f"bounce rate {bounce_rate:.1%}"
    else:
        level = "clear"
        reason = "ok"
    return {
        "level": level,
        "reason": reason,
        "sent_week": sent_week,
        "bounce_rate": round(bounce_rate, 4),
        "bounces": bounces,
        "unsubscribes": unsubscribes,
        "complaints": complaints,
    }


def adaptive_channel_daily_cap(
    channel: str,
    *,
    provider_capacity: int = 0,
    base: Path | None = None,
    now: datetime | None = None,
) -> int:
    """Compute the daily send cap for a channel in max-safe mode."""
    policy = load_policy(base)
    cfg = _channel_email_caps(policy, channel)
    if not cfg:
        return 0
    raw_cap = cfg.get("daily_cap", cfg.get("daily_send_cap", 0))
    if not _is_adaptive_cap(raw_cap):
        return _cap_value(cfg, "daily_cap", "daily_send_cap")

    max_cap = _cap_value(cfg, "max_daily_cap", "max_daily_send_cap")
    if max_cap <= 0:
        max_cap = int(provider_capacity or 0)
    if provider_capacity > 0:
        max_cap = min(max_cap, int(provider_capacity))
    if max_cap <= 0:
        return 0

    risk = deliverability_risk(base=base, now=now)
    if risk["level"] == "pause":
        return 0
    if risk["level"] == "caution":
        return min(max_cap, max(1, max(25, int(max_cap * 0.35))))
    return max_cap


def sent_counts(
    *,
    base: Path | None = None,
    now: datetime | None = None,
    recipient_domain: str = "",
    component: str | None = None,
    channel: str = "",
) -> dict[str, int]:
    """Count recent outbound sends from local logs.

    The count is global by default because domain reputation is shared across
    campaigns. If a recipient domain is supplied, same-domain daily count is
    included for per-domain throttling.
    """
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    policy = load_policy(base)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    hour_start = now - timedelta(hours=1)
    week_start = now - timedelta(days=7)
    recipient_domain = recipient_domain.lower().lstrip("@")
    counts = {"hour": 0, "day": 0, "week": 0, "same_domain_day": 0}
    channel = channel or channel_for_component(component, policy)
    channel_cfg = _channel_email_caps(policy, channel)
    paths = _channel_send_log_paths(policy, channel, base) if channel_cfg else _send_log_paths(policy, base)

    for path in paths:
        for row in _read_jsonl(path):
            if channel_cfg and not _row_matches_channel(row, channel_cfg):
                continue
            sent_at = _row_time(row)
            if not sent_at:
                continue
            if sent_at >= hour_start:
                counts["hour"] += 1
            if sent_at >= today_start:
                counts["day"] += 1
                if recipient_domain:
                    to = str(row.get("to") or row.get("email") or row.get("destination") or "").lower()
                    domain = to.rsplit("@", 1)[-1] if "@" in to else str(row.get("domain") or "").lower()
                    if domain == recipient_domain:
                        counts["same_domain_day"] += 1
            if sent_at >= week_start:
                counts["week"] += 1
    if channel_cfg and recipient_domain:
        # Same-domain risk is shared even when volume caps are per channel.
        global_counts = sent_counts(base=base, now=now, recipient_domain=recipient_domain)
        counts["same_domain_day"] = global_counts["same_domain_day"]
    return counts


def reputation_send_decision(
    component: str | None = None,
    *,
    base: Path | None = None,
    now: datetime | None = None,
    recipient_email: str = "",
) -> dict[str, Any]:
    """Return whether another outbound send is allowed under reputation caps."""
    policy = load_policy(base)
    kill_active, kill_reason = revenue_kill_switch_active(base)
    if kill_active:
        return {
            "allowed": False,
            "reason": kill_reason,
            "component": component or "",
            "channel": channel_for_component(component, policy),
            "counts": {},
            "caps": {},
        }
    if not bool(policy.get("autonomy", {}).get("enabled")):
        return {"allowed": True, "reason": "autonomy disabled; only base send switch applies", "counts": {}, "caps": {}}

    channel = channel_for_component(component, policy)
    channel_caps = _channel_email_caps(policy, channel)
    caps = channel_caps or policy.get("reputation_caps", {}).get("email", {})
    recipient_domain = recipient_email.rsplit("@", 1)[-1].lower() if "@" in recipient_email else ""
    counts = sent_counts(base=base, now=now, recipient_domain=recipient_domain, component=component, channel=channel)
    day_cap = adaptive_channel_daily_cap(channel, base=base, now=now) if channel_caps else _cap_value(caps, "daily_cap", "daily_send_cap")
    if channel_caps and _is_adaptive_cap(channel_caps.get("daily_cap", channel_caps.get("daily_send_cap"))) and day_cap <= 0:
        risk = deliverability_risk(base=base, now=now)
        return {
            "allowed": False,
            "reason": f"adaptive email cap paused: {risk.get('reason') or 'no safe capacity'}",
            "component": component or "",
            "channel": channel,
            "scope": "per_channel",
            "counts": counts,
            "caps": caps,
        }
    checks = (
        ("hour", _cap_value(caps, "hourly_cap", "hourly_send_cap"), "hourly email cap reached"),
        ("day", day_cap, "daily email cap reached"),
        ("week", _cap_value(caps, "weekly_cap", "weekly_send_cap"), "weekly email cap reached"),
        ("same_domain_day", _cap_value(caps, "same_domain_daily_cap"), "same-domain daily cap reached"),
    )
    for key, cap, reason in checks:
        if cap > 0 and counts.get(key, 0) >= cap:
            return {
                "allowed": False,
                "reason": f"{reason}: {counts.get(key, 0)}/{cap}",
                "component": component or "",
                "channel": channel,
                "scope": "per_channel" if channel_caps else "global_email",
                "counts": counts,
                "caps": caps,
            }
    return {
        "allowed": True,
        "reason": "ok",
        "component": component or "",
        "channel": channel,
        "scope": "per_channel" if channel_caps else "global_email",
        "counts": counts,
        "caps": caps,
    }


def _spend_ledger_path(policy: dict[str, Any], base: Path | None = None) -> Path:
    configured = str(policy.get("budget", {}).get("spend_ledger") or "data/hustle/revenue_spend_ledger.jsonl")
    return _base(base) / configured


def spend_decision(amount_usd: float, *, base: Path | None = None, now: datetime | None = None) -> dict[str, Any]:
    policy = load_policy(base)
    budget = policy.get("budget", {}) if isinstance(policy.get("budget"), dict) else {}
    cap_24h = float(budget.get("rolling_24h_spend_cap_usd") or 0.0)
    cap_week = float(budget.get("weekly_spend_cap_usd") or 0.0)
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    day_start = now - timedelta(hours=24)
    week_start = now - timedelta(days=7)
    spent_24h = 0.0
    spent_week = 0.0
    for row in _read_jsonl(_spend_ledger_path(policy, base)):
        when = _row_time(row)
        if not when:
            continue
        try:
            amount = float(row.get("amount_usd") or row.get("amount") or 0)
        except (TypeError, ValueError):
            continue
        if when >= day_start:
            spent_24h += amount
        if when >= week_start:
            spent_week += amount
    requested = float(amount_usd or 0)
    if requested < 0:
        return {
            "allowed": False,
            "reason": "negative spend is not valid",
            "spent_24h_usd": round(spent_24h, 2),
            "cap_24h_usd": cap_24h,
            "spent_week_usd": round(spent_week, 2),
            "cap_week_usd": cap_week,
            "requested_usd": requested,
        }
    projected_24h = spent_24h + requested
    projected_week = spent_week + requested
    if cap_week > 0 and projected_week > cap_week:
        allowed = False
        reason = f"weekly spend cap exceeded: ${projected_week:.2f}/${cap_week:.2f}"
    elif cap_24h > 0 and projected_24h > cap_24h:
        allowed = False
        reason = f"24h spend cap exceeded: ${projected_24h:.2f}/${cap_24h:.2f}"
    else:
        allowed = True
        reason = "ok"
    return {
        "allowed": allowed,
        "reason": reason,
        "spent_24h_usd": round(spent_24h, 2),
        "cap_24h_usd": cap_24h,
        "spent_week_usd": round(spent_week, 2),
        "cap_week_usd": cap_week,
        "requested_usd": requested,
    }


def record_spend(amount_usd: float, *, purpose: str, base: Path | None = None, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    decision = spend_decision(amount_usd, base=base)
    if not decision["allowed"]:
        return decision
    policy = load_policy(base)
    path = _spend_ledger_path(policy, base)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "amount_usd": float(amount_usd),
        "purpose": purpose,
        "metadata": metadata or {},
    }
    with path.open("a") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")
    return {**decision, "recorded": True}


def policy_snapshot(base: Path | None = None) -> dict[str, Any]:
    policy = load_policy(base)
    kill_active, kill_reason = revenue_kill_switch_active(base)
    budget = policy.get("budget", {}) if isinstance(policy.get("budget"), dict) else {}
    return {
        "enabled": bool(policy.get("autonomy", {}).get("enabled")),
        "kill_switch_active": kill_active,
        "kill_switch_reason": kill_reason,
        "rolling_24h_spend_cap_usd": float(budget.get("rolling_24h_spend_cap_usd") or 0.0),
        "weekly_spend_cap_usd": float(budget.get("weekly_spend_cap_usd") or 0.0),
        "email_caps": policy.get("reputation_caps", {}).get("email", {}),
        "social_caps": policy.get("reputation_caps", {}).get("social", {}),
        "channel_caps": policy.get("channel_caps", {}),
        "deliverability_risk": deliverability_risk(base),
        "stop_and_ask_on": policy.get("human_escalation", {}).get("stop_and_ask_on", []),
        "blocked": policy.get("blocked", []),
    }
