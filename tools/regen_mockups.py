#!/usr/bin/env python3
"""regen_mockups.py — rebuild every shipped rebuild-mockup after the 2026-07-09 fixes.

Why: the shipped set carried three visible defects — 56% classified "Local Business" (and some flatly
wrong: 10thstreetdental.com rendered as *Landscaping*), 1258/2421 sharing the identical H1 "Local service
you can count on", and a double-escaped `Salon &amp;amp; Spa` in the <title>. Detection, copy variants and
the title escape are fixed in core.site_generator; these files are stale artifacts of the old code.

Vertical is RE-DERIVED from the domain + company name rather than trusted from the queue, because the
queue's stored vertical came from the buggy detector.

  python3 tools/regen_mockups.py --dry     # classify only, write nothing
  python3 tools/regen_mockups.py           # regenerate all
"""
from __future__ import annotations
import html, json, os, re, sys, time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(os.path.expanduser("~/claude-stack"))
sys.path.insert(0, str(ROOT))
# Remote hero backends (gemini / cloudflare-flux / pollinations) ALL 429 under a 2421-page bulk run —
# the hero cache key includes each prospect's brand colors, so nearly every page misses. On exhaustion
# site_generator falls through to a LOCAL Metal generator that aborted the whole process (uncaught
# C++ [METAL] GPU timeout) after 80 pages. Disable AI imagery for the bulk pass: pages keep their
# cached art plus the design_kit book-cloth banner with the on-vertical line illustration, and the
# wrong-vertical pages were carrying wrong art anyway (a dental page with a landscaping hero).
os.environ.setdefault("SITE_GEN_AI_IMAGERY", "0")

from core.site_generator import generate_mockup, _guess_vertical, _name_from_domain  # noqa: E402

MOCK_DIR = ROOT / "data" / "hustle" / "hailports_dist" / "mockups"
QUEUE = ROOT / "data" / "hustle" / "broken_site_outreach_queue.jsonl"
DRY = "--dry" in sys.argv


def _name_from_existing(path: Path) -> str:
    """Recover the REAL business name from the mockup we already shipped.

    The outreach queue only retains 79 of 2421 rows, so falling back to _name_from_domain() throws away
    the scraped page title and pushes classification from 56% -> 80% generic (measured). The existing
    <title> is "<Real Business Name> — <label> in <city>", and the name half is the trustworthy part —
    it's what carries "Dental"/"Roofing"/"Law". Only the label half came from the buggy detector.
    """
    try:
        head = path.read_text(encoding="utf-8", errors="ignore")[:4000]
    except Exception:
        return ""
    m = re.search(r"<title>(.*?)</title>", head, re.S)
    if not m:
        return ""
    # The OLD titles were themselves double-escaped ("Able Auto &amp;amp; Truck"), so a single unescape
    # leaves "&amp;" inside the business NAME, which then gets escaped again on render — the bug survives
    # the rebuild. Unescape until it stops changing.
    title = " ".join(m.group(1).split())
    for _ in range(3):
        nxt = html.unescape(title)
        if nxt == title:
            break
        title = nxt
    return title.split("—")[0].strip() if "—" in title else title.strip()


LANDING = ROOT / "products_internal" / "landing" / "mockups"


def _publish() -> None:
    """Copy landing -> the shipped dist tree. Publishing ONLY at the end meant a mid-run crash left every
    prospect on the old mislabelled page while the log said work was happening. Called incrementally."""
    try:
        import subprocess
        r = subprocess.run([sys.executable, str(ROOT / "tools" / "wire_mockups_to_dist.py")],
                           capture_output=True, text=True, timeout=1800)
        print(f"  published ({(r.stdout or r.stderr or '').strip().splitlines()[-1:] or ['ok']})", flush=True)
    except Exception as e:
        print(f"  PUBLISH FAILED: {e}", flush=True)


DEPLOY_STAMP = ROOT / "data" / "hustle" / ".last_mockup_deploy"


