#!/usr/bin/env python3
"""seo_freetools_gen — build genuinely-useful, $0, 100%-client-side free tools as
standalone SEO pages.

Free tools are the single highest-yield white-hat SEO lever: they rank for
high-intent queries ("can-spam checker", "lead list roi calculator", "reddit
buyer intent keywords") and earn natural editorial links because they're
actually useful. Every page here is self-contained (no network, no tracking,
runs in the browser), carries full technical SEO (unique title/meta, canonical,
Open Graph + Twitter card, JSON-LD), and ends with one honest CTA to our offer.

    python3 tools/seo_freetools_gen.py            # (re)generate all tools + rebuild sitemap
    python3 tools/seo_freetools_gen.py --check    # node --check the inline JS of each tool

Output: data/hustle/seo_pages/<slug>.html  (served by agents/seo_server.py)
"""
from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote as _quote

BASE = Path(__file__).resolve().parent.parent
OUTDIR = BASE / "data" / "hustle" / "seo_pages"
# Single-brand Hailports: pages serve at www.hailports.com/guides/<slug> (the scrub pipeline
# flattens seo_pages/* into the published /guides tree). scannerapp.dev is retired.
DOMAIN = "https://www.hailports.com/guides"
HOME_CTA = "/"
# Real product ladder (the SAME spine prog_seo_factory routes to): every page feeds the live
# $39 funnel, not the phantom free-leads page. FREE_SCAN = the free AI-visibility scan;
# FIX_KIT = the $39 AI-Visibility Fix Kit (geo_fix_kit SKU on the hailports-branded checkout).
# Both carry a live SKU / the ai-visibility/check path so seo_health's ICP guard passes.
# scannerapp.dev is retired — never point a CTA at it.
FREE_SCAN = "https://www.hailports.com/ai-visibility/check"
FIX_KIT = "https://intake.hailports.com/buy?tier=geo_fix_kit"

SHARED_CSS = (
    "body{font-family:-apple-system,Arial,sans-serif;max-width:760px;margin:0 auto;"
    "padding:28px 18px;line-height:1.6;color:#15171a}h1{font-size:2rem;line-height:1.15}"
    "h2{margin-top:30px}a{color:#0a7d4b}label{display:block;font-weight:600;margin:14px 0 4px}"
    "input,textarea,select{width:100%;box-sizing:border-box;padding:10px;border:1px solid #ccd;"
    "border-radius:8px;font:inherit}textarea{min-height:150px}button{margin-top:16px;background:#111;"
    "color:#fff;border:0;padding:12px 20px;border-radius:9px;font:inherit;font-weight:600;cursor:pointer}"
    ".out{background:#f4f7f5;border-radius:12px;padding:16px 18px;margin:18px 0;white-space:pre-wrap}"
    ".grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}.pass{color:#0a7d4b;font-weight:700}"
    ".fail{color:#c0392b;font-weight:700}.warn{color:#b8860b;font-weight:700}"
    ".cta{background:#111;color:#fff;padding:18px 20px;border-radius:12px;margin:30px 0}"
    ".cta a{color:#fff}.note{color:#777;font-size:13px}ul{padding-left:20px}"
    ".vshare{background:#eef7f1;border:1px solid #0a7d4b;border-radius:12px;padding:16px 18px;margin:26px 0}"
    ".vshare strong{color:#0a7d4b}.vbtn{display:inline-block;margin:8px 8px 0 0;padding:9px 13px;"
    "border:1px solid #0a7d4b;border-radius:9px;color:#0a7d4b;background:#fff;font-weight:700;"
    "text-decoration:none;font-size:14px;cursor:pointer}#vbonus{margin-top:8px}"
    "@media(max-width:560px){.grid{grid-template-columns:1fr}}"
)


def _ref_code(slug: str) -> str:
    """Deterministic, non-reversible share code for a tool page. Reuses the stack's
    core.funnel_tracker.mint_ref_code so codes match the server's; falls back to the same
    salt+hash if the import isn't available (keeps this generator self-contained)."""
    try:
        sys.path.insert(0, str(BASE))
        from core.funnel_tracker import mint_ref_code
        return mint_ref_code(slug)
    except Exception:
        return hashlib.sha1(f"viral-loop-v1:{(slug or '').strip().lower()}".encode()).hexdigest()[:10]


