#!/usr/bin/env python3
"""autonomy_drill — prove the stack self-maintains WITHOUT Claude Code.

Simulates the Max sub being cancelled by making the `claude` CLI unavailable to the
launchd jobs (via the drill-bin shim honoring data/CLAUDE_OFF), then monitors what
breaks / self-heals / escalates over the window. The whole 28-day mission's acceptance
test is a clean 7-day run of this.

SAFE: only the launchd jobs (whose PATH is prepended with drill-bin) hit the shim.
Operator's interactive `claude` is never affected — drill-bin is not on his shell PATH.

    python3 tools/autonomy_drill.py start [--hours N]   # begin (default open-ended)
    python3 tools/autonomy_drill.py status              # is a drill active + live tally
    python3 tools/autonomy_drill.py report              # what broke / healed / escalated
    python3 tools/autonomy_drill.py stop                # end + final report

The break-list is assembled from signals that already exist: launchd nonzero-error
exits, new ALEX_ACTION_QUEUE escalations, alert_gateway pages, canary status, and the
blocked-claude-call log the shim writes.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FLAG = ROOT / "data" / "CLAUDE_OFF"
STATE = ROOT / "data" / "runtime" / "autonomy_drill.json"
BLOCKED_LOG = ROOT / "data" / "logs" / "claude_off_calls.log"
QUEUE = ROOT / "data" / "hustle" / "ALEX_ACTION_QUEUE.md"
CANARY = ROOT / "data" / "runtime" / "capability_canary.json"


def _now() -> float:
    return time.time()


def _read_json(p: Path, default=None):
    try:
        return json.loads(p.read_text())
    except Exception:
        return default if default is not None else {}


def _launchd_error_jobs() -> list[str]:
    """claude-stack jobs whose last exit is a POSITIVE nonzero (a real error, not a signal)."""
    out = []
    try:
        r = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=15)
        for line in r.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) >= 3 and "claude-stack" in parts[2]:
                try:
                    code = int(parts[1])
                except ValueError:
                    continue
                if code > 0:  # negative = killed by signal (restart), positive = real error
                    out.append(f"{parts[2]} (exit {code})")
    except Exception:
        pass
    return out


def _count_lines(p: Path) -> int:
    try:
        return sum(1 for _ in p.open())
    except Exception:
        return 0


def _snapshot() -> dict:
    return {
        "ts": _now(),
        "launchd_errors": _launchd_error_jobs(),
        "queue_lines": _count_lines(QUEUE),
        "blocked_calls": _count_lines(BLOCKED_LOG),
        "canary_ok": _read_json(CANARY, {}).get("overall_ok"),
    }


def start(hours: float | None) -> int:
    if FLAG.exists():
        print("drill already active — run `status` or `stop` first.")
        return 1
    STATE.parent.mkdir(parents=True, exist_ok=True)
    BLOCKED_LOG.parent.mkdir(parents=True, exist_ok=True)
    baseline = _snapshot()
    state = {
        "started": _now(),
        "started_iso": datetime.now().isoformat(timespec="seconds"),
        "planned_hours": hours,
        "baseline": baseline,
    }
    STATE.write_text(json.dumps(state, indent=2))
    FLAG.write_text(f"autonomy drill started {state['started_iso']}"
                    + (f" for {hours}h" if hours else " (open-ended)") + "\n")
    print(f"DRILL STARTED — claude CLI is now UNAVAILABLE to the launchd jobs"
          + (f" for {hours}h." if hours else " (open-ended — `stop` to end)."))
    print(f"baseline: {len(baseline['launchd_errors'])} jobs already erroring, "
          f"queue={baseline['queue_lines']} lines, canary_ok={baseline['canary_ok']}")
    print("your interactive `claude` is UNAFFECTED. run `report` anytime.")
    return 0


def _delta_report(state: dict) -> dict:
    base = state.get("baseline", {})
    cur = _snapshot()
    base_errs = set(base.get("launchd_errors", []))
    cur_errs = set(cur["launchd_errors"])
    return {
        "elapsed_h": round((cur["ts"] - state["started"]) / 3600, 2),
        "NEW_job_errors": sorted(cur_errs - base_errs),
        "cleared_job_errors": sorted(base_errs - cur_errs),
        "new_queue_escalations": cur["queue_lines"] - base.get("queue_lines", 0),
        "claude_calls_blocked": cur["blocked_calls"] - base.get("blocked_calls", 0),
        "canary_ok_now": cur["canary_ok"],
        "canary_ok_baseline": base.get("canary_ok"),
    }


def status() -> int:
    if not FLAG.exists():
        print("no drill active.")
        return 0
    state = _read_json(STATE)
    d = _delta_report(state)
    print(f"DRILL ACTIVE — {d['elapsed_h']}h elapsed (started {state.get('started_iso')})")
    print(f"  claude calls blocked: {d['claude_calls_blocked']}")
    print(f"  NEW job errors since start: {len(d['NEW_job_errors'])}")
    print(f"  new escalations to queue: {d['new_queue_escalations']}")
    print(f"  canary: {d['canary_ok_baseline']} -> {d['canary_ok_now']}")
    return 0


def report() -> int:
    state = _read_json(STATE)
    if not state:
        print("no drill state found.")
        return 1
    d = _delta_report(state)
    active = FLAG.exists()
    print(f"=== AUTONOMY DRILL REPORT ({'ACTIVE' if active else 'ENDED'}) — {d['elapsed_h']}h ===")
    print(f"claude calls blocked (jobs that needed it): {d['claude_calls_blocked']}")
    print(f"\nNEW job errors that appeared (= NOT self-healing, FIX THESE):")
    for j in d["NEW_job_errors"] or ["  (none — nothing broke that wasn't already broken)"]:
        print(f"  - {j}")
    print(f"\njob errors that CLEARED during the drill (self-healed): {len(d['cleared_job_errors'])}")
    print(f"escalations staged to ALEX_ACTION_QUEUE: {d['new_queue_escalations']}")
    print(f"canary overall_ok: baseline={d['canary_ok_baseline']} now={d['canary_ok_now']}")
    verdict = "PASS (nothing new broke)" if not d["NEW_job_errors"] and d["canary_ok_now"] else "GAPS FOUND — see NEW job errors above"
    print(f"\nVERDICT: {verdict}")
    return 0


def stop() -> int:
    if not FLAG.exists():
        print("no drill active.")
        return 0
    report()
    FLAG.unlink(missing_ok=True)
    print("\nDRILL STOPPED — claude CLI restored to the jobs.")
    return 0


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 2
    cmd = args[0]
    if cmd == "start":
        hours = None
        if "--hours" in args:
            try:
                hours = float(args[args.index("--hours") + 1])
            except Exception:
                hours = None
        return start(hours)
    if cmd == "status":
        return status()
    if cmd == "report":
        return report()
    if cmd == "stop":
        return stop()
    print(f"unknown command: {cmd}\n{__doc__}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
