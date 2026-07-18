#!/usr/bin/env python3
"""funnel_report.py — the broken-site funnel, end to end, from real logs only.

268 cold sends had produced zero replies and nothing recorded WHY: no click data, so "no replies" could not
be told apart from "never opened" or "opened, saw the wrong business, left". Every one of those 268 linked a
mockup built by the buggy generator (wrong vertical, duplicate headline). The proof pages became correct on
2026-07-09 17:05 and beaconed on 2026-07-10.

Only counts what a log line proves. No estimates.

  python3 tools/funnel_report.py
"""
from __future__ import annotations
import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(os.path.expanduser("~/claude-stack"))
SENT = ROOT / "data" / "hustle" / "broken_site_sent.jsonl"
VIEWS = ROOT / "data" / "hustle" / "mockup_views.jsonl"
LEADS = ROOT / "data" / "hustle" / "inbound_leads.jsonl"

# the moment the linked proof page stopped showing the wrong business
FIXED_AT = "2026-07-09T17:05"
# beacon verified end-to-end at 00:42Z; earlier rows are our own local + browser tests, not prospects
BEACON_LIVE = "2026-07-10T00:45"
PROBE = ("example.com", "probe", "smoketest", "healthcheck", "verify-test", "test-ignore")


def _rows(p: Path) -> list[dict]:
    if not p.exists():
        return []
    out = []
    for ln in p.read_text(errors="ignore").splitlines():
        if ln.strip():
            try:
                out.append(json.loads(ln))
            except Exception:
                pass
    return out


def main() -> int:
    sent = _rows(SENT)
    views = _rows(VIEWS)
    leads = [l for l in _rows(LEADS)
             if not any(t in json.dumps(l).lower() for t in PROBE)]   # our own probes are not leads

    pre = [s for s in sent if str(s.get("ts", "")) < FIXED_AT]
    post = [s for s in sent if str(s.get("ts", "")) >= FIXED_AT]
    ok = [s for s in sent if s.get("ok")]

    sent_domains = {s.get("domain") for s in sent if s.get("domain")}
    views = [v for v in views if str(v.get("ts", "")) >= BEACON_LIVE]
    viewed = Counter(v["domain"] for v in views if v.get("domain") in sent_domains)

    print("BROKEN-SITE FUNNEL")
    print(f"  sent            {len(sent)}  (delivered {len(ok)}, failed {len(sent) - len(ok)}"
          f" = {(len(sent)-len(ok))/max(1,len(sent))*100:.1f}%  — governor pauses >7%)")
    print(f"    with a BROKEN proof page   {len(pre)}")
    print(f"    with a CORRECT proof page  {len(post)}")
    print(f"  proof views     {sum(viewed.values())} across {len(viewed)} prospects")
    if post:
        seen = sum(1 for s in post if s.get("domain") in viewed)
        print(f"    view rate (correct-page sends): {seen}/{len(post)} = {seen/len(post)*100:.0f}%")
    print(f"  real replies    {len(leads)}")
    print()
    if not views:
        print("  NO VIEWS YET. Either nobody has opened a mail since the beacon shipped, or the beacon is")
        print("  broken. Check: curl -s -o /dev/null -w '%{http_code}' 'https://intake.hailports.com/px?d=x'")
        print("  should return 204 and append to data/hustle/mockup_views.jsonl.")
    elif not leads:
        print("  Views but no replies => the proof page or the ask is not converting. Copy problem.")
    print()
    print("  DECIDE ON VOLUME ONLY AFTER >=15 sends carry a correct proof page.")
    print(f"  so far: {len(post)}/15")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
