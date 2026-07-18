#!/usr/bin/env python3
"""build_hailports_products.py — generate hailports.com/products.html from the catalog.

ONE generator, ONE source of truth: it reads products.self_serve.hailports_catalog (the
prices, tiers and copy) and renders an on-brand (dark/terminal) sales page that:
  • leads with the recurring WATCHES (the $0-marginal, compounding priority offer)
  • lays out each engine's free → fixed FIX → recurring WATCH ladder
  • carries the higher-touch "automate the un-automatable" tier as an intake offer
  • links the live build-in-public board as proof (+ a small live strip polling live.json)
  • routes every paid CTA to intake.hailports.com/buy?tier=<sku> (hailports-branded checkout)

Anonymity (HARD): single-brand. No scannerapp/docsapp string or link — the page passes the
deploy guard. Responsive baseline is applied (tools.responsive_baseline.ensure_responsive)
so it ships mobile-perfect and lints clean.

    PYTHONPATH=. python3 tools/build_hailports_products.py            # writes dist/products.html
    PYTHONPATH=. python3 tools/build_hailports_products.py --print    # stdout only
"""
from __future__ import annotations

import sys
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from products.self_serve import hailports_catalog as cat   # noqa: E402
from tools.responsive_baseline import ensure_responsive    # noqa: E402

OUT = ROOT / "data" / "hustle" / "hailports_dist" / "products.html"
YT = "https://www.youtube.com/channel/UC_NaJ6WEpYsNlq-CIGD8yAw/live"

