#!/usr/bin/env python3
"""Demand Engine — identifies what's selling and builds targeted prospect lists.

The stack should NEVER wait for the operator to identify demand.
This engine:
1. Analyzes what products exist and what's performing
2. Identifies buyer personas for each product category
3. Builds targeted prospect lists from the 70K pool
4. Generates segment-specific outreach sequences
5. Feeds the outreach cron with the RIGHT prospects for the RIGHT products

Runs as part of the overnight cycle. Outputs:
- data/hustle/demand_signals.json — what's hot
- data/hustle/targeted_lists/ — segmented prospect lists
- products/outreach/sequences/ — auto-generated email sequences per segment
"""

import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.expanduser("~/claude-stack"))

log = logging.getLogger("demand-engine")

BASE = Path(os.path.expanduser("~/claude-stack"))
PROSPECTS_FILE = BASE / "products" / "outreach" / "prospects.json"
GUMROAD_DIR = BASE / "data" / "hustle" / "gumroad_ready"
PRODUCTS_DIR = BASE / "products" / "digital_products"
SEQUENCES_DIR = BASE / "products" / "outreach" / "sequences"
DEMAND_FILE = BASE / "data" / "hustle" / "demand_signals.json"
TARGETED_DIR = BASE / "data" / "hustle" / "targeted_lists"
SENT_LOG = BASE / "products" / "outreach" / "sent.jsonl"


# ── Buyer Personas: what kind of person buys each product category ────────────

BUYER_PERSONAS = {
    "mlm_content": {
        "description": "MLM sellers, network marketers, social sellers",
        "titles": ["independent consultant", "brand ambassador", "sales representative",
                    "direct sales", "team leader", "independent distributor"],
        "industries": ["direct selling", "network marketing", "health & wellness",
                       "beauty", "cosmetics", "nutritional supplements"],
        "keywords": ["mlm", "network market", "direct sales", "social sell",
                     "team build", "downline", "upline"],
        "tiktok_hashtags": ["#mlm", "#networkmarketing", "#bossbabe", "#socialselling",
                           "#directsales", "#workfromhome", "#mompreneur", "#sidehustle"],
        "email_hook": "content that sells for you",
        "price_range": [5, 47],
    },
    "faith_devotional": {
        "description": "Faith-based community, women's ministry, church leaders",
        "titles": ["pastor", "ministry leader", "women's ministry", "church administrator",
                    "youth director", "worship leader"],
        "industries": ["religious", "church", "ministry", "nonprofit", "faith-based"],
        "keywords": ["faith", "church", "ministry", "devotional", "scripture",
                     "bible study", "prayer", "worship"],
        "tiktok_hashtags": ["#faithcommunity", "#christianwomen", "#biblestudy",
                           "#churchlife", "#womenofgod", "#devotional"],
        "email_hook": "shareable content your community will love",
        "price_range": [7, 29],
    },
    "planner_organization": {
        "description": "Planner enthusiasts, organizers, PTA moms, type-A women",
        "titles": ["office manager", "executive assistant", "project coordinator",
                    "event planner", "administrative assistant", "pta president"],
        "industries": ["education", "event planning", "nonprofit", "school"],
        "keywords": ["planner", "organize", "template", "checklist", "printable",
                     "schedule", "productivity", "bullet journal"],
        "tiktok_hashtags": ["#plannerlife", "#organization", "#ptamom", "#momlife",
                           "#printables", "#plannergirl", "#organizedhome"],
        "email_hook": "templates that make your life easier",
        "price_range": [5, 29],
    },
    "small_biz_ops": {
        "description": "Small business owners needing SOPs, invoices, processes",
        "titles": ["owner", "founder", "ceo", "president", "general manager",
                    "operations manager", "business owner"],
        "industries": ["small business", "startup", "consulting", "professional services",
                       "retail", "restaurant", "construction", "hvac", "plumbing",
                       "landscaping", "cleaning"],
        "keywords": ["sop", "process", "invoice", "contract", "business plan",
                     "operations", "workflow"],
        "tiktok_hashtags": ["#smallbusiness", "#entrepreneur", "#businessowner",
                           "#startup", "#solopreneur", "#smallbizlife"],
        "email_hook": "professional templates ready to use today",
        "price_range": [15, 50],
    },
    "career_jobseeker": {
        "description": "Job seekers, career changers, recent grads",
        "titles": ["student", "graduate", "career", "seeking", "transitioning"],
        "industries": ["education", "staffing", "recruiting", "career services"],
        "keywords": ["resume", "cover letter", "interview", "job search", "career",
                     "linkedin", "portfolio"],
        "tiktok_hashtags": ["#jobsearch", "#resumetips", "#careerchange", "#interviewtips",
                           "#jobhunt", "#newgrad"],
        "email_hook": "land interviews with a resume that stands out",
        "price_range": [15, 50],
    },
    "sf_admin": {
        "description": "Salesforce professionals and overloaded admins with live org issues",
        "titles": ["salesforce admin", "salesforce administrator", "crm admin",
                    "crm manager", "salesforce developer", "salesforce consultant",
                    "revops", "revenue operations", "business systems",
                    "customer relationship management administrator"],
        "industries": ["technology", "saas", "software", "crm"],
        "keywords": ["salesforce", "crm", "admin", "apex", "soql", "permission",
                     "profile", "flow", "automation", "error", "broken report",
                     "validation rule", "deployment", "integration", "stuck",
                     "urgent"],
        "tiktok_hashtags": ["#salesforce", "#sfadmin", "#crmadmin", "#trailblazer",
                           "#salesforceadmin"],
        "email_hook": "on-demand Salesforce admin help from $49",
        "price_range": [49, 199],
    },
    "compliance_governance": {
        "description": "Compliance officers, audit managers, IT governance",
        "titles": ["compliance", "audit", "risk", "governance", "ciso", "security",
                    "information security", "data protection"],
        "industries": ["finance", "banking", "insurance", "healthcare", "government"],
        "keywords": ["compliance", "audit", "soc2", "gdpr", "hipaa", "risk",
                     "governance", "controls"],
        "tiktok_hashtags": [],  # not a TikTok audience
        "email_hook": "audit-ready compliance templates",
        "price_range": [29, 200],
    },
    "trades_contractor": {
        "description": "HVAC, plumbing, electrical, landscaping business owners",
        "titles": ["owner", "contractor", "foreman", "estimator", "project manager"],
        "industries": ["construction", "hvac", "plumbing", "electrical", "landscaping",
                       "roofing", "painting", "home services", "general contractor"],
        "keywords": ["contractor", "estimate", "bid", "job", "crew", "trade",
                     "license", "permit"],
        "tiktok_hashtags": ["#contractor", "#hvac", "#plumber", "#electrician",
                           "#landscaping", "#smallbizowner", "#trades"],
        "email_hook": "run your business, not your paperwork",
        "price_range": [15, 50],
    },
}


