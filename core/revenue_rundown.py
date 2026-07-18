"""Deterministic revenue-machine status rundown.

This module intentionally avoids LLM formatting. The chat surfaces use it for
short revenue status prompts so they report grounded local state instead of
inventing tool calls or dashboard facts.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core import BASE_DIR, mountain_now


REVENUE_RE = re.compile(r"\brevenue\b", re.I)
REVENUE_STATUS_RE = re.compile(
    r"(/revenue\b|"
    r"\brevenue\s+(machine|engine|rundown|status|health|snapshot)\b|"
    r"\bhow(?:'|’)?s\s+(our\s+)?revenue\b|"
    r"\bhow\s+did\s+(our\s+)?revenue\s+do\b|"
    r"\bwhat(?:'|’)?s\s+(up|going\s+on)\s+with\s+(our\s+)?revenue\b|"
    r"\bwhats\s+(up|going\s+on)\s+with\s+(our\s+)?revenue\b|"
    r"\brevenue\s+efforts\b|"
    r"\bwhy\s+is\s+nothing\s+running\b|"
    r"\b(how\s+many|where\s+are|what(?:'|’)?s|whats).{0,50}\bsends?\b|"
    r"\bcheck\s+in\s+on\s+(our\s+)?revenue\b|"
    r"\bstate\s+of\s+the\s+union\b.{0,80}\brevenue\b|"
    r"\brevenue\b.{0,80}\bstate\s+of\s+the\s+union\b)",
    re.I,
)
RUNDOWN_ASK_RE = re.compile(r"\b(30[-\s]?(second|sec)\s+rundown|rundown|status|health|snapshot|state\s+of\s+the\s+union)\b", re.I)
STATE_OF_UNION_RE = re.compile(r"\bstate\s+of\s+the\s+union\b", re.I)
REVENUE_ACTION_RE = re.compile(
    r"("
    r"\b(revenue|money|sales|gumroad|outreach|deliverability|pipeline|funnel|persona1|docsapp|BrandA|emails?|sends?|sent|blast|posts?|posting|posted|social|content|zeely|tiktok|instagram|insta)\b.{0,140}"
    r"\b(fix|solve|unblock|suggest|recommend|next|priority|priorities|broken|blocked|issue|issues|do it|run|kick|autopilot|zap|why|nothing|stuck|send|sending|sent|blast|post|posting|posted|going\s+out)\b"
    r"|"
    r"\b(fix|solve|unblock|suggest|recommend|next|priority|priorities|broken|blocked|issue|issues|do it|run|kick|autopilot|zap|why|nothing|stuck|send|sending|sent|blast|post|posting|posted|going\s+out)\b.{0,140}"
    r"\b(revenue|money|sales|gumroad|outreach|deliverability|pipeline|funnel|persona1|docsapp|BrandA|emails?|sends?|sent|blast|posts?|posting|posted|social|content|zeely|tiktok|instagram|insta)\b"
    r"|"
    r"\[(Revenue chat context|Hustle Portal context):[\s\S]*?\].{0,400}"
    r"\b(fix|solve|unblock|suggest|recommend|next|priority|priorities|broken|blocked|issue|issues|do it|run|kick|autopilot|zap|why|nothing|stuck|send|sending|sent|blast|post|posting|posted|going\s+out)\b"
    r")",
    re.I,
)
REVENUE_DISPATCH_RE = re.compile(r"\b(fix|do it|run|kick|start|launch|dispatch|zap|unblock|blast|spin\s+up|get\s+.*spinning)\b", re.I)
REVENUE_DISCOVERY_RE = re.compile(
    r"("
    r"\b(revenue|money|sales|hustle)\b.{0,120}\b(discover|discovery|find|new|streams?|channels?|ideas?|opportunities)\b"
    r"|"
    r"\b(discover|find|new)\b.{0,120}\b(revenue|money|sales|streams?|channels?|opportunities)\b"
    r"|"
    r"\bi\s+wanna\s+hustle\b"
    r")",
    re.I,
)


def is_revenue_rundown_request(text: str) -> bool:
    """Return True for the operator's revenue-machine status phrasing."""
    value = str(text or "")
    return bool(REVENUE_STATUS_RE.search(value) or (REVENUE_RE.search(value) and RUNDOWN_ASK_RE.search(value)))


def is_revenue_action_request(text: str) -> bool:
    """Return True for revenue command-center followups and next-action asks."""
    value = str(text or "")
    if re.search(r"\[(Revenue chat context|Hustle Portal context):", value, re.I) and REVENUE_DISPATCH_RE.search(value):
        return True
    return bool(REVENUE_ACTION_RE.search(value))


def is_revenue_discovery_request(text: str) -> bool:
    """Return True for asks to find or spin up new revenue lanes."""
    return bool(REVENUE_DISCOVERY_RE.search(str(text or "")))


def build_revenue_rundown_text(base_dir: Path | str = BASE_DIR, prompt: str = "") -> str:
    """Build the formatted 30-second rundown from local snapshots."""
    snapshot = build_revenue_snapshot(Path(base_dir))
    if STATE_OF_UNION_RE.search(str(prompt or "")):
        return format_revenue_state_of_union(snapshot)
    return format_revenue_rundown(snapshot)


def build_revenue_action_text(base_dir: Path | str = BASE_DIR, prompt: str = "", dispatch: bool | None = None) -> str:
    """Build a grounded revenue command-center response, optionally dispatching bounded work."""
    base = Path(base_dir)
    should_dispatch = bool(REVENUE_DISPATCH_RE.search(str(prompt or ""))) if dispatch is None else bool(dispatch)
    dispatch_result = _run_revenue_autopilot(base) if should_dispatch else None
    snapshot = build_revenue_snapshot(base)
    autopilot = _read_json(base / "data" / "hustle" / "revenue_autopilot_state.json", {})
    return format_revenue_action_plan(snapshot, autopilot if isinstance(autopilot, dict) else {}, dispatch_result)


def build_revenue_discovery_text(base_dir: Path | str = BASE_DIR, prompt: str = "", dispatch: bool | None = None) -> str:
    """Compatibility entrypoint for OpenClaw/engine revenue-discovery routing."""
    base = Path(base_dir)
    should_dispatch = True if dispatch is None else bool(dispatch)
    action_text = build_revenue_action_text(base, prompt, dispatch=should_dispatch)
    snapshot = build_revenue_snapshot(base)
    zeely = snapshot.get("zeely") or {}
    prefix = [
        "Revenue discovery: live lanes first, new lanes second.",
        "",
    ]
    if zeely.get("configured"):
        prefix.append(_zeely_live_phrase(zeely))
        prefix.append("")
    return "\n".join(prefix + [action_text]).strip()


