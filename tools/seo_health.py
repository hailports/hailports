#!/usr/bin/env python3
"""seo_health — self-verifying, self-healing watchdog for the whole Hailports SEO loop.

The trap this kills: a recurring SEO worker stays "loaded" in launchd but silently no-ops or
drifts off-brand for weeks. This checks that each worker actually PRODUCED recently, that every
public surface targets hailports (zero cross-brand), and that the live indexing path is healthy —
then SELF-HEALS by kickstarting any stale worker. It never asks a human: genuine, unrecoverable
problems route ONE deduped alert through core.alert_gateway; everything else self-repairs.

    python3 tools/seo_health.py            # check + self-heal, prints a status line per worker
    python3 tools/seo_health.py --json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
HUSTLE = ROOT / "data" / "hustle"
DIST = HUSTLE / "hailports_dist"
UID = os.getuid()
CROSS_BRAND = re.compile(r"scannerapp|docsapp|persona1|buyersignal|redacted|opsapp|"
                         r"redacted|redacted|redacted|redacted|redacted", re.I)

# worker -> (output artifact, max staleness seconds, launchd label to kickstart on staleness)
DAY = 86400
WORKERS = {
    "prog-seo-factory": (HUSTLE / "seo_pages" / "generated" / "sitemap.xml", 2 * DAY,
                         "com.claude-stack.prog-seo-factory"),
    "seo-evolver":      (HUSTLE / "seo_targets.json", 2 * DAY, "com.claude-stack.seo-evolver"),
    "seo-amplify":      (HUSTLE / "seo_offsite_queue.json", 2 * DAY, "com.claude-stack.seo-amplify"),
    "indexnow-ping":    (Path.home() / ".indexnow-ping.out", 2 * DAY, "com.claude-stack.indexnow-ping"),
}


def _age(p: Path) -> float:
    return (time.time() - p.stat().st_mtime) if p.exists() else float("inf")


def _kickstart(label: str) -> bool:
    try:
        subprocess.run(["launchctl", "kickstart", "-k", f"gui/{UID}/{label}"],
                       capture_output=True, timeout=20)
        return True
    except Exception:
        return False


def _get(url: str, timeout=20) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "SEOHealth/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def check() -> dict:
    out: dict = {"workers": {}, "live": {}, "healed": [], "problems": []}

    # 1. each recurring worker produced recently + still single-brand
    for name, (artifact, max_age, label) in WORKERS.items():
        age = _age(artifact)
        stale = age > max_age
        produced = artifact.exists() and artifact.stat().st_size > 0
        rec = {"artifact": str(artifact.relative_to(ROOT) if artifact.is_relative_to(ROOT) else artifact),
               "age_h": round(age / 3600, 1) if age != float("inf") else None,
               "produced": produced, "stale": stale}
        if stale or not produced:
            rec["healed"] = _kickstart(label)
            out["healed"].append(name)
        out["workers"][name] = rec

    # 2. staged source corpus is on-brand (deploy guard's net — catch drift before deploy)
    gen = HUSTLE / "seo_pages" / "generated"
    leaks = [f.name for f in gen.glob("*.html") if CROSS_BRAND.search(f.read_text(errors="ignore"))]
    out["workers"]["prog-seo-factory"]["cross_brand_in_source"] = len(leaks)
    if leaks:
        out["problems"].append(f"{len(leaks)} regenerated source pages carry cross-brand refs")

    # 2b. ICP guard — each generated page must route to a real product (a live checkout SKU or the
    # AI-visibility check), not just the free-leads funnel. Counts wrong-ICP drift so it re-alerts.
    try:
        from products.self_serve.hailports_catalog import CHECKOUT_TIERS
        skus = list(CHECKOUT_TIERS)
    except Exception:
        skus = ["ai_visibility_watch", "fix_plan", "geo_fix_kit", "seo_starter", "site_health_watch"]
    icp_re = re.compile("|".join(re.escape(s) for s in skus) + r"|ai-visibility/check", re.I)
    icp_violators = sum(1 for f in gen.glob("*.html")
                        if not icp_re.search(f.read_text(errors="ignore")))
    out["workers"]["prog-seo-factory"]["icp_violators"] = icp_violators
    if icp_violators:
        out["problems"].append(f"{icp_violators} generated pages route to no product "
                               "(no checkout SKU / ai-visibility check)")

    # 2c. ICP guard on the PUBLISHED surface (the live /guides tree). Scanning only generated/
    # HID the wrong-ICP drift from the other generators (freetools/programmatic/repurpose/shop)
    # and the scrub-flattened legacy pages — so scan what is actually served. The scrub pipeline's
    # ICP-spine injection should hold this at ~0; a non-zero count means off-ICP pages are live.
    pub = HUSTLE / "seo_pages_published"
    pub_dir = pub if pub.exists() else (DIST / "guides")
    pub_pages = ([f for f in pub_dir.glob("*.html") if f.name != "index.html"]
                 if pub_dir.exists() else [])
    pub_violators = sum(1 for f in pub_pages if not icp_re.search(f.read_text(errors="ignore")))
    out["live"]["published_pages"] = len(pub_pages)
    out["live"]["published_icp_violators"] = pub_violators
    if pub_violators:
        out["problems"].append(f"{pub_violators} PUBLISHED /guides pages route to no product "
                               "(no checkout SKU / ai-visibility check)")

    # 3. deployed tree single-brand (anonymity hard rail)
    dist_leaks = sum(1 for f in DIST.rglob("*.html") if CROSS_BRAND.search(f.read_text(errors="ignore")))
    out["live"]["dist_cross_brand"] = dist_leaks
    if dist_leaks:
        out["problems"].append(f"DEPLOYED dist has {dist_leaks} cross-brand pages (anonymity leak)")

    # 4. live indexing path healthy: robots -> root sitemap, sitemap reachable + non-trivial + clean
    try:
        robots = _get("https://www.hailports.com/robots.txt")
        out["live"]["robots_root_sitemap"] = "hailports.com/sitemap.xml" in robots
        sm = _get("https://www.hailports.com/sitemap.xml")
        locs = re.findall(r"<loc>(.*?)</loc>", sm)
        out["live"]["sitemap_urls"] = len(locs)
        out["live"]["sitemap_cross_brand"] = bool(CROSS_BRAND.search(sm))
        if len(locs) < 100:
            out["problems"].append(f"live sitemap only {len(locs)} urls (expected ~680+)")
        if out["live"]["sitemap_cross_brand"]:
            out["problems"].append("live sitemap contains a cross-brand URL")
        if not out["live"]["robots_root_sitemap"]:
            out["problems"].append("live robots.txt not pointing at root sitemap")
    except Exception as e:
        out["live"]["error"] = str(e)[:160]
        out["problems"].append(f"live site unreachable: {str(e)[:80]}")

    # 5. feedback signal alive: GSC pull OR the GSC-free fallback (never blocks)
    perf = HUSTLE / "gsc_performance.json"
    tgt = HUSTLE / "seo_targets.json"
    try:
        p = json.loads(perf.read_text()) if perf.exists() else {}
        out["live"]["gsc_property"] = p.get("property")
        out["live"]["gsc_live"] = p.get("property") == "sc-domain:hailports.com"
    except Exception:
        out["live"]["gsc_live"] = False
    out["live"]["targets_fresh"] = _age(tgt) < 2 * DAY
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    r = check()

    # ONE deduped alert only on a genuine, unhealed problem (zero noise otherwise)
    if r["problems"]:
        try:
            from core import alert_gateway
            alert_gateway.route("warn", "seo_health", "SEO loop needs attention",
                                "\n".join(r["problems"]), issue_key="seo_health")
        except Exception:
            pass

    if a.json:
        print(json.dumps(r, indent=2, default=str))
        return 0
    for name, w in r["workers"].items():
        flag = "STALE->healed" if name in r["healed"] else "ok"
        print(f"  {name:18} produced={w['produced']} age={w.get('age_h')}h {flag}")
    lv = r["live"]
    print(f"  live: sitemap_urls={lv.get('sitemap_urls')} root_robots={lv.get('robots_root_sitemap')} "
          f"dist_cross_brand={lv.get('dist_cross_brand')} gsc_live={lv.get('gsc_live')} "
          f"targets_fresh={lv.get('targets_fresh')}")
    print(f"  icp: published_pages={lv.get('published_pages')} "
          f"published_icp_violators={lv.get('published_icp_violators')}")
    print(f"  problems: {r['problems'] or 'none'}")
    return 1 if r["problems"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