CSS = """
*{box-sizing:border-box;margin:0;padding:0}
:root{--bg:#05080d;--bg2:#070b12;--panel:#0a121b;--panel2:#0c1622;--line:#15212f;
--grn:#39d98a;--cyn:#22d3ee;--ink:#e6f1ec;--mut:#8aa0b0;--amb:#ffd479;--red:#ff6b81;
--mono:ui-monospace,"SF Mono",SFMono-Regular,Menlo,Consolas,monospace;
--sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
html{scroll-behavior:smooth;overflow-x:clip}
body{background:var(--bg);color:var(--ink);font-family:var(--sans);line-height:1.55;
-webkit-font-smoothing:antialiased;overflow-x:clip;min-height:100vh;
background-image:radial-gradient(1100px 560px at 88% -8%,rgba(34,211,238,.10),transparent 60%),
radial-gradient(900px 500px at -8% 8%,rgba(57,217,138,.09),transparent 55%)}
a{color:var(--cyn);text-decoration:none}a:hover{text-decoration:underline}
.wrap{max-width:1120px;margin:0 auto;padding:0 20px}
.mono{font-family:var(--mono)}
header{position:sticky;top:0;z-index:20;backdrop-filter:blur(10px);
background:rgba(5,8,13,.72);border-bottom:1px solid var(--line)}
.nav{display:flex;align-items:center;justify-content:space-between;height:58px}
.brand{display:flex;align-items:center;gap:10px;font-family:var(--mono);font-weight:700;
font-size:17px;color:var(--ink)}.brand .mk{color:var(--grn)}
.nav-links{display:flex;align-items:center;gap:20px;font-size:14px}
.nav-links a{color:var(--mut)}.nav-links a:hover{color:var(--ink);text-decoration:none}
.pill{display:inline-flex;align-items:center;gap:7px;font-family:var(--mono);font-size:12px;
letter-spacing:.12em;color:var(--cyn);border:1px solid var(--line);border-radius:999px;
padding:5px 11px;background:rgba(10,18,27,.6)}
.dot{width:9px;height:9px;border-radius:50%;background:var(--mut)}
.dot.on{background:var(--grn);box-shadow:0 0 0 0 rgba(57,217,138,.7);animation:pulse 1.7s infinite}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(57,217,138,.55)}70%{box-shadow:0 0 0 12px rgba(57,217,138,0)}100%{box-shadow:0 0 0 0 rgba(57,217,138,0)}}
.hero{padding:64px 0 18px;text-align:center}
.hero .eyebrow{font-family:var(--mono);font-size:13px;color:var(--grn);letter-spacing:.16em;
text-transform:uppercase;margin-bottom:16px}
.hero h1{font-size:clamp(30px,5.4vw,52px);line-height:1.06;font-weight:800;letter-spacing:-.025em;
max-width:20ch;margin:0 auto 16px;overflow-wrap:break-word}
.hero h1 b{color:var(--grn)}.hero h1 i{color:var(--cyn);font-style:normal}
.hero p.lead{font-size:clamp(16px,2.1vw,19px);color:var(--mut);max-width:62ch;margin:0 auto 24px}
.cta{display:flex;gap:13px;justify-content:center;flex-wrap:wrap}
.btn{display:inline-flex;align-items:center;gap:8px;font-weight:600;font-size:15px;
padding:12px 20px;border-radius:11px;border:1px solid var(--line);transition:.16s}
.btn.pri{background:linear-gradient(180deg,#3ee29a,#2fb578);color:#04130c;border-color:transparent}
.btn.pri:hover{transform:translateY(-1px);text-decoration:none;box-shadow:0 8px 24px rgba(57,217,138,.22)}
.btn.sec{color:var(--ink);background:rgba(12,22,34,.6)}
.btn.sec:hover{border-color:var(--cyn);color:#fff;text-decoration:none}
.btn.sm{padding:10px 16px;font-size:14px}
.sect{margin:52px 0 0}
.sect-h{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;margin-bottom:6px}
.sect-h h2{font-size:24px;font-weight:800;letter-spacing:-.01em}
.sect-h .tag{font-family:var(--mono);font-size:12px;color:var(--amb);border:1px solid var(--line);
border-radius:999px;padding:3px 10px}
.sect .sub{color:var(--mut);font-size:15px;margin-bottom:20px;max-width:74ch}
.lead-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}
@media(max-width:720px){.lead-grid{grid-template-columns:1fr}}
.watch{border:1px solid var(--line);border-radius:16px;padding:22px 22px 20px;
background:linear-gradient(180deg,var(--panel),var(--bg2));position:relative;display:flex;
flex-direction:column}
.watch.feat{border-color:rgba(57,217,138,.5);box-shadow:0 0 0 1px rgba(57,217,138,.12)}
.watch .nm{font-size:18px;font-weight:700}
.watch .price{font-family:var(--mono);font-size:30px;font-weight:700;color:var(--grn);
margin:8px 0 2px;letter-spacing:-.01em}
.watch .price span{font-size:14px;color:var(--mut);font-weight:400}
.watch .bl{color:var(--mut);font-size:14.5px;margin:8px 0 4px}
.watch .dv{color:var(--ink);font-size:13.5px;margin:10px 0 16px;padding-left:14px;
border-left:2px solid var(--line)}
.watch .btn{margin-top:auto;justify-content:center}
.ladder{border:1px solid var(--line);border-radius:16px;overflow:hidden;margin:0 0 16px;
background:linear-gradient(180deg,var(--panel),var(--bg2))}
.ladder .lh{padding:18px 20px 14px;border-bottom:1px solid var(--line)}
.ladder .lh h3{font-size:19px;font-weight:700}
.ladder .lh .tl{color:var(--cyn);font-size:14px;margin-top:3px}
.ladder .lh .sm{color:var(--mut);font-size:14px;margin-top:6px;max-width:80ch}
.rungs{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:1px;background:var(--line)}
@media(max-width:760px){.rungs{grid-template-columns:1fr}}
.rung{background:var(--panel2);padding:18px 18px 16px;display:flex;flex-direction:column}
.rung .step{font-family:var(--mono);font-size:11px;letter-spacing:.12em;text-transform:uppercase;
color:var(--mut);margin-bottom:7px}
.rung.free .step{color:var(--cyn)}.rung.fix .step{color:var(--amb)}.rung.rec .step{color:var(--grn)}
.rung .nm{font-size:15.5px;font-weight:700}
.rung .pr{font-family:var(--mono);font-size:18px;font-weight:700;margin:4px 0 0}
.rung.free .pr{color:var(--cyn)}.rung.fix .pr{color:var(--amb)}.rung.rec .pr{color:var(--grn)}
.rung .pr span{font-size:12px;color:var(--mut);font-weight:400}
.rung .bl{color:var(--mut);font-size:13.5px;margin:8px 0 14px;flex:1}
.rung .btn{justify-content:center}
.svc{border:1px solid var(--line);border-radius:16px;padding:26px 24px;margin-top:16px;
background:linear-gradient(180deg,var(--panel),var(--bg2))}
.svc h3{font-size:21px;font-weight:800}
.svc .tl{color:var(--cyn);font-size:15px;margin:4px 0 12px}
.svc p{color:var(--mut);font-size:15px;max-width:80ch}
.svc ul{margin:14px 0 18px;padding-left:18px;color:var(--ink);font-size:14px;line-height:1.7;
columns:2;column-gap:28px}
@media(max-width:620px){.svc ul{columns:1}}
.svc .price{font-family:var(--mono);color:var(--amb);font-size:14px;margin-bottom:14px}
.proof{margin:54px 0 0;border:1px solid var(--line);border-radius:16px;
background:linear-gradient(180deg,var(--panel),var(--bg2));padding:22px 22px 18px}
.proof .ph{display:flex;align-items:center;gap:9px;font-family:var(--mono);font-size:13px;
color:var(--mut);margin-bottom:14px}
.proof .ph b{color:var(--ink)}
.metrics{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:1px;background:var(--line);
border:1px solid var(--line);border-radius:10px;overflow:hidden}
@media(max-width:620px){.metrics{grid-template-columns:repeat(2,minmax(0,1fr))}}
.tile{background:var(--panel2);padding:14px 14px 12px}
.tile .v{font-family:var(--mono);font-size:clamp(18px,3vw,24px);font-weight:700;color:var(--grn);
font-variant-numeric:tabular-nums}
.tile .l{margin-top:5px;font-size:12px;color:var(--ink);font-weight:600}
.tile .s{margin-top:2px;font-size:11px;color:var(--mut)}
.proof .pl{margin-top:14px;font-size:14px}
footer{border-top:1px solid var(--line);margin-top:48px;padding:26px 0 60px;
font-family:var(--mono);font-size:12px;color:var(--mut);line-height:1.7}
footer b{color:var(--ink)}
@media(max-width:600px){.wrap{padding:0 16px}.nav-links a:not(.pill){display:none}
.hero{padding:40px 0 14px}.cta{flex-direction:column;align-items:stretch}.cta .btn{width:100%;justify-content:center}}
"""


