#!/usr/bin/env python3
"""
hailports_crosslink.py — turn the ~705 orphaned programmatic guide pages into
topic clusters + conversion paths, as a DURABLE, idempotent deploy-time
post-processor (survives regeneration, safe to re-run — marker-guarded exactly
like tools/responsive_baseline.py).

The problem (from the SEO audit): the guides are indexed/crawled but stuck on
page 8 because they're ORPHANS — almost none cross-link to a sibling, and the
hub links 0x to /products or the free scan, so ranking equity never flows.

What this does (idempotent — re-run adds nothing):
  1. Per guide page: inject a marker-guarded <section id="hp-related"> with the
     6-10 MOST-related sibling guides (same role/vertical/topic cluster) + the
     two conversion links (free scan + products). Clustering = IDF-weighted
     shared-slug-token overlap (see related_for()), which naturally groups the
     "<role>-buyer-intent-signals-<topic>" matrix and the "<tool>-vs-*" /
     "best-<tool>-alternatives" comparison families without any hand-tuned map.
     A page already ends with a scan CTA, so we do NOT add a separate CTA banner
     — the related block itself carries the scan/products links.
  2. Hub guides/index.html: inject a marker-guarded conversion band
     (<div id="hp-hub-cta">) with prominent free-scan + products links (the hub
     had 0 on desktop) and keep the full guide index untouched.

Style matches the guides' dark card palette (#1a2230 / #d98a3d / #e7e2d6),
lightweight, internal links only, self-contained inline <style> (in-body <style>
is already used by these pages).

CLI:
  python3 tools/hailports_crosslink.py --apply <guides_dir> [--links 8]
  python3 tools/hailports_crosslink.py --check <guides_dir>
"""
from __future__ import annotations

import math
import re
import sys
from pathlib import Path

REL_MARKER = "hp-related"     # per-guide related-cluster section
HUB_MARKER = "hp-hub-cta"     # hub conversion band
DEFAULT_LINKS = 8             # sibling links per page (task asks 6-10)

SCAN_HREF = "/ai-visibility/check?utm_source=guides&utm_medium=related&utm_campaign=ai_visibility_scan"
PRODUCTS_HREF = "/products.html"
HUB_SCAN_HREF = "/ai-visibility/check?utm_source=guides&utm_medium=hub&utm_campaign=ai_visibility_scan"

_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.I | re.S)
_BODY_CLOSE_RE = re.compile(r"</body\s*>", re.I)
_BEACON_RE = re.compile(r'<script id="hp-beacon">', re.I)
_FIRST_UL_RE = re.compile(r"<ul\b", re.I)
_BRAND_SUFFIX_RE = re.compile(r"\s*[|—–-]\s*Hailports\s*$", re.I)


# ── clustering ──────────────────────────────────────────────────────────────
def _tokens(slug: str) -> list[str]:
    return [t for t in slug.split("-") if t]


def build_index(files: list[Path]) -> tuple[dict, dict, dict]:
    """Return (slug->token_set, token->doc_freq, slug->title) over the guide set."""
    toks: dict[str, set[str]] = {}
    titles: dict[str, str] = {}
    df: dict[str, int] = {}
    for p in files:
        slug = p.stem
        try:
            html = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        ts = set(_tokens(slug))
        toks[slug] = ts
        for t in ts:
            df[t] = df.get(t, 0) + 1
        m = _TITLE_RE.search(html)
        title = _BRAND_SUFFIX_RE.sub("", m.group(1).strip()) if m else slug.replace("-", " ")
        titles[slug] = re.sub(r"\s+", " ", title).strip() or slug.replace("-", " ")
    return toks, df, titles


def related_for(slug: str, toks: dict, df: dict, n: int) -> list[str]:
    """The n most-related sibling slugs, by IDF-weighted shared-token overlap.

    Rare, meaningful tokens (role / vertical / tool name / topic) dominate over
    ubiquitous scaffolding tokens ('buyer','intent','signals','vs','lead') that
    appear across most slugs, so same-role+same-topic siblings rank first, then
    same-topic, then same-role — no hand-maintained cluster map needed.
    Deterministic tie-break keeps re-runs byte-identical.
    """
    ndocs = max(len(toks), 1)
    idf = {t: math.log((ndocs + 1) / (c + 1)) + 1.0 for t, c in df.items()}
    a = toks.get(slug, set())
    if not a:
        return []
    scored = []
    for other, b in toks.items():
        if other == slug:
            continue
        shared = a & b
        if not shared:
            continue
        score = sum(idf[t] for t in shared)
        scored.append((score, len(shared), len(b), other))
    # highest score, then most shared tokens, then tighter (fewer tokens), then slug
    scored.sort(key=lambda x: (-x[0], -x[1], x[2], x[3]))
    return [s[3] for s in scored[:n]]


