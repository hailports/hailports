#!/usr/bin/env python3
"""heal_verify.py — the VERIFY edge of self-healing, as one reusable primitive.

A heal that isn't verified is a guess. Today only the engage monitor re-checks its
own fix; every other healer fires-and-forgets, so "fixed" is a hope, not a fact.
This module gives the whole stack ONE deterministic answer to "is this launchd job
actually healthy right now?" — so healers can confirm a fix held (and log the truth
to health_ledger) instead of claiming success.

Deterministic, no LLM, cheap. Pairs with core.health_ledger.record_heal:

    from core.heal_verify import verify_heal
    from core.health_ledger import record_heal
    ok = verify_heal("com.imma.engage", settle_seconds=8,
                     log_path="~/.openclaw/workspace/android-logs/browser_engage.log",
                     log_fresh_s=600)
    record_heal("com.imma.engage", signature, action="kickstart", verified=ok)

Run:  python3 -m core.heal_verify com.imma.engage --log <path> --fresh 600
"""
from __future__ import annotations
import os
import re
import subprocess
import time
from pathlib import Path

# Exit codes that are NOT a failure: 0 = clean, negative = killed by signal
# (e.g. -15 SIGTERM is launchd stopping a periodic job normally, -9 SIGKILL on
# shutdown). A POSITIVE exit code is a real error worth treating as unhealthy.
def _uid() -> int:
    return os.getuid()


def _launchctl_print(label: str) -> str:
    try:
        r = subprocess.run(["/bin/launchctl", "print", f"gui/{_uid()}/{label}"],
                           capture_output=True, text=True, timeout=12)
        return r.stdout
    except Exception:
        return ""


def _grab(pattern: str, text: str):
    m = re.search(pattern, text)
    return m.group(1) if m else None


def job_health(label: str, *, log_path: str | None = None,
               log_fresh_s: float | None = None) -> dict:
    """Deterministic verdict on a launchd job. healthy=True means EITHER actively
    running with no error exit, OR cleanly idle between StartInterval fires (clean
    last exit). If log_path+log_fresh_s are given, also require the job's log to
    have been written within that window — proof it's actually DOING work, not just
    loaded (this is what catches the engage '12h silent death' class of bug)."""
    text = _launchctl_print(label)
    if not text:
        return {"label": label, "healthy": False, "reason": "not_loaded",
                "state": None, "runs": None, "last_exit": None, "pid": None}

    state = _grab(r"state = (\S+)", text)
    runs = _grab(r"runs = (\d+)", text)
    last_exit_raw = _grab(r"last exit code = (-?\d+)", text)
    pid = _grab(r"pid = (\d+)", text)
    last_exit = int(last_exit_raw) if last_exit_raw is not None else None

    reasons = []
    healthy = True

    # A positive last-exit code = real error.
    if last_exit is not None and last_exit > 0:
        healthy = False
        reasons.append(f"error_exit={last_exit}")

    # Must be either running or cleanly idle (a not-running job with a clean/last
    # exit is normal between interval fires; only flag if it also errored).
    running = (state == "running") or (pid is not None)
    if not running and last_exit is not None and last_exit > 0:
        healthy = False
        reasons.append("down_after_error")

    # Liveness via log freshness — the real proof of work.
    if log_path and log_fresh_s is not None:
        p = Path(os.path.expanduser(log_path))
        if not p.exists():
            healthy = False
            reasons.append("log_missing")
        else:
            age = time.time() - p.stat().st_mtime
            if age > log_fresh_s:
                healthy = False
                reasons.append(f"log_stale={int(age)}s>{int(log_fresh_s)}s")

    if healthy and not reasons:
        reasons.append("running" if running else "idle_clean")

    return {"label": label, "healthy": healthy, "reason": ",".join(reasons),
            "state": state, "runs": int(runs) if runs else None,
            "last_exit": last_exit, "pid": int(pid) if pid else None}


def verify_heal(label: str, *, settle_seconds: float = 0.0,
                log_path: str | None = None, log_fresh_s: float | None = None) -> bool:
    """Call AFTER attempting a fix. Waits `settle_seconds` for the job to come back,
    then returns the deterministic health verdict. Healers pass this straight into
    health_ledger.record_heal(verified=...) so the stack learns which fixes hold."""
    if settle_seconds > 0:
        time.sleep(min(settle_seconds, 120))
    return job_health(label, log_path=log_path, log_fresh_s=log_fresh_s)["healthy"]


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("label")
    ap.add_argument("--log", default=None)
    ap.add_argument("--fresh", type=float, default=None)
    ap.add_argument("--settle", type=float, default=0.0)
    a = ap.parse_args()
    if a.settle > 0:
        time.sleep(min(a.settle, 120))
    v = job_health(a.label, log_path=a.log, log_fresh_s=a.fresh)
    mark = "✅ HEALTHY" if v["healthy"] else "❌ UNHEALTHY"
    print(f"{mark}  {v['label']}  [{v['reason']}]  "
          f"state={v['state']} runs={v['runs']} last_exit={v['last_exit']} pid={v['pid']}")
    raise SystemExit(0 if v["healthy"] else 1)