def _load_prospects():
    if PROSPECTS_FILE.exists():
        return json.loads(PROSPECTS_FILE.read_text())
    return []


def _load_gumroad_products():
    products = []
    if GUMROAD_DIR.is_dir():
        for f in GUMROAD_DIR.glob("*.json"):
            try:
                products.append(json.loads(f.read_text()))
            except Exception:
                pass
    return products


def _match_prospect_to_persona(prospect):
    """Score each prospect against every buyer persona. Return best match."""
    title = (prospect.get("job_title", "") or "").lower()
    industry = (prospect.get("industry", "") or prospect.get("segment", "") or "").lower()
    company = (prospect.get("company", "") or "").lower()
    email = (prospect.get("email", "") or "").lower()

    best_persona = None
    best_score = 0

    for persona_key, persona in BUYER_PERSONAS.items():
        score = 0

        # Title matching (strongest signal)
        for t in persona["titles"]:
            if t in title:
                score += 10
                break

        # Industry matching
        for ind in persona["industries"]:
            if ind in industry:
                score += 5
                break

        # Keyword matching against all fields
        combined = f"{title} {industry} {company}".lower()
        for kw in persona["keywords"]:
            if kw in combined:
                score += 3

        # Email domain hints
        domain = email.split("@")[-1] if "@" in email else ""
        for kw in persona["keywords"][:3]:
            if kw in domain:
                score += 2

        if score > best_score:
            best_score = score
            best_persona = persona_key

    return best_persona, best_score


