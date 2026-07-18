#!/usr/bin/env python3
"""Standfast blog + SEO builder — renders the isolated /blog and search-engine plumbing.

Reads the hand/auto-authored article fragments + manifest under the named lane and renders:
  site/blog/index.html            — blog index (field notes)
  site/blog/<slug>/index.html     — one page per article (JSON-LD BlogPosting, OG/Twitter, canonical)
  site/sitemap.xml                — home + blog index + every article
  site/robots.txt                 — allow-all + sitemap pointer
  site/blog/rss.xml               — RSS 2.0 feed

FIREWALL: this writes ONLY into the Standfast named-lane site/ tree. It shares no template,
palette, tracker, or infra with the faceless brands. Every rendered file is covered by
firewall_scan.py (which scans the whole site/ tree) and aborts the deploy on any leak.

Run:  python3 tools/standfast_blog_build.py
"""
from __future__ import annotations

import html
import json
import re
from pathlib import Path

LANE = Path(__file__).resolve().parents[1] / "data" / "hustle" / "named_worklife_lane"
CONTENT = LANE / "content"
ARTICLES_DIR = CONTENT / "articles"
MANIFEST = CONTENT / "articles.json"
SITE = LANE / "site"
BLOG = SITE / "blog"

DOMAIN = "https://getstandfast.com"
BRAND = "Standfast"
AUTHOR = "the founder · an operator who automated their own worklife"

GLYPH = ('<svg class="glyph" viewBox="0 0 32 32" aria-hidden="true">'
         '<rect width="32" height="32" rx="7" fill="#16233D"/>'
         '<g fill="none" stroke="#2E6AD1" stroke-width="2.6" stroke-linecap="round">'
         '<path d="M12 8 H9 V24 H12"/><path d="M20 8 H23 V24 H20"/></g></svg>')

CSS = """
*{box-sizing:border-box}
:root{--ink:#16233D;--sand:#F7F6F3;--accent:#2E6AD1;--slate:#46506A;--line:#E4E2DC;--card:#FFFFFF}
html{scroll-behavior:smooth}
body{margin:0;font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;
  color:var(--ink);background:var(--sand);line-height:1.65;-webkit-font-smoothing:antialiased}
.wrap{max-width:720px;margin:0 auto;padding:0 22px}
.wrap.wide{max-width:980px}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
header{padding:18px 0;border-bottom:1px solid var(--line);background:rgba(247,246,243,.92);
  backdrop-filter:saturate(150%) blur(8px);position:sticky;top:0;z-index:5}
.nav{display:flex;align-items:center;justify-content:space-between;max-width:980px;margin:0 auto;padding:0 22px}
.brand{font-weight:700;font-size:19px;letter-spacing:-.01em;display:flex;align-items:center;gap:9px;color:var(--ink)}
.brand:hover{text-decoration:none}
.glyph{width:26px;height:26px;flex:0 0 auto}
.navlinks a{font-size:15px;color:var(--slate);margin-left:18px;font-weight:600}
.crumb{font-size:13px;color:var(--slate);margin:26px 0 6px}
.crumb a{color:var(--slate)}
h1{font-size:34px;line-height:1.18;letter-spacing:-.02em;margin:6px 0 10px;max-width:24ch}
.meta{color:var(--slate);font-size:14px;margin:0 0 26px}
article h2{font-size:24px;letter-spacing:-.01em;margin:34px 0 8px}
article h3{font-size:18px;margin:24px 0 4px}
article p{margin:0 0 16px;font-size:17.5px}
article ul,article ol{margin:0 0 18px;padding-left:22px}
article li{margin:0 0 8px;font-size:17.5px}
article blockquote{margin:22px 0;padding:14px 20px;border-left:4px solid var(--accent);
  background:var(--card);border-radius:0 10px 10px 0;color:var(--slate);font-size:18px}
article a{font-weight:600}
.cta{background:var(--ink);color:#fff;border-radius:16px;padding:28px;margin:40px 0 10px;text-align:center}
.cta h3{color:#fff;font-size:21px;margin:0 0 8px}
.cta p{color:#c5cde0;margin:0 0 16px;font-size:16px}
.btn{display:inline-block;background:var(--accent);color:#fff;padding:13px 24px;border-radius:10px;
  font-weight:600;font-size:16px}
.btn:hover{background:#2356b3;text-decoration:none}
.more{margin:36px 0 0;border-top:1px solid var(--line);padding-top:22px}
.more h4{font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:var(--slate);margin:0 0 12px}
.more a{display:block;font-weight:600;margin:0 0 9px;font-size:16px}
.cards{display:grid;grid-template-columns:1fr;gap:16px;margin:8px 0 0}
.pcard{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:22px}
.pcard h2{font-size:21px;margin:0 0 8px;letter-spacing:-.01em}
.pcard h2 a{color:var(--ink)}
.pcard p{color:var(--slate);margin:0 0 12px;font-size:16px}
.pcard .read{font-weight:600;font-size:15px}
.lead{font-size:19px;color:var(--slate);margin:0 0 8px}
footer{padding:34px 0;border-top:1px solid var(--line);color:var(--slate);font-size:14px;margin-top:40px}
footer .wrap{max-width:980px}
@media(max-width:760px){h1{font-size:27px}article p,article li{font-size:17px}}
@media(prefers-reduced-motion:reduce){html{scroll-behavior:auto}}
""".strip()

