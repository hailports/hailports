#!/usr/bin/env python3
"""
responsive_baseline.py — ONE shared mobile-first responsive standard for every
generated hailports/brand page (the durable fix, baked into the generators).

Single source of truth for:
  • the viewport meta (incl. viewport-fit=cover)
  • a theme-agnostic CSS baseline (structure only — NO brand colors, so it sits on
    top of each page's own palette without restyling the dark/terminal/paper themes)
  • ensure_responsive(html): idempotent post-processor any generator can call
  • a lint/guard so a page can't ship without the viewport meta + baseline

What the baseline guarantees on every page:
  • zero horizontal overflow at 320/375/390 (box-sizing:border-box, media/table/pre
    max-width:100%, overflow-wrap:anywhere on long tokens, overflow-x:hidden net)
  • dynamic viewport fill — body is a min-height:100dvh flex column so a SHORT page
    fills the screen (footer pins to the bottom) instead of floating in dead space,
    and a LONG page scrolls cleanly. dvh (not vh) so mobile browser chrome doesn't
    cause the classic jump/cutoff.
  • safe-area insets respected on guttered content wrappers (env(safe-area-inset-*))

CLI:
  python3 tools/responsive_baseline.py --apply <dir> [--exclude name1,dir2,...]
  python3 tools/responsive_baseline.py --lint  <dir> [--exclude ...]
"""
from __future__ import annotations
import re
import sys
from pathlib import Path

MARKER = "hp-responsive"
VIEWPORT_CONTENT = "width=device-width, initial-scale=1, viewport-fit=cover"
VIEWPORT_META = f'<meta name="viewport" content="{VIEWPORT_CONTENT}">'

# Structure only — no colors/fonts, so it never restyles a page's brand theme.
BASELINE_CSS = (
    "*,*::before,*::after{box-sizing:border-box}"
    "html{-webkit-text-size-adjust:100%;text-size-adjust:100%}"
    "body{min-height:100svh;min-height:100dvh;display:flex;flex-direction:column;overflow-x:hidden}"
    "img,svg,video,canvas,iframe,embed,object,table,pre{max-width:100%}"
    "img,video,canvas{height:auto}"
    "pre{overflow-x:auto}"
    ":where(p,li,h1,h2,h3,h4,td,th,a,code,kbd,samp,dt,dd,blockquote){overflow-wrap:anywhere}"
    # dynamic viewport fill: the primary container grows, a trailing footer pins to the bottom
    "body>:where(.wrap,.container,main,article,#app,#root,.page,.shell)"
    "{flex:1 0 auto;display:flex;flex-direction:column;width:100%}"
    ":where(.wrap,.container,main,article,#app,.page,.shell)>:where(footer,.foot,.footer):last-child"
    "{margin-top:auto}"
    "body>:where(footer,.foot,.footer):last-child{margin-top:auto}"
    # safe-area gutters on guttered wrappers (full-bleed bodies are left untouched)
    "@supports(padding:max(0px)){"
    "body>.wrap,body>.container,body>main,body>article"
    "{padding-left:max(env(safe-area-inset-left),20px);padding-right:max(env(safe-area-inset-right),20px)}"
    "}"
)
BASELINE_STYLE = f'<style id="{MARKER}">{BASELINE_CSS}</style>'

