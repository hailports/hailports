#!/usr/bin/env python3
"""refresh_mockup_copy.py — slowly upgrade mockup COPY from the variant banks to bespoke local-LLM copy.

The 2026-07-09 bulk rebuild ran with SITE_GEN_NO_LLM=1: 3.9s/page instead of 49.7s/page (12.7x), which is
what let 2,440 mislabelled pages be corrected in ~90 minutes instead of ~8 hours. Correctness first — a pool
company was live as a beauty salon. The cost is generic headlines ("Careful work, fair pricing") instead of
bespoke ones.

This walks the same set at low priority and regenerates each page WITH the LLM, one domain at a time, keeping
its own ledger so it resumes across runs. The classification is unchanged (same detector), so a page can only
get better copy, never a worse label.

Never fights the rebuild: takes an exclusive lock that `regen_mockups` respects, and refuses to run while a
rebuild is in flight. Time-boxed per run so it can never hold the box.

  python3 tools/refresh_mockup_copy.py --status
  python3 tools/refresh_mockup_copy.py            # one bounded slice
"""
from __future__ import annotations
import fcntl
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(os.path.expanduser("~/claude-stack"))
sys.path.insert(0, str(ROOT))
os.environ.setdefault("SITE_GEN_AI_IMAGERY", "0")   # imagery is a separate concern; never the copy pass
os.environ.pop("SITE_GEN_NO_LLM", None)             # the whole point: bespoke copy

from core.site_generator import generate_mockup, _guess_vertical, _name_from_domain  # noqa: E402
from tools.regen_mockups import _name_from_existing, _publish, _deploy_if_stale, MOCK_DIR, LANDING  # noqa: E402

LEDGER = ROOT / "data" / "hustle" / "mockup_llm_copy.json"
LOCK = ROOT / "data" / "runtime" / "mockup_rebuild.lock"
RUN_SECONDS = int(os.environ.get("REFRESH_RUN_SECONDS", "2400"))   # 40 min slice
WORKERS = int(os.environ.get("REFRESH_WORKERS", "2"))              # Ollama serialises; more just queues


def _ledger() -> dict:
    try:
        return json.loads(LEDGER.read_text())
    except Exception:
        return {}


def _save(led: dict) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    tmp = LEDGER.with_suffix(".tmp")
    tmp.write_text(json.dumps(led))
    tmp.replace(LEDGER)


def _pending() -> list[str]:
    epoch = ROOT / "data" / "hustle" / ".render_epoch"          # same key as regen_mockups
    code_mtime = epoch.stat().st_mtime if epoch.exists() else 0
    led = _ledger()
    out = []
    for p in sorted(MOCK_DIR.glob("*.html")):
        dom = p.name[:-5]
        built = LANDING / f"{dom}.html"
        if not built.exists() or built.stat().st_mtime <= code_mtime:
            continue                      # still awaiting the correctness rebuild; not ours yet
        if float(led.get(dom, 0)) > code_mtime:
            continue                      # already has bespoke copy under the current code
        out.append(dom)
    return out


def main() -> int:
    if "--status" in sys.argv:
        led, pend = _ledger(), _pending()
        print(f"bespoke copy: {len(led)} pages | pending: {len(pend)}")
        return 0

    # Belt as well as the flock: a rebuild started before the lock existed (or from a shell) holds nothing,
    # and both processes write the same files. Yield to any live rebuild, whatever started it.
    import subprocess
    if subprocess.run(["pgrep", "-f", "regen_mockups.py"], capture_output=True).returncode == 0:
        print("rebuild process running — refresh yields", flush=True)
        return 0

    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with LOCK.open("w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("rebuild in flight — refresh yields", flush=True)
            return 0

        pending = _pending()
        print(f"{len(pending)} pages awaiting bespoke copy (slice {RUN_SECONDS}s, {WORKERS} workers)", flush=True)
        if not pending:
            _publish()
            _deploy_if_stale()
            print("copy refresh complete", flush=True)
            return 0

        led = _ledger()
        deadline = time.time() + RUN_SECONDS
        done = failed = 0

        def one(dom: str):
            name = _name_from_existing(MOCK_DIR / f"{dom}.html") or _name_from_domain(dom)
            vert = _guess_vertical(dom, name, use_llm=False)
            generate_mockup({"domain": dom, "name": name, "vertical": vert, "city": ""})
            return dom

        for i in range(0, len(pending), WORKERS * 4):
            if time.time() >= deadline:
                print("  slice budget spent", flush=True)
                break
            batch = pending[i:i + WORKERS * 4]
            with ThreadPoolExecutor(max_workers=WORKERS) as ex:
                futs = {ex.submit(one, d): d for d in batch}
                for f in as_completed(futs):
                    dom = futs[f]
                    try:
                        f.result()
                        led[dom] = time.time()
                        done += 1
                    except Exception as e:
                        failed += 1
                        print(f"  FAIL {dom}: {str(e)[:60]}", flush=True)
            _save(led)
            if done and done % 100 == 0:
                print(f"  {done}/{len(pending)} refreshed", flush=True)

        _save(led)
        _publish()
        # Deploy EVERY slice, not just on drain. The whole tree is already correct (the rebuild fixed the
        # labels); a slice only improves copy, so shipping partway is coherent. Deferring for the ~3 days
        # this takes would also trip the watchdog's 24h "rebuilt mockups undeployed" alert every day.
        _deploy_if_stale()
        print(f"refreshed={done} failed={failed} remaining={len(_pending())}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