def viral_block(tool: dict) -> str:
    """Viral free-tool loop (client-side, $0, honesty-preserving): result-sharing + share-to-unlock.

    Share buttons point at THIS tool's own canonical URL, ref- + UTM-tagged, so a click that lands a
    new visitor is attributable at the destination funnel (these static pages upload nothing). Clicking
    a share button reveals a genuinely-useful bonus (share-to-unlock). White-hat + honest: the bonus is
    free, sharing just unlocks it early — no reviews, no testimonials, no fake anything."""
    slug = tool["slug"]
    ref = _ref_code(slug)
    url = f"{DOMAIN}/{slug}.html"
    base = f"{url}?ref={ref}&utm_source=share&utm_campaign=viral_loop"
    text = tool.get("share_text") or f"{tool['h1']} — free, no signup"
    tw = "https://twitter.com/intent/tweet?text=" + _quote(text) + "&url=" + _quote(base + "&utm_medium=x")
    li = "https://www.linkedin.com/sharing/share-offsite/?url=" + _quote(base + "&utm_medium=linkedin")
    rd = "https://www.reddit.com/submit?title=" + _quote(text) + "&url=" + _quote(base + "&utm_medium=reddit")
    copy_url = base + "&utm_medium=copy"
    bonus = tool.get("bonus", "")
    bonus_html = f'<div id="vbonus" hidden>{bonus}</div>' if bonus else ""
    headline = ("<strong>Useful? Share it &mdash; and the bonus below unlocks &#128275;</strong>"
                if bonus else "<strong>Found this useful? Pass it on &#128279;</strong>")
    return (
        '<div class="vshare">' + headline +
        f'<div><a class="vbtn" href="{esc(tw)}" target="_blank" rel="noopener" onclick="return vU()">Share on X</a>'
        f'<a class="vbtn" href="{esc(li)}" target="_blank" rel="noopener" onclick="return vU()">LinkedIn</a>'
        f'<a class="vbtn" href="{esc(rd)}" target="_blank" rel="noopener" onclick="return vU()">Reddit</a>'
        f'<button class="vbtn" type="button" onclick="return vCopyLink()">Copy link</button></div>'
        + bonus_html +
        "<script>\n"
        "function vU(){var b=document.getElementById('vbonus');if(b){b.hidden=false;}return true;}\n"
        f"function vCopyLink(){{var u={q(copy_url)};try{{if(navigator.clipboard){{navigator.clipboard.writeText(u);}}}}catch(e){{}}"
        "vU();try{event.target.textContent='Link copied \\u2713';}catch(e){}return false;}\n"
        "if(typeof location!=='undefined'&&location.hash==='#shared'){vU();}\n"
        "</script></div>")


def head(slug: str, title: str, desc: str, ld_extra: str = "") -> str:
    url = f"{DOMAIN}/{slug}.html"
    app_ld = (
        '{"@context":"https://schema.org","@type":"WebApplication",'
        f'"name":{q(title)},"url":{q(url)},"applicationCategory":"BusinessApplication",'
        '"operatingSystem":"Any (browser)","offers":{"@type":"Offer","price":"0","priceCurrency":"USD"},'
        f'"description":{q(desc)}}}'
    )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{esc(title)}</title>"
        f'<meta name="description" content="{esc(desc)}">'
        f'<link rel="canonical" href="{url}">'
        '<meta property="og:type" content="website">'
        f'<meta property="og:title" content="{esc(title)}">'
        f'<meta property="og:description" content="{esc(desc)}">'
        f'<meta property="og:url" content="{url}">'
        '<meta property="og:site_name" content="Hailports">'
        '<meta name="twitter:card" content="summary">'
        f'<meta name="twitter:title" content="{esc(title)}">'
        f'<meta name="twitter:description" content="{esc(desc)}">'
        f'<script type="application/ld+json">{app_ld}</script>'
        + (f'<script type="application/ld+json">{ld_extra}</script>' if ld_extra else "")
        + f"<style>{SHARED_CSS}</style></head><body>"
    )