def _deploy_if_stale() -> None:
    """Publish the dist tree to hailports.com — the step nobody automated.

    `regen` rewrote LOCAL files; the public site is a separate Cloudflare Pages deploy that was manual and
    unscheduled, so every correction stayed invisible to the prospects who were already emailed the link.
    A pool company sat live as a beauty salon for hours because of exactly this gap.

    Fires only when the dist tree is newer than the last deploy, so the hourly no-op run does not redeploy.
    `CF_PAGES_TOKEN` is unset, but the existing `CF_API_TOKEN` carries Pages:Edit (verified against the CF
    API). deploy_hailports.sh hard-aborts on any cross-brand reference, so this cannot leak a brand.
    """
    import subprocess
    # Compare against LANDING, not dist: _publish() copies every file each run, so dist mtimes always look
    # new and the hourly no-op would redeploy 2440 pages forever. Landing only changes when a page is
    # actually rebuilt.
    try:
        newest = max((p.stat().st_mtime for p in LANDING.glob("*.html")), default=0)
    except Exception:
        return
    last = DEPLOY_STAMP.stat().st_mtime if DEPLOY_STAMP.exists() else 0
    if newest <= last:
        print("  deploy skipped — dist unchanged since last deploy", flush=True)
        return

    tok = ""
    try:
        for ln in (ROOT / ".env").read_text(errors="ignore").splitlines():
            if ln.startswith("CF_API_TOKEN="):
                tok = ln.split("=", 1)[1].strip().strip('"')
                break
    except Exception:
        pass
    if not tok:
        print("  DEPLOY SKIPPED: no CF_API_TOKEN", flush=True)
        return

    env = dict(os.environ, CF_PAGES_TOKEN=tok)
    print("  deploying to hailports.com ...", flush=True)
    try:
        # 3000s was too short for a full 1.3GB / 2451-page CF Pages upload, so the deploy timed out
        # EVERY run, the stamp never advanced, and the watchdog paged "rebuilt mockups undeployed
        # >24h" forever. Env-tunable, default 2h so the initial full upload can land (incrementals
        # after that are minutes).
        _dtmo = int(os.environ.get("MOCKUP_DEPLOY_TIMEOUT", "7200"))
        r = subprocess.run(["bash", str(ROOT / "scripts" / "deploy_hailports.sh")],
                           capture_output=True, text=True, timeout=_dtmo, env=env, cwd=str(ROOT))
    except Exception as e:
        print(f"  DEPLOY FAILED: {e}", flush=True)
        return
    tail = (r.stdout or r.stderr or "").strip().splitlines()[-1:] or [""]
    if r.returncode == 0:
        DEPLOY_STAMP.write_text(str(time.time()))
        print(f"  DEPLOYED ({tail[0][:80]})", flush=True)
    else:
        print(f"  DEPLOY FAILED rc={r.returncode}: {tail[0][:80]}", flush=True)


def _meta() -> dict:
    """domain -> {name, city} from the outreach queue (best effort; filenames are the source of truth)."""
    out: dict[str, dict] = {}
    try:
        for ln in QUEUE.read_text(errors="ignore").splitlines():
            if not ln.strip():
                continue
            try:
                d = json.loads(ln)
            except Exception:
                continue
            dom = (d.get("domain") or "").lower().strip()
            if dom and dom not in out:
                out[dom] = {"name": d.get("company") or "", "city": d.get("city") or ""}
    except Exception:
        pass
    return out


REBUILD_LOCK = ROOT / "data" / "runtime" / "mockup_rebuild.lock"


