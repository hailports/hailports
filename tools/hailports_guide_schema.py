#!/usr/bin/env python3
"""
hailports_guide_schema.py — boost the ~705 programmatic guide pages for rich
results + social shares, as a DURABLE, idempotent deploy-time post-processor
(marker-guarded exactly like tools/hailports_crosslink.py and
tools/responsive_baseline.py; safe to re-run — re-run adds nothing).

Three independent, per-feature marker-guarded passes per guide:

  1. JSON-LD (truth-by-construction — NEVER fabricated).
       • FAQPage — built ONLY from the page's own visible FAQ/"People also ask"
         content: real <h3>…?</h3> question headings + their following <p> answer.
         Added only when the page has NO existing FAQPage AND >=2 real pairs are
         extractable. A page with no extractable FAQ gets no FAQPage.
       • Article — added only when the page has NO existing page-level schema
         (Article/WebPage/BlogPosting/NewsArticle/TechArticle). headline = the
         page's own <h1>, description = its own meta description, url = its own
         canonical. No datePublished is invented (only reused if the page already
         carries one). We deliberately do NOT emit HowTo: these are signal/
         comparison guides, not ordered step-by-step instructions, so a HowTo
         would be fabricated — Article is the honest page-level type here.
  2. Share block (<div id="hp-share">): lightweight X / LinkedIn / copy-link
     buttons, self-contained inline styles + a tiny inline copy handler (no
     external requests — CSP-safe). Shares the page's own title + canonical URL.
  3. og:title / og:description — added ONLY if absent (nearly all guides already
     ship them), derived from the page's own <title> / meta description.

Everything injected is derived from the page's OWN already-published, already
anon-scrubbed visible content, so it introduces no new identity surface. No
autonomy/authorship claims are ever emitted.

CLI:
  python3 tools/hailports_guide_schema.py --apply <guides_dir>
  python3 tools/hailports_guide_schema.py --check <guides_dir>
"""
from __future__ import annotations

import html as _html
import json
import re
import sys
from pathlib import Path
from urllib.parse import quote

SHARE_MARKER = "hp-share"          # per-guide social share block
SCHEMA_MARKER = "data-hp-schema"   # injected JSON-LD stamp
DEFAULT_SITE = "https://www.hailports.com"

# Utility / hub pages in the guides dir that are NOT programmatic SEO guides.
SKIP_FILES = {"index.html", "privacy.html", "terms.html", "sample_report.html"}

MIN_FAQ = 2          # need >=2 real Q&A pairs before we assert a FAQPage
MAX_FAQ = 10
MIN_ANSWER_LEN = 15  # ignore empty/near-empty answers

