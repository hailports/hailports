"""Revenue Funnel — full pipeline metrics from prospect to payment.

Queryable via conversation: "what are our conversion numbers",
"how is the pipeline doing", "funnel report", etc.

Reads from 6 data sources, computes conversion rates between stages.
No external API calls. Pure local file aggregation.
"""

from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tools.base import BaseTool, make_tool_def

BASE = Path(os.path.expanduser("~/claude-stack"))
OUTREACH = BASE / "products" / "outreach"
LANDING = BASE / "products" / "landing"
HUSTLE = BASE / "data" / "hustle"
SENT_LOGS = [
    OUTREACH / "sent.jsonl",
    HUSTLE / "outreach_sent.jsonl",
    HUSTLE / "outbound_sends.jsonl",
]
AUTO_RESPONSE_TOKENS = ("automatic reply", "auto reply", "auto-reply", "auto response", "out of office", "out-of-office")
NON_ACTIONABLE_REPLY_CLASSES = {"auto_response", "bounce"}
SELF_TEST_REPLY_TOKENS = tuple(
    token.strip().lower()
    for token in os.environ.get(
        "OUTREACH_SELF_TEST_REPLY_TOKENS",
        "user@example.com,user",
    ).split(",")
    if token.strip()
)


def _read_json(path: Path) -> list | dict:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        return data
    except Exception:
        return []


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _clean_email(value: str) -> str:
    return str(value or "").strip().strip("<>,.;:").lower()


def _sent_recipient(row: dict) -> str:
    for key in ("to", "email", "destination", "recipient"):
        value = _clean_email(row.get(key, ""))
        if value:
            return value
    return ""


def _sent_sequence(row: dict, path: Path) -> str:
    explicit = str(row.get("sequence") or row.get("pipeline") or row.get("campaign") or "").strip()
    if explicit:
        return explicit
    if path == HUSTLE / "outreach_sent.jsonl":
        return "docsapp persona outbound"
    if path == HUSTLE / "outbound_sends.jsonl":
        return "Outbound revenue sends"
    return "unknown"


def _sent_date(row: dict) -> str:
    value = str(row.get("date") or row.get("ts") or row.get("sent_at") or "").strip()
    return value[:10] if value else "unknown"


def _read_sent_logs() -> list[dict]:
    rows: list[dict] = []
    seen = set()
    for path in SENT_LOGS:
        for row in _read_jsonl(path):
            out = dict(row)
            out["to"] = _sent_recipient(row)
            out["sequence"] = _sent_sequence(row, path)
            out["date"] = _sent_date(row)
            key = (out.get("to", ""), out.get("subject", ""), out.get("ts") or out.get("date", ""), str(path))
            if key in seen:
                continue
            seen.add(key)
            rows.append(out)
    return rows


def _reply_class(row: dict) -> str:
    cls = str(row.get("classification") or "unknown").strip().lower()
    text = f"{row.get('subject', '')} {row.get('body_preview', '')}".lower()
    if cls in {"neutral", "unknown"} and any(token in text for token in AUTO_RESPONSE_TOKENS):
        return "auto_response"
    return cls or "unknown"


def _reply_key(row: dict) -> tuple[str, str, str, str]:
    return (
        _clean_email(row.get("from") or row.get("reply_sender") or ""),
        str(row.get("subject") or "").strip().lower(),
        str(row.get("date") or "").strip(),
        str(row.get("source") or "").strip().lower(),
    )


def _is_self_test_reply(row: dict) -> bool:
    if not SELF_TEST_REPLY_TOKENS:
        return False
    text = " ".join(
        str(row.get(key, "") or "")
        for key in ("from", "reply_sender", "from_name", "sender", "subject", "source")
    ).lower()
    return any(token in text for token in SELF_TEST_REPLY_TOKENS)


def _read_replies() -> list[dict]:
    raw = _read_json(OUTREACH / "replies.json")
    if not isinstance(raw, list):
        return []
    out = []
    seen = set()
    for row in raw:
        if not isinstance(row, dict):
            continue
        item = dict(row)
        item["classification"] = _reply_class(item)
        key = _reply_key(item)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _pct(num: int, denom: int) -> str:
    if denom == 0:
        return "0.0%"
    return f"{num / denom * 100:.1f}%"