# ── injection (idempotent) ──────────────────────────────────────────────────
def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_REL_CSS = (
    "#hp-related{max-width:760px;margin:34px auto 8px;padding:20px 22px 18px;background:#1a2230;"
    "border:1px solid #2c3644;border-radius:10px;"
    "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#e7e2d6}"
    "#hp-related .hpr-h{font:700 18px/1.3 Georgia,'Times New Roman',serif;color:#e7e2d6;margin:0 0 4px}"
    "#hp-related .hpr-sub{margin:0 0 12px;color:#9aa3ad;font-size:13.5px}"
    "#hp-related ul{list-style:none;margin:0 0 14px;padding:0;columns:2;column-gap:26px}"
    "#hp-related li{margin:0 0 9px;break-inside:avoid;font-size:14px;line-height:1.45}"
    "#hp-related li a{color:#d98a3d;text-decoration:none}"
    "#hp-related li a:hover{text-decoration:underline}"
    "#hp-related .hpr-cta{display:flex;flex-wrap:wrap;gap:10px;border-top:1px solid #2c3644;padding-top:14px}"
    "#hp-related .hpr-btn{flex:1 1 220px;text-align:center;padding:12px 16px;border-radius:8px;"
    "background:#d98a3d;color:#1a2230;font-weight:700;font-size:14px;text-decoration:none}"
    "#hp-related .hpr-btn.alt{background:transparent;color:#d98a3d;border:1px solid #d98a3d}"
    "@media(max-width:520px){#hp-related ul{columns:1}}"
)


def build_related_block(slug: str, related: list[str], titles: dict) -> str:
    items = "".join(
        f'<li><a href="{r}">{_esc(titles.get(r, r.replace("-", " ")))}</a></li>' for r in related
    )
    return (
        '<section id="hp-related" aria-label="related guides">'
        f"<style>{_REL_CSS}</style>"
        '<div class="hpr-h">Related guides</div>'
        '<p class="hpr-sub">Same buyers, adjacent playbooks — keep pulling the thread.</p>'
        f"<ul>{items}</ul>"
        '<div class="hpr-cta">'
        f'<a class="hpr-btn" href="{SCAN_HREF}">See how AI answers for your category — free scan &rarr;</a>'
        f'<a class="hpr-btn alt" href="{PRODUCTS_HREF}">Products &amp; pricing &rarr;</a>'
        "</div></section>"
    )


def ensure_related(html: str, block: str) -> str:
    """Insert the related-cluster section once, just before the beacon / </body>."""
    if not html or "<html" not in html.lower() or REL_MARKER in html:
        return html
    m = _BEACON_RE.search(html) or _BODY_CLOSE_RE.search(html)
    if not m:
        return html
    return html[: m.start()] + block + html[m.start():]


_HUB_CSS = (
    "#hp-hub-cta{max-width:760px;margin:8px auto 26px;padding:20px 22px;background:#141b27;"
    "border:1px solid #2c3644;border-radius:12px;"
    "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif}"
    "#hp-hub-cta .hphc-row{display:flex;flex-wrap:wrap;gap:10px}"
    "#hp-hub-cta a.hphc-btn{flex:1 1 240px;text-align:center;padding:13px 18px;border-radius:9px;"
    "font-weight:700;font-size:15px;text-decoration:none;background:#d98a3d;color:#1a2230}"
    "#hp-hub-cta a.hphc-btn.alt{background:transparent;color:#d98a3d;border:1px solid #d98a3d}"
    "#hp-hub-cta .hphc-sub{margin:11px 0 0;color:#9aa3ad;font-size:13.5px;line-height:1.5}"
    "#hp-hub-cta .hphc-sub a{color:#d98a3d}"
)

