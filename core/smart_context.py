from __future__ import annotations

"""Build compact business context block for chat system prompts."""

import json
import logging
import re
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
HUSTLE_DIR = BASE_DIR / "data" / "hustle"
OUTREACH_SENT = HUSTLE_DIR / "outreach_sent.jsonl"
EMAIL_ANALYTICS = HUSTLE_DIR / "email_analytics.json"
REVENUE_DASH = HUSTLE_DIR / "revenue_dashboard.json"
PROSPECTS_FILE = BASE_DIR / "products" / "outreach" / "prospects.json"
IMA_STRATEGY = HUSTLE_DIR / "strategy_overrides.json"
IMA_LOOP_STATE = HUSTLE_DIR / "ima_creator_loop_state.json"
IMA_INBOX_STATE = HUSTLE_DIR / "ima_inbox_engagement_state.json"
AI_BUDGET_STATE = BASE_DIR / "data" / "runtime" / "ai_budget_state.json"
AUTH_MAINT_STATE = HUSTLE_DIR / "auth_maintenance_state.json"
MAROON_IMPORTER_STATE = HUSTLE_DIR / "maroon_salesintel_importer_state.json"
CHANNEL_DIVERSIFIER_STATE = HUSTLE_DIR / "channel_diversifier_state.json"
redacted_CONTEXT = BASE_DIR / "data" / "redacted_salesforce_context.json"


def _safe_json(path: Path) -> dict | list | None:
    try:
        if path.exists():
            return json.loads(path.read_text())
    except Exception:
        pass
    return None


def _count_today_outreach() -> int:
    today = datetime.now().strftime("%Y-%m-%d")
    count = 0
    try:
        if OUTREACH_SENT.exists():
            for line in OUTREACH_SENT.open():
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    if row.get("date", "") == today:
                        count += 1
                except Exception:
                    continue
    except Exception:
        pass
    return count