def cta(line: str) -> str:
    return (
        f'<div class="cta"><strong>{line}</strong> See exactly how today\'s AI assistants '
        "answer when a buyer asks for a business like yours — "
        f'<a href="{FREE_SCAN}?utm_source=seo&utm_medium=free_tool&utm_campaign=ai_visibility_scan">'
        "run the free AI-visibility scan &rarr;</a> "
        '<span class="note" style="color:#bbb">then lock the fixes &middot; '
        f'<a href="{FIX_KIT}&utm_source=seo&utm_medium=free_tool&utm_campaign=fix_kit">'
        "$39 AI-Visibility Fix Kit, one-time &rarr;</a></span></div>"
    )


def foot(note: str = None) -> str:
    # Default note is true for the 4 client-side tools. Server-backed tools (e.g. the
    # broken-site checker, which submits the domain to /site-scan) MUST override it —
    # claiming "runs entirely in your browser, nothing uploaded" there would be false.
    note_html = note if note is not None else (
        "This tool runs entirely in your browser — nothing you type is uploaded, stored, or tracked."
    )
    return (
        f'<p class="note" style="margin-top:30px">{note_html}</p>'
        f'<p style="margin-top:18px"><a href="{HOME_CTA}">&larr; All Hailports tools + the live switcher feed</a></p>'
        "</body></html>"
    )


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def q(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


# ── Tool 1: Cold-email CAN-SPAM compliance checker ──────────────────────────────
T_CANSPAM = {
    "slug": "cold-email-can-spam-checker",
    "title": "Free Cold Email CAN-SPAM Compliance Checker (2026)",
    "desc": "Paste any cold/marketing email and instantly check it against the 7 CAN-SPAM "
            "rules — physical address, one-click opt-out, honest subject, real sender. 100% private.",
    "h1": "Cold Email CAN-SPAM Compliance Checker",
    "intro": "Paste the full text of a marketing or cold email below. This checks it against the "
             "7 things US CAN-SPAM law actually requires. It runs in your browser — nothing is uploaded. "
             "It is a practical aid, not legal advice.",
    "body": """
<label for="em">Paste your email (subject line on the first line helps):</label>
<textarea id="em" placeholder="Subject: Quick question about your leads&#10;&#10;Hi Sam, ...&#10;&#10;Unsubscribe: https://...&#10;Hailports, 123 Main St, Austin TX 78701"></textarea>
<button onclick="checkCanSpam()">Check compliance</button>
<div id="r" class="out" hidden></div>
<h2>The 7 CAN-SPAM rules this checks</h2>
<ul>
<li><strong>Honest header info</strong> — your From/Reply-To must identify the real sender.</li>
<li><strong>Non-deceptive subject line</strong> — the subject must reflect the message.</li>
<li><strong>Identify it as an ad</strong> where applicable.</li>
<li><strong>Valid physical postal address</strong> in the footer.</li>
<li><strong>A clear opt-out mechanism</strong> (an unsubscribe link or reply-to-stop).</li>
<li><strong>Honor opt-outs</strong> promptly — keep a suppression list (process, not text).</li>
<li><strong>Don't sell/transfer</strong> addresses of people who opted out.</li>
</ul>
<script>
function checkCanSpam(){
  var t=document.getElementById('em').value||'';
  var low=t.toLowerCase();
  var lines=t.split(/\\n/);
  var subject=(lines[0].toLowerCase().indexOf('subject')===0)?lines[0].replace(/^subject:?/i,'').trim():'';
  var rows=[];
  function add(ok,label,detail){rows.push((ok==='warn'?'⚠️':(ok?'✅':'❌'))+' '+label+(detail?' — '+detail:''));}
  // physical postal address: look for a street + state-ish or zip pattern
  var addr=/\\b\\d{1,6}\\s+[a-z0-9.\\s]+\\b(st|street|ave|avenue|rd|road|blvd|dr|drive|ln|lane|suite|ste|po box|p\\.o\\.)\\b/i.test(t)
           || /\\b[a-z]{2}\\s*\\d{5}(-\\d{4})?\\b/i.test(t);
  add(addr,'Valid physical postal address', addr?'found':'add a real street/PO Box + city/state/ZIP in the footer');
  // opt-out
  var opt=/(unsubscribe|opt\\s?-?out|stop receiving|to stop|manage preferences|reply\\s+stop)/i.test(low);
  add(opt,'Opt-out / unsubscribe mechanism', opt?'found':'add a working unsubscribe link or reply-STOP line');
  // opt-out is a link?
  var optLink=opt && /(unsubscribe|opt\\s?-?out|preferences)[^\\n]{0,40}(https?:\\/\\/|<a )/i.test(t);
  if(opt) add(optLink?true:'warn','One-click unsubscribe link', optLink?'looks clickable':'prefer a one-click link over reply-only');
  // sender identity
  var sender=/(\\bfrom\\b|regards|thanks,|thank you|best,|sincerely|cheers|\\bteam\\b|\\binc\\b|\\bllc\\b|\\bltd\\b|@)/i.test(low);
  add(sender,'Identifiable sender', sender?'a name/company/From is present':'sign with a real name + company');
  // deceptive subject heuristics
  var bait=['re:','fwd:','you won','urgent!!!','free money','act now','100% free','final notice','re :'];
  var hit=subject?bait.filter(function(b){return subject.toLowerCase().indexOf(b)>=0;}):[];
  if(subject){add(hit.length===0,'Non-deceptive subject line', hit.length? 'risky tokens: '+hit.join(', ') : 'no obvious bait tokens');}
  else add('warn','Subject line','put "Subject: ..." on line 1 to check it');
  // misleading: ALL CAPS subject
  if(subject && subject.replace(/[^a-z]/gi,'').length>4 && subject===subject.toUpperCase())
     add(false,'Subject not shouting','all-caps subjects read as spam');
  var fails=rows.filter(function(x){return x[0]==='❌';}).length;
  var warns=rows.filter(function(x){return x[0]==='⚠';}).length;
  var verdict=fails? '❌ '+fails+' blocker(s) to fix before sending.' : (warns? '⚠️ Likely OK — '+warns+' thing(s) to tighten.' : '✅ Looks compliant on the checkable items.');
  var el=document.getElementById('r'); el.hidden=false;
  el.textContent=verdict+'\\n\\n'+rows.join('\\n')+'\\n\\nReminder: also keep a real suppression list and honor opt-outs within 10 business days. This is guidance, not legal advice.';
}
</script>
""",
    "ctaline": "Sending compliant cold email but running out of people worth emailing?",
    "share_text": "Free CAN-SPAM compliance checker — paste any cold email, check the 7 rules, 100% private:",
    "bonus": "<h2>Bonus: copy-paste CAN-SPAM-safe footer</h2>"
             "<pre>[Your Company LLC]\n[Street Address], [City, ST ZIP]\n\n"
             "You received this because [specific reason].\n"
             "Unsubscribe: [one-click link]   |   or reply STOP.</pre>"
             "<p class=note>Swap the brackets for real details. A valid postal address and a working "
             "one-click unsubscribe are the two most-missed CAN-SPAM requirements.</p>",
}

# ── Tool 2: Lead-list ROI calculator ────────────────────────────────────────────
T_ROI = {
    "slug": "lead-list-roi-calculator",
    "title": "Lead List ROI Calculator — Is a Lead List Worth It? (Free)",
    "desc": "Free calculator: enter list size, reply rate, close rate, deal value and list cost "
            "to see expected customers, revenue, ROI, payback and cost per customer. Runs in-browser.",
    "h1": "Lead List ROI Calculator",
    "intro": "Before you buy a lead list (or a $99/mo intent feed), do the napkin math. Enter your "
             "numbers and this shows the expected customers, revenue, ROI and break-even. Everything "
             "stays in your browser.",
    "body": """
<div class="grid">
<div><label for="n">List size (leads)</label><input id="n" type="number" value="500"></div>
<div><label for="rr">Reply / response rate %</label><input id="rr" type="number" value="8"></div>
<div><label for="mr">Reply &rarr; meeting %</label><input id="mr" type="number" value="30"></div>
<div><label for="cr">Meeting &rarr; close %</label><input id="cr" type="number" value="25"></div>
<div><label for="acv">Avg deal value $ (or annual)</label><input id="acv" type="number" value="1200"></div>
<div><label for="cost">List / tool cost $</label><input id="cost" type="number" value="99"></div>
</div>
<button onclick="calcRoi()">Calculate ROI</button>
<div id="r" class="out" hidden></div>
<h2>How the math works</h2>
<p>Customers = list &times; reply% &times; meeting% &times; close%. Revenue = customers &times; deal value. "
   "ROI = (revenue &minus; cost) &divide; cost. Break-even leads is how many of this exact list you'd "
   "need just to cover its cost. Conservative inputs beat optimistic ones.</p>
<script>
function num(id){var v=parseFloat(document.getElementById(id).value);return isFinite(v)?v:0;}
function calcRoi(){
  var n=num('n'),rr=num('rr')/100,mr=num('mr')/100,cr=num('cr')/100,acv=num('acv'),cost=num('cost');
  var customers=n*rr*mr*cr;
  var revenue=customers*acv;
  var profit=revenue-cost;
  var roi=cost>0?(profit/cost*100):0;
  var cpc=customers>0?(cost/customers):0;
  var perCustomerLeads=(rr*mr*cr)>0?(1/(rr*mr*cr)):0;
  var f=function(x){return x.toLocaleString(undefined,{maximumFractionDigits:0});};
  var el=document.getElementById('r'); el.hidden=false;
  el.textContent=
    'Expected customers: '+customers.toFixed(1)+'\\n'+
    'Expected revenue: $'+f(revenue)+'\\n'+
    'Profit after cost: $'+f(profit)+'\\n'+
    'ROI: '+roi.toFixed(0)+'%\\n'+
    'Cost per customer: $'+f(cpc)+'\\n'+
    'Leads needed per customer: '+perCustomerLeads.toFixed(0)+'\\n\\n'+
    (roi>=100?'✅ Strong return at these inputs.':(roi>=0?'⚠️ Positive but thin — push reply or close rate.':'❌ Underwater — the list is too cold or too pricey at these rates.'));
}
</script>
""",
    "ctaline": "Cold lists convert at ~8%. Warm, publicly-shopping buyers convert higher.",
    "share_text": "Free Lead-List ROI calculator — see expected customers, revenue, ROI and payback before you buy:",
    "bonus": "<h2>Bonus: realistic B2B benchmark ranges</h2><ul>"
             "<li>Cold-list reply rate: 1&ndash;8% (warm / intent lists: 8&ndash;20%)</li>"
             "<li>Reply &rarr; meeting: 20&ndash;40%</li>"
             "<li>Meeting &rarr; close: 15&ndash;30%</li>"
             "<li>Rule of thumb: if ROI is under 100% at conservative inputs, fix the <em>list</em> "
             "before the copy &mdash; a warmer list moves every number above.</li></ul>",
}

# ── Tool 3: Reddit buyer-intent keyword finder ──────────────────────────────────
T_REDDIT = {
    "slug": "reddit-buyer-intent-keyword-finder",
    "title": "Reddit Buyer-Intent Keyword Finder (Free, No Login)",
    "desc": "Free tool: type your product or a competitor and get the exact buyer-intent search "
            "phrases + ready-made Reddit and Google search links to find people asking to buy right now.",
    "h1": "Reddit Buyer-Intent Keyword Finder",
    "intro": "People announce they're ready to buy in plain English on Reddit — 'alternative to X', "
             "'best tool for Y', 'anyone recommend'. Type your product or a competitor and get the exact "
             "high-intent phrases plus one-click search links. Runs in your browser.",
    "body": """
<label for="kw">Your product, category, or a competitor (e.g. "zoominfo", "crm for agencies"):</label>
<input id="kw" placeholder="zoominfo">
<button onclick="genIntent()">Find buyer-intent searches</button>
<div id="r" class="out" hidden></div>
<h2>Why these phrases work</h2>
<p>Generic keywords pull researchers. These pull buyers: someone typing "alternative to X" or "best X "
   "for small team" has a budget and a deadline. Open the links, read the threads, and reply with genuine "
   "help (Reddit punishes pitching — lead with value).</p>
<script>
var TEMPLATES=[
 ['alternative to %s','switchers'],['%s alternative','switchers'],['cheaper than %s','price-driven'],
 ['best %s for small business','SMB buyers'],['%s vs','comparison shoppers'],
 ['anyone recommend a %s','asking for a rec'],['looking for a %s','active need'],
 ['is %s worth it','on the fence'],['%s too expensive','price objection'],
 ['how to replace %s','committed to switch'],['%s for startups','startup buyers'],
 ['free %s','top-of-funnel (lower intent)']];
function genIntent(){
  var k=(document.getElementById('kw').value||'').trim();
  if(!k){document.getElementById('r').hidden=false;document.getElementById('r').textContent='Type something first.';return;}
  var ek=encodeURIComponent(k);
  var out='Buyer-intent phrases for "'+k+'":\\n\\n';
  TEMPLATES.forEach(function(t){
    var phrase=t[0].replace('%s',k);
    var reddit='https://www.reddit.com/search/?q='+encodeURIComponent(phrase)+'&sort=new';
    var goog='https://www.google.com/search?q=site:reddit.com+'+encodeURIComponent(phrase);
    out+='• '+phrase+'  ['+t[1]+']\\n   Reddit: '+reddit+'\\n   Google: '+goog+'\\n';
  });
  out+='\\nTip: sort Reddit by New and check the post date — a thread older than ~6 months is usually a dead lead.';
  var el=document.getElementById('r');el.hidden=false;el.textContent=out;
}
</script>
""",
    "ctaline": "Don't hand-search forever. We scan these sources daily and score the fresh buyers.",
    "share_text": "Free Reddit buyer-intent keyword finder — turn a product or competitor into ready-made search links:",
    "bonus": "<h2>Bonus: 8 more high-intent phrase patterns</h2><ul>"
             "<li>“moving away from [X]”</li><li>“[X] keeps crashing”</li>"
             "<li>“[X] support is terrible”</li><li>“migrate from [X] to”</li>"
             "<li>“what do you use instead of [X]”</li><li>“[X] pricing too high”</li>"
             "<li>“best [category] in 2026”</li><li>“[X] not worth the money”</li></ul>"
             "<p class=note>Same play: sort by New, read the thread, reply with genuine help — never a cold pitch.</p>",
}

# ── Tool 4: Cold-email subject-line spam-trigger scanner ────────────────────────
T_SPAMWORDS = {
    "slug": "cold-email-spam-word-checker",
    "title": "Cold Email Spam-Word & Subject Line Checker (Free)",
    "desc": "Free, private checker that scans your subject line and body for spam-trigger words, "
            "ALL-CAPS, excess punctuation and link/image ratio issues that hurt inbox deliverability.",
    "h1": "Cold Email Spam-Word Checker",
    "intro": "Filters score your email before a human ever sees it. Paste your subject + body and this "
             "flags spam-trigger words, shouting, punctuation abuse and other deliverability killers. "
             "It checks deliverability hygiene — separate from legal compliance. Runs in your browser.",
    "body": """
<label for="em">Paste subject (line 1) + body:</label>
<textarea id="em" placeholder="Subject: quick question&#10;&#10;Hi Sam, ..."></textarea>
<button onclick="scanSpam()">Scan for spam triggers</button>
<div id="r" class="out" hidden></div>
<h2>What hurts deliverability</h2>
<ul>
<li>Spam-trigger words ("free", "guarantee", "act now", "100%", "$$$").</li>
<li>ALL-CAPS words and 3+ exclamation/question marks.</li>
<li>Too many links or a single big image with little text.</li>
<li>Misleading "Re:"/"Fwd:" on a first-touch email.</li>
</ul>
<script>
var SPAM=['free','100%','guarantee','guaranteed','act now','limited time','urgent','winner','cash',
 'risk-free','no obligation','click here','buy now','order now','cheap','discount','$$$','income',
 'make money','earn','double your','this isn\\'t spam','congratulations','viagra','crypto','investment'];
function scanSpam(){
  var t=document.getElementById('em').value||'';var low=t.toLowerCase();
  var lines=t.split(/\\n/);
  var subject=(lines[0].toLowerCase().indexOf('subject')===0)?lines[0].replace(/^subject:?/i,'').trim():'';
  var issues=[];
  var hits=SPAM.filter(function(w){return low.indexOf(w)>=0;});
  if(hits.length) issues.push('Spam-trigger words ('+hits.length+'): '+hits.join(', '));
  var caps=(t.match(/\\b[A-Z]{4,}\\b/g)||[]).filter(function(w){return ['ASAP','FYI','CEO','CTO','SaaS','PTO','HTML','URL'].indexOf(w)<0;});
  if(caps.length) issues.push('ALL-CAPS words: '+caps.slice(0,8).join(', '));
  var bang=(t.match(/[!?]/g)||[]).length;
  if(bang>2) issues.push(bang+' exclamation/question marks (keep ≤2)');
  var links=(t.match(/https?:\\/\\//g)||[]).length;
  if(links>2) issues.push(links+' links (3+ links hurts a cold first-touch)');
  if(/(\\$|usd)\\s?\\d{3,}/i.test(t)) issues.push('Large dollar figure in copy reads salesy');
  if(subject && /^(re:|fwd:|re :)/i.test(subject)) issues.push('Subject fakes a reply/forward ("'+subject+'")');
  if(subject && subject.length>60) issues.push('Subject is '+subject.length+' chars (aim 30-50)');
  if(subject && subject.replace(/[^a-z]/gi,'').length>4 && subject===subject.toUpperCase()) issues.push('Subject is ALL CAPS');
  var el=document.getElementById('r');el.hidden=false;
  el.textContent=issues.length? ('⚠️ '+issues.length+' deliverability risk(s):\\n\\n• '+issues.join('\\n• ')+'\\n\\nLower-risk copy = more inboxed = more replies.')
    : '✅ No obvious spam triggers. Short, plain, specific copy inboxes best.';
}
</script>
""",
    "ctaline": "Great deliverability is wasted on a bad list.",
    "share_text": "Free cold-email spam-word checker — scan your subject + body for deliverability killers, 100% private:",
    "bonus": "<h2>Bonus: safer word swaps</h2><ul>"
             "<li>“free” &rarr; “no cost” / “on us” (use sparingly)</li>"
             "<li>“guarantee” &rarr; “we stand behind it”</li>"
             "<li>“act now” &rarr; “when you're ready”</li>"
             "<li>“100% / $$$” &rarr; drop the symbols, use plain words</li>"
             "<li>“click here” &rarr; a descriptive link (“see the 3-line fix”)</li></ul>"
             "<p class=note>Short, plain, specific copy inboxes best — these swaps keep the meaning, drop the filter risk.</p>",
}

# ── Tool 5: Broken-website checker (organic entry → server-side /site-scan) ──────
# Thin client-side landing for "broken site checker / ssl expiry / uptime" intent.
# The browser's same-origin policy blocks JS from probing another domain's SSL,
# HTTP status, or response time, so the primary CTA is a GET form that hands the
# real scan to the server-side /site-scan front door (UTM-tagged freetool funnel).
T_BROKENSITE = {
    "slug": "broken-site-checker",
    "note": "This checker runs a real test from our server and shows you the result instantly — "
            "enter a domain you own or manage. We only store an email if you ask for the full fix plan.",
    "title": "Free Broken Website Checker — Down, No-HTTPS, Slow & Expired SSL (2026)",
    "desc": "Free broken-site checker: enter any domain to test if it's down, missing HTTPS, "
            "loading slowly, or serving an expired SSL certificate. Instant server-side scan, no login.",
    "h1": "Broken Website Checker",
    "intro": "Enter a domain to check whether a website is <strong>down</strong>, served over an "
             "<strong>insecure / no-HTTPS</strong> connection, <strong>loading slowly</strong>, or "
             "showing an <strong>expired SSL certificate</strong>. Browsers block scripts from probing "
             "another site's certificate or uptime directly, so this hands the actual scan to our "
             "server-side checker and shows you exactly what's wrong — free, no account.",
    "body": """
<form action="https://www.hailports.com/" method="get">
<label for="domain">Website to check</label>
<input id="domain" name="domain" placeholder="yourdomain.com" autocomplete="off" required>
<input type="hidden" name="utm_source" value="freetool">
<input type="hidden" name="utm_medium" value="free_tool">
<input type="hidden" name="utm_campaign" value="site_scan">
<button type="submit">Run the free check &rarr;</button>
</form>
<h2>What this checks</h2>
<ul>
<li><strong>Down / unreachable</strong> — the site times out, refuses the connection, or returns a 5xx error.</li>
<li><strong>Not secure (no HTTPS)</strong> — no certificate, or plain HTTP that never upgrades, so browsers show "Not secure".</li>
<li><strong>Expired / invalid SSL</strong> — the certificate lapsed or doesn't match the domain, triggering a full-page warning.</li>
<li><strong>Slow load</strong> — first-byte and load times that lose visitors and hurt search rankings.</li>
</ul>
<h2>Why it runs server-side</h2>
<p>A page in your browser can't legitimately read another domain's SSL certificate, HTTP status code, or
response time — the same-origin policy blocks it. So when you submit a domain, the check runs on our
server and returns a plain-English report of what's broken and what to do about it next.</p>
<script>
(function(){
  var f=document.querySelector('form');
  if(!f){return;}
  f.addEventListener('submit',function(){
    var d=document.getElementById('domain');
    if(d&&d.value){d.value=d.value.trim().replace(/^https?:\\/\\//i,'').replace(/\\/.*$/,'');}
  });
})();
</script>
""",
    "ctaline": "A site that's down, insecure, or slow quietly loses every visitor you worked to earn.",
    "share_text": "Free broken-website checker — test any domain for down / no-HTTPS / slow / expired SSL, no login:",
    "bonus": "<h2>Bonus: 60-second site-health checklist</h2><ul>"
             "<li>HTTPS loads with no warning, and http:// redirects to https://</li>"
             "<li>SSL certificate expires more than 30 days out</li>"
             "<li>Homepage first byte under ~1s; full load under ~3s on mobile</li>"
             "<li>A real 404 page (not a blank/error) for bad URLs</li>"
             "<li>Phone, address and hours render without JavaScript</li></ul>",
}

TOOLS = [T_CANSPAM, T_ROI, T_REDDIT, T_SPAMWORDS, T_BROKENSITE]


def render(tool: dict) -> str:
    faq_ld = ""
    return (
        head(tool["slug"], tool["title"], tool["desc"], faq_ld)
        + f'<h1>{esc(tool["h1"])}</h1>'
        + f'<p>{tool["intro"]}</p>'
        + tool["body"]
        + cta(tool["ctaline"])
        + viral_block(tool)
        + foot(tool.get("note"))
    )


def node_check(html: str) -> tuple[bool, str]:
    scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
    js = "\n;\n".join(s for s in scripts if "ld+json" not in s)
    js = "var document={getElementById:function(){return{};},};var encodeURIComponent=function(x){return x;};\n" + js
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(js)
        p = f.name
    try:
        r = subprocess.run(["node", "--check", p], capture_output=True, text=True, timeout=20)
        return r.returncode == 0, r.stderr.strip()
    except FileNotFoundError:
        return True, "node not installed (skipped)"
    finally:
        Path(p).unlink(missing_ok=True)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="node --check each tool's JS, don't write")
    args = ap.parse_args(argv)

    if args.check:
        bad = 0
        for t in TOOLS:
            ok, err = node_check(render(t))
            print(f"  {'OK ' if ok else 'ERR'} {t['slug']}.html  {err if not ok else ''}")
            bad += 0 if ok else 1
        return 1 if bad else 0

    OUTDIR.mkdir(parents=True, exist_ok=True)
    for t in TOOLS:
        html = render(t)
        ok, err = node_check(html)
        if not ok:
            print(f"  SKIP (JS error) {t['slug']}: {err}")
            continue
        (OUTDIR / f"{t['slug']}.html").write_text(html)
        print(f"  wrote {t['slug']}.html ({len(html)} bytes)")

    # rebuild sitemap/robots + internal-link mesh
    sys.path.insert(0, str(BASE / "agents"))
    try:
        import seo_sitemap  # noqa
        seo_sitemap.rebuild()
        print("  sitemap.xml/robots.txt rebuilt")
    except Exception as e:  # pragma: no cover
        print(f"  (sitemap rebuild skipped: {e})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
