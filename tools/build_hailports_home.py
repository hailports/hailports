#!/usr/bin/env python3
"""Render the hailports.com homepage (data/hustle/hailports_dist/index.html) from the
single-source template tools/hailports_home_template.html.

Injects the guide internal-link list (scanned from the deployed /guides tree, preserving
SEO) and a baked metrics fallback (so the live dashboard never renders empty before the
JS fetch of intake.hailports.com/live.json lands). The page is fully static + responsive;
all live numbers come client-side.

    PYTHONPATH=. .venv/bin/python tools/build_hailports_home.py
"""
from __future__ import annotations

import html
import json
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
TPL = BASE / "tools" / "hailports_home_template.html"
LIVE_TPL = BASE / "tools" / "hailports_live_template.html"
DIST = BASE / "data" / "hustle" / "hailports_dist"

_FALLBACK = [
    {"label": "Automation jobs running", "value": "40+", "sub": "bucketed, self-healing"},
    {"label": "Operations processed", "value": "18K+", "sub": "lifetime, anonymized"},
    {"label": "Local AI completions", "value": "10K+", "sub": "run on-stack, $0 marginal"},
    {"label": "Paid AI spend", "value": "$400+", "sub": "kept lean on purpose"},
    {"label": "Est. premium-API avoided", "value": "$2700+", "sub": "local vs paid"},
    {"label": "Live products", "value": "22", "sub": "shipped + buyable"},
    {"label": "Live status", "value": "on", "sub": "continuous board"},
    {"label": "Uptime", "value": "99.9%+", "sub": "self-healing"},
    {"label": "Manual action policy", "value": "gated", "sub": "outward changes staged for owner"},
]


# Homepage shows a bounded sample of guides; the full set lives at /guides/ (linked
# from the section). Dumping all ~740 inline made the mobile page ~46k px tall (single
# column) and diluted internal-link equity — the index hub is the correct crawl target.
HOME_GUIDE_LIMIT = 60


def _guide_links() -> str:
    gdir = DIST / "guides"
    if not gdir.exists():
        return ""
    slugs = [f.stem for f in sorted(gdir.glob("*.html")) if f.name != "index.html"]
    # Taking the first N alphabetically clusters near-duplicate templated families at the
    # top (e.g. 14x "account-executives-buyer-intent-signals-*" then 14x "agencies-*"),
    # so the sample reads as a spam wall instead of showcasing the library's breadth.
    # Round-robin across first-token buckets so each shown guide is a distinct topic family.
    buckets: dict[str, list[str]] = {}
    for s in slugs:
        buckets.setdefault(s.split("-", 1)[0], []).append(s)
    order = sorted(buckets)
    picked: list[str] = []
    i = 0
    while len(picked) < HOME_GUIDE_LIMIT and any(buckets[k] for k in order):
        k = order[i % len(order)]
        if buckets[k]:
            picked.append(buckets[k].pop(0))
        i += 1
    return "\n".join(
        f'<li><a href="/guides/{html.escape(s)}.html">{html.escape(s.replace("-", " "))}</a></li>'
        for s in picked
    )


def _metrics_fallback(tiles) -> str:
    cells = []
    for t in tiles:
        sub = f'<div class="s">{html.escape(str(t.get("sub","")))}</div>' if t.get("sub") else ""
        cells.append(
            f'<div class="tile"><div class="v">{html.escape(str(t.get("value","")))}</div>'
            f'<div class="l">{html.escape(str(t.get("label","")))}</div>{sub}</div>'
        )
    return "\n".join(cells)


def _tiles():
    try:
        from tools import public_case_study_dashboard as pcsd
        return pcsd.build_metrics() or _FALLBACK
    except Exception:
        return _FALLBACK


def build() -> str:
    tpl = TPL.read_text(encoding="utf-8")
    page = (tpl
            .replace("<!--GUIDE_LINKS-->", _guide_links())
            .replace("<!--METRICS_FALLBACK-->", _metrics_fallback(_tiles())))
    return page


def build_live() -> str:
    tpl = LIVE_TPL.read_text(encoding="utf-8")
    return tpl.replace("<!--METRICS_FALLBACK-->", _metrics_fallback(_tiles()))


def main() -> int:
    DIST.mkdir(parents=True, exist_ok=True)
    page = build()
    (DIST / "index.html").write_text(page, encoding="utf-8")
    n = page.count('<li><a href="/guides/')
    print(f"wrote {DIST/'index.html'} ({len(page)//1024}KB, {n} guide links)")

    live = build_live()
    (DIST / "live").mkdir(parents=True, exist_ok=True)
    (DIST / "live" / "index.html").write_text(live, encoding="utf-8")
    print(f"wrote {DIST/'live'/'index.html'} ({len(live)//1024}KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