def compute_funnel() -> dict:
    """Compute full pipeline funnel from all data sources."""
    # 1. Prospects
    prospects = _read_json(OUTREACH / "prospects.json")
    if not isinstance(prospects, list):
        prospects = []

    total_pool = len(prospects)
    status_counts = Counter(p.get("status", "unknown") for p in prospects)

    # 2. Sent emails
    sent_rows = _read_sent_logs()
    total_sent = len(sent_rows)
    unique_recipients = len({r.get("to", "").lower() for r in sent_rows if r.get("to")})

    # sent by sequence
    seq_sends = Counter(r.get("sequence", "unknown") for r in sent_rows)

    # sent by date
    daily_sends = Counter(r.get("date", "unknown") for r in sent_rows)

    # 3. Opens (tracking pixel)
    opens = _read_jsonl(LANDING / "opens.jsonl")
    total_opens = len(opens)
    unique_opens = len({o.get("track_id", "") for o in opens})

    # match opens to sent emails via track_id
    sent_track_ids = {r.get("track_id", "") for r in sent_rows if r.get("track_id")}
    matched_opens = len({o.get("track_id", "") for o in opens if o.get("track_id") in sent_track_ids})

    # 4. Replies
    replies = _read_replies()
    reply_classes = Counter(r.get("classification", "unknown") for r in replies)
    self_test_replies = [r for r in replies if _is_self_test_reply(r)]
    external_replies = [r for r in replies if not _is_self_test_reply(r)]
    external_reply_classes = Counter(r.get("classification", "unknown") for r in external_replies)
    action_needed_replies = [
        r
        for r in external_replies
        if r.get("classification") not in NON_ACTIONABLE_REPLY_CLASSES
    ]
    pending_neutral_review = [
        r
        for r in external_replies
        if r.get("classification") in {"neutral", "unknown"}
    ]
    total_replies = len(action_needed_replies)

    # 5. Suppressions
    suppressions = _read_jsonl(OUTREACH / "suppressions.jsonl")
    total_suppressed = len(suppressions)
    suppression_reasons = Counter(s.get("reason", "unknown")[:30] for s in suppressions)

    # 6. Strategy health
    strategy = {}
    strategy_file = HUSTLE / "strategy.json"
    if strategy_file.exists():
        try:
            strategy = json.loads(strategy_file.read_text())
        except Exception:
            pass

    # 7. Gig proposals
    draft_queue = _read_json(BASE / "data" / "draft_queue.json")
    if not isinstance(draft_queue, list):
        draft_queue = []

    # 8. Auto-replies sent
    auto_replies = _read_jsonl(OUTREACH / "auto_replies.jsonl")
    auto_replies_sent = sum(1 for r in auto_replies if r.get("sent"))

    # 9. Blog/content
    published = _read_json(BASE / "products" / "content" / "published_posts.json")
    if not isinstance(published, list):
        published = []

    # Build funnel
    hot_leads = external_reply_classes.get("positive", 0)
    converted = 0  # stripe payments — future

    funnel = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "pipeline_health": strategy.get("pipeline_health", "unknown"),
        "funnel": {
            "prospect_pool": total_pool,
            "contacted": unique_recipients,
            "emails_sent": total_sent,
            "opened": unique_opens,
            "replied": total_replies,
            "detected_replies": len(replies),
            "external_detected_replies": len(external_replies),
            "pending_neutral_review": len(pending_neutral_review),
            "self_test_replies": len(self_test_replies),
            "auto_responses": reply_classes.get("auto_response", 0),
            "bounces": reply_classes.get("bounce", 0),
            "hot_leads": hot_leads,
            "converted": converted,
            "auto_replied": auto_replies_sent,
            "suppressed": total_suppressed,
        },
        "conversion_rates": {
            "pool_to_contacted": _pct(unique_recipients, total_pool),
            "contacted_to_opened": _pct(unique_opens, unique_recipients),
            "opened_to_replied": _pct(total_replies, max(unique_opens, unique_recipients)),
            "replied_to_hot": _pct(hot_leads, total_replies) if total_replies else "n/a",
            "hot_to_converted": _pct(converted, hot_leads) if hot_leads else "n/a",
        },
        "by_sequence": dict(seq_sends.most_common()),
        "by_day": dict(sorted(daily_sends.items())),
        "reply_breakdown": dict(reply_classes),
        "external_reply_breakdown": dict(external_reply_classes),
        "prospect_statuses": dict(status_counts.most_common()),
        "suppression_breakdown": dict(suppression_reasons.most_common(5)),
        "content": {
            "blog_posts_published": len(published),
            "gig_proposals_drafted": len(draft_queue),
        },
        "days_active": len(daily_sends),
        "avg_daily_sends": round(total_sent / max(len(daily_sends), 1), 1),
    }

    return funnel