FAVICON = ("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E"
           "%3Crect width='32' height='32' rx='7' fill='%2316233D'/%3E"
           "%3Cg fill='none' stroke='%232E6AD1' stroke-width='2.6' stroke-linecap='round'%3E"
           "%3Cpath d='M12 8 H9 V24 H12'/%3E%3Cpath d='M20 8 H23 V24 H20'/%3E%3C/g%3E%3C/svg%3E")


def _head(title: str, desc: str, canonical: str, og_type: str, ld: dict | None) -> str:
    t = html.escape(title)
    d = html.escape(desc)
    ld_block = ""
    if ld:
        ld_block = ('\n<script type="application/ld+json">'
                    + json.dumps(ld, ensure_ascii=False) + "</script>")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{t}</title>
<meta name="description" content="{d}">
<link rel="canonical" href="{canonical}">
<meta name="robots" content="index,follow">
<meta name="theme-color" content="#16233D">
<link rel="icon" href="{FAVICON}">
<meta property="og:type" content="{og_type}">
<meta property="og:url" content="{canonical}">
<meta property="og:title" content="{t}">
<meta property="og:description" content="{d}">
<meta property="og:site_name" content="{BRAND}">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{t}">
<meta name="twitter:description" content="{d}">
<style>{CSS}</style>{ld_block}
</head>
<body>
<header><div class="nav">
  <a class="brand" href="/">{GLYPH}{BRAND}</a>
  <div class="navlinks"><a href="/blog/">Field notes</a><a href="/#waitlist">Get early access</a></div>
</div></header>
"""


FOOTER = (f'<footer><div class="wrap">{BRAND} · built by an operator who runs their job on it. '
          'It drafts. You decide.<br><span style="opacity:.8">'
          '<a href="/">getstandfast.com</a> · <a href="/blog/">field notes</a> · '
          'early access · contact: user@example.com</span></div></footer>\n</body></html>')


def _cta() -> str:
    return ('<div class="cta"><h3>Stop being your own assistant.</h3>'
            '<p>One daily brief of what needs you, with the replies already drafted in your voice. '
            'It drafts. You decide. Nothing is sent without you.</p>'
            '<a class="btn" href="/#waitlist">Join the early-access list</a></div>')


def _read_manifest() -> list[dict]:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    arts = data.get("articles", [])
    # keep only those whose fragment exists, newest first by date then manifest order
    out = []
    for i, a in enumerate(arts):
        frag = ARTICLES_DIR / f"{a['slug']}.html"
        if frag.exists():
            a = dict(a)
            a["_body"] = frag.read_text(encoding="utf-8").strip()
            a["_order"] = i
            out.append(a)
    out.sort(key=lambda a: (a.get("date", ""), -a["_order"]), reverse=True)
    return out


def _related(arts: list[dict], slug: str, n: int = 3) -> list[dict]:
    return [a for a in arts if a["slug"] != slug][:n]


def render_article(a: dict, arts: list[dict]) -> str:
    slug = a["slug"]
    url = f"{DOMAIN}/blog/{slug}/"
    title = a["title"]
    desc = a["description"]
    ld = {
        "@context": "https://schema.org",
        "@type": "BlogPosting",
        "headline": title,
        "description": desc,
        "url": url,
        "datePublished": a.get("date", ""),
        "dateModified": a.get("date", ""),
        "author": {"@type": "Organization", "name": BRAND, "url": DOMAIN},
        "publisher": {"@type": "Organization", "name": BRAND, "url": DOMAIN},
        "mainEntityOfPage": {"@type": "WebPage", "@id": url},
        "keywords": ", ".join(a.get("keywords", [])),
    }
    more = _related(arts, slug)
    more_html = ""
    if more:
        links = "".join(f'<a href="/blog/{m["slug"]}/">{html.escape(m["title"])}</a>' for m in more)
        more_html = f'<div class="more"><h4>More field notes</h4>{links}</div>'
    body = _head(f"{title} — {BRAND}", desc, url, "article", ld)
    body += f"""<div class="wrap">
<div class="crumb"><a href="/">Home</a> &rsaquo; <a href="/blog/">Field notes</a> &rsaquo; {html.escape(title)}</div>
<h1>{html.escape(title)}</h1>
<p class="meta">By {AUTHOR}</p>
<article>
{a['_body']}
</article>
{_cta()}
{more_html}
</div>
"""
    body += FOOTER
    return body


def render_index(arts: list[dict]) -> str:
    url = f"{DOMAIN}/blog/"
    title = f"Field notes — {BRAND}"
    desc = ("Field notes for over-extended operators: tool sprawl, the morning triage trap, "
            "knowing what actually needs you, and AI that drafts but never sends.")
    ld = {
        "@context": "https://schema.org",
        "@type": "Blog",
        "name": f"{BRAND} — Field notes",
        "url": url,
        "description": desc,
        "blogPost": [
            {"@type": "BlogPosting", "headline": a["title"],
             "url": f"{DOMAIN}/blog/{a['slug']}/", "datePublished": a.get("date", "")}
            for a in arts
        ],
    }
    body = _head(title, desc, url, "website", ld)
    cards = ""
    for a in arts:
        cards += (f'<div class="pcard"><h2><a href="/blog/{a["slug"]}/">{html.escape(a["title"])}</a></h2>'
                  f'<p>{html.escape(a["description"])}</p>'
                  f'<a class="read" href="/blog/{a["slug"]}/">Read &rarr;</a></div>')
    body += f"""<div class="wrap">