def _run_revenue_autopilot(base_dir: Path) -> dict[str, Any]:
    script = base_dir / "agents" / "revenue_autopilot.py"
    if not script.exists():
        return {"ok": False, "error": "revenue_autopilot.py is missing"}
    py = base_dir / ".venv" / "bin" / "python3"
    cmd = [str(py if py.exists() else sys.executable), "-u", str(script), "--once", "--dispatch", "--max-actions", "3", "--json"]
    env = dict(os.environ)
    env.setdefault("PYTHONPATH", str(base_dir))
    env.setdefault("HOME", str(Path.home()))
    try:
        proc = subprocess.run(cmd, cwd=str(base_dir), env=env, capture_output=True, text=True, timeout=130)
        if proc.returncode != 0:
            return {"ok": False, "error": (proc.stderr or proc.stdout or "").strip()[:1000]}
        try:
            return {"ok": True, "state": json.loads(proc.stdout or "{}")}
        except Exception:
            return {"ok": True, "output": (proc.stdout or "").strip()[:1000]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "revenue autopilot timed out"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def build_revenue_snapshot(base_dir: Path | str = BASE_DIR) -> dict[str, Any]:
    base = Path(base_dir)
    handoff = _read_text(base / "data" / "codex_handoffs" / "live-midnight-qa.md")
    metrics = _parse_handoff_metrics(handoff)
    pipeline = _read_pipeline(base)
    dashboard = _read_revenue_dashboard(base)
    health = _read_revenue_health(base)
    autopilot = _read_json(base / "data" / "hustle" / "revenue_autopilot_state.json", {})
    channels = _read_channels(base)
    works = _extract_bullets(handoff, "WHAT WORKS")
    blockers = _extract_bullets(handoff, "WHAT NEEDS WORK")
    priorities = _extract_bullets(handoff, "TOMORROW PRIORITIES")

    if not blockers:
        blockers = _strategy_problems(base)
    if not priorities:
        priorities = _default_priorities(metrics, pipeline)

    return {
        "as_of": _freshest_status_timestamp(dashboard, health, autopilot, pipeline, handoff),
        "metrics": metrics,
        "pipeline": pipeline,
        "autopilot": autopilot if isinstance(autopilot, dict) else {},
        "dashboard": dashboard,
        "lanes": _read_revenue_lanes(base),
        "gumroad": _read_gumroad_state(base),
        "assets": _read_content_asset_state(base),
        "zeely": _read_zeely_state(base),
        "salesintel": _read_salesintel_state(base),
        "health": health,
        "channels": channels,
        "works": works,
        "blockers": blockers,
        "priorities": priorities,
        "landing_pages_count": _count_files(base / "products" / "landing", "*.html"),
        "seo_posts_count": _count_files(base / "products" / "content" / "seo_posts", "*.html"),
        "published_posts_count": _count_json_list(base / "products" / "content" / "published_posts.json"),
        "scope_gate_active": "CompanyA Mutual-Benefit Scope Gate" in handoff,
    }


def format_revenue_state_of_union(snapshot: dict[str, Any]) -> str:
    dashboard = snapshot.get("dashboard") or {}
    today = dashboard.get("today") or {}
    lanes = snapshot.get("lanes") or {}
    gumroad = snapshot.get("gumroad") or {}
    assets = snapshot.get("assets") or {}
    salesintel = snapshot.get("salesintel") or {}
    health = snapshot.get("health") or {}
    pipeline = snapshot.get("pipeline") or {}

    as_of = _snapshot_as_of(snapshot)
    revenue = str(today.get("total_revenue") or today.get("gumroad_revenue") or "$0.00")
    if revenue in {"$0.00", "$0", "0", "0.0"}:
        revenue_sentence = "Revenue is still $0. No Gumroad sale or paid conversion is recorded in the live files."
    else:
        revenue_sentence = f"Revenue recorded in the live dashboard is {revenue}."

    persona1 = lanes.get("ima_docsapp") or {}
    BrandA = lanes.get("maroon_standard") or {}
    ima_products = _product_phrase(gumroad)
    content_phrase = _content_phrase(assets)
    health_phrase = _health_phrase(health)

    lines = [
        f"REVENUE STACK - STATE OF THE UNION ({as_of})",
        "",
        f"Here is the honest read: {revenue_sentence} The stack is not imaginary, but the bottleneck is conversion and deliverability, not infrastructure.",
        "",
        "Two lanes:",
        f"- persona1 / docsapp: {_lane_prospects(persona1)}. Status: {_lane_status(persona1, fallback='active/sendable')}. Sender: via Brevo/docsapp. Angle: AI tools, CRM coaching, and digital products for the MLM / small-business audience. Products: {ima_products}. Content: {content_phrase}.",
        f"- Operator / BrandA Standard: {_lane_prospects(BrandA)}. Status: {_lane_status(BrandA, fallback='warmup hold')}. Sender: user@example.com, held until the domain/sender is ready. Angle: Salesforce consulting, org audits, and negotiated work, not public product sales.",
        "",
        "What's working:",
    ]

    working = [
        _salesintel_phrase(salesintel),
        _brevo_phrase(today),
        _asset_working_phrase(assets),
        _gumroad_working_phrase(gumroad),
        health_phrase,
    ]
    lines.extend(f"- {item}" for item in working if item)

    broken = _state_union_blockers(snapshot)
    lines.extend(["", "What's broken or stalled:"])
    lines.extend(f"- {item}" for item in broken[:8])

    next_up = _state_union_next_up(snapshot)
    lines.extend(["", "What I would watch next:"])
    lines.extend(f"{idx}. {item}" for idx, item in enumerate(next_up[:5], start=1))

    lines.extend(
        [
            "",
            f"Bottom line: {revenue_sentence.replace('Revenue is still $0. ', '') if revenue.startswith('$0') else revenue_sentence} The machine is loaded and alive; now it has to prove deliverability and make somebody buy.",
        ]
    )
    return "\n".join(lines).strip()


def _read_revenue_dashboard(base_dir: Path) -> dict[str, Any]:
    data = _read_json(base_dir / "data" / "hustle" / "revenue_dashboard.json", {})
    return data if isinstance(data, dict) else {}


def _read_revenue_health(base_dir: Path) -> dict[str, Any]:
    data = _read_json(base_dir / "data" / "hustle" / "revenue_health.json", {})
    if not isinstance(data, dict):
        return {}
    summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
    return {
        "summary": summary,
        "timestamp": data.get("timestamp") or "",
    }


def _read_revenue_lanes(base_dir: Path) -> dict[str, Any]:
    prospects = _read_json(base_dir / "products" / "outreach" / "prospects.json", [])
    if not isinstance(prospects, list):
        prospects = []

    lanes = {
        "ima_docsapp": {"count": 0, "statuses": Counter(), "sequences": Counter()},
        "maroon_standard": {"count": 0, "statuses": Counter(), "sequences": Counter()},
    }
    for row in prospects:
        if not isinstance(row, dict):
            continue
        brand = str(row.get("brand") or "").lower()
        sequence = str(row.get("sequence_id") or "").lower()
        lane_key = "maroon_standard" if brand == "maroon_standard" or sequence == "sf_consulting_alex" else "ima_docsapp"
        lanes[lane_key]["count"] += 1
        lanes[lane_key]["statuses"][str(row.get("status") or "unknown")] += 1
        lanes[lane_key]["sequences"][str(row.get("sequence_id") or "unknown")] += 1

    for lane in lanes.values():
        lane["statuses"] = dict(lane["statuses"])
        lane["sequences"] = dict(lane["sequences"])
    return lanes


def _read_gumroad_state(base_dir: Path) -> dict[str, Any]:
    listed = _read_json(base_dir / "data" / "hustle" / "gumroad_listed.json", {})
    queue = _read_json(base_dir / "data" / "hustle" / "gumroad_create_queue.json", [])
    sales = _read_json(base_dir / "data" / "hustle" / "gumroad_sales.json", {})
    if not isinstance(listed, dict):
        listed = {}
    rows = [row for row in listed.values() if isinstance(row, dict)]
    published = sum(1 for row in rows if row.get("published") is True)
    unpublished = sum(1 for row in rows if str(row.get("publish_status") or "") == "unpublished_needs_payment")
    created = sum(1 for row in rows if row.get("api_status") == "created")
    sales_rows = sales.get("sales") if isinstance(sales, dict) else []
    return {
        "listed": len(rows),
        "published": published,
        "created": created,
        "unpublished_needs_payment": unpublished,
        "ready": _count_files(base_dir / "data" / "hustle" / "gumroad_ready", "*.json"),
        "queue": len(queue) if isinstance(queue, list) else 0,
        "sales": len(sales_rows) if isinstance(sales_rows, list) else 0,
    }


def _read_content_asset_state(base_dir: Path) -> dict[str, Any]:
    render_dir = base_dir / "data" / "image_production" / "renders"
    image_count = _count_files_recursive(render_dir, "*.png") + _count_files_recursive(render_dir, "*.jpg") + _count_files_recursive(render_dir, "*.jpeg")
    latest = ""
    try:
        files = [path for path in render_dir.glob("**/*") if path.is_file() and path.suffix.lower() in {".png", ".jpg", ".jpeg"}]
        if files:
            latest = str(max(files, key=lambda path: path.stat().st_mtime))
    except Exception:
        latest = ""
    return {
        "image_count": image_count,
        "latest_image": latest,
    }


def _read_zeely_state(base_dir: Path) -> dict[str, Any]:
    hustle = base_dir / "data" / "hustle"
    policy = _read_json(hustle / "zeely_policy.json", {})
    state = _read_json(hustle / "zeely_operator_state.json", {})
    guard = _read_json(hustle / "local_content_creation_paused.json", {})
    queue_rows = _read_jsonl(hustle / "zeely_brief_queue.jsonl")
    ledger_rows = _read_jsonl(hustle / "zeely_operator_ledger.jsonl")
    if not isinstance(policy, dict):
        policy = {}
    if not isinstance(state, dict):
        state = {}
    if not isinstance(guard, dict):
        guard = {}

    queue_counts = Counter(str(row.get("status") or "queued") for row in queue_rows if isinstance(row, dict))
    submitted = _int(queue_counts.get("submitted_to_zeely"))
    queued = sum(_int(queue_counts.get(key)) for key in ("queued", "retry", "handoff_prepared", "handoff_ready_for_manual_zeely"))
    daily = state.get("daily") if isinstance(state.get("daily"), dict) else {}
    today_key = mountain_now().date().isoformat()
    today = daily.get(today_key) if isinstance(daily.get(today_key), dict) else {}
    cap = _int(policy.get("max_zeely_handoffs_per_day")) or 0
    handoffs_today = _int(today.get("handoffs"))
    last_result = state.get("last_result") if isinstance(state.get("last_result"), dict) else {}
    last_event = ledger_rows[-1] if ledger_rows else {}
    content_system = policy.get("content_system") if isinstance(policy.get("content_system"), dict) else {}
    return {
        "configured": bool(policy),
        "organic_only": bool(policy.get("organic_only", True)),
        "max_daily_ad_spend_usd": _int(policy.get("max_daily_ad_spend_usd")),
        "cap": cap,
        "handoffs_today": handoffs_today,
        "cap_reached": bool(cap and handoffs_today >= cap),
        "queued": queued,
        "submitted": submitted,
        "queue_total": len(queue_rows),
        "last_result": last_result,
        "last_run_at": state.get("last_run_at") or "",
        "last_prepared_at": state.get("last_prepared_at") or "",
        "last_event": last_event,
        "local_content_paused": bool(guard.get("local_final_content_generation_enabled") is False or guard.get("paused") is True),
        "channels": len(content_system.get("channels") or []) if isinstance(content_system.get("channels"), list) else 0,
        "offers": len(policy.get("offers") or []) if isinstance(policy.get("offers"), list) else 0,
    }


def _read_salesintel_state(base_dir: Path) -> dict[str, Any]:
    scraped = _read_json(base_dir / "data" / "hustle" / "salesintel_scraped.json", [])
    if not isinstance(scraped, list):
        scraped = []
    log_text = "\n".join(
        _read_text(path)
        for path in (
            base_dir / "data" / "logs" / "salesintel-scraper.log",
            base_dir / "data" / "logs" / "hustle" / "salesintel-scraper.out.log",
        )
    )
    latest_total = _last_int_match(log_text, r"saved\s+\d+\s+new\s+\((\d+)\s+total\)")
    latest_new = _last_int_match(log_text, r"saved\s+(\d+)\s+new\s+\(\d+\s+total\)")
    return {
        "scraped": len(scraped),
        "latest_total": latest_total,
        "latest_new": latest_new,
        "last_warning": _last_log_line(log_text, ("WARNING", "ERROR")),
    }


def _snapshot_as_of(snapshot: dict[str, Any]) -> str:
    label = _display_datetime(mountain_now())
    if label:
        return label
    dashboard = snapshot.get("dashboard") or {}
    for value in (dashboard.get("generated_at"), snapshot.get("as_of")):
        label = _display_datetime(value)
        if label:
            return label
    return _display_datetime(mountain_now()) or "now"


def _freshest_status_timestamp(
    dashboard: dict[str, Any],
    health: dict[str, Any],
    autopilot: dict[str, Any],
    pipeline: dict[str, Any],
    handoff: str,
) -> str:
    """Prefer live generated state over archived audit handoff timestamps."""
    candidates = [
        (dashboard or {}).get("generated_at"),
        (health or {}).get("timestamp"),
        (autopilot or {}).get("generated_at"),
    ]
    parsed = [(dt, value) for value in candidates if (dt := _coerce_datetime(value))]
    if parsed:
        return _display_datetime(max(parsed, key=lambda item: item[0])[1])
    for value in candidates:
        label = _display_datetime(value)
        if label:
            return label
    return _latest_audit_timestamp(handoff)


def _coerce_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except Exception:
            return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _display_datetime(value: Any) -> str:
    if not value:
        return ""
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value)
        try:
            if re.match(r"^[A-Z][a-z]{2}\s+\d{1,2},\s+\d{2}:\d{2}", text):
                return text
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except Exception:
            return text
    try:
        dt = dt.astimezone(mountain_now().tzinfo)
    except Exception:
        pass
    hour = dt.strftime("%I").lstrip("0") or "0"
    return f"{dt.strftime('%b')} {dt.day}, {hour}:{dt.strftime('%M')}{dt.strftime('%p').lower()}"