# ── shared CANONICAL header (opt-in via --mobilenav) ────────────────────────
# Pages ship heterogeneous (or missing) headers, so on mobile the top looked different
# page-to-page and a floating burger overlapped bespoke LIVE pills. This injects ONE
# canonical sticky header (brand + LIVE pill + hamburger, <=820px only) that renders
# IDENTICALLY on every page, and hides each page's own top nav (.topbar / a <header>
# that isn't the hero) so there's exactly one consistent header. Idempotent, auto-
# migrates off the old floating burger, and skips pages that own an in-header burger
# (id="navtoggle": the home/live templates, which are already the reference look).
HDR_MARKER = "hp-hdr"
_YT = "https://www.youtube.com/channel/UC_NaJ6WEpYsNlq-CIGD8yAw/live"
HDR_SNIPPET = (
    '<header id="hp-hdr"><div class="hp-hin">'
    '<a class="hp-brand" href="/"><span class="hp-mk">❯</span> hailports</a>'
    '<div class="hp-hr">'
    f'<a class="hp-live" href="{_YT}" target="_blank" rel="noopener"><span class="hp-dot"></span> LIVE <span class="hp-ar">↗</span></a>'
    '<button class="hp-bg" id="hp-bg" type="button" aria-label="open menu" aria-expanded="false" aria-controls="hp-menu">☰</button>'
    '</div></div>'
    '<div class="hp-bd" id="hp-bd"></div>'
    '<nav class="hp-menu" id="hp-menu" aria-label="site">'
    '<a href="/">home</a><a href="/ai-visibility/check">free scan</a><a href="/playbook">playbook</a>'
    '<a href="/products.html">products</a><a href="/experiment">the experiment</a><a href="/guides/">guides</a>'
    f'<a href="{_YT}" target="_blank" rel="noopener">watch live ↗</a>'
    '</nav></header>'
    '<style id="hp-hdr-css">'
    '#hp-hdr{display:none}'
    '@media(max-width:820px){'
    '.topbar,body>header:not(.hero):not(#hp-hdr){display:none!important}'
    '#hp-hdr{display:block;position:sticky;top:0;z-index:XPHONEX;background:rgba(5,8,13,.94);'
    '-webkit-backdrop-filter:blur(10px);backdrop-filter:blur(10px);border-bottom:1px solid #15212f;'
    'font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}'
    '#hp-hdr .hp-hin{display:flex;align-items:center;justify-content:space-between;gap:10px;height:56px;'
    'max-width:1120px;margin:0 auto;padding:0 16px}'
    '#hp-hdr .hp-brand{display:flex;align-items:center;gap:8px;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;'
    'font-weight:700;font-size:17px;letter-spacing:-.01em;color:#e6f1ec;text-decoration:none}'
    '#hp-hdr .hp-mk{color:#39d98a}'
    '#hp-hdr .hp-hr{display:flex;align-items:center;gap:10px}'
    '#hp-hdr .hp-live{display:inline-flex;align-items:center;gap:6px;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;'
    'font-size:12px;letter-spacing:.12em;color:#22d3ee;text-decoration:none;border:1px solid #15212f;'
    'border-radius:999px;padding:6px 11px;background:rgba(10,18,27,.6);white-space:nowrap}'
    '#hp-hdr .hp-dot{width:8px;height:8px;border-radius:50%;background:#39d98a}'
    '#hp-hdr .hp-ar{opacity:.6}'
    '#hp-hdr .hp-bg{width:40px;height:40px;flex:0 0 auto;display:inline-flex;align-items:center;justify-content:center;'
    'font-size:17px;line-height:1;color:#e6f1ec;background:rgba(10,18,27,.6);border:1px solid #2a3a4d;border-radius:9px;cursor:pointer;padding:0}'
    '#hp-hdr .hp-bg:focus-visible{outline:2px solid #39d98a;outline-offset:2px}'
    '#hp-hdr .hp-menu{position:absolute;top:100%;left:0;right:0;z-index:2;display:none;flex-direction:column;'
    'padding:4px 16px 12px;background:rgba(5,8,13,.98);border-bottom:1px solid #15212f;box-shadow:0 16px 40px rgba(0,0,0,.55)}'
    '#hp-hdr.open .hp-menu{display:flex}'
    '#hp-hdr .hp-menu a{padding:12px 2px;font-size:15px;font-weight:600;color:#e6f1ec;text-decoration:none;border-top:1px solid #15212f}'
    '#hp-hdr .hp-menu a:first-child{border-top:0}'
    '#hp-hdr .hp-bd{position:fixed;left:0;right:0;top:56px;bottom:0;z-index:-1;background:rgba(0,0,0,.5);opacity:0;'
    'pointer-events:none;transition:opacity .15s}'
    '#hp-hdr.open .hp-bd{opacity:1;pointer-events:auto}'
    '}'
    '@media(prefers-reduced-motion:reduce){#hp-hdr .hp-bd{transition:none}}'
    '</style>'
    '<script id="hp-hdr-js">(function(){var r=document.getElementById("hp-hdr");if(!r)return;'
    'var b=document.getElementById("hp-bg"),m=document.getElementById("hp-menu"),d=document.getElementById("hp-bd");'
    'function set(o){r.classList.toggle("open",o);b.setAttribute("aria-expanded",o?"true":"false");'
    'b.setAttribute("aria-label",o?"close menu":"open menu");b.textContent=o?"✕":"☰";'
    'if(o){var f=m.querySelector("a");if(f)f.focus();}}'
    'b.addEventListener("click",function(e){e.stopPropagation();set(!r.classList.contains("open"));});'
    'd.addEventListener("click",function(){set(false);});'
    'm.addEventListener("click",function(e){if(e.target.closest("a"))set(false);});'
    'document.addEventListener("keydown",function(e){if(e.key==="Escape"&&r.classList.contains("open")){set(false);b.focus();}});'
    '})();</script>'
)
_OLD_MNAV_RE = re.compile(r'<div id="hp-mnav">.*?</script>', re.S)
_BODY_OPEN_RE = re.compile(r'<body\b[^>]*>', re.I)
_BODY_CLOSE_RE = re.compile(r'</body\s*>', re.I)

# first-party pageview beacon -> the worker's /api/hp/hit (self-hosted analytics, no external dep)
BEACON_MARKER = "hp-beacon"
BEACON_SNIPPET = (
    '<script id="hp-beacon">(function(){try{var u="/api/hp/hit?p="+encodeURIComponent(location.pathname);'
    'if(navigator.sendBeacon){navigator.sendBeacon(u);}else{(new Image()).src=u+"&r="+Date.now();}}catch(e){}})();</script>'
)


def ensure_beacon(html: str) -> str:
    """Idempotently inject the first-party pageview beacon just before </body>."""
    if not html or "<html" not in html.lower() or BEACON_MARKER in html:
        return html
    m = _BODY_CLOSE_RE.search(html)
    if not m:
        return html
    return html[: m.start()] + BEACON_SNIPPET + html[m.start():]