HUB_BAND = (
    '<div id="hp-hub-cta"><style>' + _HUB_CSS + "</style>"
    '<div class="hphc-row">'
    f'<a class="hphc-btn" href="{HUB_SCAN_HREF}">Run the free AI-visibility scan &rarr;</a>'
    f'<a class="hphc-btn alt" href="{PRODUCTS_HREF}">See products &amp; pricing &rarr;</a>'
    "</div>"
    '<p class="hphc-sub">The free scan shows how today&rsquo;s AI assistants answer when buyers '
    'ask for the best in your category &mdash; then lock the one-time $39 fix kit. '
    '<a href="/">See the live board</a>.</p>'
    "</div>"
)


def ensure_hub_cta(html: str) -> str:
    """Insert the hub conversion band once, right before the guide index <ul>."""
    if not html or HUB_MARKER in html:
        return html
    m = _FIRST_UL_RE.search(html)
    if not m:
        m = _BEACON_RE.search(html) or _BODY_CLOSE_RE.search(html)
        if not m:
            return html
    return html[: m.start()] + HUB_BAND + html[m.start():]


# ── driver ──────────────────────────────────────────────────────────────────
def _guide_files(root: Path) -> list[Path]:
    return sorted(p for p in root.glob("*.html") if p.name != "index.html")


def apply(root: Path, n: int) -> dict:
    files = _guide_files(root)
    toks, df, titles = build_index(files)

    changed = 0
    total_links = 0
    for p in files:
        slug = p.stem
        try:
            html = p.read_text(encoding="utf-8")
        except Exception:
            continue
        if "<html" not in html.lower() or REL_MARKER in html:
            continue
        related = related_for(slug, toks, df, n)
        if not related:
            continue
        out = ensure_related(html, build_related_block(slug, related, titles))
        if out != html:
            p.write_text(out, encoding="utf-8")
            changed += 1
            total_links += len(related)

    hub_changed = 0
    hub = root / "index.html"
    if hub.exists():
        h = hub.read_text(encoding="utf-8")
        out = ensure_hub_cta(h)
        if out != h:
            hub.write_text(out, encoding="utf-8")
            hub_changed = 1

    return {
        "scanned": len(files),
        "changed": changed,
        "hub_changed": hub_changed,
        "avg_links": round(total_links / changed, 2) if changed else 0.0,
    }


def check(root: Path) -> dict:
    files = _guide_files(root)
    toks, df, _ = build_index(files)
    missing, skipped = [], 0
    for p in files:
        has = REL_MARKER in p.read_text(encoding="utf-8", errors="ignore")
        if has:
            continue
        # only a real failure if the page HAS clusterable siblings but lacks the block;
        # utility/legal pages (privacy/terms/etc.) have no cluster and are intentionally skipped
        if related_for(p.stem, toks, df, 1):
            missing.append(p.name)
        else:
            skipped += 1
    hub = root / "index.html"
    hub_ok = hub.exists() and HUB_MARKER in hub.read_text(encoding="utf-8", errors="ignore")
    return {"total": len(files), "missing_related": len(missing), "no_cluster_skipped": skipped,
            "hub_cta": hub_ok, "sample_missing": missing[:10]}


def _cli(argv) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="cluster + wire the hailports guide pages (idempotent)")
    ap.add_argument("--apply", metavar="DIR")
    ap.add_argument("--check", metavar="DIR")
    ap.add_argument("--links", type=int, default=DEFAULT_LINKS, help="sibling links per page (6-10)")
    a = ap.parse_args(argv)
    n = max(1, min(a.links, 12))

    if a.apply:
        r = apply(Path(a.apply), n)
        print(
            f"crosslink --apply {a.apply}: {r['changed']}/{r['scanned']} guides updated, "
            f"avg {r['avg_links']} related links/page; hub {'updated' if r['hub_changed'] else 'unchanged'}"
        )
        return 0
    if a.check:
        r = check(Path(a.check))
        print(
            f"crosslink --check {a.check}: {r['total'] - r['missing_related']}/{r['total']} guides have hp-related "
            f"({r['no_cluster_skipped']} non-cluster utility pages skipped); "
            f"hub_cta={'yes' if r['hub_cta'] else 'NO'}"
            + (f"; MISSING e.g. {r['sample_missing']}" if r["missing_related"] else "")
        )
        return 0 if r["missing_related"] == 0 and r["hub_cta"] else 1

    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