<div class="crumb"><a href="/">Home</a> &rsaquo; Field notes</div>
<h1>Field notes</h1>
<p class="lead">Notes from building the brief I needed — on tool sprawl, the morning scramble,
and why I won't let software act in my name.</p>
<div class="cards">{cards}</div>
{_cta()}
</div>
"""
    body += FOOTER
    return body


def render_sitemap(arts: list[dict]) -> str:
    urls = [(f"{DOMAIN}/", "1.0", ""), (f"{DOMAIN}/blog/", "0.8", arts[0].get("date", "") if arts else "")]
    for a in arts:
        urls.append((f"{DOMAIN}/blog/{a['slug']}/", "0.7", a.get("date", "")))
    items = ""
    for loc, pri, lastmod in urls:
        lm = f"\n    <lastmod>{lastmod}</lastmod>" if lastmod else ""
        items += f"\n  <url>\n    <loc>{loc}</loc>{lm}\n    <priority>{pri}</priority>\n  </url>"
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            f'{items}\n</urlset>\n')


def render_robots() -> str:
    return ("User-agent: *\nAllow: /\n\n"
            "# No auth/admin surfaces to disallow.\n"
            f"Sitemap: {DOMAIN}/sitemap.xml\n")


def _rfc822(date: str) -> str:
    # date is YYYY-MM-DD; emit a stable RFC-822-ish stamp (noon UTC).
    try:
        from datetime import datetime, timezone
        dt = datetime.strptime(date, "%Y-%m-%d").replace(hour=12, tzinfo=timezone.utc)
        return dt.strftime("%a, %d %b %Y %H:%M:%S +0000")
    except Exception:
        return ""


def render_rss(arts: list[dict]) -> str:
    items = ""
    for a in arts:
        link = f"{DOMAIN}/blog/{a['slug']}/"
        pub = _rfc822(a.get("date", ""))
        pub_line = f"\n      <pubDate>{pub}</pubDate>" if pub else ""
        items += (f"\n    <item>\n      <title>{html.escape(a['title'])}</title>\n"
                  f"      <link>{link}</link>\n      <guid isPermaLink=\"true\">{link}</guid>\n"
                  f"      <description>{html.escape(a['description'])}</description>{pub_line}\n    </item>")
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<rss version="2.0"><channel>\n'
            f'  <title>{BRAND} — Field notes</title>\n'
            f'  <link>{DOMAIN}/blog/</link>\n'
            '  <description>Field notes for over-extended operators. It drafts. You decide.</description>\n'
            '  <language>en-us</language>'
            f'{items}\n</channel></rss>\n')


def _link_blog_into_home() -> bool:
    """Idempotently add a 'Field notes' link to the home page nav so the blog is discoverable."""
    home = SITE / "index.html"
    if not home.exists():
        return False
    t = home.read_text(encoding="utf-8")
    if "/blog/" in t:
        return False
    needle = '<a class="btn accent" href="#waitlist">Get early access</a>'
    if needle in t:
        repl = ('<span style="display:flex;align-items:center;gap:18px">'
                '<a href="/blog/" style="font-size:15px;color:var(--slate);font-weight:600">Field notes</a>'
                '<a class="btn accent" href="#waitlist">Get early access</a></span>')
        t = t.replace(needle, repl, 1)
        home.write_text(t, encoding="utf-8")
        return True
    return False


def main() -> int:
    arts = _read_manifest()
    if not arts:
        print("No articles found in manifest — nothing to build.")
        return 1
    BLOG.mkdir(parents=True, exist_ok=True)
    (BLOG / "index.html").write_text(render_index(arts), encoding="utf-8")
    for a in arts:
        d = BLOG / a["slug"]
        d.mkdir(parents=True, exist_ok=True)
        (d / "index.html").write_text(render_article(a, arts), encoding="utf-8")
    (SITE / "sitemap.xml").write_text(render_sitemap(arts), encoding="utf-8")
    (SITE / "robots.txt").write_text(render_robots(), encoding="utf-8")
    (BLOG / "rss.xml").write_text(render_rss(arts), encoding="utf-8")
    linked = _link_blog_into_home()
    print(f"Built blog: {len(arts)} articles + index + sitemap + robots + rss"
          f"{'  (added Field-notes link to home)' if linked else ''}")
    for a in arts:
        print(f"  /blog/{a['slug']}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
