#!/usr/bin/env python3
"""yt_drain_retitle.py — make the HailPorts Shorts channel POST cleanly, on its own.

The upload wizard is unreliable two ways: it intermittently saves a DRAFT instead of publishing,
and the title it sets in the wizard does NOT persist (every Short ended up with the same auto
"I rebuilt a dead small-business..." title -> a dupe-dump that "looks like a joke").

Both have a PROVEN reliable path, which this drains on a schedule:
  1. PUBLISH every Draft via the upload wizard (Next->visibility=PUBLIC->Publish). Reliable.
  2. RETITLE any public video whose title is a known dupe/placeholder, on its /edit page via the
     Save button + a real InputEvent (the edit path persists where the wizard path doesn't).

Idempotent + safe to run on a cron: publishes whatever drafts exist, retitles only dupe/placeholder
titles, leaves already-distinct videos alone. Read-only against anything that's already clean.

  run:  venv/bin/python tools/yt_drain_retitle.py [--max N]
"""
from __future__ import annotations
import argparse, json, re, sys, time, urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agents.youtube_uploader import CDP, DEEP, PORT, ensure_chrome  # proven CDP client + shadow helper

UC = "UC_NaJ6WEpYsNlq-CIGD8yAw"

# titles that mean "not yet given a real title" -> safe to overwrite. Anything else is left alone.
DUPE_MARKERS = ("i rebuilt a dead small-business", "video", "untitled", "short", "day 18: an autonomous")

# distinct build-in-public hooks; we assign the first that isn't already live on the channel.
TITLE_POOL = [
    "watch an autonomous AI run a real business, live",
    "I handed a business to an AI and walked away. day by day",
    "zero employees. one AI. a real P&L",
    "an AI is running my whole operation while I sleep",
    "I let software run the company. here's the scoreboard",
    "no humans in the loop — just an AI shipping work daily",
    "an autonomous agent fixed a dead small-business site in minutes",
    "watch AI redesign a broken business website start to finish",
    "this site was costing the owner customers. AI rebuilt it",
    "I gave an AI a broken website. here's what it shipped",
    "the AI found what google couldn't see about this business",
    "AI-visibility check: why buyers never hear this business's name",
    "an AI graded 1,000 local businesses overnight. most failed",
    "I automated the busywork. now the AI runs the business processes end-to-end",
    "day in the life of a business with no employees",
    "the machine did months of dev work while I slept",
    "an AI is quietly outworking a whole team right now",
    "I stopped doing the work. the AI didn't",
    "watch an AI turn a dead website into a working one",
    "real business, real revenue, zero humans touching it",
    "the autonomous stack just shipped again. here's what",
    "I built a company that runs itself. proof inside",
    "an AI handles the grind so the owner doesn't have to",
    "what an AI sees when it audits your business",
]


def _ev(cdp, e):
    return cdp.eval(e)


def _rss_titles() -> dict:
    """{video_id: title} for current PUBLIC videos (authoritative, no shadow-DOM scraping)."""
    try:
        url = f"https://www.youtube.com/feeds/videos.xml?channel_id={UC}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        x = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "replace")
    except Exception:
        return {}
    out = {}
    for e in re.findall(r"<entry>(.*?)</entry>", x, re.S):
        vid = (re.search(r"<yt:videoId>(.*?)</yt:videoId>", e) or [None, ""])[1]
        t = (re.search(r"<title>(.*?)</title>", e, re.S) or [None, ""])[1]
        if vid:
            out[vid] = t.strip()
    return out


