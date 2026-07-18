#!/usr/bin/env python3
"""AI-Visibility Index — the converged #1 play. ONE generation scores a whole town.

Over the real GEO scan ledger (core.geo_visibility_probe, already querying paid GPT-4o /
Claude / Gemini and caching each (category, city) MARKET so the whole town costs one set of
calls), this:

  1. AGGREGATOR  — pulls a vertical+metro's public brand list from local_biz_leads.jsonl,
                   scores every business against the ONE cached market, derives percentile
                   bands + a ranked town leaderboard, writes a public live.json.
  2. INDEX PAGE  — a free, faceless, premium (warm-editorial, via core.design_kit) public
                   leaderboard: "ask a leading AI assistant for the best <trade> in <city> —
                   who gets named, who's invisible." STRICTLY factual / reproducible: the exact
                   recorded prompts + timestamp + count of assistants. Public surface shows
                   COUNTS + percentile bands + the businesses AI actually named (a fact). It
                   never disparages and never prints a "you're invisible" verdict on a named
                   business. NEUTRAL branding — no model trademarks in any title/label.
  3. EMPTY CHAIR — the per-prospect first-touch hero page (hailports.com/ai-visibility/<slug>).
                   PRIVATE 1:1 (noindex, unlisted): the verbatim REAL model answer naming the
                   competitors with the prospect absent, fully self-verifiable (exact prompt +
                   recorded model ids + timestamp). If no hosted model answered, it is labelled
                   a "simulation" — a local sim is NEVER passed off as a real assistant.
  4. STAGE       — the low-ranked (grade D/F) businesses are written to a prospect list for
                   first-touch. Sending is a SEPARATE gated step; this only stages.

Truth by construction: every grade is derived from a real model's real ranking. No fabrication.

  python3 -m tools.ai_visibility_index --category dentist --city portland --refresh
  python3 -m tools.ai_visibility_index --category dentist --city portland --check
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(os.environ.get("CLAUDE_STACK_DIR", Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(ROOT))

from core import design_kit as dk  # noqa: E402
from core.geo_visibility_probe import (  # noqa: E402
    score_business, query_market, grade_from_score, _slug as _probe_slug,
    QUERY_TEMPLATES,
)
from tools.geo_visibility_pages import (  # noqa: E402
    load_kept, _city_display, _cat_label,
)
from tools.responsive_baseline import (  # noqa: E402
    VIEWPORT_META as _RESP_VIEWPORT, BASELINE_CSS as _RESP_CSS,
)

OUT_DATA = ROOT / "data" / "hustle" / "ai_visibility_index"
DIST = ROOT / "data" / "hustle" / "hailports_dist"
DIST_AV = DIST / "ai-visibility"
PUBLIC_BASE = "https://www.hailports.com/ai-visibility"
BRAND_DESC = "AI Visibility Index"  # neutral, warm-editorial — never a model trademark

# ── paid close. The hailports-branded checkout starters (intake.hailports.com/buy → Stripe
# brand="hailport"); the $39 Fix Kit auto-fulfills to the inbox, the $24/mo watch is the upsell.
BUY_PRIMARY = "https://intake.hailports.com/buy?tier=geo_fix_kit"          # $39 one-time, auto-fulfilled
BUY_SECONDARY = "https://intake.hailports.com/buy?tier=site_health_watch"  # $24/mo always-on watch


def _buy_cta(primary_label: str = "Get the AI-Visibility Fix Kit — $39") -> str:
    """The paid close, reused on every funnel surface: the $39 one-time Fix Kit (auto-fulfilled to
    the inbox in minutes) as the primary CTA, the $24/mo watch as the recurring upsell underneath."""
    return (
        f'<p style="margin:8px 0 0"><a class="cta" href="{BUY_PRIMARY}">{primary_label} &rarr;</a></p>'
        f'<p style="margin:7px 0 0;font-size:14px;color:var(--muted)">prefer it on autopilot? '
        f'<a href="{BUY_SECONDARY}">put it on watch for $24/mo</a></p>'
    )


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(s or "").lower()).strip("-")


def page_slug(business: str, city: str) -> str:
    return _slug(f"{business}-{city}")[:90]


# ───────────────────────────────────────────────────────── neutral model phrasing (public)
# HARD anonymity/trademark rail: the PUBLIC index never prints a model trademark in any
# title/label. We disclose the exact prompts + count + timestamp (fully reproducible — anyone
# can paste the prompt into any assistant) without naming Claude/Anthropic/etc. publicly.
def _public_model_phrase(n_models: int) -> str:
    if n_models >= 3:
        return "three leading AI assistants"
    if n_models == 2:
        return "two leading AI assistants"
    return "a leading AI assistant"


# The PRIVATE 1:1 Empty Chair page MUST be self-verifiable, so it lists the real recorded model
# ids (requirement: "exact prompt, model ids, timestamp so they can re-check"). Those ids appear
# only in the factual evidence/sources block — never in a page title, CTA, or marketing label.
def _private_model_labels(scan: dict) -> list[str]:
    out, seen = [], set()
    for a in scan.get("_market", {}).get("answers", []):
        lbl = (a.get("model_label") or a.get("source") or "").strip()
        if lbl and lbl not in seen:
            seen.add(lbl)
            out.append(lbl)
    if out:
        return out
    return list(scan.get("sources") or [])


# ───────────────────────────────────────────────────────── aggregator
def load_brand_list(category: str, city: str, limit: int = 0) -> list[dict]:
    """Public brand list for (lead-category, lead-city) from the leads ledger, readable-name
    filtered + de-duped (reuses tools.geo_visibility_pages.load_kept)."""
    cat = category.strip().lower()
    cy = city.strip().lower()
    out, seen = [], set()
    for b in load_kept():
        if b["category"].lower() != cat:
            continue
        if b["city"].lower() != cy:
            continue
        key = b["name"].lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(b)
    if limit:
        out = out[:limit]
    return out


def _band(score: float, appearances: int) -> str:
    if appearances == 0:
        return "never"
    if score >= 70:
        return "strong"
    if score >= 40:
        return "sometimes"
    return "rarely"


_BAND_LABEL = {
    "strong": "Named in most answers",
    "sometimes": "Named occasionally",
    "rarely": "Rarely named",
    "never": "Never named",
}


def score_town(category: str, city: str, *, refresh: bool = False, limit: int = 0) -> dict:
    """Score every business in the brand list against the ONE cached market.

    refresh=True regenerates the MARKET exactly once (a single set of paid calls), then every
    business is scored off that cached market at $0 — the whole-town-for-one-generation play."""
    brands = load_brand_list(category, city, limit=limit)
    if not brands:
        raise SystemExit(f"no brand list for {category!r} in {city!r} — check leads ledger")
    city_disp = brands[0]["city_display"] or _city_display(city)
    cat_label = _cat_label(category)

    # Refresh the market once up front (not per-business), so the town costs one generation.
    market = query_market(cat_label, city_disp, refresh=refresh)

    scored: list[dict] = []
    for b in brands:
        scan = score_business(b["name"], city_disp, cat_label, refresh=False)
        scored.append({
            "name": b["name"], "website": b.get("website", ""),
            "grade": scan["grade"], "visibility_score": scan["visibility_score"],
            "appearances": scan["appearances"], "prompts_run": scan["prompts_run"],
            "avg_rank": scan["avg_rank"], "band": _band(scan["visibility_score"], scan["appearances"]),
            "slug": page_slug(b["name"], city_disp),
            "_scan": scan,
        })

    # rank by visibility (desc), then appearances, then better avg_rank
    scored.sort(key=lambda s: (-s["visibility_score"], -s["appearances"],
                               s["avg_rank"] if s["avg_rank"] is not None else 99))
    n = len(scored)
    for i, s in enumerate(scored):
        s["rank"] = i + 1
        # percentile = share of the town this business sits at or above (100 = best)
        s["percentile"] = round(100.0 * (n - i) / n)

    # businesses the AI ACTUALLY named, across the market (a fact — safe to show publicly)
    named: dict[str, int] = {}
    for a in market.get("answers", []):
        for bz in a.get("businesses", []):
            named[bz] = named.get(bz, 0) + 1
    named_leaderboard = sorted(
        ({"name": k, "mentions": v} for k, v in named.items() if len(k) > 2),
        key=lambda x: -x["mentions"],
    )[:12]

    bands = {k: sum(1 for s in scored if s["band"] == k) for k in _BAND_LABEL}
    low = [s for s in scored if s["grade"] in ("D", "F")]

    return {
        "category": category, "category_label": cat_label, "city": city, "city_display": city_disp,
        "n_businesses": n, "n_low": len(low),
        "bands": bands, "named_leaderboard": named_leaderboard,
        "market_sources": market.get("sources", []),
        "n_models": len(market.get("models_queried", [])) or len(market.get("sources", [])),
        "is_simulation": bool(market.get("is_simulation", True)),
        "market_generated_at": market.get("generated_at"),
        "prompts": sorted({a.get("prompt", "") for a in market.get("answers", []) if a.get("prompt")}),
        "scored": scored,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "_market": market,
    }


# ───────────────────────────────────────────────────────── premium CSS (warm editorial)
def _css() -> str:
    p = dk.PALETTES[0]  # "claude"-register warm paper/ink/clay (neutral codename, internal only)
    fp = dk.FONT_PAIRS[0]
    ts = dk.TYPE_SCALE
    return f"""