def _rung(step_class: str, step_label: str, name: str, price_html: str, blurb: str,
          cta_text: str, cta_url: str, cta_class: str) -> str:
    return (
        f'<div class="rung {step_class}"><div class="step">{escape(step_label)}</div>'
        f'<div class="nm">{escape(name)}</div><div class="pr">{price_html}</div>'
        f'<div class="bl">{escape(blurb)}</div>'
        f'<a class="btn {cta_class} sm" href="{escape(cta_url)}">{escape(cta_text)} →</a></div>'
    )


def _engine_ladder(e: dict) -> str:
    free, fix, rec = e["free"], e["fix"], e["recurring"]
    free_cta = free.get("cta", "Try it free")
    rungs = (
        _rung("free", "free proof", free["name"], '$0 <span>free</span>', free["blurb"],
              free_cta, free["url"], "sec")
        + _rung("fix", "one-time fix", fix["name"],
                f'{escape(fix["price"])} <span>{escape(fix["unit"])}</span>', fix["blurb"],
                "Get it", cat.buy_url(fix["tier"]), "sec")
        + _rung("rec", "recurring watch", rec["name"],
                f'{escape(rec["price"])} <span>{escape(rec["unit"])}</span>', rec["blurb"],
                "Start the watch", cat.buy_url(rec["tier"]), "pri")
    )
    # pay-what-you-want on the kit: lower the entry bar without losing the $39 anchor
    pwyw = ""
    if fix.get("tier") == "geo_fix_kit" and "geo_fix_kit_pwyw" in getattr(cat, "CHECKOUT_TIERS", []):
        pwyw = (f'<div style="text-align:center;margin:4px 0 2px;font-size:13px">'
                f'<a href="{escape(cat.buy_url("geo_fix_kit_pwyw"))}" '
                f'style="color:var(--cyn);text-decoration:none;border-bottom:1px dashed currentColor">'
                f'not ready for $39? name your price, from $9 →</a></div>')
    return (
        f'<div class="ladder"><div class="lh"><h3>{escape(e["name"])}</h3>'
        f'<div class="tl">{escape(e["tagline"])}</div>'
        f'<div class="sm">{escape(e["summary"])}</div></div>'
        f'<div class="rungs">{rungs}</div>{pwyw}</div>'
    )


def _watch_card(e: dict, featured: bool) -> str:
    rec = e["recurring"]
    cls = "watch feat" if featured else "watch"
    return (
        f'<div class="{cls}"><div class="nm">{escape(rec["name"])}</div>'
        f'<div class="price">{escape(rec["price"])} <span>{escape(rec["unit"])}</span></div>'
        f'<div class="bl">{escape(rec["blurb"])}</div>'
        f'<div class="dv">{escape(rec["deliverable"])}</div>'
        f'<a class="btn pri" href="{escape(cat.buy_url(rec["tier"]))}">Start {escape(rec["name"])} →</a></div>'
    )