def _lane_prospects(lane: dict[str, Any]) -> str:
    count = _int(lane.get("count"))
    statuses = lane.get("statuses") if isinstance(lane.get("statuses"), dict) else {}
    if not count:
        return "no prospects loaded"
    details = []
    for key, label in (("active", "active"), ("new", "staged"), ("warmup_hold", "warmup hold"), ("paused_bad_fit", "paused")):
        value = _int(statuses.get(key))
        if value:
            details.append(f"{value:,} {label}")
    suffix = f" ({', '.join(details)})" if details else ""
    return f"{count:,} prospects{suffix}"


def _lane_status(lane: dict[str, Any], fallback: str) -> str:
    statuses = lane.get("statuses") if isinstance(lane.get("statuses"), dict) else {}
    total = max(_int(lane.get("count")), 1)
    warmup = _int(statuses.get("warmup_hold"))
    active = _int(statuses.get("active"))
    staged = _int(statuses.get("new"))
    if warmup >= total * 0.5:
        return "warmup hold"
    if active:
        if staged:
            return f"active/sendable, with {staged:,} staged"
        return "active/sendable"
    if staged:
        return "staged, not fully sendable yet"
    return fallback


def _product_phrase(gumroad: dict[str, Any]) -> str:
    published = _int(gumroad.get("published"))
    listed = _int(gumroad.get("listed"))
    ready = _int(gumroad.get("ready"))
    queue = _int(gumroad.get("queue"))
    parts = []
    if published:
        parts.append(f"{published:,} published on Gumroad")
    elif listed:
        parts.append(f"{listed:,} created on Gumroad")
    if listed and published and listed != published:
        parts.append(f"{listed:,} total listed/created")
    if ready:
        parts.append(f"{ready:,} ready")
    if queue:
        parts.append(f"{queue:,} queued")
    return ", ".join(parts) if parts else "no product inventory snapshot found"


