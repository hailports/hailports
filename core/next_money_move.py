from __future__ import annotations

"""Next Money Move — blocker-aware strategy engine.

Scans every revenue channel, identifies what's blocked and why,
and generates priority-ordered actions for RIGHT NOW and TOMORROW.
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, os.path.expanduser("~/claude-stack"))

log = logging.getLogger("next-money-move")

BASE = Path(os.path.expanduser("~/claude-stack"))
HUSTLE = BASE / "data" / "hustle"
LOGS = BASE / "data" / "logs"

DEFAULT_EMAIL_DAILY_CAP = 180
GUMROAD_RETRY_HOURS = 6

# ── New product/offer angle queue ─────────────────────────────────────────────

OFFER_ANGLES = [
    {"name": "24-hour Salesforce permission review", "type": "service", "price": 149, "segment": "sf_admin"},
    {"name": "IEP/ARD meeting prep kit for parents", "type": "digital", "price": 19, "segment": "planner_organization"},
    {"name": "Household command center template", "type": "digital", "price": 12, "segment": "planner_organization"},
    {"name": "Flow documentation sprint", "type": "service", "price": 299, "segment": "sf_admin"},
    {"name": "Salesforce org health scorecard", "type": "service", "price": 0, "segment": "sf_admin"},
    {"name": "Social media 30-day content calendar", "type": "digital", "price": 15, "segment": "mlm_content"},
    {"name": "Compliance audit checklist bundle", "type": "digital", "price": 29, "segment": "compliance_governance"},
    {"name": "Small business SOP starter pack", "type": "digital", "price": 25, "segment": "small_biz_ops"},
]

# ── Priority tiers (higher index = lower priority, only if above not blocked) ─

PRIORITY_ORDER = [
    "reply_warm_leads",
    "publish_ready_products",
    "promote_unblocked_channels",
    "create_service_offers",
    "build_more_products",
]


def _read_json(path: Path) -> dict | list | None:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return None
    return None


def _log_age_seconds(path: Path) -> float | None:
    if not path.exists():
        return None
    try:
        mtime = path.stat().st_mtime
        return (datetime.now().timestamp() - mtime)
    except Exception:
        return None


def _today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _count_today_sends() -> int:
    sent_file = HUSTLE / "outreach_sent.jsonl"
    if not sent_file.exists():
        return 0
    today = _today_str()
    count = 0
    for line in sent_file.read_text().strip().split("\n"):
        if today in line:
            count += 1
    return count


def _policy() -> dict:
    try:
        from core.revenue_autonomy import load_policy

        policy = load_policy(BASE)
        return policy if isinstance(policy, dict) else {}
    except Exception:
        return {}


def _channel_daily_cap(channel: str, fallback: int = DEFAULT_EMAIL_DAILY_CAP) -> int:
    policy = _policy()
    caps = policy.get("channel_caps", {}).get(channel, {}) if isinstance(policy.get("channel_caps"), dict) else {}
    if isinstance(caps, dict):
        try:
            value = int(caps.get("daily_cap") or caps.get("daily_send_cap") or 0)
        except Exception:
            value = 0
        if value > 0:
            return value
    email_caps = policy.get("reputation_caps", {}).get("email", {}) if isinstance(policy.get("reputation_caps"), dict) else {}
    try:
        value = int(email_caps.get("daily_send_cap") or 0)
    except Exception:
        value = 0
    return value if value > 0 else fallback


# ── Channel scanners ─────────────────────────────────────────────────────────

def scan_email() -> dict:
    """Email outreach channel state."""
    sent_today = _count_today_sends()
    cap = _channel_daily_cap("email_docsapp")
    at_cap = sent_today >= cap
    sent_file = HUSTLE / "outreach_sent.jsonl"
    total_sent = 0
    if sent_file.exists():
        total_sent = len(sent_file.read_text().strip().split("\n"))

    # Check if outreach cron is running
    health = _read_json(HUSTLE / "revenue_health.json") or {}
    agents = health.get("agents", [])
    outreach_agent = next((a for a in agents if "outreach" in a.get("label", "")), None)
    cron_status = outreach_agent["status"] if outreach_agent else "unknown"

    blocker = None
    if at_cap:
        blocker = f"Hit {cap}/day Brevo cap. Next window: 7am tomorrow. Alt: LinkedIn/Reddit"
    elif cron_status in ("dead", "stuck"):
        blocker = f"Outreach cron is {cron_status}. Needs restart."

    outbound_hold_active = False
    strategy = _read_json(HUSTLE / "strategy.json") or {}
    directives = strategy.get("directives") if isinstance(strategy, dict) else {}
    if isinstance(directives, dict) and directives.get("outbound_hold"):
        outbound_hold_active = True
        reason = str(directives.get("outbound_hold_reason") or "strategy outbound hold").strip()
        blocker = f"Strategy outbound hold active: {reason}"

    return {
        "channel": "email_outreach",
        "channel_id": "email_docsapp",
        "cap_scope": "per_channel",
        "target_mode": "max_out_per_channel",
        "sent_today": sent_today,
        "daily_cap": cap,
        "daily_remaining": max(0, cap - sent_today),
        "total_sent": total_sent,
        "at_cap": at_cap,
        "outbound_hold_active": outbound_hold_active,
        "cron_status": cron_status,
        "blocker": blocker,
    }


def scan_gumroad() -> dict:
    """Gumroad product channel state."""
    ready_dir = HUSTLE / "gumroad_ready"
    listed_file = HUSTLE / "gumroad_listed.json"
    ready_count = len(list(ready_dir.glob("*.json"))) if ready_dir.is_dir() else 0
    listed = _read_json(listed_file)
    listed_count = len(listed) if isinstance(listed, (list, dict)) else 0
    unlisted = max(0, ready_count - listed_count)

    # Check lister agent status
    health = _read_json(HUSTLE / "revenue_health.json") or {}
    agents = health.get("agents", [])
    lister = next((a for a in agents if "gumroad" in a.get("label", "")), None)
    lister_status = lister["status"] if lister else "unknown"
    has_errors = lister.get("has_errors", False) if lister else False

    # Check for 429 in logs
    gumroad_log = LOGS / "gumroad-lister.log"
    hit_429 = False
    if gumroad_log.exists():
        try:
            tail = gumroad_log.read_text()[-5000:]
            hit_429 = "429" in tail or "rate limit" in tail.lower()
        except Exception:
            pass

    blocker = None
    if hit_429:
        blocker = f"429 rate limit. {unlisted} products ready but unlisted. Retry in {GUMROAD_RETRY_HOURS} hours."
    elif lister_status in ("dead", "stuck"):
        blocker = f"Gumroad lister is {lister_status}. {unlisted} products waiting."
    elif unlisted > 0 and lister_status != "healthy":
        blocker = f"Lister not healthy ({lister_status}). {unlisted} products queued."

    return {
        "channel": "gumroad",
        "products_ready": ready_count,
        "products_listed": listed_count,
        "unlisted": unlisted,
        "lister_status": lister_status,
        "hit_429": hit_429,
        "blocker": blocker,
    }


def scan_fiverr() -> dict:
    """Fiverr channel state."""
    gigs = _read_json(HUSTLE / "fiverr_gigs.json") or {}
    created = gigs.get("created_gigs", {})
    gig_count = len(created) if isinstance(created, dict) else 0

    health = _read_json(HUSTLE / "revenue_health.json") or {}
    agents = health.get("agents", [])
    seller = next((a for a in agents if a.get("label", "") == "com.claude-stack.fiverr-seller"), None)
    proposals = next((a for a in agents if a.get("label", "") == "com.claude-stack.fiverr-proposals"), None)

    seller_status = seller["status"] if seller else "unknown"
    proposals_status = proposals["status"] if proposals else "unknown"
    proposals_errors = proposals.get("has_errors", False) if proposals else False

    blocker = None
    if proposals_status in ("dead", "stuck"):
        blocker = f"0 buyer request proposals sent. Blocker: proposals agent {proposals_status}"
        if proposals_errors:
            blocker += " (session expired)"
    elif seller_status in ("dead", "stuck"):
        blocker = f"Fiverr seller agent {seller_status}."

    return {
        "channel": "fiverr",
        "gigs_live": gig_count,
        "seller_status": seller_status,
        "proposals_status": proposals_status,
        "blocker": blocker,
    }


def scan_upwork() -> dict:
    """Upwork channel state."""
    seen = _read_json(HUSTLE / "upwork_seen.json")
    jobs_seen = len(seen) if isinstance(seen, list) else 0

    health = _read_json(HUSTLE / "revenue_health.json") or {}
    agents = health.get("agents", [])
    scraper = next((a for a in agents if a.get("label", "") == "com.claude-stack.upwork-scraper"), None)
    submitter = next((a for a in agents if a.get("label", "") == "com.claude-stack.upwork-submitter"), None)

    scraper_status = scraper["status"] if scraper else "unknown"
    submitter_status = submitter["status"] if submitter else "unknown"

    blocker = None
    if submitter_status in ("dead", "stuck"):
        blocker = f"Upwork submitter is {submitter_status}. {jobs_seen} jobs seen but no proposals going out."
    elif scraper_status in ("dead", "stuck"):
        blocker = f"Upwork scraper is {scraper_status}. No new jobs being found."

    return {
        "channel": "upwork",
        "jobs_seen": jobs_seen,
        "scraper_status": scraper_status,
        "submitter_status": submitter_status,
        "blocker": blocker,
    }


def scan_social() -> dict:
    """TikTok / Instagram / X engagement state."""
    health = _read_json(HUSTLE / "revenue_health.json") or {}
    agents = health.get("agents", [])

    platforms = {}
    for platform, label_part in [("tiktok", "tiktok-engagement"), ("instagram", "persona1-rhythm"), ("twitter", "twitter-engagement")]:
        agent = next((a for a in agents if label_part in a.get("label", "")), None)
        status = agent["status"] if agent else "unknown"

        # Try to get engagement counts from logs
        log_file = LOGS / f"engagement-blitz.log"
        actions_today = 0
        if log_file.exists():
            try:
                today = _today_str()
                for line in log_file.read_text().strip().split("\n")[-500:]:
                    if today in line and platform in line.lower():
                        actions_today += 1
            except Exception:
                pass

        platforms[platform] = {
            "status": status,
            "actions_today": actions_today,
        }

    # Aggregate blocker
    dead = [p for p, d in platforms.items() if d["status"] in ("dead", "stuck")]
    blocker = None
    if dead:
        blocker = f"Social engagement dead on: {', '.join(dead)}"

    return {
        "channel": "social",
        "platforms": platforms,
        "blocker": blocker,
    }


def scan_content_factory() -> dict:
    """Content factory / product builder state."""
    health = _read_json(HUSTLE / "revenue_health.json") or {}
    agents = health.get("agents", [])
    factory = next((a for a in agents if "content-factory" in a.get("label", "")), None)
    overnight = next((a for a in agents if "overnight-factory" in a.get("label", "")), None)

    factory_status = factory["status"] if factory else "unknown"
    overnight_status = overnight["status"] if overnight else "unknown"

    # Count digital products
    dp = BASE / "products" / "digital_products"
    product_count = sum(1 for _ in dp.rglob("*.html")) if dp.is_dir() else 0

    # Queue depth
    queue = _read_json(HUSTLE / "mom_product_queue.json")
    queue_depth = len(queue) if isinstance(queue, list) else 0

    blocker = None
    if factory_status in ("dead", "stuck") and overnight_status in ("dead", "stuck", "erroring"):
        blocker = f"Both factories down ({factory_status}/{overnight_status}). Queue: {queue_depth} items."
    elif factory_status in ("dead", "stuck"):
        blocker = f"Content factory {factory_status}. Overnight: {overnight_status}."

    return {
        "channel": "content_factory",
        "products_built": product_count,
        "queue_depth": queue_depth,
        "factory_status": factory_status,
        "overnight_status": overnight_status,
        "blocker": blocker,
    }


def scan_replies() -> dict:
    """Reply detector / warm lead state."""
    state = _read_json(HUSTLE / "reply_detector_state.json") or {}
    scanned = len(state.get("processed_ids", []))
    last_scan = state.get("last_scan")

    health = _read_json(HUSTLE / "revenue_health.json") or {}
    agents = health.get("agents", [])
    detector = next((a for a in agents if "reply-detector" in a.get("label", "")), None)
    detector_status = detector["status"] if detector else "unknown"

    # Check for positive replies
    positive_file = HUSTLE / "positive_replies.json"
    hot_file = HUSTLE / "hot_leads.json"
    positive = _read_json(positive_file)
    hot = _read_json(hot_file)
    positive_count = len(positive) if isinstance(positive, list) else 0
    hot_count = len(hot) if isinstance(hot, list) else 0

    blocker = None
    if detector_status in ("dead", "stuck"):
        blocker = f"Reply detector is {detector_status}. Missing warm leads."

    return {
        "channel": "reply_detector",
        "emails_scanned": scanned,
        "last_scan": last_scan,
        "positive_replies": positive_count,
        "hot_leads": hot_count,
        "detector_status": detector_status,
        "blocker": blocker,
    }


# ── Strategy engine ──────────────────────────────────────────────────────────

def _all_channels() -> list[dict]:
    """Scan every channel and return list of states."""
    return [
        scan_email(),
        scan_gumroad(),
        scan_fiverr(),
        scan_upwork(),
        scan_social(),
        scan_content_factory(),
        scan_replies(),
    ]


def _channel_blocked(ch: dict) -> bool:
    return ch.get("blocker") is not None


def _generate_moves_now(channels: list[dict]) -> list[dict]:
    """Top 3 moves for RIGHT NOW based on priority order and blockers."""
    moves = []
    ch_map = {c["channel"]: c for c in channels}

    email = ch_map.get("email_outreach", {})
    gumroad = ch_map.get("gumroad", {})
    fiverr = ch_map.get("fiverr", {})
    upwork = ch_map.get("upwork", {})
    social = ch_map.get("social", {})
    factory = ch_map.get("content_factory", {})
    replies = ch_map.get("reply_detector", {})

    # Priority 1: Reply/follow-up warm leads
    if replies.get("hot_leads", 0) > 0:
        moves.append({
            "priority": 1,
            "action": f"Follow up {replies['hot_leads']} hot leads immediately",
            "reason": "Warm leads convert 10-50x better than cold. This is money on the table.",
            "channel": "reply_detector",
            "tier": "reply_warm_leads",
        })
    elif replies.get("positive_replies", 0) > 0:
        moves.append({
            "priority": 1,
            "action": f"Respond to {replies['positive_replies']} positive replies",
            "reason": "Positive replies are highest-value actions. Respond within 1 hour.",
            "channel": "reply_detector",
            "tier": "reply_warm_leads",
        })
    elif not _channel_blocked(replies):
        # Detector working but no leads yet -- still worth noting
        pass

    # Priority 2: Publish/list already-built products
    unlisted = gumroad.get("unlisted", 0)
    if unlisted > 0 and not gumroad.get("hit_429"):
        moves.append({
            "priority": 2,
            "action": f"Review {unlisted} ready product listings for Gumroad/marketplace publishing",
            "reason": f"Products are built; keep publish/upload actions behind operator review.",
            "channel": "gumroad",
            "tier": "publish_ready_products",
        })

    # Priority 3: Promote on unblocked channels
    if not email.get("at_cap") and not _channel_blocked(email):
        remaining = email.get("daily_cap", 180) - email.get("sent_today", 0)
        if remaining > 10:
            moves.append({
                "priority": 3,
                "action": (
                    f"Send {remaining} more {email.get('channel_id', 'email')} emails today "
                    f"to max the channel ({email.get('sent_today', 0)}/{email.get('daily_cap', 180)})"
                ),
                "reason": "Per-channel cap not hit. Max this channel without borrowing capacity from other outbound lanes.",
                "channel": "email_outreach",
                "tier": "promote_unblocked_channels",
            })

    social_platforms = social.get("platforms", {})
    for plat, info in social_platforms.items():
        if info.get("status") == "healthy" and info.get("actions_today", 0) < 20:
            moves.append({
                "priority": 3,
                "action": f"Prepare manual engagement queue for {plat} (only {info['actions_today']} actions today)",
                "reason": f"{plat} has room for distribution; use drafts and operator review before platform actions.",
                "channel": "social",
                "tier": "promote_unblocked_channels",
            })

    # Priority 4: Create high-intent service offers
    if fiverr.get("gigs_live", 0) < 15 and not _channel_blocked(fiverr):
        moves.append({
            "priority": 4,
            "action": f"Prepare Fiverr gig listing drafts (currently {fiverr.get('gigs_live', 0)} live, cap is ~20)",
            "reason": "Gig drafts are safe inventory; publishing remains an operator-approved platform action.",
            "channel": "fiverr",
            "tier": "create_service_offers",
        })

    if upwork.get("jobs_seen", 0) > 0 and not _channel_blocked(upwork):
        moves.append({
            "priority": 4,
            "action": f"Review and draft proposal notes for {upwork['jobs_seen']} Upwork jobs",
            "reason": "Jobs are already scraped; applications/submissions stay manual-review only.",
            "channel": "upwork",
            "tier": "create_service_offers",
        })

    # Priority 5: Build more products (only if distribution isn't blocked)
    dist_blocked = (
        _channel_blocked(ch_map.get("email_outreach", {}))
        and _channel_blocked(ch_map.get("gumroad", {}))
        and _channel_blocked(ch_map.get("social", {}))
    )
    if not dist_blocked and factory.get("queue_depth", 0) > 0:
        moves.append({
            "priority": 5,
            "action": f"Build {min(factory['queue_depth'], 5)} products from queue ({factory['queue_depth']} total queued)",
            "reason": "Distribution channels open. More products = more listings = more revenue.",
            "channel": "content_factory",
            "tier": "build_more_products",
        })

    # Sort by priority, return top 3
    moves.sort(key=lambda m: m["priority"])
    return moves[:3]


def _generate_moves_tomorrow(channels: list[dict]) -> list[dict]:
    """Top 3 moves for TOMORROW MORNING."""
    moves = []
    ch_map = {c["channel"]: c for c in channels}

    email = ch_map.get("email_outreach", {})
    gumroad = ch_map.get("gumroad", {})
    factory = ch_map.get("content_factory", {})

    # Tomorrow: Brevo cap resets
    if email.get("at_cap") and not email.get("outbound_hold_active"):
        moves.append({
            "priority": 1,
            "action": f"Resume email outreach at 7am (cap resets, {email.get('daily_cap', 180)} sends available)",
            "reason": "Cap resets overnight. First-send advantage in morning inboxes.",
            "channel": "email_outreach",
        })

    # Tomorrow: Gumroad rate limit resets
    if gumroad.get("hit_429"):
        moves.append({
            "priority": 2,
            "action": f"Retry Gumroad listing ({gumroad.get('unlisted', 0)} products waiting)",
            "reason": "429 rate limit should clear overnight. List everything queued.",
            "channel": "gumroad",
        })

    # Fix dead agents overnight
    blockers = [c for c in channels if _channel_blocked(c)]
    dead_agents = [c["channel"] for c in blockers if "dead" in str(c.get("blocker", "")) or "stuck" in str(c.get("blocker", ""))]
    if dead_agents:
        moves.append({
            "priority": 3,
            "action": f"Restart dead agents: {', '.join(dead_agents[:4])}",
            "reason": "Dead agents = zero revenue from those channels. Fix first thing.",
            "channel": "system",
        })

    # Build products overnight for morning listing
    if factory.get("queue_depth", 0) > 0:
        moves.append({
            "priority": 4,
            "action": f"Overnight factory: build {min(factory['queue_depth'], 10)} products for morning listing",
            "reason": "Products built overnight can be listed at market open.",
            "channel": "content_factory",
        })

    # Scan for new Upwork/Fiverr buyer requests
    moves.append({
        "priority": 5,
        "action": "Scan fresh Fiverr buyer requests and Upwork jobs at 6am",
        "reason": "Buyer requests posted overnight have less competition early morning.",
        "channel": "freelance",
    })

    moves.sort(key=lambda m: m["priority"])
    return moves[:3]


def _alternate_channels(channels: list[dict]) -> list[dict]:
    """Suggest alternate channels when primary ones are blocked."""
    alts = []
    ch_map = {c["channel"]: c for c in channels}

    if _channel_blocked(ch_map.get("email_outreach", {})):
        alts.append({
            "blocked": "email_outreach",
            "alternatives": [
                "LinkedIn value-post drafts for operator review",
                "Reddit value-answer drafts for niche subreddits after rule check",
                "Quora answer drafts with soft CTA after rule check",
            ],
        })

    if _channel_blocked(ch_map.get("gumroad", {})):
        alts.append({
            "blocked": "gumroad",
            "alternatives": [
                "Prepare Etsy listing drafts for the same products",
                "QA direct-sales landing pages and Stripe checkout paths",
                "Prepare AppSumo or Creative Market listing drafts",
            ],
        })

    if _channel_blocked(ch_map.get("social", {})):
        social = ch_map.get("social", {})
        dead_platforms = [p for p, d in social.get("platforms", {}).items() if d.get("status") in ("dead", "stuck")]
        alive = [p for p, d in social.get("platforms", {}).items() if d.get("status") not in ("dead", "stuck")]
        if dead_platforms and alive:
            alts.append({
                "blocked": f"social ({', '.join(dead_platforms)})",
                "alternatives": [f"Prepare manual content/engagement drafts for {', '.join(alive)}"],
            })
        elif dead_platforms:
            alts.append({
                "blocked": "social (all platforms)",
                "alternatives": [
                "Blog/SEO content (auto-blogger agent)",
                    "Medium/Substack draft preparation",
                    "YouTube Shorts script/asset preparation",
                ],
            })

    if _channel_blocked(ch_map.get("fiverr", {})):
        alts.append({
            "blocked": "fiverr",
            "alternatives": [
                "Upwork proposal notes for operator review",
                "Guru.com / PeoplePerHour listing drafts",
                "Salesforce community value-post drafts after rule check",
            ],
        })

    return alts


def _offer_angle_queue() -> list[dict]:
    """New product/offer angles to queue for factory."""
    # Check which angles are already built
    ready_dir = HUSTLE / "gumroad_ready"
    existing_names = set()
    if ready_dir.is_dir():
        for f in ready_dir.glob("*.json"):
            try:
                d = json.loads(f.read_text())
                existing_names.add(d.get("name", "").lower())
            except Exception:
                pass

    new_angles = []
    for angle in OFFER_ANGLES:
        if angle["name"].lower() not in existing_names:
            new_angles.append(angle)

    return new_angles


# ── Public API ───────────────────────────────────────────────────────────────

def compute() -> dict:
    """Full Next Money Move computation. Returns the complete strategy object."""
    channels = _all_channels()
    blockers = [c for c in channels if _channel_blocked(c)]
    moves_now = _generate_moves_now(channels)
    moves_tomorrow = _generate_moves_tomorrow(channels)
    alt_channels = _alternate_channels(channels)
    new_angles = _offer_angle_queue()

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "channels": channels,
        "blockers": [{"channel": c["channel"], "reason": c["blocker"]} for c in blockers],
        "blocker_count": len(blockers),
        "moves_now": moves_now,
        "moves_tomorrow": moves_tomorrow,
        "alternate_channels": alt_channels,
        "new_offer_angles": new_angles,
        "summary": _summary(channels, moves_now, blockers),
    }

    # Persist for other agents to read
    out = HUSTLE / "next_money_move.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2))

    return result


def _summary(channels: list[dict], moves: list[dict], blockers: list[dict]) -> str:
    """One-line human-readable summary."""
    blocked_names = [b["channel"] for b in blockers]
    ok_count = len(channels) - len(blockers)
    top_move = moves[0]["action"] if moves else "No moves available"
    return f"{ok_count}/{len(channels)} channels operational. {len(blockers)} blocked. Top move: {top_move}"


def get_cached(max_age_seconds: int = 300) -> dict | None:
    """Return cached result if fresh enough, else None."""
    path = HUSTLE / "next_money_move.json"
    if not path.exists():
        return None
    age = _log_age_seconds(path)
    if age is not None and age <= max_age_seconds:
        try:
            return json.loads(path.read_text())
        except Exception:
            return None
    return None


def get_or_compute(max_age_seconds: int = 300) -> dict:
    """Return cached if fresh, otherwise recompute."""
    cached = get_cached(max_age_seconds)
    if cached:
        return cached
    return compute()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = compute()
    print(json.dumps(result, indent=2))