def _pack_card(d: dict, lead: bool = False) -> str:
    # One-time, instant-download finished kit. The CHEAPEST pack is the low-friction entry — it
    # gets an "easiest start" badge + a primary button so a first-time buyer has an obvious tiny yes.
    badge = '<div class="tag" style="margin-bottom:8px">★ easiest start</div>' if lead else ''
    btn = "pri" if lead else "sec"
    return (
        f'<div class="watch">{badge}<div class="nm">{escape(d["name"])}</div>'
        f'<div class="price">{escape(d["price"])} <span>one-time</span></div>'
        f'<div class="bl">{escape(d["blurb"])}</div>'
        f'<a class="btn {btn}" href="{escape(cat.buy_url(d["tier"]))}">Get it →</a></div>'
    )


def _service(s: dict) -> str:
    items = "".join(f"<li>{escape(x)}</li>" for x in s["examples"])
    return (
        f'<div class="svc"><h3>{escape(s["name"])}</h3>'
        f'<div class="tl">{escape(s["tagline"])}</div>'
        f'<p>{escape(s["summary"])}</p><ul>{items}</ul>'
        f'<div class="price">{escape(s["price_display"])} — we scope it with you first</div>'
        f'<a class="btn sec" href="{escape(cat.INTAKE)}/guides/book-audit">{escape(s["cta"])} →</a></div>'
    )