def main() -> int:
    # Shared with tools/refresh_mockup_copy.py: the slow bespoke-copy pass must never run against the same
    # pages as a correctness rebuild, or they overwrite each other's output.
    import fcntl
    REBUILD_LOCK.parent.mkdir(parents=True, exist_ok=True)
    _lk = REBUILD_LOCK.open("w")
    try:
        fcntl.flock(_lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("copy refresh in flight — rebuild yields", flush=True)
        return 0

    meta = _meta()
    domains = sorted(p.name[:-5] for p in MOCK_DIR.glob("*.html"))
    print(f"{len(domains)} mockups | {len(meta)} queue rows | dry={DRY}", flush=True)

    # RESUMABLE. A landing page newer than the RENDER EPOCH counts as current, so a restart skips finished
    # work and a crash costs one page, not the run.
    #
    # The epoch used to be site_generator.py's mtime, which meant ANY edit to that file — even a one-line
    # bugfix touching 43 pages — invalidated all 2441 and forced a full ~90-minute rebuild. That cost was
    # paid three times in one day. It is now an explicit stamp: bump it ONLY when a change should re-render
    # the whole set (`touch data/hustle/.render_epoch`), and rebuild affected pages directly otherwise.
    epoch = ROOT / "data" / "hustle" / ".render_epoch"
    if not epoch.exists():
        epoch.parent.mkdir(parents=True, exist_ok=True)
        epoch.write_text("render epoch — touch to force a full rebuild\n")
    code_mtime = epoch.stat().st_mtime

    verts, done, failed = Counter(), 0, 0
    t0 = time.time()
    jobs = []
    for dom in domains:
        m = meta.get(dom, {})
        name = m.get("name") or _name_from_existing(MOCK_DIR / f"{dom}.html") or _name_from_domain(dom)
        vert = _guess_vertical(dom, name, use_llm=False)
        verts[vert] += 1
        built = LANDING / f"{dom}.html"
        if not DRY and built.exists() and built.stat().st_mtime > code_mtime:
            continue                                   # already rebuilt with the current code
        jobs.append({"domain": dom, "name": name, "vertical": vert, "city": m.get("city", "")})

    if not DRY:
        print(f"{len(domains) - len(jobs)} already current, {len(jobs)} to rebuild", flush=True)
        if not jobs:
            _publish()
            _deploy_if_stale()          # drained: make sure what we built is actually LIVE
            print("backlog drained — nothing to do", flush=True)
            return 0

    if DRY:
        total = sum(verts.values())
        print("\nvertical distribution:", flush=True)
        for v, n in verts.most_common():
            print(f"  {n:5}  {n*100//total:3}%  {v}", flush=True)
        return 0

    # ~14s/page (hero cache miss + fallback backend); serial would be ~9h. 4 workers keeps the box usable.
    with ThreadPoolExecutor(max_workers=int(os.environ.get("REGEN_WORKERS", "4"))) as ex:
        futs = {ex.submit(generate_mockup, j): j["domain"] for j in jobs}
        for i, f in enumerate(as_completed(futs), 1):
            try:
                f.result()
                done += 1
            except Exception as e:
                failed += 1
                print(f"  FAIL {futs[f]}: {str(e)[:70]}", flush=True)
            if i % 50 == 0:
                rate = i / max(1e-9, time.time() - t0)
                print(f"  {i}/{len(jobs)} ok={done} fail={failed} {time.time()-t0:.0f}s "
                      f"eta={(len(jobs)-i)/max(rate,1e-9)/60:.0f}m", flush=True)
            # _publish() wire-copies the WHOLE 2440-file tree, so calling it every 200 pages spent more
            # wall-clock copying than rendering (rate fell 39/min -> 20/min). Every 800 still bounds a crash
            # to <800 pages of lost publish, and the final _publish() below always runs.
            if i % 800 == 0:
                _publish()

    print("\npublishing to dist...", flush=True)
    _publish()
    # Deploy only when the whole set is current — a half-rebuilt tree would ship a mix of old and new to
    # prospects. Incremental _publish() keeps dist safe locally; the public site flips once, coherently.
    remaining = [d for d in domains if not ((LANDING / f"{d}.html").exists()
                 and (LANDING / f"{d}.html").stat().st_mtime > code_mtime)]
    if remaining:
        print(f"  deploy deferred — {len(remaining)} pages still stale", flush=True)
    else:
        _deploy_if_stale()

    total = sum(verts.values())
    print("\nvertical distribution:", flush=True)
    for v, n in verts.most_common():
        print(f"  {n:5}  {n*100//total:3}%  {v}", flush=True)
    print(f"\nregenerated={done} failed={failed} elapsed={time.time()-t0:.0f}s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