def _content_phrase(assets: dict[str, Any]) -> str:
    images = _int(assets.get("image_count"))
    if images:
        return f"{images:,} local image asset(s) rendered"
    return "content asset state unknown"


def _salesintel_phrase(salesintel: dict[str, Any]) -> str:
    total = _int(salesintel.get("latest_total")) or _int(salesintel.get("scraped"))
    scraped = _int(salesintel.get("scraped"))
    if total and scraped and total != scraped:
        return f"SalesIntel scraper is feeding BrandA: {scraped:,} loaded now, scraper log last reported {total:,} total captured"
    if total:
        return f"SalesIntel scraper has {total:,} contacts loaded"
    return ""


def _brevo_phrase(today: dict[str, Any]) -> str:
    sent = _int(today.get("emails_sent"))
    delivered = _int(today.get("emails_delivered"))
    opened = _int(today.get("emails_opened"))
    clicks = _int(today.get("emails_clicked"))
    if not sent:
        return ""
    return f"Brevo tracker is live: {sent:,} requests, {delivered:,} delivered, {opened:,} unique opens, {clicks:,} clicks"


def _asset_working_phrase(assets: dict[str, Any]) -> str:
    images = _int(assets.get("image_count"))
    latest = str(assets.get("latest_image") or "")
    if not (images or latest):
        return ""
    bits = []
    if images:
        bits.append(f"{images:,} image asset(s) exist")
    if latest:
        bits.append(f"latest {Path(latest).name}")
    return "Local image generation is available: " + ", ".join(bits)


def _gumroad_working_phrase(gumroad: dict[str, Any]) -> str:
    published = _int(gumroad.get("published"))
    ready = _int(gumroad.get("ready"))
    if not (published or ready):
        return ""
    return f"Gumroad inventory exists: {published:,} published, {ready:,} ready assets"


def _health_phrase(health: dict[str, Any]) -> str:
    summary = health.get("summary") if isinstance(health.get("summary"), dict) else {}
    total = _int(summary.get("total"))
    healthy = _int(summary.get("healthy"))
    if total:
        return f"Revenue watchdog reports {healthy}/{total} agents healthy"
    return ""


def _zeely_live_phrase(zeely: dict[str, Any]) -> str:
    if not zeely.get("configured"):
        return ""
    bits = ["Zeely content engine wired"]
    submitted = _int(zeely.get("submitted"))
    queued = _int(zeely.get("queued"))
    handoffs_today = _int(zeely.get("handoffs_today"))
    cap = _int(zeely.get("cap"))
    if submitted:
        bits.append(f"{submitted:,} briefs submitted")
    if queued:
        bits.append(f"{queued:,} queued")
    if cap:
        bits.append(f"{handoffs_today:,}/{cap:,} handoffs used today")
    if zeely.get("local_content_paused"):
        bits.append("local final content factories paused")
    if zeely.get("organic_only") and not _int(zeely.get("max_daily_ad_spend_usd")):
        bits.append("organic/no-spend mode")
    return "; ".join(bits)


def _autopilot_priority_lines(autopilot: dict[str, Any]) -> list[str]:
    items: list[str] = []
    selected = autopilot.get("selected") if isinstance(autopilot.get("selected"), list) else []
    candidates = selected or (autopilot.get("candidates") if isinstance(autopilot.get("candidates"), list) else [])
    for row in candidates[:3]:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").strip()
        reason = str(row.get("reason") or "").strip()
        if not title:
            continue
        if reason:
            items.append(f"{title}: {reason}")
        else:
            items.append(title)
    return items


def _bottom_line(snapshot: dict[str, Any]) -> str:
    dashboard = snapshot.get("dashboard") or {}
    today = dashboard.get("today") if isinstance(dashboard.get("today"), dict) else {}
    zeely = snapshot.get("zeely") or {}
    if zeely.get("cap_reached"):
        return "Bottom line: the Mini is back up; Zeely is capped for today, and the next real proof is fresh sends, replies, Zeely outputs, or a paid conversion."
    if zeely.get("configured") and _int(today.get("emails_sent")) == 0:
        return "Bottom line: automations are alive, content is Zeely-bound, and today's dashboard still needs send movement before revenue can move."
    if _int(today.get("emails_clicked")) == 0:
        return "Bottom line: the stack is running, but the current choke point is clicks/replies/conversions, not more local content generation."
    return "Bottom line: keep pressure on the channels that produced live movement and ignore anything that does not create a reply, click, or sale."


