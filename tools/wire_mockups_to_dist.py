#!/usr/bin/env python3
"""Wire the broken-site-rescue mockups into the hailports deploy tree.

Engine #2 (broken-site rescue) builds a free per-prospect rebuild mockup in
core.site_generator (products_internal/landing/mockups/<domain>.html) and the
gift channel ships a proof link `https://www.hailports.com/mockups/<domain>.html`.
Those links 404 because the mockups were never copied into the deployed tree
(data/hustle/hailports_dist/). This copies them in so the proof links resolve.

Safety properties enforced here (the reason this is a tool, not a `cp`):
- every published mockup is forced noindex,nofollow — a per-prospect gift is not
  an SEO page; 2k+ crawlable business redesigns on the host brand would be an
  obvious outreach-operation footprint (anonymity rail).
- any mockup carrying a cross-brand / PII / model-trademark token is SKIPPED so
  it can never reach the public tree (defense-in-depth vs deploy_hailports.sh's
  own abort guard).
- manifests (.manifest.json) are NOT published — they expose the generator's
  palette/variant fingerprints and correlate the mockups as machine-made.

Nothing here is outward-facing: it only writes local files. Going live still
requires `bash scripts/deploy_hailports.sh` (staged in ALEX_ACTION_QUEUE.md).
Reversible: `python3 -m tools.wire_mockups_to_dist --clean`.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FORCE = "--force" in sys.argv        # one-time backfill (e.g. injecting the proof beacon)
SRC = ROOT / "products_internal" / "landing" / "mockups"
DEST = ROOT / "data" / "hustle" / "hailports_dist" / "mockups"

# same token set deploy_hailports.sh aborts on, plus Operator PII + model trademarks.
FORBIDDEN = re.compile(
    r"scannerapp|docsapp|persona1|buyersignal|redacted|redacted|redacted"
    r"|redacted|redacted|redacted|Operator|escarpment|\bredacted\b"
    r"|anthropic|\bclaude\b|openai|chatgpt",
    re.I,
)
VIEWPORT = '<meta name="viewport" content="width=device-width,initial-scale=1">'
ROBOTS = '<meta name="robots" content="noindex,nofollow">'


BEACON_MARK = "hp-proof-beacon"


def _ensure_beacon(html: str, domain: str) -> str:
    """Log that this prospect actually LOADED their proof page.

    268 cold sends produced zero replies and we could not tell why: never opened, or opened and bounced off
    a mockup that showed the wrong business (every one of those 268 linked a page built by the buggy
    generator). Without this, "no replies" is unattributable.

    First-party only — a beacon to hailports' own intake host, which is also the brand in the email body, so
    the sender-brand == body-link rail holds. No third-party script, no email open-pixel, no cookie. It
    records the domain and a timestamp; nothing about the visitor.
    """
    if BEACON_MARK in html:
        return html
    # NOT navigator.sendBeacon: it issues a POST, and /px is a GET route — the beacon fired and logged
    # nothing (caught by driving a real browser at the deployed page). An image GET is cross-origin-safe,
    # needs no CORS, and works with JS disabled if we ever inline it as <img>.
    tag = (f'<script id="{BEACON_MARK}">'
           f'try{{new Image().src="https://intake.hailports.com/px?d={domain}&t="+Date.now()}}catch(e){{}}'
           f'</script>')
    if "</body>" in html:
        return html.replace("</body>", tag + "</body>", 1)
    return html + tag


def _ensure_noindex(html: str) -> str:
    if "noindex" in html:
        return html
    if VIEWPORT in html:
        return html.replace(VIEWPORT, VIEWPORT + "\n" + ROBOTS, 1)
    # fallback: inject right after <head>
    return re.sub(r"(<head>)", r"\1\n" + ROBOTS, html, count=1)


def clean() -> int:
    if not DEST.exists():
        print("nothing to clean (dist/mockups absent)")
        return 0
    n = 0
    for f in DEST.glob("*.html"):
        f.unlink()
        n += 1
    try:
        DEST.rmdir()
    except OSError:
        pass
    print(f"removed {n} wired mockups from {DEST}")
    return 0


def wire(limit: int | None = None) -> int:
    if not SRC.is_dir():
        print(f"ERROR: source dir missing: {SRC}", file=sys.stderr)
        return 1
    DEST.mkdir(parents=True, exist_ok=True)
    srcs = sorted(SRC.glob("*.html"))
    if limit:
        srcs = srcs[:limit]
    wired = skipped = noindexed = unchanged = 0
    skips: list[str] = []
    for src in srcs:
        # 2441 files / 1.31 GB get a full FORBIDDEN regex scan + noindex rewrite on every call. The rebuild
        # publishes repeatedly, so re-scanning untouched pages pushed one publish past 900s and it was killed
        # mid-run. Skip anything whose published copy is already newer than its source; safety is unchanged
        # because a page can only reach dist by having passed the scan when it was last written.
        dst = DEST / src.name
        if not FORCE and dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
            unchanged += 1
            continue
        html = src.read_text(encoding="utf-8", errors="replace")
        if FORBIDDEN.search(html):
            skipped += 1
            skips.append(src.name)
            continue
        fixed = _ensure_noindex(html)
        if fixed != html:
            noindexed += 1
        fixed = _ensure_beacon(fixed, src.name[:-5])
        (DEST / src.name).write_text(fixed, encoding="utf-8")
        wired += 1
    print(f"source mockups:   {len(srcs)}")
    print(f"unchanged (skip): {unchanged}")
    print(f"wired -> dist:    {wired}")
    print(f"  noindex added:  {noindexed} (rest already had it)")
    print(f"skipped (tokens): {skipped}")
    if skips:
        print("  " + ", ".join(skips[:20]) + (" …" if len(skips) > 20 else ""))
    print(f"dest: {DEST}")
    print("manifests NOT published (anonymity); .html only.")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--clean", action="store_true", help="remove the wired dist/mockups tree")
    ap.add_argument("--limit", type=int, default=None, help="cap how many to wire (testing)")
    ap.add_argument("--force", action="store_true", help="rewrite even unchanged pages (beacon backfill)")
    args = ap.parse_args(argv)
    return clean() if args.clean else wire(args.limit)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
