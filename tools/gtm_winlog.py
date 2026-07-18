#!/usr/bin/env python3
"""gtm_winlog — the one win condition for the 0->1 GTM loop, per brand.

Collapse each brand's game to one number. BrandA = calls booked; faceless
brands (hailports, scannerapp, ...) = self-serve conversions (signup/paid/reply),
no calls. Append-only jsonl; no external deps. See .claude/skills/gtm-loop.

  win    --brand B --type call|signup|paid|reply --who "who" --via ch [--note ..]  log a win
  touch  --brand B --who "who" --via channel [--note "..."]                        log a send (denominator)
  board  [--brand B | --all] [--days N]                                            scoreboard (default 30d)
"""
import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

LOG = Path.home() / "claude-stack" / "data" / "hustle" / "gtm" / "winlog.jsonl"


def _now():
    return datetime.now(timezone.utc).isoformat()


def _append(row):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(json.dumps(row) + "\n")


def _rows():
    if not LOG.exists():
        return []
    out = []
    for line in LOG.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _log(kind, a):
    row = {"ts": _now(), "kind": kind, "brand": a.brand, "who": a.who,
           "via": a.via, "note": a.note or ""}
    if kind == "win":
        row["type"] = a.type
    _append(row)
    if kind == "win":
        icon = {"call": "📞", "signup": "✍️", "paid": "💰", "reply": "↩️"}.get(a.type, "✅")
        verb = f"{icon} WIN[{a.type}]"
    else:
        verb = "· touch"
    print(f"{verb}  {a.brand}  {a.who}  via {a.via}"
          + (f"  ({a.note})" if a.note else ""))


def _board(a):
    cutoff = datetime.now(timezone.utc) - timedelta(days=a.days)
    books = defaultdict(int)
    touches = defaultdict(int)
    recent = []
    for r in _rows():
        try:
            ts = datetime.fromisoformat(r["ts"])
        except (ValueError, KeyError):
            continue
        if ts < cutoff:
            continue
        b = r.get("brand", "?")
        if a.brand and b != a.brand:
            continue
        if r.get("kind") in ("win", "book"):
            books[b] += 1
            recent.append(r)
        else:
            touches[b] += 1
    brands = sorted(set(books) | set(touches))
    print(f"\n🎯 WINS — last {a.days}d"
          + (f" — {a.brand}" if a.brand else " — all brands") + "\n")
    print(f"{'brand':<22} {'wins':>7} {'touches':>8} {'win-rate':>10}")
    print("-" * 50)
    tot_b = tot_t = 0
    for b in brands:
        bk, tc = books[b], touches[b]
        tot_b += bk
        tot_t += tc
        rate = f"{100*bk/tc:.0f}%" if tc else "—"
        print(f"{b:<22} {bk:>7} {tc:>8} {rate:>10}")
    print("-" * 50)
    rate = f"{100*tot_b/tot_t:.0f}%" if tot_t else "—"
    print(f"{'TOTAL':<22} {tot_b:>7} {tot_t:>8} {rate:>10}")
    if recent:
        print("\nrecent wins:")
        for r in sorted(recent, key=lambda x: x["ts"], reverse=True)[:10]:
            d = r["ts"][:10]
            t = f"[{r['type']}] " if r.get("type") else ""
            print(f"  {d}  {r['brand']:<16} {t}{r.get('who','?')}"
                  f"  via {r.get('via','?')}"
                  + (f"  — {r['note']}" if r.get("note") else ""))
    print()


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("win")
    w.add_argument("--brand", required=True)
    w.add_argument("--type", required=True,
                   choices=["call", "signup", "paid", "reply"])
    w.add_argument("--who", required=True)
    w.add_argument("--via", required=True)
    w.add_argument("--note", default="")
    t = sub.add_parser("touch")
    t.add_argument("--brand", required=True)
    t.add_argument("--who", required=True)
    t.add_argument("--via", required=True)
    t.add_argument("--note", default="")
    b = sub.add_parser("board")
    b.add_argument("--brand", default=None)
    b.add_argument("--all", action="store_true")
    b.add_argument("--days", type=int, default=30)
    a = p.parse_args()
    if a.cmd in ("win", "touch"):
        _log(a.cmd, a)
    else:
        _board(a)


if __name__ == "__main__":
    sys.exit(main())