def _state_union_blockers(snapshot: dict[str, Any]) -> list[str]:
    dashboard = snapshot.get("dashboard") or {}
    today = dashboard.get("today") or {}
    gumroad = snapshot.get("gumroad") or {}
    salesintel = snapshot.get("salesintel") or {}
    blockers = []

    if str(today.get("total_revenue") or "$0").replace("$", "").replace(",", "") in {"0", "0.00", "0.0"}:
        blockers.append("Revenue is still $0: no sale/conversion dollars are in the live dashboard")
    if _int(today.get("emails_clicked")) == 0 and _int(today.get("emails_sent")):
        blockers.append(f"Email clicks are still 0 after {_int(today.get('emails_sent')):,} Brevo requests")
    if _int(today.get("bounces")):
        blockers.append(f"Brevo bounces are high: {_int(today.get('bounces')):,} total bounces on the latest dashboard pull")
    if _int(gumroad.get("sales")) == 0 and (_int(gumroad.get("published")) or _int(gumroad.get("listed"))):
        blockers.append(f"Gumroad has {_int(gumroad.get('published')):,} published products but 0 recorded sales")
    if _int(gumroad.get("unpublished_needs_payment")):
        blockers.append(f"{_int(gumroad.get('unpublished_needs_payment')):,} Gumroad products still show unpublished_needs_payment")
    if salesintel.get("last_warning"):
        blockers.append(f"SalesIntel latest warning: {salesintel['last_warning']}")

    blockers.extend(snapshot.get("blockers") or [])
    return _dedupe_clean(blockers) or ["No blocker files were found, which is itself a visibility gap."]


def _state_union_next_up(snapshot: dict[str, Any]) -> list[str]:
    lanes = snapshot.get("lanes") or {}
    persona1 = lanes.get("ima_docsapp") or {}
    BrandA = lanes.get("maroon_standard") or {}
    gumroad = snapshot.get("gumroad") or {}
    zeely = snapshot.get("zeely") or {}
    today = (snapshot.get("dashboard") or {}).get("today") or {}
    items = []
    if zeely.get("queued"):
        items.append("Review Zeely outputs and keep final content generation inside Zeely, not local factories")
    if _int(persona1.get("count")):
        items.append("Keep the persona1/docsapp lane sendable, but do not bypass DNC, quiet-hour, or deliverability guards")
    if _int(BrandA.get("count")):
        items.append("Keep BrandA Standard on warmup hold until the sender/domain is ready")
    if _int(today.get("emails_sent")):
        items.append("Watch the next Brevo pull for clicks/replies, not just opens")
    if _int(gumroad.get("unpublished_needs_payment")) or _int(gumroad.get("queue")):
        items.append("Fix Gumroad publish/file state before chasing more listings")
    items.append("Monitor for first sale or qualified reply and treat that as the only score that matters")
    return _dedupe_clean(items)


def _count_files_recursive(path: Path, pattern: str) -> int:
    try:
        return len([p for p in path.glob(f"**/{pattern}") if p.is_file()])
    except Exception:
        return 0


def _last_int_match(text: str, pattern: str) -> int:
    matches = re.findall(pattern, text or "", flags=re.I)
    if not matches:
        return 0
    value = matches[-1]
    if isinstance(value, tuple):
        value = next((part for part in value if part), "0")
    return _int(str(value).replace(",", ""))


def _last_log_line(text: str, markers: tuple[str, ...]) -> str:
    for line in reversed((text or "").splitlines()):
        if any(marker in line for marker in markers):
            return _clean_markdown(line[-220:])
    return ""


def format_revenue_rundown(snapshot: dict[str, Any]) -> str:
    metrics = snapshot.get("metrics") or {}
    pipeline = snapshot.get("pipeline") or {}
    channels = snapshot.get("channels") or {}

    header = "30-Second Revenue Machine Rundown"
    if snapshot.get("as_of"):
        header += f" (as of {snapshot['as_of']})"

    sales_count = _metric_int(metrics, "gumroad sales")
    converted = _int(pipeline.get("converted"))
    revenue_line = "Revenue: $0 so far."
    if sales_count not in (None, 0) or converted:
        revenue_line = f"Revenue: {sales_count or converted} recorded sale/conversion(s); no dollar total found in local snapshots."
    revenue_line += " The stack is wired, but no purchase/conversion dollars are recorded yet."

    live = _live_lines(snapshot)
    blockers = _blocker_lines(snapshot)
    priorities = _priority_lines(snapshot)

    lines = [header, "", revenue_line, "", "What's live:"]
    lines.extend(f"- {line}" for line in live[:6])
    lines.extend(["", "What's broken/blocked:"])
    lines.extend(f"- {line}" for line in blockers[:6])
    lines.extend(["", "Top priorities today:"])
    lines.extend(f"{idx}. {line}" for idx, line in enumerate(priorities[:5], start=1))

    lines.extend(["", _bottom_line(snapshot)])
    return "\n".join(lines).strip()