def render() -> str:
    # lead with the recurring watches (dedupe by tier so Site Health Watch shows once)
    seen, watches = set(), []
    for e in cat.ENGINES:
        t = e["recurring"]["tier"]
        if t in seen:
            continue
        seen.add(t)
        watches.append(e)
    watch_cards = "".join(_watch_card(e, i == 0) for i, e in enumerate(watches))
    ladders = "".join(_engine_ladder(e) for e in cat.ENGINES)
    # cheapest-first so the lowest-friction impulse buy ($19) leads; mark it as the easiest start
    cheap = cat.downloads_cheapest_first() if hasattr(cat, "downloads_cheapest_first") else getattr(cat, "DOWNLOADS", [])
    pack_cards = "".join(_pack_card(d, i == 0) for i, d in enumerate(cheap))
    starter = cheap[0] if cheap else None
    support = getattr(cat, "SUPPORT", {})
    starter_cta = (f'<a class="btn sec" href="{escape(cat.buy_url(starter["tier"]))}">'
                   f'or grab a {escape(starter["price"])} starter kit →</a>') if starter else ""
    support_block = (f'<section class="sect" id="support"><div class="sect-h"><h2>support the build</h2>'
                     f'<span class="tag">pay what you want</span></div><p class="sub">{escape(support.get("blurb",""))}</p>'
                     f'<a class="btn sec" href="{escape(support.get("url",""))}">{escape(support.get("cta","Support the build"))} →</a>'
                     f'</section>') if support.get("url") else ""

    html = f"""<!doctype html><html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Hailports — buy what the machine builds</title>
<meta name="description" content="Free, real proof of what's costing your business customers — then a fixed-price fix and an always-on watch. AI-visibility, broken-site rescue, and site-health, run by an autonomous stack. See the live board.">
<link rel="canonical" href="{cat.SITE}/products.html">
<meta name="robots" content="index, follow">
<meta property="og:title" content="Hailports — buy what the machine builds">
<meta property="og:description" content="Free real proof → a fixed-price fix → an always-on watch. AI-visibility, broken-site rescue, site-health. Built + run by an autonomous stack.">
<meta property="og:type" content="website">
<meta property="og:url" content="{cat.SITE}/products.html">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='7' fill='%2305080d'/%3E%3Ctext x='16' y='22' font-size='18' text-anchor='middle' fill='%2339d98a' font-family='monospace'%3E%E2%9D%AF%3C/text%3E%3C/svg%3E">
<style>{CSS}</style></head><body>
<header><div class="wrap nav">
  <div class="brand"><span class="mk">❯</span> <a href="/" style="color:inherit">hailports</a></div>
  <div class="nav-links">
    <a href="/#dashboard">live board</a>
    <a href="/products.html">products</a>
    <a href="/products.html#downloads">kits</a>
    <a href="/#guides">guides</a>
    <a class="pill" href="{YT}" target="_blank" rel="noopener"><span class="dot" id="livedot"></span> <span id="livetxt">LIVE</span> <span style="opacity:.65">↗</span></a>
  </div>
</div></header>

<main class="wrap">
  <section class="hero">
    <div class="eyebrow">free proof · fixed-price fix · always-on watch</div>
    <h1>buy what the machine <b>builds</b>, <i>watches</i> &amp; fixes</h1>
    <p class="lead">an autonomous stack that runs real business processes — finding what's quietly costing you customers, fixing it, and watching it so it stays fixed. start free: see the real proof on your own business, then keep what's worth keeping.</p>
    <div class="cta">
      <a class="btn pri" href="{cat.SITE}/ai-visibility/check">run a free scan of your business →</a>
      <a class="btn sec" href="/#dashboard">see the live board</a>
      {starter_cta}
    </div>
  </section>

  <section class="sect" id="watches">
    <div class="sect-h"><h2>always-on watches</h2><span class="tag">the lead offer · cancel anytime</span></div>
    <p class="sub">the quiet workhorses: we keep scanning so you don't have to, and email you only when it matters. $0 to run, so they're priced to keep — start one and forget it.</p>
    <div class="lead-grid">{watch_cards}</div>
  </section>

  <section class="sect" id="engines">
    <div class="sect-h"><h2>the three engines</h2><span class="tag">free → fix → watch</span></div>
    <p class="sub">each one gives you a real, graded result for free first. like what you see? take the one-time fix, then put it on watch.</p>
    {ladders}
  </section>

  <section class="sect" id="downloads">
    <div class="sect-h"><h2>ready-made kits</h2><span class="tag">instant download · one-time</span></div>
    <p class="sub">finished, ready-to-use packs built by the same stack — the moment you pay, the files are yours. no subscription, no waiting, yours to keep.</p>
    <div class="lead-grid">{pack_cards}</div>
  </section>

  <section class="sect" id="automate">
    <div class="sect-h"><h2>need something custom?</h2><span class="tag">higher-touch · scoped</span></div>
    <p class="sub">the part most tools can't do — because it runs the real app, not an API.</p>
    {_service(cat.SERVICE)}
  </section>

  {support_block}

  <section class="proof" id="proof">
    <div class="ph"><span class="dot on"></span> <b>live board</b> — aggregate, anonymized · auto-refreshing</div>
    <div class="metrics" id="metrics"><div class="tile"><div class="v">live</div><div class="l">build-in-public</div><div class="s">numbers rounded + ranged</div></div></div>
    <div class="pl">this is the same stack that builds + runs everything above — watch it work on the <a href="/#dashboard">live board</a> or the <a href="{YT}" target="_blank" rel="noopener">24/7 stream ↗</a>.</div>
  </section>
</main>

<footer><div class="wrap">
  # hailports &middot; build-in-public &middot; <b>hailports.com</b> &nbsp;|&nbsp; free proof is real (derived from a live probe of your business) &middot; metrics aggregate, anonymized &amp; auto-refreshing &middot; the operator stays anonymous — on purpose
</div></footer>

<script>
(function(){{
  var EP="https://intake.hailports.com/live.json",mEl=document.getElementById("metrics");
  function esc(s){{return String(s==null?"":s).replace(/[&<>"]/g,function(c){{
    return {{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}}[c];}});}}
  function render(arr){{ if(!arr||!arr.length) return;
    mEl.innerHTML=arr.slice(0,4).map(function(t){{
      return '<div class="tile"><div class="v">'+esc(t.value)+'</div><div class="l">'+esc(t.label)+
        '</div>'+(t.sub?'<div class="s">'+esc(t.sub)+'</div>':'')+'</div>';}}).join("");}}
  function pull(){{ fetch(EP,{{cache:"no-store"}}).then(function(r){{return r.json();}})
    .then(function(d){{render(d.metrics);}}).catch(function(){{}}); }}
  pull(); setInterval(pull,15000);
}})();
</script>
</body></html>"""
    return ensure_responsive(html)


def main(argv: list[str]) -> int:
    html = render()
    if "--print" in argv:
        sys.stdout.write(html)
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(html, encoding="utf-8")
    print(f"wrote {OUT} ({len(html):,} bytes) · checkout tiers: {', '.join(cat.all_checkout_tiers())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
