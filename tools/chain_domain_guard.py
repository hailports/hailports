#!/usr/bin/env python3
"""chain_domain_guard.py — auto-catch chain/franchise HQ domains the static block misses.

The scrapers resolve a chain's corporate inbox for a local query (user@example.com for an
"Austin plumbing" search), so chains keep reappearing in the pools. A static blocklist is
whack-a-mole. SIGNAL: a real local independent uses its own domain (1-2 leads); a chain/HQ
domain shows up under MANY DIFFERENT business names. So any domain shared by >= THRESHOLD
distinct business names is a chain HQ -> add to enterprise_block.txt + purge its leads.

Runs each cycle from the revenue lead-feed (before routing) so the next hopdoddy is blocked
before it can be sent. Read-only against everything except enterprise_block + the lead pools.

  run:  venv/bin/python tools/chain_domain_guard.py [--threshold 5] [--dry-run]
"""
from __future__ import annotations
import argparse, glob, json, os, re
from collections import defaultdict

BASE = os.path.expanduser("~/claude-stack")
EB = os.path.join(BASE, "data/hustle/enterprise_block.txt")
PERSONAL = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com", "aol.com",
            "me.com", "msn.com", "live.com", "comcast.net", "sbcglobal.net", "att.net", "verizon.net"}
VALID = re.compile(r'^[a-z0-9][a-z0-9.\-]*\.[a-z]{2,}$')
POOLS = (glob.glob(BASE + "/data/hustle/local_biz_leads.jsonl")
         + glob.glob(BASE + "/data/hustle/*outreach_queue*.jsonl")
         + glob.glob(BASE + "/data/hustle/intent_sku_routed_queue.jsonl")
         + glob.glob(BASE + "/data/hustle/contactable_leads.jsonl")
         + glob.glob(BASE + "/data/hustle/enriched_leads.jsonl")
         + glob.glob(BASE + "/data/hustle/multi_market_leads.json")
         + [BASE + "/products/outreach/prospects.json"])


def _dom(email: str) -> str:
    e = str(email or "")
    return e.rsplit("@", 1)[-1].lower().strip().rstrip(".") if "@" in e else ""


def _name(r: dict) -> str:
    for k in ("business", "company", "name", "business_name", "biz", "who"):
        v = str(r.get(k) or "").strip().lower()
        if v:
            return v
    return ""


def _rows(p: str):
    if p.endswith(".json"):
        try:
            d = json.load(open(p))
            return d if isinstance(d, list) else []
        except Exception:
            return []
    out = []
    for l in open(p, errors="ignore"):
        l = l.strip()
        if l:
            try:
                out.append(json.loads(l))
            except Exception:
                pass
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=int, default=5,
                    help="distinct business names sharing a domain => chain HQ")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    blocked = set(l.strip().lower() for l in open(EB, errors="ignore")
                  if l.strip() and not l.startswith("#"))

    names_by_dom: dict[str, set] = defaultdict(set)
    for p in POOLS:
        if not os.path.exists(p):
            continue
        for r in _rows(p):
            if not isinstance(r, dict):
                continue
            d = _dom(r.get("email") or r.get("to"))
            if d and d not in PERSONAL and VALID.match(d):
                names_by_dom[d].add(_name(r) or d)

    chains = sorted(d for d, names in names_by_dom.items()
                    if len(names) >= args.threshold and d not in blocked)

    if not args.dry_run and chains:
        with open(EB, "a") as f:
            for d in chains:
                f.write(d + "\n")

    # purge the freshly-detected chains from the pools
    purged = 0
    block_now = blocked | set(chains)
    if not args.dry_run and chains:
        for p in POOLS:
            if not os.path.exists(p):
                continue
            if p.endswith(".json"):
                try:
                    data = json.load(open(p))
                except Exception:
                    continue
                if not isinstance(data, list):
                    continue
                kept = [r for r in data if not (isinstance(r, dict) and _dom(r.get("email") or r.get("to")) in block_now)]
                if len(kept) != len(data):
                    purged += len(data) - len(kept)
                    json.dump(kept, open(p, "w"), default=str)
            else:
                lines = open(p, errors="ignore").read().splitlines()
                kept, rem = [], 0
                for l in lines:
                    if l.strip() and '"' in l:
                        try:
                            r = json.loads(l)
                            if _dom(r.get("email") or r.get("to")) in block_now:
                                rem += 1
                                continue
                        except Exception:
                            pass
                    kept.append(l)
                if rem:
                    purged += rem
                    open(p, "w").write("\n".join(kept) + ("\n" if kept else ""))

    print(json.dumps({
        "threshold": args.threshold,
        "new_chains_detected": len(chains),
        "leads_purged": purged,
        "dry_run": args.dry_run,
        "sample": chains[:20],
    }, indent=2))


if __name__ == "__main__":
    main()