def format_revenue_action_plan(snapshot: dict[str, Any], autopilot: dict[str, Any], dispatch_result: dict[str, Any] | None = None) -> str:
    dashboard = snapshot.get("dashboard") or {}
    today = dashboard.get("today") or {}
    gumroad = snapshot.get("gumroad") or {}
    assets = snapshot.get("assets") or {}
    pipeline = snapshot.get("pipeline") or {}
    summary = autopilot.get("summary") if isinstance(autopilot.get("summary"), dict) else {}
    selected = autopilot.get("selected") if isinstance(autopilot.get("selected"), list) else []
    dispatches = autopilot.get("dispatches") if isinstance(autopilot.get("dispatches"), list) else []
    blockers = autopilot.get("blockers") if isinstance(autopilot.get("blockers"), list) else []

    lines = ["Revenue machine: here is the move.", ""]

    if dispatch_result is not None:
        if dispatch_result.get("ok"):
            lines.append("I ran the bounded revenue autopilot. It does not freestyle; it only dispatches known capped jobs.")
        else:
            lines.append(f"I tried to run the revenue autopilot, but it did not start cleanly: {dispatch_result.get('error') or 'unknown error'}")
        lines.append("")

    revenue = str(today.get("total_revenue") or today.get("gumroad_revenue") or "$0.00")
    sent = _int(today.get("emails_sent") or pipeline.get("emails_sent"))
    opens = _int(today.get("emails_opened") or pipeline.get("opened"))
    clicks = _int(today.get("emails_clicked"))
    bounces = _int(today.get("bounces"))
    ready_assets = _int(assets.get("image_count"))
    published = _int(gumroad.get("published"))
    sales = _int(gumroad.get("sales"))

    lines.append("Current read:")
    lines.append(f"- Revenue recorded: {revenue}")
    if sent or opens or clicks or bounces:
        lines.append(f"- Outreach today: {sent:,} sent/requested, {opens:,} opens, {clicks:,} clicks, {bounces:,} bounces")
    if published or sales:
        lines.append(f"- Gumroad: {published:,} published, {sales:,} recorded sales")
    if ready_assets:
        lines.append(f"- Image/content assets: {ready_assets:,} rendered local asset(s)")

    if selected:
        lines.extend(["", "Autopilot selected:"])
        for item in selected[:5]:
            if not isinstance(item, dict):
                continue
            title = item.get("title") or item.get("action_id") or "revenue action"
            reason = item.get("reason") or ""
            score = item.get("score")
            suffix = f" ({score})" if score is not None else ""
            lines.append(f"- {title}{suffix}" + (f": {reason}" if reason else ""))
    elif summary:
        lines.extend(["", f"Autopilot state: {summary.get('selected_count', 0)} selected, {summary.get('dispatch_count', 0)} dispatched, {summary.get('blocked_count', 0)} blocked."])

    if dispatches:
        lines.extend(["", "Dispatched/result:"])
        for row in dispatches[:5]:
            if not isinstance(row, dict):
                continue
            title = row.get("title") or row.get("action_id") or "action"
            status = row.get("status") or "unknown"
            detail = row.get("detail") or ""
            lines.append(f"- {title}: {status}" + (f" - {detail}" if detail else ""))

    if blockers:
        lines.extend(["", "Still blocked:"])
        for row in blockers[:5]:
            if isinstance(row, dict):
                title = row.get("title") or row.get("action_id") or "blocked action"
                why = row.get("blocked_by") or row.get("detail") or ""
                if isinstance(why, list):
                    why = "; ".join(str(v) for v in why[:3])
                lines.append(f"- {title}: {why}")
            else:
                lines.append(f"- {row}")

    next_up = _state_union_next_up(snapshot)
    lines.extend(["", "Next pressure points:"])
    for idx, item in enumerate(next_up[:4], start=1):
        lines.append(f"{idx}. {item}")

    lines.append("")
    lines.append("Bottom line: keep the machine focused on replies, deliverability, publish integrity, and one real conversion signal. More infrastructure is not the bottleneck.")
    return "\n".join(lines).strip()


def _live_lines(snapshot: dict[str, Any]) -> list[str]:
    metrics = snapshot.get("metrics") or {}
    pipeline = snapshot.get("pipeline") or {}
    channels = snapshot.get("channels") or {}
    dashboard = snapshot.get("dashboard") or {}
    today = dashboard.get("today") if isinstance(dashboard.get("today"), dict) else {}
    gumroad_state = snapshot.get("gumroad") or {}
    assets = snapshot.get("assets") or {}
    zeely = snapshot.get("zeely") or {}
    salesintel = snapshot.get("salesintel") or {}
    health_state = snapshot.get("health") or {}
    health_summary = health_state.get("summary") if isinstance(health_state.get("summary"), dict) else {}
    lanes = snapshot.get("lanes") if isinstance(snapshot.get("lanes"), dict) else {}

    lines: list[str] = []

    health = metrics.get("_revenue_health")
    agents_loaded = _metric_value(metrics, "agents loaded")
    live_total = _int(health_summary.get("total"))
    live_healthy = _int(health_summary.get("healthy"))
    if live_total:
        agent_bits = [f"{live_healthy}/{live_total} revenue agents healthy"]
        if agents_loaded:
            agent_bits.append(f"{agents_loaded} agents loaded")
        lines.append("; ".join(agent_bits))
    elif isinstance(health, dict) and health.get("total"):
        agent_bits = [f"{health.get('healthy', 0)}/{health.get('total')} revenue agents healthy"]
        if agents_loaded:
            agent_bits.append(f"{agents_loaded} agents loaded")
        lines.append("; ".join(agent_bits))
    elif agents_loaded:
        lines.append(f"{agents_loaded} agents loaded")

    product_bits = []
    ready = _int(gumroad_state.get("ready"))
    listed = _int(gumroad_state.get("listed"))
    published = _int(gumroad_state.get("published"))
    queued = _int(gumroad_state.get("queue"))
    if ready:
        product_bits.append(f"{ready:,} Gumroad-ready assets")
    if listed:
        product_bits.append(f"{listed:,} listed/created")
    if published:
        product_bits.append(f"{published:,} published")
    if queued:
        product_bits.append(f"{queued:,} queued")
    if not product_bits:
        products_built = _metric_value(metrics, "products built today") or _metric_value(metrics, "digital products built")
        products_ready = _metric_value(metrics, "products ready")
        gumroad = _metric_value(metrics, "products on gumroad")
        if products_built:
            product_bits.append(f"{products_built} products built today")
        if products_ready:
            product_bits.append(f"{products_ready} products ready")
        if gumroad:
            product_bits.append(f"{gumroad} on Gumroad")
    if product_bits:
        lines.append("; ".join(product_bits))

    emails = (
        _format_int(today.get("emails_sent"))
        if "emails_sent" in today
        else _format_int(pipeline.get("emails_sent")) or _metric_value(metrics, "emails sent today")
    )
    delivered = _format_int(today.get("emails_delivered")) if "emails_delivered" in today else ""
    opens = (
        _format_int(today.get("emails_opened"))
        if "emails_opened" in today
        else (_format_int(pipeline.get("opened")) if _int(pipeline.get("opened")) else "") or _metric_value(metrics, "email opens")
    )
    clicks = _format_int(today.get("emails_clicked")) if "emails_clicked" in today else _metric_value(metrics, "email clicks")
    bounces = _format_int(today.get("bounces")) if "bounces" in today else ""
    prospects = _format_int(pipeline.get("prospect_pool")) or _format_int(sum(_int(lane.get("count")) for lane in lanes.values() if isinstance(lane, dict)))
    outreach_bits = []
    if emails:
        outreach_bits.append(f"{emails} emails sent")
    if delivered:
        outreach_bits.append(f"{delivered} delivered")
    if opens:
        outreach_bits.append(f"{opens} opens tracked")
    if clicks:
        outreach_bits.append(f"{clicks} clicks")
    if bounces:
        outreach_bits.append(f"{bounces} bounces")
    if prospects:
        outreach_bits.append(f"{prospects} prospects in the pool")
    if outreach_bits:
        lines.append("; ".join(outreach_bits))

    image_count = _int(assets.get("image_count"))
    if image_count:
        lines.append(f"{image_count:,} local image asset(s) rendered")

    zeely_line = _zeely_live_phrase(zeely)
    if zeely_line:
        lines.append(zeely_line)

    salesintel_line = _salesintel_phrase(salesintel)
    if salesintel_line:
        lines.append(salesintel_line)

    landing = _metric_value(metrics, "landing pages") or _live_count(snapshot.get("landing_pages_count"), "landing pages")
    seo = _metric_value(metrics, "seo blog posts") or _live_count(snapshot.get("seo_posts_count"), "SEO posts")
    if landing and "landing" not in landing.lower():
        landing = f"Landing pages: {landing}"
    if seo and "seo" not in seo.lower() and "blog" not in seo.lower():
        seo = f"SEO blog posts: {seo}"
    page_bits = [bit for bit in (landing, seo) if bit]
    if page_bits:
        lines.append("; ".join(page_bits))

    channel_count = channels.get("count")
    if channel_count:
        labels = channels.get("labels") or []
        if labels:
            lines.append(f"{channel_count} content channels configured ({', '.join(labels[:4])})")
        else:
            lines.append(f"{channel_count} content channels configured")

    works = snapshot.get("works") or []
    wired = _summarize_wired_systems(works)
    if wired:
        lines.append(wired)

    return lines or ["No live revenue snapshot files were found; only the chat route is active."]


