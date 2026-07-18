#!/usr/bin/env python3
"""gig_source_probe.py — the HONEST unit-economics probe for the dev-shop demand side.

The redacted radar reported ok:true/raw:0 while Craigslist + Reddit silently 403'd at the network
layer — i.e. it measured a 200 from a search proxy, not actual gigs, and lied that the pipeline was
healthy. This probe hits each real source directly, reports TRUE reachability (LIVE / BLOCKED-403 /
AUTH-NEEDED / EMPTY), and counts the ACTUAL client-hiring gigs reachable RIGHT NOW. The output number
is the whole unit economics: if ~3 real biddable gigs exist this week, the dev-shop is a thin side
lane, not a money machine — and you should know that hard number before building anything on it.

Doubles as the working HN harvester (the one live source): parses the monthly
"Ask HN: Freelancer? Seeking freelancer?" thread into structured client gigs.

CLI: python3 tools/gig_source_probe.py
"""
from __future__ import annotations

import json
import re
import urllib.request

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1 Safari/605.1"


def _get(url: str, timeout: int = 20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    return urllib.request.urlopen(req, timeout=timeout)


def _strip(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or "")).strip()


def probe_hn(threads: int = 2) -> dict:
    """The ONE live free source. Pull the latest N monthly 'Freelancer? Seeking freelancer?' threads,
    keep only SEEKING FREELANCER (clients hiring) — drop SEEKING WORK (freelancers = competition)."""
    out = {"source": "hn_freelancer", "status": "EMPTY", "gigs": [], "note": ""}
    try:
        s = json.load(_get("https://hn.algolia.com/api/v1/search_by_date?query=freelancer%20seeking%20"
                           "freelancer&tags=story&hitsPerPage=" + str(threads + 2)))
        stories = [h for h in s.get("hits", []) if "seeking freelancer" in (h.get("title") or "").lower()][:threads]
        if not stories:
            out["note"] = "no seeking-freelancer story found"
            return out
        for st in stories:
            sid = st.get("objectID")
            month = (st.get("title") or "")[-9:]
            item = json.load(_get("https://hn.algolia.com/api/v1/items/" + str(sid)))
            for c in item.get("children", []):
                txt = _strip(c.get("text", ""))
                if not txt:
                    continue
                # client posts lead with "SEEKING FREELANCER"; skip the "SEEKING WORK" self-ads
                if not re.match(r"seeking[\s_]*freelancer", txt, re.I):
                    continue
                email = (re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", txt) or [None])
                email = email.group(0) if hasattr(email, "group") else None
                out["gigs"].append({"thread": month, "id": c.get("id"),
                                    "blurb": txt[:240], "email": email,
                                    "remote": bool(re.search(r"remote", txt, re.I))})
        out["status"] = "LIVE" if out["gigs"] else "EMPTY(no clients hiring this cycle)"
    except Exception as e:  # noqa: BLE001
        out["status"] = "ERROR"
        out["note"] = str(e)[:120]
    return out


def probe_craigslist() -> dict:
    """The supposed auto-send lane. Test the RSS endpoint the radar's premise depends on."""
    o = {"source": "craigslist_relay", "status": "?", "gigs": [], "note": ""}
    try:
        r = _get("https://sfbay.craigslist.org/search/cpg?format=rss")
        body = r.read().decode("utf-8", "ignore")
        n = body.count("<item")
        o["status"] = "LIVE" if n else "EMPTY"
        o["note"] = f"{n} items"
    except Exception as e:  # noqa: BLE001
        code = getattr(e, "code", None)
        o["status"] = "BLOCKED-403" if code == 403 else "BLOCKED"
        o["note"] = (f"HTTP {code}; " if code else "") + \
            "CL bans bot/RSS access. Search-proxy fallback can't see fresh (CL noindexes posts). " \
            "Auto-send lane is effectively dead; only a real logged-in human browser can read fresh CL."
    return o


def probe_reddit() -> dict:
    """r/forhire — the radar's other primary source."""
    o = {"source": "reddit_forhire", "status": "?", "gigs": [], "note": ""}
    try:
        r = _get("https://www.reddit.com/r/forhire/new.json?limit=25")
        d = json.loads(r.read().decode("utf-8", "ignore"))
        kids = d.get("data", {}).get("children", [])
        hiring = [c for c in kids if "hiring" in (c["data"].get("link_flair_text") or "").lower()]
        o["status"] = "LIVE" if hiring else "EMPTY"
        o["gigs"] = [{"title": c["data"].get("title", "")[:90],
                      "url": "https://reddit.com" + c["data"].get("permalink", "")} for c in hiring[:10]]
    except Exception as e:  # noqa: BLE001
        code = getattr(e, "code", None)
        o["status"] = "AUTH-NEEDED" if code in (403, 429) else "BLOCKED"
        o["note"] = (f"HTTP {code}; " if code else "") + \
            "Reddit walled off anon JSON. FIXABLE: register a free Reddit OAuth app (client_id/secret) " \
            "and hit oauth.reddit.com — but bids on Reddit are human-tap (bot-bannable) regardless."
    return o


def main():
    sources = [probe_hn(), probe_craigslist(), probe_reddit()]
    biddable = sum(len(s["gigs"]) for s in sources)
    print("=" * 72)
    print("DEV-SHOP DEMAND REALITY — biddable client gigs reachable RIGHT NOW")
    print("=" * 72)
    for s in sources:
        print(f"\n[{s['status']:>26}]  {s['source']}  ({len(s['gigs'])} gigs)")
        if s.get("note"):
            print(f"      note: {s['note']}")
        for g in s["gigs"][:6]:
            line = g.get("title") or g.get("blurb") or ""
            tag = (" <" + g["email"] + ">") if g.get("email") else ""
            print(f"      - {line[:96]}{tag}")
    print("\n" + "-" * 72)
    print(f"TOTAL biddable client gigs reachable now: {biddable}")
    auto = [s for s in sources if s["status"] == "LIVE" and s["source"] == "craigslist_relay"]
    print("AUTONOMOUS auto-send lane (CL relay) live: " + ("YES" if auto else "NO — it's 403-blocked"))
    print("Verdict: " + ("thin/human-bid lane, not an autonomous engine" if biddable < 15
                         else "enough flow to justify a harvester"))
    return biddable


if __name__ == "__main__":
    main()