def publish_drafts(cdp, max_n: int) -> int:
    published = 0
    for _ in range(max_n + 2):
        cdp.navigate(f"https://studio.youtube.com/channel/{UC}/videos/upload", settle=8.0)
        cdp.eval(DEEP)
        opened = _ev(cdp, r"""(function(){
          var rows=window.__deep('ytcp-video-row');
          for(var i=0;i<rows.length;i++){var r=rows[i],t='';try{t=r.innerText||'';}catch(e){}
            if(/Draft/.test(t)){var l=window.__deep('a#video-title, a#thumbnail-anchor, #video-title-container a',r)[0]||window.__deep('#video-title',r)[0];
              if(l){l.click();return 'opened';}}}
          return 'no-draft';})()""")
        if opened == "no-draft":
            break
        time.sleep(6); cdp.eval(DEEP)
        reached = False
        for _s in range(6):
            vr = json.loads(_ev(cdp, r"""(function(){var r=window.__deep('tp-yt-paper-radio-button').filter(function(x){return /PRIVATE|PUBLIC|UNLISTED/.test(x.getAttribute('name')||'');});return JSON.stringify(r.map(function(x){return x.offsetParent!==null;}));})()"""))
            if any(vr):
                reached = True; break
            _ev(cdp, "(function(){var b=window.__deep('#next-button')[0];if(b&&!b.hasAttribute('disabled'))b.click();})()"); time.sleep(2.5)
        if not reached:
            continue
        _ev(cdp, r"""(function(){var t=window.__deep('tp-yt-paper-radio-button').find(function(x){return (x.getAttribute('name')||'')==='PUBLIC';});if(!t)return;try{t.click();}catch(e){}try{var l=window.__deep('#radioLabel,#radioContainer',t)[0];if(l)l.click();}catch(e){}try{t.checked=true;t.setAttribute('aria-checked','true');t.dispatchEvent(new Event('change',{bubbles:true}));}catch(e){}})()""")
        time.sleep(2)
        clicked = False
        for _w in range(20):
            st = json.loads(_ev(cdp, r"""(function(){var d=window.__deep('#done-button')[0];if(!d)return JSON.stringify({e:false,l:''});return JSON.stringify({e:(!d.hasAttribute('disabled')&&d.offsetParent!==null),l:(d.innerText||'').trim()});})()"""))
            if st.get("e") and "publish" in (st.get("l", "").lower()):
                _ev(cdp, "(function(){window.__deep('#done-button')[0].click();})()"); clicked = True; break
            if st.get("e"):
                _ev(cdp, r"""(function(){var t=window.__deep('tp-yt-paper-radio-button').find(function(x){return (x.getAttribute('name')||'')==='PUBLIC';});if(t){t.checked=true;t.setAttribute('aria-checked','true');t.dispatchEvent(new Event('change',{bubbles:true}));}})()""")
            time.sleep(2)
        if clicked:
            published += 1
            time.sleep(5)
        if published >= max_n:
            break
    return published


def retitle_dupes(cdp) -> int:
    titles = _rss_titles()
    live = {t.strip().lower() for t in titles.values()}
    pool = [t for t in TITLE_POOL if t.lower() not in live]
    fixed = 0
    for vid, cur in titles.items():
        low = (cur or "").strip().lower()
        if not any(low.startswith(m) or low == m for m in DUPE_MARKERS):
            continue  # already distinct -> leave it
        if not pool:
            break
        new = pool.pop(0)
        cdp.navigate(f"https://studio.youtube.com/video/{vid}/edit", settle=7.0); cdp.eval(DEEP)
        _ev(cdp, r"""(function(){
          var box=window.__deep('#title-textarea #textbox')[0]; if(!box)return 'no-box';
          box.focus(); var sel=window.getSelection(),r=document.createRange();
          r.selectNodeContents(box); sel.removeAllRanges(); sel.addRange(r);
          document.execCommand('selectAll',false,null); document.execCommand('insertText',false,%s);
          box.dispatchEvent(new InputEvent('input',{bubbles:true,inputType:'insertText',data:%s}));
          box.dispatchEvent(new Event('change',{bubbles:true})); return 'set';
        })()""" % (json.dumps(new), json.dumps(new)))
        time.sleep(1.5)
        for _w in range(12):
            st = json.loads(_ev(cdp, r"""(function(){var s=window.__deep('#save')[0]||window.__deep('ytcp-button#save')[0];if(!s)return JSON.stringify({f:false});return JSON.stringify({f:true,dis:s.hasAttribute('disabled'),vis:s.offsetParent!==null});})()"""))
            if st.get("f") and not st.get("dis") and st.get("vis"):
                _ev(cdp, "(function(){(window.__deep('#save')[0]||window.__deep('ytcp-button#save')[0]).click();})()")
                fixed += 1; break
            time.sleep(1.5)
        time.sleep(3)
    return fixed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=8, help="max drafts to publish this run")
    args = ap.parse_args()
    ensure_chrome(PORT, headless=True)
    cdp = CDP(PORT); cdp.connect(timeout=20)
    try:
        pub = publish_drafts(cdp, args.max)
        time.sleep(6)  # let newly-published videos register before reading RSS
        fixed = retitle_dupes(cdp)
        print(json.dumps({"published": pub, "retitled": fixed}))
    finally:
        cdp.close()


if __name__ == "__main__":
    main()
