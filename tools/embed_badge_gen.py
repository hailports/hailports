#!/usr/bin/env python3
"""AI Visibility Score badge — embeddable SVG + copy-paste backlink snippet.

White-hat distribution flywheel: a business pastes ONE <a><img> snippet on its site to display
its real AI-visibility grade. The wrapping anchor is a permanent do-follow backlink to its
scannerapp report — compounding SEO + referral traffic off one build, reusing the exact same
scan that powers /ai-visibility and the /v/<slug> proof pages (core.geo_visibility_probe).

Why this is legitimate (not a link scheme): the badge shows the ACTUAL grade the scan produced,
so it's an honest widget backlink in the BBB / Trustpilot / shields.io tradition — the business
chooses to display a true, verifiable result. No hidden links, no keyword stuffing, no nofollow
games. Brand-agnostic by design (base_url param) so a white-hat public brand could reuse the same
generator on its own domain; the default base stays on the anon scannerapp domain (firewall-safe).

CLI:
  python3 -m tools.embed_badge_gen --slug a-a-jewelry-supply-los-angeles-jewelry
  python3 -m tools.embed_badge_gen --grade A --score 91 --label "AI Visibility"
  python3 -m tools.embed_badge_gen --all --out data/hustle/badges   # pre-render every page SVG
  python3 -m tools.embed_badge_gen --selftest
"""
from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GEO_PAGES_DIR = ROOT / "data" / "hustle" / "geo_pages"
DEFAULT_BASE = "https://scannerapp.dev"

# Match the proof-page grade palette (_GEO_CSS): A/B green, C amber, D orange, F red.
GRADE_COLOR = {"A": "#2da44e", "B": "#2da44e", "C": "#dfb317", "D": "#fe7d37", "F": "#e05d44"}
LABEL_BG = "#3b3b52"


def _esc(s) -> str:
    return html.escape(str(s), quote=True)


def _text_width(s: str) -> float:
    """Approximate Verdana 11px advance widths (px) — enough for a tidy two-segment badge."""
    narrow = set("iIl.,:;'|!ftrj()[]· ")
    wide = set("mwMW@%")
    w = 0.0
    for c in s:
        if c in narrow:
            w += 4.0
        elif c in wide:
            w += 10.0
        elif c.isupper() or c.isdigit():
            w += 7.6
        else:
            w += 6.4
    return w


def _segments(label: str, grade: str, score=None) -> tuple[str, str, int, int]:
    grade = str(grade or "?").upper().strip()[:3] or "?"
    if score is None:
        rtext = grade
    else:
        try:
            rtext = f"{grade} · {int(round(float(score)))}"
        except (TypeError, ValueError):
            rtext = grade
    lseg = round(_text_width(label)) + 12
    rseg = round(_text_width(rtext)) + 16
    return label, rtext, lseg, rseg


def badge_width(grade, score=None, label: str = "AI Visibility") -> int:
    _, _, lseg, rseg = _segments(label, grade, score)
    return lseg + rseg


def badge_svg(grade, score=None, label: str = "AI Visibility") -> str:
    """Self-contained, accessible shields-style SVG. No external fonts/assets — renders anywhere."""
    label, rtext, lseg, rseg = _segments(label, grade, score)
    total = lseg + rseg
    h = 20
    rcolor = GRADE_COLOR.get(str(grade or "?").upper().strip()[:1], "#9f9f9f")
    aria = f"{label}: {rtext}"
    # text is scaled .1, so coordinates are in 1/10px; place each label at its segment centre
    lx = lseg / 2 * 10
    rx = (lseg + rseg / 2) * 10
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{total}" height="{h}" '
        f'role="img" aria-label="{_esc(aria)}" viewBox="0 0 {total} {h}">'
        f'<title>{_esc(aria)}</title>'
        f'<linearGradient id="s" x2="0" y2="100%">'
        f'<stop offset="0" stop-color="#bbb" stop-opacity=".1"/>'
        f'<stop offset="1" stop-opacity=".1"/></linearGradient>'
        f'<clipPath id="r"><rect width="{total}" height="{h}" rx="3" fill="#fff"/></clipPath>'
        f'<g clip-path="url(#r)">'
        f'<rect width="{lseg}" height="{h}" fill="{LABEL_BG}"/>'
        f'<rect x="{lseg}" width="{rseg}" height="{h}" fill="{rcolor}"/>'
        f'<rect width="{total}" height="{h}" fill="url(#s)"/></g>'
        f'<g fill="#fff" text-anchor="middle" '
        f'font-family="Verdana,DejaVu Sans,Geneva,sans-serif" font-size="11">'
        f'<text x="{lx}" y="150" fill="#010101" fill-opacity=".3" '
        f'transform="scale(.1)" textLength="{(lseg-12)*10}">{_esc(label)}</text>'
        f'<text x="{lx}" y="140" transform="scale(.1)" textLength="{(lseg-12)*10}">{_esc(label)}</text>'
        f'<text x="{rx}" y="150" fill="#010101" fill-opacity=".3" '
        f'transform="scale(.1)" textLength="{(rseg-16)*10}">{_esc(rtext)}</text>'
        f'<text x="{rx}" y="140" transform="scale(.1)" textLength="{(rseg-16)*10}">{_esc(rtext)}</text>'
        f'</g></svg>'
    )


