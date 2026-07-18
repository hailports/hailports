#!/usr/bin/env python3
"""Local-search visibility probe — does a business rank for "<trade> <city>"?

Passive + $0: queries the local SearXNG (127.0.0.1:8890, no API key, no LLM) for the search
a customer would actually type, then checks where the business's OWN domain lands among the
real (non-aggregator) results. Absent from the first page, or buried past the top few = a
verifiable "you're invisible for your own core search" opportunity.

Emits the same {snag,severity,angle} shape as core.web_failure_probe._opportunity_signals so
it drops straight into an outreach play. It can't live INSIDE that fn (which gets only the
already-fetched body and must make no network calls) — local rank needs the SERP lookup — so
it's a separate passive detector. No page fetch, no active scan, no third-party probing: it
only reads the public SERP the owner can reproduce in their own browser.

  python3 -m core.local_rank_probe bigbirgeplumbing.com plumber "Omaha NE"
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agents.local_biz_scraper import _searx  # noqa: E402

SERP_WINDOW = 20   # how deep into the SERP we look
MIN_RESULTS = 5    # need at least this many distinct real results to judge fairly
TOP_GOOD = 5       # ranking inside the top this-many = healthy, no signal


def _norm_host(domain: str) -> str:
    d = (domain or "").strip().lower()
    d = re.sub(r"^[a-z]+://", "", d).split("/")[0].split("?")[0].split(":")[0]
    if d.startswith("www."):
        d = d[4:]
    return d.strip(". ")


def _base(host: str) -> str:
    return ".".join(host.split(".")[-2:]) if host else host


def _rank_of(target: str, ordered: list[str]):
    """1-based position of target among the de-duped ordered SERP, or None if absent.
    Matches on registrable base so www./subdomain variants still count as 'them'."""
    want = _base(_norm_host(target))
    seen: set = set()
    pos = 0
    for h in ordered:
        b = _base(_norm_host(h))
        if not b or b in seen:
            continue
        seen.add(b)
        pos += 1
        if b == want:
            return pos
    return None


def rank_signals(domain: str, trade: str, city: str) -> list[dict]:
    """Return [] or one {snag,severity,angle} for the business's local-search visibility.

    Never raises. Returns [] when the search itself is inconclusive (SearXNG down or too few
    real results) so we never fabricate a 'not ranking' claim, and [] when they already rank
    in the top few (no opportunity)."""
    try:
        host = _norm_host(domain)
        if not host or "." not in host:
            return []
        query = f"{trade} {city}".strip()
        ordered = _searx(query, limit=SERP_WINDOW)
        uniq: list[str] = []
        seen: set = set()
        for h in ordered:
            b = _base(_norm_host(h))
            if b and b not in seen:
                seen.add(b)
                uniq.append(h)
        if len(uniq) < MIN_RESULTS:
            return []
        rank = _rank_of(host, uniq)
        if rank is not None and rank <= TOP_GOOD:
            return []
        if rank is None:
            return [{
                "snag": "not_ranking_locally",
                "severity": "high",
                "angle": (f"I searched \"{query}\" the way your customers do and your website "
                          f"didn't come up on the first page — the shops that do are getting "
                          f"those calls. I can get you showing for the searches that actually "
                          f"bring in work."),
            }]
        return [{
            "snag": "not_ranking_locally",
            "severity": "medium",
            "angle": (f"When I searched \"{query}\", your site showed up around #{rank} — past "
                      f"where most people ever scroll. The top few spots take almost all the "
                      f"clicks, and I can help you climb into them."),
        }]
    except Exception:
        return []


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    if len(argv) < 3:
        print('usage: python3 -m core.local_rank_probe <domain> <trade> "<city>"')
        return 1
    domain, trade, city = argv[0], argv[1], argv[2]
    out = {"domain": _norm_host(domain), "query": f"{trade} {city}",
           "signals": rank_signals(domain, trade, city)}
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
