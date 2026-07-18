"""Controlled first-wave prep for the hailport re-arm: regenerate fresh, GATED mockups for
firewall-cleared REAL local businesses and wire them into the hailports deploy tree. Nothing
sends here — this only guarantees that when the clamped sender fires, every proof link is a
beautiful, gate-passed page on hailports.com (never a stale void).

Run in small CHUNKS (fresh process each) so the mini's ram_warden doesn't cull the Playwright
gate mid-batch; progress is tracked in a done-file so chunks advance through the queue:

  python3 tools/rearm_regen_hailport.py 5     # do the next 5 not-yet-done
"""
import json, os, re, sys, shutil, time
from pathlib import Path

os.environ.setdefault("SITE_GEN_NO_LLM", "1")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from core import brand_dedup as bd
from core.site_generator import generate_mockup

N = int(sys.argv[1]) if len(sys.argv) > 1 else 5
DEST = ROOT / "data" / "hustle" / "hailports_dist" / "mockups"
DEST.mkdir(parents=True, exist_ok=True)
DONE_FILE = ROOT / "data" / "hustle" / "rearm_done.txt"
done = set(DONE_FILE.read_text().split()) if DONE_FILE.exists() else set()

# skip anything that isn't a real local-business site: SaaS/demo/host-panel subdomains, IPs.
_SKIP = re.compile(r"(?:^|\.)(3cx|myshopify|wixsite|weebly|blogspot|wordpress\.com|github\.io|"
                   r"pages\.dev|netlify\.app|herokuapp|firebaseapp|web\.app|sharepoint|"
                   r"godaddysites|square\.site|godaddy)\b|^\d{1,3}(\.\d{1,3}){3}$", re.I)

def log(m): print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)

rows = [json.loads(l) for l in (ROOT / "data/hustle/broken_site_outreach_queue.jsonl").read_text().splitlines() if l.strip()]
ok = fail = 0
for p in rows:
    if ok >= N:
        break
    dom = (p.get("domain") or "").strip().lower()
    email = (p.get("candidate_recipient") or p.get("contact_email") or "").strip()
    if not dom or not email or dom in done:
        continue
    if _SKIP.search(dom) or dom.count(".") > 2:   # deep subdomain / SaaS host = not a local biz
        done.add(dom); continue
    if not bd.may_contact(email, "hailport", domain=dom).get("allowed"):
        continue
    try:
        r = generate_mockup({"domain": dom, "vertical": p.get("vertical"), "name": p.get("company"),
                             "city": p.get("city"), "contact_phone": p.get("contact_phone"),
                             "candidate_recipient": email})
    except Exception as e:
        log(f"FAIL {dom}: {type(e).__name__}"); done.add(dom); continue
    done.add(dom)   # attempted -> don't retry (gate-fails stay excluded)
    if not r.get("valid"):
        fail += 1; log(f"gate-blocked {dom}: {r.get('gate_reasons')}"); continue
    shutil.copy(Path(r["path"]), DEST / Path(r["path"]).name)
    ok += 1
    log(f"OK {dom} -> {r.get('name')!r}")

DONE_FILE.write_text(" ".join(sorted(done)))
wired = len(list(DEST.glob("*.html")))
log(f"chunk done: +{ok} gated+wired, {fail} blocked | dest now {wired} mockups | processed {len(done)}")