def _blocker_lines(snapshot: dict[str, Any]) -> list[str]:
    metrics = snapshot.get("metrics") or {}
    pipeline = snapshot.get("pipeline") or {}
    dashboard = snapshot.get("dashboard") or {}
    today = dashboard.get("today") if isinstance(dashboard.get("today"), dict) else {}
    zeely = snapshot.get("zeely") or {}
    blockers = list(snapshot.get("blockers") or [])

    converted = _int(pipeline.get("converted"))
    hot_leads = _int(pipeline.get("hot_leads"))
    clicks = _metric_value(metrics, "email clicks")
    if converted == 0 and hot_leads == 0:
        leading = "No conversions or hot leads recorded yet"
        if clicks:
            leading += f"; email clicks are {clicks}"
        blockers.insert(0, leading)

    if zeely.get("cap_reached"):
        blockers.append(f"Zeely hit today's handoff cap ({_int(zeely.get('handoffs_today'))}/{_int(zeely.get('cap'))}); queued briefs wait for the next scheduled run")
    elif zeely.get("configured") and not zeely.get("submitted") and zeely.get("last_result"):
        status = str((zeely.get("last_result") or {}).get("status") or "unknown")
        if status not in {"submitted", "no_handoffs"}:
            blockers.append(f"Zeely last result is {status}; check the Zeely session before relying on social content output")
    if _int(today.get("emails_sent")) == 0 and _int(pipeline.get("emails_sent")):
        blockers.append("Today dashboard shows 0 sends even though historical pipeline sends exist; scheduled-send state needs a fresh live check")

    return _dedupe_clean(blockers) or ["No blocker section was found in the latest local snapshot."]


def _priority_lines(snapshot: dict[str, Any]) -> list[str]:
    dashboard = snapshot.get("dashboard") or {}
    today = dashboard.get("today") if isinstance(dashboard.get("today"), dict) else {}
    zeely = snapshot.get("zeely") or {}
    gumroad = snapshot.get("gumroad") or {}
    autopilot = snapshot.get("autopilot") if isinstance(snapshot.get("autopilot"), dict) else {}
    priorities: list[str] = []

    if zeely.get("configured"):
        if zeely.get("cap_reached"):
            priorities.append("Let Zeely resume on its next window; keep old local content factories paused")
        elif zeely.get("queued"):
            priorities.append("Feed the next Zeely queued briefs through the logged-in TikTok/Instagram workflow")
        if zeely.get("submitted"):
            priorities.append("Pull usable Zeely outputs into persona1 social and product-led channels without using company identity")

    if _int(today.get("emails_sent")) == 0:
        priorities.append("Verify scheduled sends and sender queues after the reboot before judging deliverability")
    elif _int(today.get("emails_clicked")) == 0:
        priorities.append("Watch the next Brevo pull for clicks/replies from the latest send window")

    priorities.extend(_autopilot_priority_lines(autopilot))

    if _int(gumroad.get("unpublished_needs_payment")) or _int(gumroad.get("queue")):
        priorities.append("Fix Gumroad publish/file state before adding more listings")

    priorities.extend(_scope_safe_priority(priority) for priority in (snapshot.get("priorities") or []))
    return _dedupe_clean(priorities) or _default_priorities(snapshot.get("metrics") or {}, snapshot.get("pipeline") or {})


def _read_pipeline(base_dir: Path) -> dict[str, Any]:
    outreach = base_dir / "products" / "outreach"
    landing = base_dir / "products" / "landing"
    hustle = base_dir / "data" / "hustle"

    prospects = _read_json(outreach / "prospects.json", [])
    if not isinstance(prospects, list):
        prospects = []

    sent_rows = _read_jsonl(outreach / "sent.jsonl")
    opens = _read_jsonl(landing / "opens.jsonl")
    replies = _read_json(outreach / "replies.json", [])
    if not isinstance(replies, list):
        replies = []

    strategy = _read_json(hustle / "strategy.json", {})
    if not isinstance(strategy, dict):
        strategy = {}
    digest = _read_json(hustle / "revenue_digest.json", {})
    digest_outreach = digest.get("outreach", {}) if isinstance(digest, dict) else {}
    if not isinstance(digest_outreach, dict):
        digest_outreach = {}

    sent_track_ids = {row.get("track_id") for row in sent_rows if row.get("track_id")}
    unique_opens = {
        row.get("track_id")
        for row in opens
        if row.get("track_id") and (not sent_track_ids or row.get("track_id") in sent_track_ids)
    }
    unique_recipients = {
        str(row.get("to", "")).lower()
        for row in sent_rows
        if row.get("to")
    }
    reply_classes = Counter(row.get("classification", "unknown") for row in replies if isinstance(row, dict))
    daily_sends = Counter(row.get("date", "unknown") for row in sent_rows if isinstance(row, dict))

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "pipeline_health": strategy.get("pipeline_health", "unknown"),
        "prospect_pool": max(len(prospects), _int(digest_outreach.get("prospects"))),
        "contacted": len(unique_recipients),
        "emails_sent": max(len(sent_rows), _int(digest_outreach.get("sent_total"))),
        "opened": len(unique_opens),
        "replied": len(replies),
        "hot_leads": int(reply_classes.get("positive", 0)),
        "converted": _count_purchase_events(base_dir),
        "days_active": len(daily_sends),
        "avg_daily_sends": round(len(sent_rows) / max(len(daily_sends), 1), 1),
    }


def _count_purchase_events(base_dir: Path) -> int:
    purchase_events = {"purchase", "sale", "conversion", "checkout_completed", "payment_succeeded"}
    rows = _read_jsonl(base_dir / "products" / "landing" / "funnel_events.jsonl")
    return sum(1 for row in rows if str(row.get("event", "")).lower() in purchase_events)


