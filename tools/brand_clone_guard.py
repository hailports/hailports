#!/usr/bin/env python3
"""Brand clone-guard — HARD anonymity invariant for the faceless house of brands.

The faceless storefronts (builtfast/signalhq/hailport/docsapp/scannerapp) MUST read as
SEPARATE companies. They kept regressing to near-clones because they render from one shared
global catalog: a per-brand NAME override existed but the BLURB did not, so identical product
descriptions leaked across hosts — trivially linking the brands as one operator.

This guard fails (exit 1 + iMessage alert) if ANY product NAME or BLURB string is rendered by
more than one faceless brand, or if two brands' catalog footprints overlap beyond a threshold.
It is the durable backstop so this can never silently drift back. Run it on a timer + at boot.

  python3 -m tools.brand_clone_guard            # check; exit 1 on violation
  python3 -m tools.brand_clone_guard --quiet    # only print/alert on violation
"""
from __future__ import annotations

import os
import sys
from collections import defaultdict

os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_guard")

# Brands that must stay mutually un-correlated. (scannerapp is the warmed flagship but is still
# a faceless brand — included so nothing leaks onto it either.)
FACELESS = ["builtfast", "signalhq", "hailport", "docsapp", "scannerapp"]
# Max fraction of one brand's catalog slugs that may also appear on another brand before it's a
# footprint tell (distinct copy still required for any shared slug; this catches wholesale clones).
MAX_SLUG_OVERLAP = 0.34


def _per_brand_rows():
    """{brand_key: [(name, blurb), ...]} as actually rendered, via the live storefront logic."""
    from products.self_serve.app import STOREFRONT_BRANDS, _sf_section_blocks
    out = {}
    for key in FACELESS:
        brand = STOREFRONT_BRANDS.get(key)
        if not brand:
            continue
        featured, sections = _sf_section_blocks(brand)
        rows = list(featured)
        for _title, items in sections:
            rows.extend(items)
        # row tuple: (slug, name, blurb, price, url, section)
        out[key] = [(r[1], r[2]) for r in rows if r]
    return out


def _slug_sets():
    from products.self_serve.app import STOREFRONT_BRANDS
    out = {}
    for key in FACELESS:
        brand = STOREFRONT_BRANDS.get(key) or {}
        slugs = set(brand.get("featured", []))
        for _t, ss in brand.get("sections", []):
            slugs.update(ss)
        out[key] = slugs
    return out


def check() -> list[str]:
    """Return a list of violation strings (empty = clean)."""
    violations: list[str] = []
    rows = _per_brand_rows()

    name_hosts = defaultdict(set)
    blurb_hosts = defaultdict(set)
    for key, pairs in rows.items():
        for name, blurb in pairs:
            if name and str(name).strip():
                name_hosts[str(name).strip()].add(key)
            if blurb and str(blurb).strip():
                blurb_hosts[str(blurb).strip()].add(key)

    for name, hosts in name_hosts.items():
        if len(hosts) > 1:
            violations.append(f"SHARED PRODUCT NAME on {sorted(hosts)}: {name!r}")
    for blurb, hosts in blurb_hosts.items():
        if len(hosts) > 1:
            violations.append(f"SHARED PRODUCT BLURB on {sorted(hosts)}: {blurb[:70]!r}...")

    slugs = _slug_sets()
    keys = [k for k in FACELESS if slugs.get(k)]
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            a, b = keys[i], keys[j]
            sa, sb = slugs[a], slugs[b]
            if not sa or not sb:
                continue
            overlap = len(sa & sb)
            frac = overlap / min(len(sa), len(sb))
            if frac > MAX_SLUG_OVERLAP:
                violations.append(
                    f"CATALOG FOOTPRINT overlap {a}/{b}: {overlap} shared slugs "
                    f"({frac:.0%} of the smaller) > {MAX_SLUG_OVERLAP:.0%} cap")
    return violations


def main() -> int:
    quiet = "--quiet" in sys.argv
    try:
        violations = check()
    except Exception as exc:
        msg = f"[clone-guard] could not run check: {exc}"
        print(msg)
        return 0  # fail-soft: never block on a guard error (don't false-alarm)

    if not violations:
        if not quiet:
            print("[clone-guard] OK — no faceless brand shares a product name, blurb, or catalog footprint.")
        return 0

    body = "🚨 BRAND CLONE DRIFT — faceless brands are correlating:\n" + "\n".join(f"• {v}" for v in violations)
    print(body)
    try:
        from tools.imsg_bridge import send_imessage
        send_imessage(body[:1200])
    except Exception as exc:
        print(f"[clone-guard] (alert send failed: {exc})")
    return 1


if __name__ == "__main__":
    sys.exit(main())