def ensure_header(html: str) -> str:
    """Inject the canonical sticky header; auto-migrate off the old floating burger;
    skip pages that own an in-header burger (home/live templates)."""
    if not html or "<html" not in html.lower():
        return html
    if 'id="navtoggle"' in html:
        return html
    html = _OLD_MNAV_RE.sub("", html, count=1)  # drop any previously-injected floating burger
    if HDR_MARKER in html:
        return html
    m = _BODY_OPEN_RE.search(html)
    if not m:
        return html
    return html[: m.end()] + HDR_SNIPPET + html[m.end():]


_VP_RE = re.compile(r'<meta\s+[^>]*name\s*=\s*["\']?viewport["\']?[^>]*>', re.I)
_HEAD_OPEN_RE = re.compile(r'<head\b[^>]*>', re.I)
_HEAD_CLOSE_RE = re.compile(r'</head\s*>', re.I)
_CHARSET_RE = re.compile(r'<meta\s+[^>]*charset[^>]*>', re.I)


def has_responsive(html: str) -> bool:
    return MARKER in html


def ensure_responsive(html: str) -> str:
    """Idempotently guarantee viewport meta (with viewport-fit=cover) + the baseline
    <style>. Safe to call on already-processed HTML and on full documents only."""
    if not html or "<html" not in html.lower():
        return html  # fragment / non-document — leave it alone
    if MARKER in html:
        return html  # already baselined

    # 1) viewport meta — upgrade an existing one (adds viewport-fit=cover) or insert it
    if _VP_RE.search(html):
        html = _VP_RE.sub(VIEWPORT_META, html, count=1)
    else:
        m = _CHARSET_RE.search(html) or _HEAD_OPEN_RE.search(html)
        if m:
            html = html[: m.end()] + VIEWPORT_META + html[m.end():]
        else:
            return html  # no head to anchor to

    # 2) baseline <style> — inject last in <head> so it wins the cascade over page styles
    m = _HEAD_CLOSE_RE.search(html)
    if m:
        html = html[: m.start()] + BASELINE_STYLE + html[m.start():]
    else:
        # no </head>: drop it right before <body>, else after the head open tag
        mb = re.search(r'<body\b[^>]*>', html, re.I)
        if mb:
            html = html[: mb.start()] + BASELINE_STYLE + html[mb.start():]
        else:
            return html
    return html


def lint_html(html: str) -> list[str]:
    """Return a list of problems; empty == compliant."""
    problems = []
    if "<html" not in html.lower():
        return problems  # not a full document — not our surface
    vp = _VP_RE.search(html)
    if not vp:
        problems.append("missing viewport meta")
    elif "viewport-fit=cover" not in vp.group(0).replace(" ", "").replace('"', "").replace("'", ""):
        problems.append("viewport meta missing viewport-fit=cover")
    if MARKER not in html:
        problems.append("missing responsive baseline (<style id=hp-responsive>)")
    return problems


def _iter_html(root: Path, exclude: set[str]):
    # exclude entries match either an exact relative path (e.g. "index.html" = root only)
    # or any ANCESTOR directory name (e.g. "live" = the whole live/ tree).
    for p in sorted(root.rglob("*.html")):
        rel = p.relative_to(root)
        if rel.as_posix() in exclude:
            continue
        if any(part in exclude for part in rel.parts[:-1]):
            continue
        yield p


def _cli(argv) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="apply/lint the shared responsive baseline")
    ap.add_argument("--apply", metavar="DIR")
    ap.add_argument("--lint", metavar="DIR")
    ap.add_argument("--exclude", default="", help="comma-sep file/dir names to skip")
    ap.add_argument("--mobilenav", action="store_true",
                    help="also inject the shared overlay hamburger nav (hailports pages)")
    a = ap.parse_args(argv)
    exclude = {x.strip() for x in a.exclude.split(",") if x.strip()}

    if a.apply:
        root = Path(a.apply)
        changed = scanned = 0
        for p in _iter_html(root, exclude):
            scanned += 1
            try:
                html = p.read_text(encoding="utf-8")
            except Exception:
                continue
            out = ensure_responsive(html)
            if a.mobilenav:
                out = ensure_header(out)
                out = ensure_beacon(out)
            if out != html:
                p.write_text(out, encoding="utf-8")
                changed += 1
        print(f"responsive --apply {root}: {changed}/{scanned} updated "
              f"(excluded: {sorted(exclude) or 'none'})")
        return 0

    if a.lint:
        root = Path(a.lint)
        bad = []
        scanned = 0
        for p in _iter_html(root, exclude):
            scanned += 1
            try:
                probs = lint_html(p.read_text(encoding="utf-8"))
            except Exception as e:
                probs = [f"read error: {e}"]
            if probs:
                bad.append((p, probs))
        if bad:
            for p, probs in bad[:50]:
                print(f"  FAIL {p}: {'; '.join(probs)}")
            print(f"responsive --lint {root}: {len(bad)}/{scanned} pages NON-compliant")
            return 1
        print(f"responsive --lint {root}: all {scanned} pages compliant")
        return 0

    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