def format_funnel_report(funnel: dict) -> str:
    """Format funnel data into a readable report."""
    f = funnel["funnel"]
    cr = funnel["conversion_rates"]

    lines = [
        "REVENUE FUNNEL REPORT",
        f"Generated: {funnel['generated_at'][:19]}",
        f"Pipeline Health: {funnel['pipeline_health']}/100",
        f"Days Active: {funnel['days_active']}",
        "",
        "FUNNEL",
        f"  Prospect Pool:    {f['prospect_pool']:,}",
        f"  Contacted:        {f['contacted']:,}  ({cr['pool_to_contacted']} of pool)",
        f"  Emails Sent:      {f['emails_sent']:,}",
        f"  Tracked Opens:    {f['opened']:,}  ({cr['contacted_to_opened']} of contacted; pixel-based)",
        f"  Action Needed:    {f['replied']:,}  ({f.get('pending_neutral_review', 0)} neutral review, {f['hot_leads']} hot)",
        f"  Detected Inbound: {f.get('detected_replies', f['replied']):,} total; {f.get('external_detected_replies', f['replied'])} external; {f.get('self_test_replies', 0)} self/test; {f.get('auto_responses', 0)} auto; {f.get('bounces', 0)} bounce",
        f"  Hot Leads:        {f['hot_leads']:,}  ({cr['replied_to_hot']})",
        f"  Auto-Replied:     {f.get('auto_replied', 0):,}",
        f"  Converted:        {f['converted']:,}  ({cr['hot_to_converted']})",
        f"  Suppressed:       {f['suppressed']:,}",
    ]

    if funnel.get("by_sequence"):
        lines.append("")
        lines.append("BY SEQUENCE")
        for seq, count in funnel["by_sequence"].items():
            lines.append(f"  {seq[:50]}: {count}")

    if funnel.get("by_day"):
        lines.append("")
        lines.append("BY DAY")
        for day, count in funnel["by_day"].items():
            lines.append(f"  {day}: {count} sent")

    if funnel.get("reply_breakdown"):
        lines.append("")
        lines.append("REPLIES")
        for cls, count in funnel["reply_breakdown"].items():
            lines.append(f"  {cls}: {count}")

    lines.append("")
    lines.append("CONTENT")
    lines.append(f"  Blog posts: {funnel['content']['blog_posts_published']}")
    lines.append(f"  Gig proposals: {funnel['content']['gig_proposals_drafted']}")

    return "\n".join(lines)


class RevenueFunnelTool(BaseTool):
    name = "revenue_funnel"
    description = "Revenue pipeline funnel — conversion metrics from prospect pool to payment"

    def get_definitions(self) -> list[dict]:
        return [
            make_tool_def(
                "revenue_funnel_report",
                "Get full revenue pipeline funnel report with conversion rates at every stage: "
                "prospect pool → contacted → opened → replied → hot leads → converted. "
                "Use when asked about conversion numbers, pipeline health, funnel metrics, "
                "outreach performance, or revenue status.",
                {
                    "format": {
                        "type": "string",
                        "enum": ["summary", "detailed", "json"],
                        "description": "Output format (default: summary)",
                    },
                },
                [],
            ),
        ]

    async def handle(self, tool_name: str, tool_input: dict) -> str:
        funnel = compute_funnel()
        fmt = tool_input.get("format", "summary")

        if fmt == "json":
            return json.dumps(funnel, indent=2)
        elif fmt == "detailed":
            return format_funnel_report(funnel)
        else:
            f = funnel["funnel"]
            cr = funnel["conversion_rates"]
            return (
                f"Pipeline Health: {funnel['pipeline_health']}/100 | "
                f"Days Active: {funnel['days_active']}\n"
                f"Pool: {f['prospect_pool']:,} → "
                f"Contacted: {f['contacted']:,} ({cr['pool_to_contacted']}) → "
                f"Tracked opens: {f['opened']:,} ({cr['contacted_to_opened']}; pixel-based) → "
                f"Action-needed external replies: {f['replied']:,} "
                f"({f.get('pending_neutral_review', 0)} neutral review, {f['hot_leads']} hot) → "
                f"Hot: {f['hot_leads']:,} → "
                f"Converted: {f['converted']:,}\n"
                f"Detected inbound: {f.get('detected_replies', f['replied']):,} total "
                f"({f.get('external_detected_replies', f['replied'])} external, "
                f"{f.get('self_test_replies', 0)} self/test, "
                f"{f.get('auto_responses', 0)} auto, {f.get('bounces', 0)} bounce) | "
                f"Suppressed: {f['suppressed']:,} | "
                f"Avg {funnel['avg_daily_sends']}/day\n"
                "Interpretation guard: zero tracked opens can mean tracking blocked, not guaranteed zero delivery; "
                "do not call neutral, self/test, bounce, or auto-response items real buyer replies."
            )
