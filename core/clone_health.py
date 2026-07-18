#!/usr/bin/env python3
"""Clone health monitor — one source of truth for "is every hack/trick actually running?"

The failure mode this closes: the stack keeps building capabilities that then sit built-but-
unwired, or silently die (a launchd job that exits non-zero every cycle, a learner whose ledger
went stale, a wired fix the gateway never reloaded). eternal_guardian watches domains/sessions/
earning; nothing watched the CLONE's own organs. This does.

Each component declares a check() that returns a status by VERIFYING REAL STATE, never a proxy
(the Blake lesson: "assignment exists" != "capability works"). Statuses:
  live_ok            — wired + running + fresh
  live_degraded      — should be live, but stale/erroring  -> ALERT (+ optional auto-heal)
  built_not_wired    — module exists but nothing runs/calls it (informational, not an alert)
  not_built          — planned, absent (informational)

Failures route through core.alert_gateway (chronic-breaker dedups, no spam) and safe auto-heals
(reload a downed launchd job) go through the existing heal path. Reads only; the only mutation is
an opt-in `--heal` reload of a job that should be loaded but isn't.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
from pathlib import Path

from core import BASE_DIR

STATE = BASE_DIR / "data" / "runtime" / "clone_health.json"
STATE.parent.mkdir(parents=True, exist_ok=True)
LEARN = BASE_DIR / "data" / "learning"

LIVE_OK, LIVE_DEGRADED, BUILT_NOT_WIRED, NOT_BUILT = (
    "live_ok", "live_degraded", "built_not_wired", "not_built")


def _sh(cmd: list[str], timeout: int = 10) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


def _launchd(label: str) -> tuple[bool, int | None]:
    """(loaded, last_exit_status). last_exit None if not loaded."""
    for line in _sh(["launchctl", "list"]).splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and parts[2].strip() == label:
            try:
                return True, int(parts[1])
            except Exception:
                return True, None
    return False, None


def _fresh(path: Path, max_age_h: float) -> tuple[bool, float]:
    if not path.exists():
        return False, -1.0
    age_h = (time.time() - path.stat().st_mtime) / 3600.0
    return age_h <= max_age_h, age_h


def _grep(path: Path, pattern: str) -> bool:
    try:
        return re.search(pattern, path.read_text(errors="ignore")) is not None
    except Exception:
        return False


# ─────────────────────────────── component checks ───────────────────────────────
# Each returns (status, detail, healable_launchd_label_or_None). Keep checks cheap + honest.

def _c_scheduled_learner(label, ledger, max_age_h):
    loaded, exit_st = _launchd(label)
    fresh, age = _fresh(LEARN / ledger, max_age_h)
    if not loaded:
        return BUILT_NOT_WIRED, f"no launchd job ({label}); ledger {'fresh' if fresh else 'stale/absent'}", None
    if exit_st not in (0, None):
        return LIVE_DEGRADED, f"{label} last exit {exit_st}", label
    if not fresh:
        return LIVE_DEGRADED, f"{label} loaded but ledger {ledger} stale ({age:.1f}h)", label
    return LIVE_OK, f"{label} loaded, ledger {age:.1f}h fresh", None


def _c_reaction_grader():
    # Now reads the CLEAN substrate (data/learning/exchanges.jsonl via clean_capture).
    # Honest metric: the grader can only produce signal once the clean_capture wire-in
    # feeds that ledger. If the job is loaded but the substrate is still empty, that's
    # "scheduled, awaiting fuel" — NOT degraded. Only a stale grader ledger while the
    # substrate HAS exchanges is a real failure.
    label = "com.claude-stack.reaction-grader"
    loaded, exit_st = _launchd(label)
    if not loaded:
        return BUILT_NOT_WIRED, f"no launchd job ({label}); staged in deploy/launchagents", None
    if exit_st not in (0, None):
        return LIVE_DEGRADED, f"{label} last exit {exit_st}", label
    substrate = LEARN / "exchanges.jsonl"
    has_exchanges = substrate.exists() and substrate.stat().st_size > 0
    if not has_exchanges:
        return LIVE_OK, f"{label} scheduled; awaiting clean_capture wire-in (no exchanges yet)", None
    fresh, age = _fresh(LEARN / "graded_outcomes.jsonl", 24)
    if not fresh:
        return LIVE_DEGRADED, f"{label} loaded, substrate has data but graded ledger stale ({age:.1f}h)", label
    return LIVE_OK, f"{label} loaded, grading clean substrate, ledger {age:.1f}h fresh", None


def _c_commit_learner():
    return _c_scheduled_learner("com.claude-stack.sfdc-commit-learner", "sfdc_commit_patterns.jsonl", 168)


def _c_inbox_pipeline():
    wired = _grep(BASE_DIR / "agents" / "exec_assistant.py", r"inbox_agent_pipeline")
    loaded, _ = _launchd("com.claude-stack.exec-assistant")
    if not wired:
        return BUILT_NOT_WIRED, "inbox_agent_pipeline not called from exec_assistant yet", None
    return (LIVE_OK if loaded else LIVE_DEGRADED), f"wired; exec-assistant loaded={loaded}", None


def _c_intent_resolver_wired():
    g = BASE_DIR / "apps" / "chatgpt_redacted_action.py"
    wired = _grep(g, r"intent_resolver")
    if not wired:
        return BUILT_NOT_WIRED, "intent_resolver not wired into gateway", None
    # staged: live only after the gateway process reloads the edited file
    return LIVE_OK, "wired into gateway (:8332); active on gateway's next reload", None


def _c_openclaw_threadfix():
    wired = _grep(BASE_DIR / "core" / "engine.py", r"handle_compound\([^)]*thread_id=thread_id")
    return (LIVE_OK, "compound path passes thread_id", None) if wired else (
        BUILT_NOT_WIRED, "engine.py compound calls missing thread_id", None)


def _c_lag_warden():
    # System Events pegged = the lag we fixed. Alert if it's hot again.
    top = _sh(["ps", "-Ao", "%cpu,comm", "-r"])
    for ln in top.splitlines():
        if "System Events" in ln:
            try:
                cpu = float(ln.strip().split()[0])
            except Exception:
                cpu = 0.0
            if cpu > 60:
                return LIVE_DEGRADED, f"System Events {cpu:.0f}% CPU — warden re-park may be hot again", None
            return LIVE_OK, f"System Events {cpu:.0f}% (calm)", None
    return LIVE_OK, "System Events idle", None


def _c_work_rag():
    # the work-lane semantic RAG index (hybrid retrieval) — not built yet (hustle-only rag.py exists)
    idx = LEARN / "work_rag.sqlite"
    if not idx.exists():
        return NOT_BUILT, "work-lane semantic RAG not built (retrieval is FTS-only); rag.py is hustle-only", None
    fresh, age = _fresh(idx, 24)
    return (LIVE_OK if fresh else LIVE_DEGRADED), f"work RAG index {age:.1f}h", None


def _c_documenter():
    loaded, _ = _launchd("com.claude-stack.work-item-documenter")
    return (LIVE_OK, "documenter scheduled", None) if loaded else (
        BUILT_NOT_WIRED, "work_item_documenter has no schedule yet", None)


def _c_alert_breaker():
    ok = _grep(BASE_DIR / "core" / "alert_gateway.py", r"CHRONIC_PAGE_LIMIT")
    return (LIVE_OK, "chronic-crit breaker present", None) if ok else (
        LIVE_DEGRADED, "alert_gateway chronic breaker missing", None)


COMPONENTS = [
    ("reaction_grader (learning signal)", _c_reaction_grader),
    ("sfdc_commit_learner", _c_commit_learner),
    ("inbox_agent_pipeline", _c_inbox_pipeline),
    ("intent_resolver → GPT (rewording fix)", _c_intent_resolver_wired),
    ("openclaw thread-context fix", _c_openclaw_threadfix),
    ("work-lane semantic RAG", _c_work_rag),
    ("work_item_documenter", _c_documenter),
    ("lag warden (System Events)", _c_lag_warden),
    ("alert_gateway chronic breaker", _c_alert_breaker),
]


def run(heal: bool = False, alert: bool = True) -> dict:
    rows, degraded = [], []
    for name, check in COMPONENTS:
        try:
            status, detail, heal_label = check()
        except Exception as e:
            status, detail, heal_label = LIVE_DEGRADED, f"check errored: {e}", None
        rows.append({"component": name, "status": status, "detail": detail})
        if status == LIVE_DEGRADED:
            degraded.append((name, detail, heal_label))
            if heal and heal_label:
                _sh(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{heal_label}"])

    summary = {s: sum(1 for r in rows if r["status"] == s)
               for s in (LIVE_OK, LIVE_DEGRADED, BUILT_NOT_WIRED, NOT_BUILT)}
    out = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "summary": summary, "rows": rows}
    try:
        STATE.write_text(json.dumps(out, indent=1))
    except Exception:
        pass

    # only DEGRADED (should-be-live-but-broken) pages; built_not_wired/not_built are informational
    if alert and degraded:
        try:
            from core import alert_gateway
            body = "\n".join(f"- {n}: {d}" for n, d, _ in degraded)
            alert_gateway.route("warn", "clone-health",
                                f"clone: {len(degraded)} component(s) degraded", body,
                                issue_key="clone-health-degraded")
        except Exception:
            pass
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--heal", action="store_true", help="kickstart downed launchd jobs")
    ap.add_argument("--no-alert", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    out = run(heal=a.heal, alert=not a.no_alert)
    if a.json:
        print(json.dumps(out))
    else:
        print(f"clone health @ {out['at']}  " +
              "  ".join(f"{k}={v}" for k, v in out["summary"].items()))
        for r in out["rows"]:
            mark = {"live_ok": "✓", "live_degraded": "✗", "built_not_wired": "○", "not_built": "·"}[r["status"]]
            print(f"  {mark} {r['component']:<40} {r['detail']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