def _read_channels(base_dir: Path) -> dict[str, Any]:
    data = _read_json(base_dir / "config" / "channel_strategies.json", {})
    channels = data.get("channels") if isinstance(data, dict) else {}
    if not isinstance(channels, dict):
        channels = {}
    labels = []
    key_labels = {
        "ima_furad": "persona1",
        "shameful": "Shameful",
        "money_puppy_penny": "persona4",
    }
    for key in channels:
        labels.append(key_labels.get(key, key.replace("_", " ").title()))
    return {"count": len(channels), "labels": labels}


def _parse_handoff_metrics(text: str) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    if not text:
        return metrics

    for raw_key, raw_value in re.findall(r"^\|\s*([^|\n]+?)\s*\|\s*([^|\n]+?)\s*\|", text, flags=re.M):
        key = _normalize_key(raw_key)
        value = raw_value.strip()
        if key and key not in {"metric", "value", "--------"}:
            metrics[key] = value

    bullet_patterns = {
        "emails sent today": r"(\d[\d,]*)\s+emails\s+today",
        "email opens": r"(\d[\d,]*)\s+unique\s+opens|\bEmail opens\s*\|\s*([^|\n]+)",
        "tiktok creators found": r"(\d[\d,]*)\s+TikTok creators scraped",
        "digital products built": r"(\d[\d,]*)\s+digital products built",
        "products ready": r"(\d[\d,]*)\s+Gumroad-ready",
        "products on gumroad": r"(\d[\d,]*)\s+live on Gumroad",
    }
    for key, pattern in bullet_patterns.items():
        matches = re.findall(pattern, text, flags=re.I)
        if matches and key not in metrics:
            last = matches[-1]
            if isinstance(last, tuple):
                last = next((part for part in last if part), "")
            if last:
                metrics[key] = str(last).strip()

    health_matches = re.findall(r"Revenue health:\s*(\{[^\n]+\})", text)
    if health_matches:
        try:
            metrics["_revenue_health"] = ast.literal_eval(health_matches[-1])
        except Exception:
            pass

    return metrics


def _latest_audit_timestamp(text: str) -> str:
    if not text:
        return ""
    matches = re.findall(r"Revenue Audit.*?(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})\s+([A-Z]{2,4})", text)
    matches += re.findall(r"Audit Report.*?(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})\s+([A-Z]{2,4})", text)
    if not matches:
        return ""
    date_s, time_s, zone = matches[-1]
    try:
        dt = datetime.strptime(f"{date_s} {time_s}", "%Y-%m-%d %H:%M")
        return dt.strftime("%b %-d, %H:%M") + f" {zone}"
    except ValueError:
        return f"{date_s} {time_s} {zone}"


def _extract_bullets(text: str, heading: str) -> list[str]:
    if not text:
        return []
    match = re.search(
        rf"^##\s+{re.escape(heading)}\s*\n(?P<body>.*?)(?=^##\s+|\Z)",
        text,
        flags=re.M | re.S,
    )
    if not match:
        return []
    items: list[str] = []
    for line in match.group("body").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        numbered = re.match(r"^\d+\.\s+(.+)$", stripped)
        bulleted = re.match(r"^[-*]\s+(.+)$", stripped)
        raw = numbered.group(1) if numbered else bulleted.group(1) if bulleted else ""
        if raw:
            items.append(_clean_markdown(raw))
    return _dedupe_clean(items)


def _strategy_problems(base_dir: Path) -> list[str]:
    data = _read_json(base_dir / "data" / "hustle" / "strategy.json", {})
    problems = data.get("problems") if isinstance(data, dict) else []
    if not isinstance(problems, list):
        return []
    lines = []
    for problem in problems:
        if isinstance(problem, dict) and problem.get("reason"):
            lines.append(str(problem["reason"]))
    return lines


def _default_priorities(metrics: dict[str, Any], pipeline: dict[str, Any]) -> list[str]:
    priorities = [
        "Check deliverability and open/click movement from the latest send window",
        "Verify product publishing/file state before adding more listings",
        "Keep browser and Ollama contention from starving core work",
        "Review zero-reply segments before increasing volume",
        "Monitor for the first sale or qualified reply",
    ]
    if _int(pipeline.get("emails_sent")) == 0 and not _metric_value(metrics, "emails sent today"):
        priorities[0] = "Restart or inspect outreach before reporting deliverability"
    return priorities


def _summarize_wired_systems(works: list[str]) -> str:
    wanted = [
        "Revenue watchdog",
        "Reply detector",
        "Revenue tracker",
        "Affiliate program",
        "Flash sale engine",
        "Cart chaser",
    ]
    present = []
    joined = "\n".join(works).lower()
    for label in wanted:
        if label.lower() in joined:
            present.append(label)
    if not present:
        return ""
    return ", ".join(present[:6]) + " wired"


def _scope_safe_priority(priority: str) -> str:
    lower = priority.lower()
    if "gumroad" in lower or "products list" in lower:
        return "Verify Gumroad publication/file state before listing more products"
    if "dm outreach" in lower or "tiktok" in lower or "ig bio" in lower:
        return "Keep social/DM automation inside the approved send and identity guardrails"
    if "etsy" in lower:
        return "Skip marketplace side quests unless they feed the current approved offers"
    return priority


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    try:
        lines = path.read_text().splitlines()
    except Exception:
        return []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _read_text(path: Path) -> str:
    try:
        return path.read_text() if path.exists() else ""
    except Exception:
        return ""


def _count_files(path: Path, pattern: str) -> int:
    try:
        return len([p for p in path.glob(pattern) if p.is_file()])
    except Exception:
        return 0


def _count_json_list(path: Path) -> int:
    data = _read_json(path, [])
    return len(data) if isinstance(data, list) else 0


def _metric_value(metrics: dict[str, Any], key: str) -> str:
    value = metrics.get(_normalize_key(key))
    if value is None:
        return ""
    return str(value).strip()


def _metric_int(metrics: dict[str, Any], key: str) -> int | None:
    value = _metric_value(metrics, key)
    if not value:
        return None
    match = re.search(r"-?\d[\d,]*", value)
    if not match:
        return None
    return int(match.group(0).replace(",", ""))


def _normalize_key(key: str) -> str:
    return re.sub(r"\s+", " ", str(key or "").strip().lower())


def _clean_markdown(text: str) -> str:
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
    text = text.replace("`", "")
    text = text.replace("\u2014", "-")
    text = text.replace("\u2192", "->")
    return re.sub(r"\s+", " ", text).strip(" -")


def _dedupe_clean(items: list[str]) -> list[str]:
    seen = set()
    clean = []
    for item in items:
        value = _clean_markdown(str(item))
        if not value:
            continue
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        clean.append(value)
    return clean


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _format_int(value: Any) -> str:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return ""
    return f"{number:,}"


def _live_count(count: Any, label: str) -> str:
    try:
        number = int(count)
    except (TypeError, ValueError):
        return ""
    if number <= 0:
        return ""
    return f"{number} {label}"


def _iso_to_display(value: Any) -> str:
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return ""
    return dt.strftime("%b %-d, %H:%M UTC")