# ── extraction regexes ───────────────────────────────────────────────────────
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.I | re.S)
_DESC_RE = re.compile(
    r'<meta\s+[^>]*name\s*=\s*["\']description["\'][^>]*content\s*=\s*["\'](.*?)["\']', re.I | re.S
)
_CANON_RE = re.compile(
    r'<link\s+[^>]*rel\s*=\s*["\']canonical["\'][^>]*href\s*=\s*["\'](.*?)["\']', re.I | re.S
)
_DATEPUB_RE = re.compile(r'"datePublished"\s*:\s*"([0-9]{4}-[0-9]{2}-[0-9]{2})"')
# h3 question directly followed by its answer paragraph (real FAQ / PAA markup)
_QA_RE = re.compile(r"<h3[^>]*>(.*?)</h3>\s*<p[^>]*>(.*?)</p>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

_HEAD_CLOSE_RE = re.compile(r"</head\s*>", re.I)
_BODY_CLOSE_RE = re.compile(r"</body\s*>", re.I)
_OG_TITLE_RE = re.compile(r'<meta\s+[^>]*property\s*=\s*["\']og:title["\']', re.I)
_OG_DESC_RE = re.compile(r'<meta\s+[^>]*property\s*=\s*["\']og:description["\']', re.I)
_HAS_FAQ_RE = re.compile(r'"@type"\s*:\s*"FAQPage"')
_HAS_DOC_RE = re.compile(r'"@type"\s*:\s*"(?:Article|WebPage|BlogPosting|NewsArticle|TechArticle)"')

# earliest stable end-of-content anchor to sit the share block in front of
_SHARE_ANCHORS = ('<a id="hp-live"', '<div id="hp-capture"', '<script id="hp-beacon"')


def _text(raw: str) -> str:
    """Strip tags, unescape entities, collapse whitespace."""
    return _WS_RE.sub(" ", _html.unescape(_TAG_RE.sub(" ", raw or ""))).strip()


def _meta_content(html: str, prop: str) -> str:
    """content="" of a <meta property=prop> (used for existing og values)."""
    m = re.search(
        r'<meta\s+[^>]*property\s*=\s*["\']' + re.escape(prop) + r'["\'][^>]*content\s*=\s*["\'](.*?)["\']',
        html, re.I | re.S,
    )
    return _html.unescape(m.group(1).strip()) if m else ""


def _canonical(html: str, slug: str) -> str:
    m = _CANON_RE.search(html)
    if m:
        return _html.unescape(m.group(1).strip())
    return f"{DEFAULT_SITE}/guides/{slug}"


def _title(html: str) -> str:
    m = _TITLE_RE.search(html)
    return _text(m.group(1)) if m else ""


def _headline(html: str) -> str:
    m = _H1_RE.search(html)
    if m:
        t = _text(m.group(1))
        if t:
            return t
    return _title(html)


def _description(html: str) -> str:
    m = _DESC_RE.search(html)
    return _text(m.group(1)) if m else ""


# ── FAQ extraction (real content only) ───────────────────────────────────────
def extract_faq(html: str) -> list[tuple[str, str]]:
    """Real Q&A pairs from the page's own FAQ / 'People also ask' markup.

    A pair qualifies only when the <h3> heading text ends in '?' (an actual
    question the page authored) and the immediately-following <p> is a non-trivial
    answer. Nothing is generated — this is a strict read of visible content.
    """
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for m in _QA_RE.finditer(html):
        q = _text(m.group(1))
        a = _text(m.group(2))
        if not q.endswith("?"):
            continue
        if len(q) < 8 or len(q) > 240 or len(a) < MIN_ANSWER_LEN:
            continue
        key = q.lower()
        if key in seen:
            continue
        seen.add(key)
        pairs.append((q, a))
        if len(pairs) >= MAX_FAQ:
            break
    return pairs


def _ldjson(obj: dict, kind: str) -> str:
    payload = json.dumps(obj, ensure_ascii=False).replace("</", "<\\/")
    return f'<script type="application/ld+json" {SCHEMA_MARKER}="{kind}">{payload}</script>'


def build_faq_ld(pairs: list[tuple[str, str]]) -> str:
    obj = {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [
            {"@type": "Question", "name": q,
             "acceptedAnswer": {"@type": "Answer", "text": a}}
            for q, a in pairs
        ],
    }
    return _ldjson(obj, "faq")


def build_article_ld(html: str, slug: str) -> str:
    url = _canonical(html, slug)
    obj: dict = {
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": _headline(html) or slug.replace("-", " "),
        "mainEntityOfPage": url,
        "url": url,
        "author": {"@type": "Organization", "name": "Hailports"},
        "publisher": {"@type": "Organization", "name": "Hailports"},
    }
    desc = _description(html)
    if desc:
        obj["description"] = desc
    md = _DATEPUB_RE.search(html)  # reuse an existing date; never invent one
    if md:
        obj["datePublished"] = md.group(1)
    return _ldjson(obj, "article")


# ── share block ──────────────────────────────────────────────────────────────
_SHARE_CSS = (
    "#hp-share{max-width:760px;margin:30px auto 6px;padding:13px 16px;border:1px solid #e2e6e2;"
    "border-radius:10px;background:#fbfcfb;display:flex;align-items:center;gap:10px;flex-wrap:wrap;"
    "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif}"
    "#hp-share .hps-l{font-size:14px;font-weight:700;color:#15171a;margin-right:2px}"
    "#hp-share a.hps-b,#hp-share button.hps-b{display:inline-flex;align-items:center;gap:6px;"
    "padding:8px 13px;border-radius:8px;font-size:13.5px;font-weight:700;line-height:1;"
    "text-decoration:none;cursor:pointer;border:1px solid transparent}"
    "#hp-share a.hps-x{background:#111;color:#fff}"
    "#hp-share a.hps-li{background:#0a66c2;color:#fff}"
    "#hp-share button.hps-cp{background:#fff;color:#0a7d4b;border-color:#0a7d4b}"
)


def build_share_block(title: str, url: str) -> str:
    t = quote(title, safe="")
    u = quote(url, safe="")
    x_href = f"https://twitter.com/intent/tweet?text={t}&url={u}"
    li_href = f"https://www.linkedin.com/sharing/share-offsite/?url={u}"
    u_attr = _html.escape(url, quote=True)
    return (
        '<div id="hp-share" aria-label="share this guide">'
        f"<style>{_SHARE_CSS}</style>"
        '<span class="hps-l">Share this guide</span>'
        f'<a class="hps-b hps-x" href="{x_href}" target="_blank" rel="noopener nofollow">Post on X</a>'
        f'<a class="hps-b hps-li" href="{li_href}" target="_blank" rel="noopener nofollow">Share on LinkedIn</a>'
        f'<button type="button" class="hps-b hps-cp" data-u="{u_attr}" '
        "onclick=\"var u=this.getAttribute('data-u');try{navigator.clipboard.writeText(u);"
        "this.textContent='Copied \\u2713';}catch(e){window.prompt('Copy link:',u);}return false;\">"
        "Copy link</button>"
        "</div>"
    )


def _share_insert_pos(html: str) -> int:
    idxs = [html.find(a) for a in _SHARE_ANCHORS]
    idxs = [i for i in idxs if i != -1]
    if idxs:
        return min(idxs)
    m = _BODY_CLOSE_RE.search(html)
    return m.start() if m else -1


# ── per-file processing (idempotent) ─────────────────────────────────────────
def process(html: str, slug: str) -> tuple[str, set[str]]:
    added: set[str] = set()
    if not html or "<html" not in html.lower():
        return html, added

    # 1) JSON-LD into <head> (before </head>); compute from ORIGINAL html.
    head_m = _HEAD_CLOSE_RE.search(html)
    if head_m:
        inject = ""
        if not _HAS_DOC_RE.search(html):
            inject += build_article_ld(html, slug)
            added.add("article")
        if not _HAS_FAQ_RE.search(html):
            pairs = extract_faq(html)
            if len(pairs) >= MIN_FAQ:
                inject += build_faq_ld(pairs)
                added.add("faq")
        # 2) og fill-ins (only if missing), derived from the page's own head.
        og_add = ""
        if not _OG_TITLE_RE.search(html):
            t = _title(html)
            if t:
                og_add += f'<meta property="og:title" content="{_html.escape(t, quote=True)}">'
                added.add("og:title")
        if not _OG_DESC_RE.search(html):
            d = _description(html)
            if d:
                og_add += f'<meta property="og:description" content="{_html.escape(d, quote=True)}">'
                added.add("og:description")
        if inject or og_add:
            pos = head_m.start()
            html = html[:pos] + og_add + inject + html[pos:]

    # 3) share block into <body>.
    if SHARE_MARKER not in html:
        pos = _share_insert_pos(html)
        if pos != -1:
            title = _title(html) or slug.replace("-", " ")
            url = _canonical(html, slug)
            html = html[:pos] + build_share_block(title, url) + html[pos:]
            added.add("share")

    return html, added


def _guide_files(root: Path) -> list[Path]:
    return sorted(p for p in root.glob("*.html") if p.name not in SKIP_FILES)


def apply(root: Path) -> dict:
    files = _guide_files(root)
    changed = 0
    tally: dict[str, int] = {}
    for p in files:
        try:
            html = p.read_text(encoding="utf-8")
        except Exception:
            continue
        out, added = process(html, p.stem)
        if out != html:
            p.write_text(out, encoding="utf-8")
            changed += 1
            for k in added:
                tally[k] = tally.get(k, 0) + 1
    return {"scanned": len(files), "changed": changed, "tally": tally}


def check(root: Path) -> dict:
    files = _guide_files(root)
    have_share = have_faq = have_doc = 0
    missing_share: list[str] = []
    for p in files:
        html = p.read_text(encoding="utf-8", errors="ignore")
        if SHARE_MARKER in html:
            have_share += 1
        else:
            missing_share.append(p.name)
        if _HAS_FAQ_RE.search(html):
            have_faq += 1
        if _HAS_DOC_RE.search(html):
            have_doc += 1
    return {
        "total": len(files),
        "have_share": have_share,
        "have_faq": have_faq,
        "have_doc_schema": have_doc,
        "missing_share": len(missing_share),
        "sample_missing": missing_share[:10],
    }


def _cli(argv) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        description="inject rich-result JSON-LD + share buttons into hailports guides (idempotent)"
    )
    ap.add_argument("--apply", metavar="DIR")
    ap.add_argument("--check", metavar="DIR")
    a = ap.parse_args(argv)

    if a.apply:
        r = apply(Path(a.apply))
        t = r["tally"]
        parts = ", ".join(f"{k}:{v}" for k, v in sorted(t.items())) or "nothing"
        print(f"guide_schema --apply {a.apply}: {r['changed']}/{r['scanned']} guides updated ({parts})")
        return 0
    if a.check:
        r = check(Path(a.check))
        print(
            f"guide_schema --check {a.check}: share {r['have_share']}/{r['total']}, "
            f"FAQPage {r['have_faq']}/{r['total']}, doc-schema {r['have_doc_schema']}/{r['total']}"
            + (f"; MISSING share e.g. {r['sample_missing']}" if r["missing_share"] else "")
        )
        return 0 if r["missing_share"] == 0 else 1

    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
