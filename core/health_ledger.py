#!/usr/bin/env python3
"""health_ledger.py — the stack's shared health MEMORY + the learn loop.

The missing keystone of self-improvement. The diagnostician produces a fresh
snapshot every cycle (data/diagnostic_report.json) and then throws the per-service
detail away — diagnostic.jsonl keeps only aggregate counts. So nothing could ever
learn that a specific service fails *repeatedly*: every healer acted blind and
forgot. This module fixes that.

Three jobs:
  1. record_cycle()  — append each cycle's detailed findings to an append-only
                       ledger (idempotent per diagnostic cycle).
  2. record_heal()   — healers/watchdogs log every heal attempt + whether the fix
                       VERIFIED, into the same ledger. Lets us learn which fixes hold.
  3. chronic()       — read the ledger over a window and surface failures that
                       recur across N distinct cycles = root-cause bugs, not blips.
                       write_chronic_signal() hands those to the self-improvement
                       planner as high-priority "fix the root cause" evidence.

Dependency-free, defensive, cheap. Run: python3 -m core.health_ledger
"""
from __future__ import annotations
import json
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
LOG_DIR = DATA_DIR / "logs"
RUNTIME_DIR = DATA_DIR / "runtime"
REPORT = DATA_DIR / "diagnostic_report.json"
LEDGER = LOG_DIR / "health_findings.jsonl"
STATE = DATA_DIR / "health_ledger_state.json"
CHRONIC_OUT = RUNTIME_DIR / "chronic_failures.json"

for _d in (LOG_DIR, RUNTIME_DIR):
    _d.mkdir(parents=True, exist_ok=True)

LEVEL_RANK = {"info": 0, "warn": 1, "warning": 1, "critical": 2, "crit": 2}


def _load_json(path: Path, fallback):
    try:
        return json.loads(path.read_text())
    except Exception:
        return fallback


def _append(rows: list[dict]) -> None:
    if not rows:
        return
    with open(LEDGER, "a") as f:
        for r in rows:
            f.write(json.dumps(r, default=str) + "\n")


def record_cycle() -> int:
    """Snapshot the latest diagnostic findings into the ledger, once per cycle.
    Idempotent: a given diagnostic `as_of` is recorded at most once. Returns the
    number of finding rows appended."""
    report = _load_json(REPORT, {})
    findings = report.get("findings") or []
    as_of = str(report.get("as_of") or report.get("as_of_iso") or "")
    if not as_of:
        return 0
    state = _load_json(STATE, {})
    if state.get("last_as_of") == as_of:
        return 0  # already recorded this cycle
    ts = time.time()
    rows = []
    for fd in findings:
        if not isinstance(fd, dict):
            continue
        rows.append({
            "kind": "finding",
            "ts": ts,
            "as_of": as_of,
            "level": str(fd.get("level", "info")).lower(),
            "signature": str(fd.get("signature", "") or fd.get("message", ""))[:200],
            "service": str(fd.get("service", "") or "None"),
        })
    _append(rows)
    state["last_as_of"] = as_of
    state["last_record_ts"] = ts
    STATE.write_text(json.dumps(state))
    return len(rows)


def record_heal(service: str, signature: str, action: str, verified: bool | None,
                detail: str = "") -> None:
    """Any healer/watchdog calls this after attempting a fix. `verified` is True if
    the fix was programmatically confirmed to hold, False if it didn't, None if not
    yet checked. This is how the stack learns which fixes actually work."""
    _append([{
        "kind": "heal",
        "ts": time.time(),
        "service": str(service or "")[:120],
        "signature": str(signature or "")[:200],
        "action": str(action or "")[:200],
        "verified": verified,
        "detail": str(detail or "")[:300],
    }])


def _read_ledger(window_hours: float):
    cutoff = time.time() - window_hours * 3600
    rows = []
    if not LEDGER.exists():
        return rows
    for line in LEDGER.read_text().splitlines():
        try:
            r = json.loads(line)
        except Exception:
            continue
        if float(r.get("ts", 0)) >= cutoff:
            rows.append(r)
    return rows


def chronic(window_hours: float = 168.0, min_cycles: int = 3, min_level: str = "warn"):
    """Failures that recur across >= min_cycles DISTINCT diagnostic cycles in the
    window. Distinct-cycle counting (not raw rows) is what separates a genuinely
    chronic root-cause bug from one noisy cycle. Returns a sorted list of dicts."""
    floor = LEVEL_RANK.get(min_level, 1)
    rows = [r for r in _read_ledger(window_hours) if r.get("kind") == "finding"]
    groups: dict[tuple, dict] = {}
    for r in rows:
        if LEVEL_RANK.get(r.get("level", "info"), 0) < floor:
            continue
        key = (r.get("service", "None"), r.get("signature", ""))
        g = groups.setdefault(key, {"service": key[0], "signature": key[1],
                                    "cycles": set(), "level": r.get("level"),
                                    "first_ts": r["ts"], "last_ts": r["ts"]})
        g["cycles"].add(r.get("as_of"))
        g["first_ts"] = min(g["first_ts"], r["ts"])
        g["last_ts"] = max(g["last_ts"], r["ts"])
        if LEVEL_RANK.get(r.get("level"), 0) >= LEVEL_RANK.get(g["level"], 0):
            g["level"] = r.get("level")
    out = []
    for g in groups.values():
        n = len(g["cycles"])
        if n >= min_cycles:
            out.append({"service": g["service"], "signature": g["signature"],
                        "level": g["level"], "recurrence_cycles": n,
                        "first_seen_ts": g["first_ts"], "last_seen_ts": g["last_ts"]})
    # heal effectiveness: signatures we keep "fixing" that keep coming back = the
    # loudest signal that the fix is a band-aid, not a root-cause fix.
    heal_counts: dict[str, int] = {}
    for r in _read_ledger(window_hours):
        if r.get("kind") == "heal":
            heal_counts[r.get("signature", "")] = heal_counts.get(r.get("signature", ""), 0) + 1
    for o in out:
        o["heal_attempts"] = heal_counts.get(o["signature"], 0)
        o["band_aid"] = o["heal_attempts"] >= 2 and o["recurrence_cycles"] >= min_cycles
    out.sort(key=lambda o: (LEVEL_RANK.get(o["level"], 0), o["recurrence_cycles"],
                            o["heal_attempts"]), reverse=True)
    return out


def write_chronic_signal(window_hours: float = 168.0, min_cycles: int = 3) -> dict:
    """Persist the chronic list where self_improvement_planner.gather_evidence() can
    read it. This is the handoff from 'we keep breaking' to 'fix the root cause'."""
    items = chronic(window_hours=window_hours, min_cycles=min_cycles)
    payload = {
        "generated_ts": time.time(),
        "generated_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "window_hours": window_hours,
        "min_cycles": min_cycles,
        "chronic_count": len(items),
        "chronic": items[:50],
    }
    CHRONIC_OUT.write_text(json.dumps(payload, indent=2, default=str))
    return payload


if __name__ == "__main__":
    recorded = record_cycle()
    sig = write_chronic_signal()
    chronic_items = sig["chronic"]
    print(f"[health_ledger] recorded {recorded} finding rows this cycle")
    print(f"[health_ledger] chronic failures (>= {sig['min_cycles']} cycles / {int(sig['window_hours'])}h): {sig['chronic_count']}")
    for c in chronic_items[:10]:
        flag = " 🩹BAND-AID" if c.get("band_aid") else ""
        print(f"  [{c['level']}] {c['service']} :: {c['signature'][:70]} "
              f"(x{c['recurrence_cycles']} cycles, {c['heal_attempts']} heals){flag}")
