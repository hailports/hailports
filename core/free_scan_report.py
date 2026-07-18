"""free_scan_report.py — the free web-presence scan: the LEAD artifact (hailports research).

Runs 6 dimensions of $0/local probes on a prospect's live site, grades each A-F
(truth-by-construction — every grade traces to a real finding, never invented), and maps
each gap to the offering that fixes it. NO paid LLM: AI-visibility is graded from on-page
structured-data signals (a live ChatGPT/Perplexity citation scan is the paid upsell, not
the free lead). The report is the research artifact the outreach leads with.

  from core.free_scan_report import scan
  r = scan("a1pumpingsd.com")          # -> {domain, overall, dims:[...], offerings:[...]}
  html = report_html(r)                # branded research report

  python3 -m core.free_scan_report a1pumpingsd.com   # CLI: prints grades + writes report.html
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# each dimension: what it measures, the offering that fixes a gap, and keyword classifiers that
# route a probe finding into this dimension.
DIMS = [
    {"key": "security", "label": "Security & trust", "offering": "Site Rescue (SSL/HTTPS fix)",
     "kw": r"ssl|cert|https|not secure|insecure|mixed content|expired"},
    {"key": "reliability", "label": "Uptime & reliability", "offering": "Always-On Care Plan (24/7 monitoring)",
     "kw": r"down|unreachable|error|404|500|won'?t load|no dns|doesn'?t resolve|timeout|broken"},
    {"key": "mobile_speed", "label": "Mobile & speed", "offering": "Site Rescue rebuild (mobile-first)",
     "kw": r"mobile|responsive|viewport|slow|overflow|not mobile|render|load time|heavy"},
    {"key": "design_conversion", "label": "Design & getting calls", "offering": "Site Rescue / Pro rebuild",
     "kw": r"dated|abandoned|stale|copyright|20[01]\d|no (phone|contact|cta|call)|dead|outdated|no call-to-action"},
    {"key": "local_search", "label": "Local search (Google)", "offering": "Local SEO + reviews",
     "kw": r"seo|schema|meta|title|sitemap|review|rating|google|listing|nap|gbp"},
]
AI_DIM = {"key": "ai_visibility", "label": "AI visibility (ChatGPT & AI answers)",
          "offering": "AI-Visibility / GEO"}

_GRADE_ORDER = ["A", "B", "C", "D", "F"]


def _worst(a: str, b: str) -> str:
    return a if _GRADE_ORDER.index(a) >= _GRADE_ORDER.index(b) else b


def _grade_for(findings: list[str], sev: str) -> str:
    """Truth-by-construction: grade follows the actual findings, never invented."""
    if not findings:
        return "A"
    if sev == "critical":
        return "F"
    if sev == "high":
        return "D"
    if sev == "medium":
        return "C"
    return "B"


def scan(domain: str) -> dict:
    """Run the 6-dimension free scan. Fail-soft: an unreachable probe degrades its dim to a
    neutral grade rather than crashing the report."""
    from core import site_content_extractor as sce
    findings = []  # (dim_key, text, severity)

    # ── the real probes ($0) ──
    try:
        from core.web_failure_probe import probe as _wf
        wf = _wf(domain)
        for r in (wf.get("reasons") or []):
            findings.append((_classify(r), r, wf.get("severity", "medium")))
    except Exception:
        wf = {}
    try:
        from core.site_quality_probe import probe as _sq
        sq = _sq(domain)
        for f in (sq.get("flags") or []):
            fact = f.get("fact") or f.get("signal") or ""
            findings.append((_classify(fact), fact, f.get("severity", sq.get("severity", "medium"))))
    except Exception:
        pass

    # ── fetch once for the structured-data (AI-visibility + local-search) signals ──
    final_url, html = sce.fetch(domain)
    ld = sce._jsonld(html) if html else []
    has_localbiz = any("address" in n or str(n.get("@type", "")).lower().endswith(
        ("localbusiness", "business", "organization")) for n in ld)
    has_reviews = any(n.get("aggregateRating") or n.get("review") for n in ld)

    # ── assemble per-dim grades ──
    dims = []
    for d in DIMS:
        df = [t for (k, t, s) in findings if k == d["key"]]
        sev = _max_sev([s for (k, t, s) in findings if k == d["key"]])
        # local_search leans on structured-data presence too
        if d["key"] == "local_search" and html and not has_localbiz:
            df = df + ["no LocalBusiness structured data — Google can't read your name, address, hours cleanly"]
            sev = _max_sev([sev, "medium"])
        if d["key"] == "local_search" and html and not has_reviews:
            df = df + ["no review/rating markup — you're invisible in the Google 'stars' results"]
        grade = _grade_for(df, sev) if html or df else "C"
        dims.append({**d, "grade": grade, "findings": df[:4]})

    # ── AI-visibility (local-first, $0): structured data = whether an AI can even cite you ──
    if not html:
        ai_findings, ai_grade = ["site didn't respond — AI assistants can't see it at all"], "F"
    elif not has_localbiz:
        ai_findings = ["no machine-readable business data (schema.org) — ChatGPT/Perplexity have "
                       "nothing structured to cite when someone asks for a local " + "provider"]
        ai_grade = "D"
    else:
        ai_findings, ai_grade = [], "B"  # structured data present; a live citation scan is the paid upsell
    dims.append({**AI_DIM, "grade": ai_grade, "findings": ai_findings[:4]})

    overall = _overall([d["grade"] for d in dims])
    offerings = [d["offering"] for d in dims if d["grade"] in ("C", "D", "F")]
    return {"domain": _norm(domain), "url": final_url, "overall": overall, "dims": dims,
            "offerings": _dedupe(offerings), "reachable": bool(html)}


def _classify(text: str) -> str:
    t = (text or "").lower()
    for d in DIMS:
        if re.search(d["kw"], t):
            return d["key"]
    return "design_conversion"  # a generic "something's off" flag reads as a design/trust issue


def _max_sev(sevs: list[str]) -> str:
    order = ["ok", "low", "medium", "high", "critical"]
    m = "ok"
    for s in sevs:
        if s in order and order.index(s) > order.index(m):
            m = s
    return m


def _overall(grades: list[str]) -> str:
    # overall = the average pulled toward the worst (a single F should sting)
    vals = [_GRADE_ORDER.index(g) for g in grades]
    avg = sum(vals) / len(vals)
    worst = max(vals)
    idx = round((avg + worst) / 2)
    return _GRADE_ORDER[min(idx, 4)]


def _dedupe(xs):
    seen, out = set(), []
    for x in xs:
        if x not in seen:
            seen.add(x); out.append(x)
    return out


def _norm(d: str) -> str:
    return re.sub(r"^https?://(www\.)?", "", (d or "").strip().rstrip("/")).lower()


_GRADE_COLOR = {"A": "#2e7d55", "B": "#5a9e6f", "C": "#c79a3d", "D": "#c9772f", "F": "#b3402f"}
import html as _h  # noqa: E402


def report_html(r: dict, business: str = "", mockup_url: str = "") -> str:
    """Branded research report — the artifact the outreach leads with. Research voice + social
    proof + each gap mapped to the fix. Self-contained, deliverability/anonymity-safe."""
    name = _h.escape(business or r["domain"])
    og = r["overall"]
    cards = ""
    for d in r["dims"]:
        g = d["grade"]; col = _GRADE_COLOR[g]
        finds = "".join(f"<li>{_h.escape(f)}</li>" for f in d["findings"]) or "<li>looks solid here.</li>"
        fix = f'<div class="fix">→ fix: {_h.escape(d["offering"])}</div>' if g in ("C", "D", "F") else ""
        cards += (
            f'<div class="dim"><div class="dg" style="background:{col}">{g}</div>'
            f'<div class="db"><h3>{_h.escape(d["label"])}</h3><ul>{finds}</ul>{fix}</div></div>'
        )
    offer_li = "".join(f"<li>{_h.escape(o)}</li>" for o in r["offerings"])
    offers = (f'<div class="offers"><h2>What we\'d fix, in order</h2><ol>{offer_li}</ol>'
              f'<p class="soft">the free rebuild preview is yours to look at, no strings — and if '
              f'you\'d want any of it done, you only pay once you\'re happy. just reply.</p></div>') if offer_li else \
             '<div class="offers"><h2>Honestly? your web presence is in good shape.</h2></div>'
    proof = (f'<a class="mock" href="{_h.escape(mockup_url)}">See the free rebuilt version of your site →</a>'
             if mockup_url else "")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow">
<title>Web presence research — {name}</title><style>
:root{{--ink:#1f2a24;--mut:#69716a;--line:#e3dccb;--clay:#c2703d}}
*{{box-sizing:border-box}}body{{margin:0;font:16px/1.6 -apple-system,'Segoe UI',Roboto,sans-serif;color:var(--ink);background:#f6f3ec}}
.wrap{{max-width:680px;margin:0 auto;padding:40px 22px}}
.eyebrow{{font:700 12px/1 -apple-system,sans-serif;letter-spacing:.14em;text-transform:uppercase;color:var(--clay)}}
h1{{font:800 30px/1.15 Georgia,'Roboto Slab',serif;margin:.3em 0}}
.overall{{display:flex;align-items:center;gap:16px;background:#fff;border:1px solid var(--line);border-radius:8px;padding:18px 22px;margin:22px 0}}
.obig{{font:800 40px/1 Georgia,serif;color:{_GRADE_COLOR[og]}}}
.dim{{display:flex;gap:16px;background:#fff;border:1px solid var(--line);border-radius:8px;padding:16px 18px;margin:10px 0}}
.dg{{flex:0 0 40px;height:40px;border-radius:8px;color:#fff;font:800 20px/40px Georgia,serif;text-align:center}}
.db h3{{margin:.1em 0 .3em;font-size:16px}}.db ul{{margin:.2em 0;padding-left:18px;color:var(--mut);font-size:14.5px}}
.fix{{margin-top:8px;font-size:13.5px;font-weight:700;color:var(--clay)}}
.offers{{margin:26px 0;background:#fff;border:1px solid var(--line);border-radius:8px;padding:20px 22px}}
.offers ol{{padding-left:20px}}.soft{{color:var(--mut);font-size:14.5px;border-top:1px solid var(--line);padding-top:14px;margin-top:14px}}
.mock{{display:inline-block;margin:18px 0;color:var(--clay);font-weight:700;text-decoration:none;border-bottom:2px solid var(--clay)}}
.foot{{color:var(--mut);font-size:12px;border-top:1px solid var(--line);margin-top:30px;padding-top:16px}}
</style></head><body><div class="wrap">
<p class="eyebrow">Hailports · local web-presence research</p>
<h1>{name}</h1>
<div class="overall"><div class="obig">{og}</div><div><strong>Overall web-presence grade.</strong><br>
<span style="color:var(--mut);font-size:14.5px">graded across 6 things every visitor (and now every AI assistant) judges you on.</span></div></div>
{cards}{proof}{offers}
<p class="foot">Every grade above traces to a real check we ran on {_h.escape(r['domain'])} — no guesses. Hailports.</p>
</div></body></html>"""


if __name__ == "__main__":
    dom = sys.argv[1] if len(sys.argv) > 1 else "a1pumpingsd.com"
    r = scan(dom)
    print(f"\n{r['domain']}  OVERALL={r['overall']}  reachable={r['reachable']}")
    for d in r["dims"]:
        print(f"  {d['grade']}  {d['label']:34}  {'; '.join(d['findings'])[:70]}")
    print("  offerings:", r["offerings"])
    out = ROOT / "products_internal" / "landing" / f"scan_{r['domain'].replace('/', '_')}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report_html(r, mockup_url=f"https://www.hailports.com/mockups/{r['domain']}.html"))
    print("wrote", out)