def report_url(slug: str, base_url: str = DEFAULT_BASE) -> str:
    base = base_url.rstrip("/")
    return (f"{base}/v/{slug}?utm_source=badge&utm_medium=referral"
            f"&utm_campaign=ai_visibility_badge")


def badge_url(slug: str, base_url: str = DEFAULT_BASE) -> str:
    return f"{base_url.rstrip('/')}/badge/{slug}.svg"


def embed_snippet(slug: str, business: str, base_url: str = DEFAULT_BASE,
                  grade=None, score=None, brand: str = "scannerapp") -> str:
    """The one line a business pastes. Do-follow anchor (no rel=nofollow) = the SEO payload;
    rel=noopener is security-only and does not strip link equity."""
    rep = report_url(slug, base_url)
    img = badge_url(slug, base_url)
    alt = f"{business} — AI Visibility grade by {brand}"
    title = f"See the AI Visibility report for {business} on {brand}"
    dims = ""
    if grade is not None:
        dims = f' width="{badge_width(grade, score)}" height="20"'
    return (f'<a href="{_esc(rep)}" title="{_esc(title)}" target="_blank" rel="noopener">'
            f'<img src="{_esc(img)}" alt="{_esc(alt)}"{dims} loading="lazy" '
            f'style="border:0;display:inline-block"></a>')


def _page_for_slug(slug: str) -> dict | None:
    fp = GEO_PAGES_DIR / f"{slug}.json"
    if not fp.exists():
        return None
    try:
        return json.loads(fp.read_text(encoding="utf-8"))
    except Exception:
        return None


def _selftest() -> int:
    ok = True
    for g, sc in [("A", 91.0), ("F", 7.3), ("C", 50), ("?", None)]:
        svg = badge_svg(g, sc)
        assert svg.startswith("<svg") and svg.endswith("</svg>"), "svg shape"
        assert "<title>" in svg and 'role="img"' in svg, "a11y"
        assert badge_width(g, sc) > 40, "width sane"
    snip = embed_snippet("acme-omaha-plumber", "Acme & Sons <Co>", grade="A", score=91)
    assert "rel=\"noopener\"" in snip and "nofollow" not in snip, "must be do-follow"
    assert "utm_source=badge" in snip, "attribution"
    assert "<Co>" not in snip and "&lt;Co&gt;" in snip, "html-escaped business name"
    assert snip.startswith("<a ") and snip.endswith("</a>"), "snippet shape"
    print("[OK] embed_badge_gen selftest passed", "" if ok else "(with warnings)")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug")
    ap.add_argument("--grade")
    ap.add_argument("--score", type=float)
    ap.add_argument("--label", default="AI Visibility")
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--all", action="store_true", help="pre-render every geo page's SVG to --out")
    ap.add_argument("--out", default=str(GEO_PAGES_DIR.parent / "badges"))
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        return _selftest()

    if args.all:
        outd = Path(args.out)
        outd.mkdir(parents=True, exist_ok=True)
        n = 0
        for f in sorted(GEO_PAGES_DIR.glob("*.json")):
            if f.stem == "_index":
                continue
            try:
                p = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            slug = p.get("slug") or f.stem
            (outd / f"{slug}.svg").write_text(
                badge_svg(p.get("grade"), p.get("visibility_score")), encoding="utf-8")
            n += 1
        print(f"wrote {n} badge SVGs -> {outd}")
        return 0

    if args.slug:
        p = _page_for_slug(args.slug)
        if not p:
            print(f"no geo page for slug {args.slug!r}", file=sys.stderr)
            return 1
        grade, score, biz = p.get("grade"), p.get("visibility_score"), p.get("business", "")
        print("# SVG badge:")
        print(badge_svg(grade, score, args.label))
        print("\n# Embed snippet (paste on the business site):")
        print(embed_snippet(args.slug, biz, args.base, grade=grade, score=score))
        return 0

    if args.grade:
        print(badge_svg(args.grade, args.score, args.label))
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