def segment_prospects():
    """Re-segment all prospects by matching to buyer personas."""
    prospects = _load_prospects()
    products = _load_gumroad_products()

    segments = {k: [] for k in BUYER_PERSONAS}
    segments["unmatched"] = []

    matched = 0
    for p in prospects:
        persona, score = _match_prospect_to_persona(p)
        if persona and score >= 5:
            p["matched_persona"] = persona
            p["persona_score"] = score
            segments[persona].append(p)
            matched += 1
        else:
            segments["unmatched"].append(p)

    # Save demand signals
    signals = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_prospects": len(prospects),
        "matched": matched,
        "unmatched": len(segments["unmatched"]),
        "segments": {},
        "products_by_persona": {},
    }

    for persona_key, persona_prospects in segments.items():
        if persona_key == "unmatched":
            continue
        signals["segments"][persona_key] = {
            "count": len(persona_prospects),
            "description": BUYER_PERSONAS.get(persona_key, {}).get("description", ""),
            "email_hook": BUYER_PERSONAS.get(persona_key, {}).get("email_hook", ""),
            "tiktok_hashtags": BUYER_PERSONAS.get(persona_key, {}).get("tiktok_hashtags", []),
        }

    # Match products to personas
    for product in products:
        name = (product.get("name", "") or "").lower()
        cat = (product.get("category", "") or "").lower()
        for persona_key, persona in BUYER_PERSONAS.items():
            for kw in persona["keywords"]:
                if kw in name or kw in cat:
                    signals["products_by_persona"].setdefault(persona_key, []).append({
                        "name": product.get("name", ""),
                        "price": product.get("price", 0),
                    })
                    break

    # Save
    DEMAND_FILE.parent.mkdir(parents=True, exist_ok=True)
    DEMAND_FILE.write_text(json.dumps(signals, indent=2))

    # Save targeted lists
    TARGETED_DIR.mkdir(parents=True, exist_ok=True)
    for persona_key, persona_prospects in segments.items():
        if not persona_prospects:
            continue
        list_file = TARGETED_DIR / f"{persona_key}.json"
        list_file.write_text(json.dumps(persona_prospects, indent=2))

    log.info("Segmented %d prospects: %d matched, %d unmatched",
             len(prospects), matched, len(segments["unmatched"]))
    for k, v in sorted(segments.items(), key=lambda x: -len(x[1])):
        if v:
            log.info("  %s: %d", k, len(v))

    return signals


def generate_sequence(persona_key):
    """Generate an email sequence for a buyer persona using local LLM."""
    persona = BUYER_PERSONAS.get(persona_key)
    if not persona:
        return None

    # For now, generate a simple 3-step sequence from the persona data
    hook = persona["email_hook"]
    desc = persona["description"]

    sequence = {
        "segment": persona_key,
        "from_name": "docsapp",
        "description": desc,
        "steps": [
            {
                "subject": f"{{first_name}}, {hook}",
                "body": (
                    f"Hi {{first_name}},\n\n"
                    f"I came across {{company}} and thought this might be useful — "
                    f"we've been helping {desc} with ready-to-use templates and tools "
                    f"that save hours of work.\n\n"
                    f"Would it be helpful if I sent over a sample?\n\n"
                    f"Best,\nThe docsapp Team"
                ),
                "delay_days": 0,
            },
            {
                "subject": f"Re: {hook}",
                "body": (
                    f"Hi {{first_name}},\n\n"
                    f"Just following up — I know things get busy. "
                    f"We put together a quick resource specifically for {desc} "
                    f"that's been getting great feedback.\n\n"
                    f"Happy to share if you're interested.\n\n"
                    f"Best,\nThe docsapp Team"
                ),
                "delay_days": 3,
            },
            {
                "subject": f"Last one from me, {{first_name}}",
                "body": (
                    f"Hi {{first_name}},\n\n"
                    f"Don't want to be a pest — just wanted to make sure this "
                    f"landed on your radar. We help {desc} with professional "
                    f"templates and tools at a fraction of the cost.\n\n"
                    f"If the timing's not right, no worries at all.\n\n"
                    f"Best,\nThe docsapp Team"
                ),
                "delay_days": 5,
            },
        ],
    }

    SEQUENCES_DIR.mkdir(parents=True, exist_ok=True)
    seq_file = SEQUENCES_DIR / f"{persona_key}.json"
    seq_file.write_text(json.dumps(sequence, indent=2))
    log.info("Generated sequence for %s", persona_key)
    return sequence


def run():
    """Full demand engine run: segment prospects + generate sequences."""
    signals = segment_prospects()

    # Generate sequences for personas that have prospects
    for persona_key in signals.get("segments", {}):
        count = signals["segments"][persona_key]["count"]
        if count > 0:
            generate_sequence(persona_key)

    return signals


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run()