def build_chat_context(user_id: str, query_text: str = "") -> str:
    """Return a compact text block with current business state."""
    parts: list[str] = []
    lowered = str(query_text or "").lower()
    is_week_brief = bool(re.search(r"\b(my\s+week|this\s+week|next\s+week|the\s+week|week|missed at work|what did i miss|catch me up)\b", lowered))

    # Context brain snapshot
    try:
        from core.context_brain import get_now_snapshot
        snap = get_now_snapshot(user_id)
        if snap:
            phase = snap.get("day_phase", "")
            guidance = snap.get("phase_guidance", "")
            if phase:
                parts.append(f"Day phase: {phase}. {guidance}")

            workos = snap.get("workos", {})
            if workos:
                active = workos.get("active_items", 0)
                tickets = workos.get("open_tickets", 0)
                if active or tickets:
                    parts.append(f"WorkOS: {active} active items, {tickets} open tickets")

            cal = snap.get("calendar", [])
            if cal and isinstance(cal, list) and cal:
                c = cal[0] if isinstance(cal[0], dict) else {}
                today_events = c.get("today", "")
                tomorrow_events = c.get("tomorrow", "")
                upcoming_events = c.get("upcoming", "")
                if today_events and today_events != "unavailable":
                    summary = str(today_events)[:220]
                    parts.append(f"Calendar today: {summary}")
                if is_week_brief and tomorrow_events and tomorrow_events != "unavailable":
                    summary = str(tomorrow_events)[:220]
                    parts.append(f"Calendar tomorrow: {summary}")
                if is_week_brief and upcoming_events and upcoming_events != "unavailable":
                    summary = str(upcoming_events)[:260]
                    parts.append(f"Calendar this week: {summary}")
            if is_week_brief:
                upcoming = snap.get("calendar_upcoming", "")
                if upcoming and upcoming != "unavailable":
                    parts.append(f"Calendar this week: {str(upcoming)[:320]}")
            if is_week_brief and workos:
                top_items = workos.get("top_items", []) if isinstance(workos.get("top_items"), list) else []
                if top_items:
                    focused = ", ".join(
                        str(item.get("subject") or "")[:90]
                        for item in top_items[:4]
                        if isinstance(item, dict) and item.get("subject")
                    )
                    if focused:
                        parts.append(f"WorkOS top items this week: {focused}")
    except Exception as e:
        log.debug("context_brain failed: %s", e)

    # CompanyA work context
    CompanyA = _safe_json(redacted_CONTEXT)
    if isinstance(CompanyA, dict):
        org = CompanyA.get("organization") or {}
        principles = CompanyA.get("salesforce_operating_principles") or []
        focus_areas = CompanyA.get("focus_areas") or []
        fa_labels = [f.get("label", "") for f in focus_areas if isinstance(f, dict) and f.get("label")]
        parts.append(
            "CompanyA work context: "
            f"org={org.get('name')}; "
            f"segments={', '.join(org.get('business_segments') or [])}; "
            f"focus_areas={', '.join(fa_labels[:4])}; "
            f"operating_principle={principles[0] if principles else 'n/a'}"
        )

    if is_week_brief:
        try:
            from tools.littlebird_local import _reconcile_meetings_with_calendar

            meeting_blob = _reconcile_meetings_with_calendar("Operator")
            meeting_lines = [line.strip() for line in str(meeting_blob).splitlines() if line.strip() and "|" in line]
            if meeting_lines:
                highlights: list[str] = []
                for line in meeting_lines[:4]:
                    parts_line = [p.strip() for p in line.split("|")]
                    if len(parts_line) >= 2:
                        when = parts_line[0]
                        subject = parts_line[1]
                        org_name = parts_line[2] if len(parts_line) > 2 else ""
                        text = " - ".join(piece for piece in (subject, org_name, when) if piece)
                        text = text[:160]
                        if text:
                            highlights.append(text)
                if highlights:
                    parts.append("LittleBird meeting notes available for: " + "; ".join(highlights[:3]))
                    remaining = max(len(meeting_lines) - 3, 0)
                    if remaining:
                        parts.append(f"{remaining} additional meetings exist in LittleBird if you want a deeper summary.")
        except Exception as e:
            log.debug("littlebird weekly context failed: %s", e)

    # Revenue dashboard
    dash = _safe_json(REVENUE_DASH)
    if isinstance(dash, dict):
        today = dash.get("today", {})
        if today:
            sent = today.get("emails_sent", 0)
            opened = today.get("emails_opened", 0)
            clicked = today.get("emails_clicked", 0)
            replies = today.get("replies_positive", 0)
            revenue = today.get("total_revenue", "$0.00")
            parts.append(
                f"Revenue today: {sent} emails sent, {opened} opened, "
                f"{clicked} clicked, {replies} replies, {revenue} revenue"
            )
        funnel = dash.get("funnel", {})
        if funnel:
            parts.append(
                f"Funnel: {funnel.get('prospects', 0)} prospects, "
                f"{funnel.get('emailed', 0)} emailed, "
                f"{funnel.get('opened', 0)} opened, "
                f"{funnel.get('replied_positive', 0)} replied"
            )
        hot = dash.get("hot_leads", [])
        if hot:
            names = [f"{h.get('name', '?')} ({h.get('action', '?')})" for h in hot[:3]]
            parts.append(f"Hot leads: {', '.join(names)}")

    # Prospect + product counts
    prospects = _safe_json(PROSPECTS_FILE)
    if isinstance(prospects, list):
        BrandA = [p for p in prospects if isinstance(p, dict) and p.get("brand") == "maroon_standard"]
        maroon_sendable = [
            p for p in BrandA
            if p.get("status") in {"active", "new", "queued", "probing", "role_address"}
            and p.get("email")
            and "*" not in str(p.get("email"))
        ]
        docsapp = [p for p in prospects if isinstance(p, dict) and p.get("brand") != "maroon_standard"]
        parts.append(
            f"Total prospects in CRM: {len(prospects)} "
            f"(BrandA Standard {len(BrandA)} total/{len(maroon_sendable)} sendable, docsapp {len(docsapp)} total)"
        )

    # Emails sent today from outreach log
    today_sent = _count_today_outreach()
    if today_sent:
        parts.append(f"Outreach emails sent today (log): {today_sent}")

    # Agent health
    health = _safe_json(HUSTLE_DIR / "revenue_health.json")
    if isinstance(health, dict):
        summary = health.get("summary", {})
        total = summary.get("total", 0)
        healthy = summary.get("healthy", 0)
        dead = summary.get("dead", 0)
        if total:
            parts.append(f"Agents: {healthy}/{total} healthy, {dead} dead")

    # persona1 creator/growth machine
    strategy_blob = _safe_json(IMA_STRATEGY)
    ima_strategy = strategy_blob.get("persona1") if isinstance(strategy_blob, dict) else {}
    loop = _safe_json(IMA_LOOP_STATE)
    inbox = _safe_json(IMA_INBOX_STATE)
    if isinstance(ima_strategy, dict) and ima_strategy:
        growth = ima_strategy.get("growth") if isinstance(ima_strategy.get("growth"), dict) else {}
        platforms = growth.get("platforms") or ["tiktok", "instagram", "x"]
        parts.append(
            "persona1 strategy: "
            f"feed_posts_enabled={ima_strategy.get('feed_posts_enabled')}; "
            f"stories_only={ima_strategy.get('stories_only')}; "
            f"growth_platforms={', '.join(map(str, platforms))}; "
            f"positioning={str(ima_strategy.get('positioning') or '')[:180]}"
        )
    if isinstance(loop, dict) and loop:
        facts = loop.get("facts") if isinstance(loop.get("facts"), dict) else {}
        parts.append(
            "persona1 loop: "
            f"selected={loop.get('selected')}; "
            f"feed_posts_today={(facts.get('daily_counts') or {}).get('feed_posts')}; "
            f"inbox_replies_today={(facts.get('daily_counts') or {}).get('inbox_replies')}; "
            f"tiktok_cooldown={facts.get('tiktok_cooldown') or 'none'}"
        )
    if isinstance(inbox, dict) and inbox:
        parts.append(
            "persona1 inbox: "
            f"sent_count={inbox.get('sent_count')}; "
            f"daily_cap={inbox.get('daily_cap')}; "
            f"auth_blocked={inbox.get('auth_blocked_channels') or []}"
        )
    budget = _safe_json(AI_BUDGET_STATE)
    if isinstance(budget, dict) and budget:
        parts.append(
            "AI budget: "
            f"monthly_target=${budget.get('budget_usd')}; "
            f"spent=${budget.get('spent_usd')}; "
            f"remaining=${budget.get('remaining_usd')}"
        )

    auth_state = _safe_json(AUTH_MAINT_STATE)
    if isinstance(auth_state, dict) and auth_state:
        summary = auth_state.get("summary") if isinstance(auth_state.get("summary"), dict) else {}
        blocked = []
        for platform, row in (auth_state.get("browser_sessions") or {}).items():
            if isinstance(row, dict) and not row.get("ok") and row.get("configured") is not False:
                blocked.append(str(platform))
        parts.append(
            "Auth maintenance: "
            f"ok={summary.get('ok_count')}/{summary.get('configured_count')}; "
            f"human_required={summary.get('human_required_count')}; "
            f"browser_blocked={blocked[:6]}"
        )

    maroon_import = _safe_json(MAROON_IMPORTER_STATE)
    if isinstance(maroon_import, dict) and maroon_import:
        parts.append(
            "BrandA SalesIntel: "
            f"salesintel_total={maroon_import.get('salesintel_total')}; "
            f"eligible={maroon_import.get('eligible_total')}; "
            f"added={maroon_import.get('added')}; "
            f"would_add={maroon_import.get('would_add')}"
        )

    channel_state = _safe_json(CHANNEL_DIVERSIFIER_STATE)
    if isinstance(channel_state, dict) and channel_state:
        missing = channel_state.get("missing_owned_content") or []
        needs_video = channel_state.get("owned_needing_video") or []
        parts.append(
            "Owned channel lanes: "
            f"missing_content={missing}; "
            f"needs_video={needs_video}"
        )

    if not parts:
        return ""

    return "CURRENT BUSINESS STATE:\n" + "\n".join(f"- {p}" for p in parts)


def build_week_brief(user_id: str = "Operator") -> str:
    """Return a week-aware context blob for the model, not a fixed response."""
    return build_chat_context(user_id, "what did i miss at work this week")
