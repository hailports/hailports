"""Stage a 'site forming' reveal short into the hailports mirror distributor.

The subject is a DUMMY/fictional business (generated name + local stock photos), never a real
prospect — we have no customer testimonials authorized to share. Research/social-proof caption.
Staged + ledger-registered in the SAME format as hailports_short_distributor.generate_one, so the
distributor's fan-out posts it to YT/TikTok/X like any other short (scrub-gated, dry-run default).

  python3 tools/stage_reveal_short.py [N]   # stage N reveal shorts (default 2)
"""
import json, os, sys, time, tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("SITE_GEN_NO_LLM", "1")
from core.site_generator import generate_site
from agents import hailports_short_distributor as dist
from agents import mockup_reveal_video as rev

# fictional businesses (never a real prospect); each maps to a bank vertical for stock photos
DUMMIES = [
    ("plumber", "Northside Plumbing", "northside-plumbing.example"),
    ("electrician", "Bright Spark Electric", "brightspark-electric.example"),
    ("hvac", "Comfort Air Co.", "comfortair-co.example"),
    ("roofing", "Summit Roofing", "summit-roofing.example"),
    ("landscaping", "Green Acre Lawn", "greenacre-lawn.example"),
    ("dentist", "Bluebonnet Dental", "bluebonnet-dental.example"),
]

N = int(sys.argv[1]) if len(sys.argv) > 1 else 2


def stage_one(i: int) -> dict:
    vert, name, dom = DUMMIES[i % len(DUMMIES)]
    html = generate_site({"domain": dom, "vertical": vert, "name": name, "city": "Austin, TX"})
    mock = Path(tempfile.mkdtemp(prefix="reveal_")) / "dummy.html"
    mock.write_text(html, encoding="utf-8")

    base = time.strftime("%Y%m%d_%H%M%S"); ts, k = base, 0
    while True:
        run = dist.QUEUE_DIR / ts
        try:
            run.mkdir(parents=True, exist_ok=False); break
        except FileExistsError:
            k += 1; ts = f"{base}_{k}"
    vid = run / "video.mp4"
    rev.make(str(mock), str(vid), variant=i, trade=vert, count=300 + i * 7)
    if not vid.exists():
        return {"ok": False, "reason": "render failed"}

    hook, _end = rev.pick_caption(i, vert, 300 + i * 7)
    title = f"{hook} (a {vert}, rebuilt live)"[:90]
    desc = (f"part of our local web-presence research — a {vert}'s site rebuilt clean, one of hundreds.\n\n"
            "→ free scan + rebuild preview of YOUR site: https://hailports.com/?utm_source=youtube&utm_medium=short&utm_campaign=reveal\n"
            "build log: https://x.com/hailports\n\n#shorts #smallbusiness #websitedesign #localbusiness #beforeandafter")
    (run / "metadata.json").write_text(json.dumps({"title": title, "description": desc,
        "tags": ["website", "small business", "web design", "local business", "before and after",
                 "website makeover", "shorts"]}, indent=2))
    dist._append_upload_queue({"channel": "hailports", "video": str(vid), "fingerprint": ts,
                               "status": "ready_for_upload", "uploaded": False})
    L = dist.load_ledger()
    L["shorts"].append({
        "fp": ts, "video": str(vid), "title": title,
        "created": datetime.now(timezone.utc).isoformat(), "generated": True, "kind": "reveal",
        "channels": {"youtube": dist._new_chan(), "tiktok": dist._new_chan(), "x": dist._new_chan()},
    })
    L["shorts"].sort(key=lambda s: s["fp"])
    dist.save_ledger(L)
    return {"ok": True, "fp": ts, "title": title, "video": str(vid)}


for i in range(N):
    r = stage_one(i)
    print(("  ✓ " if r["ok"] else "  ✗ ") + (r.get("title") or r.get("reason", "")))
print("staged. run the distributor to fan out (dry-run default; HAILPORTS_MIRROR_SEND=1 to publish).")