*{{box-sizing:border-box;margin:0;padding:0}}
:root{{--bg:{p['bg']};--surface:{p['surface']};--panel:{p['panel']};--ink:{p['ink']};
--muted:{p['muted']};--border:{p['border']};--primary:{p['primary']};--accent:{p['accent']}}}
html{{-webkit-text-size-adjust:100%}}
body{{background:var(--bg);color:var(--ink);font-family:{fp['body']};line-height:{ts['line_height']};
-webkit-font-smoothing:antialiased;font-size:{ts['body']}px}}
.wrap{{max-width:880px;margin:0 auto;padding:clamp(28px,6vw,72px) 20px 96px}}
h1,h2,h3{{font-family:{fp['head']};letter-spacing:{ts['head_tracking']}em;line-height:1.12;font-weight:600}}
h1{{font-size:clamp(30px,6vw,{ts['hero']}px);margin:14px 0 10px}}
h2{{font-size:clamp(22px,4vw,{ts['h2']}px);margin:48px 0 14px}}
h3{{font-size:{ts['h3']}px;margin:0 0 6px}}
p{{margin:0 0 14px}}
.eyebrow{{font:600 13px/1.2 {fp['body']};letter-spacing:.14em;text-transform:uppercase;color:var(--primary)}}
.rule{{width:48px;height:4px;border-radius:2px;background:var(--accent);margin:14px 0 0}}
.lead{{font-size:clamp(17px,2.4vw,{ts['lead']}px);color:var(--muted)}}
.card{{background:var(--surface);border:1px solid var(--border);border-radius:16px;
padding:24px 22px;margin:18px 0;box-shadow:0 1px 0 var(--border),0 18px 40px -28px rgba(20,20,19,.28)}}
.bands{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:18px 0}}
.band{{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:16px 16px 14px}}
.band .v{{font-family:{fp['head']};font-size:38px;line-height:1;font-weight:600}}
.band .l{{font-size:13px;color:var(--muted);margin-top:6px}}
.band.never .v,.band.rarely .v{{color:var(--primary)}}
.band.strong .v{{color:#2f6f4f}}
table{{border-collapse:collapse;width:100%;font-size:15px;margin:8px 0}}
th,td{{text-align:left;padding:10px 12px;border-bottom:1px solid var(--border)}}
th{{font:600 12px/1.2 {fp['body']};letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}}
td.n{{font-variant-numeric:tabular-nums;color:var(--muted)}}
.pill{{display:inline-block;font:600 12px/1 {fp['body']};padding:5px 9px;border-radius:999px;
background:var(--panel);color:var(--primary)}}
.method{{background:var(--panel);border:1px solid var(--border);border-radius:14px;padding:18px 20px;font-size:14px;color:var(--muted)}}
.method code{{background:var(--surface);border:1px solid var(--border);border-radius:7px;padding:2px 7px;
font-family:{fp.get('mono','ui-monospace,Menlo,monospace')};font-size:13px;color:var(--ink);display:inline-block;margin:3px 0}}
.grade{{font-family:{fp['head']};font-weight:600;font-size:64px;line-height:1}}
.g-f,.g-d{{color:var(--primary)}}.g-c{{color:#b5872f}}.g-b,.g-a{{color:#2f6f4f}}
.verbatim{{background:var(--panel);border-left:3px solid var(--accent);border-radius:0 10px 10px 0;
padding:14px 18px;margin:12px 0;font-size:15px;white-space:pre-wrap}}
.foot{{margin-top:40px;font-size:13px;color:var(--muted);border-top:1px solid var(--border);padding-top:18px}}
a{{color:var(--primary)}}
.cta{{display:inline-block;background:var(--ink);color:var(--bg);text-decoration:none;font-weight:600;
padding:13px 22px;border-radius:12px;margin:8px 0}}
""" + _RESP_CSS


# Anonymity/trademark fail-closed scrub for anything we emit into machine-readable schema:
# if a model trademark or an operator-identity token ever slips into the structured data, drop
# the block entirely (omit > leak). Public schema stays as neutral as the visible page.
_SCHEMA_LEAK = re.compile(
    r"\b(chatgpt|gpt-?\d|openai|claude|anthropic|gemini|bard|perplexity|copilot|llama|mistral|"
    r"Operator|Operator|CompanyA)\b", re.I)


def _jsonld(obj: dict) -> str:
    """Serialize a schema.org object to a fail-closed <script type=application/ld+json>.
    Returns '' (no block) if the payload would leak a model trademark or operator identity."""
    try:
        payload = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return ""
    if _SCHEMA_LEAK.search(payload):
        return ""
    return f'<script type="application/ld+json">{payload}</script>'


def _head(title: str, desc: str, *, noindex: bool, canonical: str, head_extra: str = "") -> str:
    robots = "noindex,nofollow" if noindex else "index,follow"
    return (f"<!DOCTYPE html><html lang=en><head><meta charset=utf-8>"
            f'{_RESP_VIEWPORT}'
            f"<title>{title}</title><meta name=description content=\"{desc}\">"
            f'<meta name=robots content="{robots}">'
            f'<link rel=canonical href="{canonical}">'
            f"{head_extra}"
            f"<style>{_css()}</style></head><body><div class=wrap>")


def _fmt_ts(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%b %-d, %Y %H:%M UTC")
    except Exception:
        return str(iso)


# ───────────────────────────────────────────────────────── public index page
def render_index_page(town: dict) -> str:
    cat = town["category_label"]
    city = town["city_display"]
    n = town["n_businesses"]
    bands = town["bands"]
    sim = town["is_simulation"]
    models_phrase = _public_model_phrase(town["n_models"])
    ts = _fmt_ts(town["market_generated_at"])
    title = f"{BRAND_DESC}: {cat.title()}s in {city}"
    desc = (f"When you ask {models_phrase} for the best {cat} in {city}, who gets named and who is "
            f"invisible? {bands['never']} of {n} local {cat} businesses were named zero times.")
    canonical = f"{PUBLIC_BASE}/{_slug(cat+'s-'+city)}"

    band_cells = "".join(
        f'<div class="band {k}"><div class="v">{bands[k]}</div>'
        f'<div class="l">{_BAND_LABEL[k]}</div></div>'
        for k in ("strong", "sometimes", "rarely", "never")
    )
    # named leaderboard = businesses the AI actually named (a fact; positive framing, no disparagement)
    named_rows = "".join(
        f"<tr><td>{i+1}</td><td>{_esc(b['name'])}</td>"
        f'<td class="n">{b["mentions"]} {"mention" if b["mentions"] == 1 else "mentions"}</td></tr>'
        for i, b in enumerate(town["named_leaderboard"])
    ) or '<tr><td colspan=3 class="n">No business was named consistently.</td></tr>'

    prompts_html = "".join(f"<code>{_esc(p)}</code><br>" for p in town["prompts"][:6])
    sim_note = ("" if not sim else
                '<p class="lead"><strong>Note:</strong> this run used an open-model simulation '
                '(no hosted assistant answered); treat it as indicative, not a live assistant result.</p>')

    dataset = _jsonld({
        "@context": "https://schema.org",
        "@type": "Dataset",
        "name": title,
        "description": desc,
        "url": canonical,
        "creator": {"@type": "Organization", "name": "Hailports",
                    "url": "https://www.hailports.com"},
        "publisher": {"@type": "Organization", "name": "Hailports",
                      "url": "https://www.hailports.com"},
        "dateModified": town.get("market_generated_at") or "",
        "spatialCoverage": {"@type": "Place", "name": city},
        "measurementTechnique": ("Buyer-intent prompts run against leading AI assistants; the "
                                 "businesses each assistant named in its answers are recorded verbatim."),
        "variableMeasured": f"How often each local {cat} business is named in AI assistant answers",
        "isAccessibleForFree": True,
    })
    return _head(title, desc, noindex=False, canonical=canonical, head_extra=dataset) + f"""
<div class="eyebrow">{BRAND_DESC}</div>
<h1>Ask AI for the best {cat} in {city}.<br>Who gets named — and who's invisible?</h1>
<div class="rule"></div>
<p class="lead">We asked {models_phrase} the everyday questions a customer types when they want a
{cat} in {city}, and recorded — verbatim — which local businesses got named. This is what AI
recommends in {city} right now.</p>
{sim_note}

<div class="bands">{band_cells}</div>
<p class="lead"><strong>{bands['never']} of {n}</strong> local {cat} businesses we checked were
named <strong>zero times</strong> across every answer — effectively invisible when a customer asks AI.</p>

<h2>Who AI names in {city}</h2>
<p class="lead">These businesses were named, by AI, in answer to the prompts below — most-mentioned first.</p>
<table><thead><tr><th>#</th><th>Business AI named</th><th>Times named</th></tr></thead>
<tbody>{named_rows}</tbody></table>

<h2>How this was measured</h2>
<div class="method">
<p>We asked {models_phrase} the exact buyer questions below and recorded which {cat} businesses
each one named, in order. Grades reflect how often a business was named across the answers. The
method is reproducible — paste any prompt into a leading assistant and check for yourself:</p>
{prompts_html}
<p style="margin-top:10px">Run captured: <strong>{ts}</strong> · assistants queried: <strong>{town['n_models']}</strong>
· businesses checked: <strong>{n}</strong>. Informational only; AI answers vary by model and date.
Each business's own per-assistant detail (with the exact assistant identifiers) is on its private report.</p>
</div>

<p style="margin-top:28px">Run a business that should be on this list? <a class="cta"
href="{PUBLIC_BASE}/check">See your own AI-visibility report — free</a></p>
<p style="margin:14px 0 4px;font-size:15px;color:var(--muted)">Ready to get named in AI answers now?</p>
{_buy_cta()}

<div class="foot">{BRAND_DESC} · a {city} snapshot of how AI assistants answer "best {cat} in {city}."
Names shown are businesses AI named in its own answers. Not affiliated with, or endorsed by, any
business or AI provider. Counts and bands only; no business is singled out as "invisible" here.</div>
</div></body></html>"""


# ───────────────────────────────────────────────────────── per-prospect Empty Chair page
def render_empty_chair(s: dict, town: dict, *, checkout_url: str = "#") -> str:
    """The private 1:1 first-touch hero page for ONE prospect: the verbatim REAL assistant answer
    naming competitors with the prospect absent, fully self-verifiable. NEUTRAL title/CTA; the
    real model ids appear only in the factual evidence block."""
    scan = s["_scan"]
    biz = s["name"]
    cat = town["category_label"]
    city = town["city_display"]
    sim = bool(scan.get("is_simulation", True))
    grade = s["grade"]
    ap, npr = s["appearances"], s["prompts_run"]
    ts = _fmt_ts(scan.get("_market", {}).get("generated_at"))
    labels = _private_model_labels(scan)
    title = f"AI Visibility Report — {biz}"
    canonical = f"{PUBLIC_BASE}/{s['slug']}"

    # pick the single most damning verbatim answer: one that names competitors but NOT the prospect
    from core.geo_visibility_probe import _same_business
    best = None
    for a in scan.get("_market", {}).get("answers", []):
        biz_named = any(_same_business(biz, x) for x in a.get("businesses", []))
        if a.get("businesses") and not biz_named:
            best = a
            break
    if best is None:  # fallback: any answer
        ans = [a for a in scan.get("_market", {}).get("answers", []) if a.get("answer")]
        best = ans[0] if ans else None

    if best:
        verbatim = best.get("answer", "").strip()
        b_prompt = best.get("prompt", "")
        b_label = best.get("model_label") or best.get("source") or "an AI assistant"
        b_label_disp = b_label if not sim else f"{b_label} (simulation)"
        evidence = f"""
<h2>The exact answer, verbatim</h2>
<p class="lead">Asked <strong>"{_esc(b_prompt)}"</strong>, {_esc(b_label_disp)} answered:</p>
<div class="verbatim">{_esc(verbatim)}</div>
<p>{_esc(biz)} is not in that answer. The customer never sees you.</p>"""
    else:
        evidence = "<p>No assistant answer was captured for this market.</p>"

    sources_line = ", ".join(_esc(x) for x in labels) or "open AI models"
    sim_banner = ("" if not sim else
                  '<div class="method" style="border-color:var(--accent)"><strong>Simulation:</strong> '
                  "no hosted assistant answered for this market in this run, so the result above is from "
                  "an open model and is labelled a simulation — not a live assistant result.</div>")
    verdict = (f"AI never named {biz}" if ap == 0
               else f"AI named {biz} in only {ap} of {npr} answers")

    return _head(title, f"{biz}: how AI answers \"best {cat} in {city}\".",
                 noindex=True, canonical=canonical) + f"""
<div class="eyebrow">{BRAND_DESC} · private report</div>
<h1>{verdict} for "{cat} in {city}."</h1>
<div class="rule"></div>
<p class="grade g-{grade.lower()}">{grade}</p>
<p class="lead">We asked {_public_model_phrase(town['n_models'])} the questions a {city} customer
asks when they want a {cat}. {biz} was named in <strong>{ap} of {npr}</strong> answers.</p>
{sim_banner}
{evidence}

<h2>Verify it yourself</h2>
<div class="method">
<p>Nothing here is our opinion — it's the assistant's own output. To re-check:</p>
<p>Assistant(s) queried: <strong>{sources_line}</strong><br>
Captured: <strong>{ts}</strong><br>
Paste any prompt below into that assistant and read the answer:</p>
{''.join(f'<code>{_esc(p)}</code><br>' for p in town['prompts'][:6])}
</div>

<p style="margin-top:26px"><strong>Want {_esc(biz)} named in those answers?</strong> The
AI-Visibility Fix Kit gives you everything to become machine-readable — a ready-to-upload llms.txt,
homepage schema, and the GEO content templates AI pulls answers from — delivered to your inbox in minutes.</p>
{_buy_cta()}
<p style="margin-top:16px"><a href="{_esc(checkout_url)}">Want to see how it works first? Show me how to get named &rarr;</a></p>
<div class="foot">Private report for {_esc(biz)}. Grades reflect AI model output at the time of the
scan and vary by model and date. Informational only; not affiliated with any AI provider.</div>
</div></body></html>"""


def _esc(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


# ───────────────────────────────────────────────────────── public self-serve /check funnel page
# Town-independent. The CTA every index + Empty Chair page points at. Two jobs, both safe:
#   1. IMMEDIATE free value — the real buyer prompts to paste into any assistant right now (the
#      free-proof on-ramp; nothing to wait for, no backend).
#   2. Capture a free done-for-you report via the SAME proven intake endpoint the guide pages use
#      (intake.hailports.com/guides/capture, {email,website,slug}). No new backend, no auto-send.
# HARD rails: neutral (no model trademarks), faceless (no real name/PII, no cross-brand refs).
def render_check_page() -> str:
    title = f"{BRAND_DESC}: does AI name your business?"
    desc = ("When a customer asks a leading AI assistant for the best business in your area, does "
            "yours get named? Run the same prompts we use — free — or get a done-for-you report.")
    canonical = f"{PUBLIC_BASE}/check"
    intake = "https://intake.hailports.com/guides/capture"

    # show the real prompt bank as fill-in-the-blank buyer questions (reproducible, neutral)
    prompt_codes = "".join(
        f"<code>{_esc(t.format(category='[your service]', city='[your city]'))}</code><br>"
        for t in QUERY_TEMPLATES
    )

    form = f"""
<form id="cf" onsubmit="cfSubmit(event)" style="margin:14px 0 0">
  <input id="cf-email" type="email" required placeholder="user@example.com"
   autocomplete="email" style="width:100%;box-sizing:border-box;padding:12px 13px;margin:0 0 9px;
   border-radius:11px;border:1px solid var(--border);background:var(--surface);color:var(--ink);font-size:15px">
  <input id="cf-site" type="text" placeholder="yourbusiness.com (so we can see what AI sees)"
   style="width:100%;box-sizing:border-box;padding:12px 13px;margin:0 0 9px;border-radius:11px;
   border:1px solid var(--border);background:var(--surface);color:var(--ink);font-size:15px">
  <input id="cf-biz" type="text" placeholder="business name + city (optional)"
   style="width:100%;box-sizing:border-box;padding:12px 13px;margin:0 0 11px;border-radius:11px;
   border:1px solid var(--border);background:var(--surface);color:var(--ink);font-size:15px">
  <button class="cta" type="submit" style="border:0;cursor:pointer;width:100%">Send me my free report &rarr;</button>
</form>
<p id="cf-msg" style="margin:11px 0 0;font-size:13px;color:var(--muted);min-height:17px"></p>
<script>
function cfSubmit(e){{e.preventDefault();
 var em=document.getElementById('cf-email').value.trim(),
     st=document.getElementById('cf-site').value.trim(),
     bz=document.getElementById('cf-biz').value.trim(),
     m=document.getElementById('cf-msg');
 m.textContent='Sending…';
 fetch('{intake}',{{method:'POST',headers:{{'Content-Type':'application/json'}},
  body:JSON.stringify({{email:em,website:st,business:bz,slug:'ai-visibility-check'}})}})
  .then(function(r){{return r.json()}}).then(function(d){{
   m.textContent=d&&d.ok?'Got it — your report is on the way (check spam, just in case).'
    :'Hmm, that didn\\'t go through. Mind trying again?';
   if(d&&d.ok){{document.getElementById('cf').style.display='none'}}}})
  .catch(function(){{m.textContent='Network hiccup — try again in a sec.'}});}}
</script>"""

    faq = _jsonld({
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [
            {"@type": "Question",
             "name": "Does AI name my business when a customer asks for the best in my area?",
             "acceptedAnswer": {"@type": "Answer", "text": (
                 "More buyers now ask an AI assistant “who’s the best [service] near me?” "
                 "before they open a map or a search page. The assistant names a handful of businesses "
                 "and skips the rest. If it never names yours, you’re invisible at the moment "
                 "someone is ready to buy.")}},
            {"@type": "Question",
             "name": "How do I check whether AI recommends my business?",
             "acceptedAnswer": {"@type": "Answer", "text": (
                 "Open any leading AI assistant and paste the real buyer questions — “best "
                 "[service] in [city]?” and similar — with your service and city filled in. "
                 "Read whether your business shows up, and which businesses it names instead.")}},
            {"@type": "Question",
             "name": "Can I get a done-for-you AI-visibility report?",
             "acceptedAnswer": {"@type": "Answer", "text": (
                 "Yes, for free. We run the full prompt set for your category and city, record verbatim "
                 "which businesses AI names, and send a plain report: your grade, who is getting named "
                 "instead of you, and the exact prompts and timestamp so you can re-check it yourself.")}},
            {"@type": "Question",
             "name": "How does the AI-visibility check work?",
             "acceptedAnswer": {"@type": "Answer", "text": (
                 "We ask leading AI assistants the everyday questions a customer types when they want a "
                 "business like yours, and record which businesses each one names. Grades reflect how "
                 "often you are named across the answers. It is fully reproducible — every prompt "
                 "and timestamp is on the report. Informational only; AI answers vary by model and date.")}},
        ],
    })
    return _head(title, desc, noindex=False, canonical=canonical, head_extra=faq) + f"""
<div class="eyebrow">{BRAND_DESC}</div>
<h1>When a customer asks AI for the best in your field,<br>does your business get named?</h1>
<div class="rule"></div>
<p class="lead">More buyers now ask an AI assistant "who's the best [service] near me?" before they ever
open a map or a search page. The assistant names a handful of businesses — and skips the rest. If it
never names yours, you're invisible at the exact moment someone's ready to buy.</p>

<h2>Check it yourself — right now, free</h2>
<div class="method">
<p>No tool required. Open any leading AI assistant and paste these — the real buyer questions we run,
with your service and city filled in. Read whether your business shows up, and who it names instead:</p>
{prompt_codes}
<p style="margin-top:10px">If your name isn't in the answer, that's the gap. The businesses it <em>does</em>
name are the ones winning that customer.</p>
</div>

<h2>Or get a done-for-you report — free</h2>
<p class="lead">We'll run the full prompt set for your category and city, record verbatim which
businesses AI names, and send you a plain report: your grade, who's getting named instead of you,
and the exact prompts + timestamp so you can re-check anything yourself. No charge, no obligation.</p>
{form}

<h2>Ready to get named now?</h2>
<p class="lead">Skip the wait — the AI-Visibility Fix Kit gives you everything to become machine-readable:
a ready-to-upload llms.txt, homepage JSON-LD schema, and 5 GEO content templates AI pulls answers
from — delivered to your inbox in minutes.</p>
{_buy_cta()}

<h2>How it works</h2>
<div class="method">
<p>We ask leading AI assistants the everyday questions a customer types when they want a business
like yours, and record — verbatim — which businesses each one names. Grades reflect how often you're
named across the answers. It's fully reproducible: every prompt and timestamp is on your report, so
you can paste any of them into an assistant and see the same thing. Informational only; AI answers
vary by model and date.</p>
</div>

<div class="foot">{BRAND_DESC} · a free check of how AI assistants answer "best in your area." Names
shown in any report are businesses AI named in its own answers. Not affiliated with, or endorsed by,
any business or AI provider.</div>
</div></body></html>"""


def write_check_page() -> str:
    """Write the town-independent /check funnel page into the deployable tree. No scoring, no API."""
    DIST_AV.mkdir(parents=True, exist_ok=True)
    out = DIST_AV / "check.html"
    out.write_text(render_check_page(), encoding="utf-8")
    return str(out)


# ───────────────────────────────────────────────────────── public live.json (neutral)
def public_live_json(town: dict) -> dict:
    """Public-safe JSON: counts/bands + named leaderboard + neutral methodology. No prospect
    names, no per-business 'invisible' verdicts, no model trademarks."""
    return {
        "index": BRAND_DESC,
        "category": town["category_label"], "city": town["city_display"],
        "businesses_checked": town["n_businesses"],
        "assistants_queried": town["n_models"],
        "bands": {_BAND_LABEL[k]: v for k, v in town["bands"].items()},
        "named_by_ai": town["named_leaderboard"],
        "prompts": town["prompts"][:6],
        "is_simulation": town["is_simulation"],
        "captured": town["market_generated_at"],
        "methodology": (f"Asked {_public_model_phrase(town['n_models'])} the listed buyer prompts; "
                        "recorded which businesses each named. Reproducible; informational only."),
        "generated_at": town["generated_at"],
    }


# ───────────────────────────────────────────────────────── build / stage / write
def build(category: str, city: str, *, refresh: bool = False, limit: int = 0,
          checkout_url: str = "#", write_dist: bool = True) -> dict:
    town = score_town(category, city, refresh=refresh, limit=limit)
    OUT_DATA.mkdir(parents=True, exist_ok=True)
    key = _slug(f"{town['category_label']}s-{town['city_display']}")

    # full internal record (keeps per-business scans for audit) — strip the heavy _market dupes
    internal = {k: v for k, v in town.items() if k != "_market"}
    internal["scored"] = [{k: v for k, v in s.items() if k != "_scan"} for s in town["scored"]]
    (OUT_DATA / f"{key}.json").write_text(json.dumps(internal, indent=2), encoding="utf-8")

    # public live.json
    (OUT_DATA / f"{key}.live.json").write_text(
        json.dumps(public_live_json(town), indent=2), encoding="utf-8")

    # stage prospects (low-ranked = D/F = pre-qualified buyers). NO SEND — list only.
    prospects = [s for s in town["scored"] if s["grade"] in ("D", "F")]
    with (OUT_DATA / f"prospects_{key}.jsonl").open("w", encoding="utf-8") as f:
        for s in prospects:
            f.write(json.dumps({
                "name": s["name"], "website": s.get("website", ""),
                "city": town["city_display"], "category": town["category_label"],
                "grade": s["grade"], "visibility_score": s["visibility_score"],
                "appearances": s["appearances"], "prompts_run": s["prompts_run"],
                "rank": s["rank"], "percentile": s["percentile"],
                "empty_chair_slug": s["slug"],
                "empty_chair_url": f"{PUBLIC_BASE}/{s['slug']}",
                "staged_at": datetime.now(timezone.utc).isoformat(),
                "sent": False,
            }) + "\n")

    written = {"data": str(OUT_DATA / f'{key}.json'),
               "live_json": str(OUT_DATA / f'{key}.live.json'),
               "prospects": str(OUT_DATA / f'prospects_{key}.jsonl')}

    if write_dist:
        DIST_AV.mkdir(parents=True, exist_ok=True)
        # public Index page (indexable) — at /ai-visibility/<cat>s-<city> AND /ai-visibility/ index
        idx_html = render_index_page(town)
        (DIST_AV / f"{key}.html").write_text(idx_html, encoding="utf-8")
        (DIST_AV / "index.html").write_text(idx_html, encoding="utf-8")
        (DIST_AV / f"{key}.live.json").write_text(
            json.dumps(public_live_json(town), indent=2), encoding="utf-8")
        # per-prospect Empty Chair pages (noindex, unlisted) — only the low-ranked staged buyers
        for s in prospects:
            html_ = render_empty_chair(s, town, checkout_url=checkout_url)
            (DIST_AV / f"{s['slug']}.html").write_text(html_, encoding="utf-8")
        written["index_page"] = str(DIST_AV / f"{key}.html")
        written["index_default"] = str(DIST_AV / "index.html")
        written["empty_chair_dir"] = str(DIST_AV)
        written["empty_chair_count"] = len(prospects)
        # the town-independent /check funnel page every page CTAs to (kept fresh on each build)
        written["check_page"] = write_check_page()

    summary = {
        "category": town["category_label"], "city": town["city_display"],
        "n_businesses": town["n_businesses"], "n_low_ranked": town["n_low"],
        "bands": {_BAND_LABEL[k]: v for k, v in town["bands"].items()},
        "is_simulation": town["is_simulation"],
        "models": town["n_models"], "sources": town["market_sources"],
        "captured": town["market_generated_at"],
        "public_index_url": f"{PUBLIC_BASE}/{key}",
        "sample_empty_chair": (f"{PUBLIC_BASE}/{prospects[0]['slug']}" if prospects else None),
        "written": written,
    }
    return summary


def check(category: str, city: str) -> int:
    brands = load_brand_list(category, city)
    cat_label = _cat_label(category)
    city_disp = brands[0]["city_display"] if brands else _city_display(city)
    cache = query_market(cat_label, city_disp, refresh=False)
    print(f"brand list: {len(brands)} {cat_label} businesses in {city_disp}")
    print(f"market cache: backend={cache.get('backend')} is_sim={cache.get('is_simulation')} "
          f"n_answers={cache.get('n_answers')} sources={cache.get('sources')}")
    print(f"cache from_cache={cache.get('from_cache')} generated_at={cache.get('generated_at')}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--category", default="dentist")
    ap.add_argument("--city", default="portland")
    ap.add_argument("--refresh", action="store_true", help="regenerate the market once (paid calls)")
    ap.add_argument("--limit", type=int, default=0, help="cap brand list size (testing)")
    ap.add_argument("--checkout-url", default="https://www.hailports.com/ai-visibility/check")
    ap.add_argument("--no-dist", action="store_true", help="skip writing the deployable HTML")
    ap.add_argument("--check", action="store_true", help="report brand-list + cache, no scoring")
    ap.add_argument("--build-check", action="store_true",
                    help="write ONLY the /check funnel page (no scoring, no API calls)")
    args = ap.parse_args(argv)
    if args.build_check:
        print("check_page:", write_check_page())
        return 0
    if args.check:
        return check(args.category, args.city)
    summary = build(args.category, args.city, refresh=args.refresh, limit=args.limit,
                    checkout_url=args.checkout_url, write_dist=not args.no_dist)
    print("SUMMARY:", json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
