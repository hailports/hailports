"""cortex.babysitting — rank what actually keeps needing Operator.

Instead of guessing, this reads the real intervention signals the stack already
records and ranks them by how much they need HIM specifically (a dead session or an
unaddressed approval blocks everything until he acts; a self-heal doesn't). Output is
a grounded, ranked ledger the cortex loop works instead of a hand-wavy list.

Sources (all real, from the signal map):
  approval backlog   data/hustle/ALEX_ACTION_QUEUE.md, fire_queue_pending.jsonl,
                     data/logs/myos_approval_ledger.jsonl
  session re-auth    data/hustle/session_status.json (human_required/expired/missing)
  blocking gates     data/hustle/revenue_scoreboard.jsonl (gates_blocking)
  restart/heal churn data/logs/health_findings.jsonl (kind=heal), runaway_guard.incidents.jsonl
  chronic outcomes   data/integrity/outcome_heal_ledger.json (fails)
  pending review     data/work_action_learned.json (pending_review)
  halted lanes       data/hustle/*OFF kill-switch flags

Every driver cites a real number. Fail-soft: a missing/broken source contributes nothing.
"""
from __future__ import annotations

import glob
import json
import time
from pathlib import Path

from core.cortex import gates

ROOT = gates.ROOT
HUSTLE = ROOT / "data" / "hustle"
LOGS = ROOT / "data" / "logs"
OUT = ROOT / "data" / "cortex" / "babysitting_ledger.json"
DIGEST = ROOT / "data" / "cortex" / "babysitting.txt"

# How much each category needs Operator specifically (not just how noisy it is). A self-healing
# restart barely needs him; a dead session or a staged approval blocks work until he acts.
COST = {
    "page": 5,             # already interrupted him
    "session_reauth": 5,   # only he can log in; the channel is dead until he does
    "blocking_gate": 4,    # revenue blocked on his action
    "approval_backlog": 4, # nothing happens until he decides — and it's piling up
    "halted_lane": 3,      # a lane is off awaiting his re-arm
    "chronic_outcome": 2,  # self-retries; noisy, not blocking him
    "runaway_kill": 2,     # auto-handled, but signals instability that will escalate
    "pending_review": 2,
    "restart_churn": 1,    # self-heals; only matters when chronic-escalated
    "display_contention": 3,  # remote screen shaped for the wrong device — blocks Operator's actual UX
}


def _score(cat: str, count: int) -> int:
    # cost-tier dominant, frequency as tiebreak (capped so one huge count can't swamp a tier)
    return COST.get(cat, 1) * 1000 + min(int(count), 999)


def _driver(cat, key, count, evidence, source, **extra):
    return {"category": cat, "key": key, "count": int(count), "cost": COST.get(cat, 1),
            "score": _score(cat, count), "evidence": evidence, "source": source, **extra}


def _read_json(p: Path, default):
    try:
        return json.loads(p.read_text(errors="ignore"))
    except Exception:
        return default


def _iter_jsonl(p: Path):
    try:
        for ln in p.read_text(errors="ignore").splitlines():
            ln = ln.strip()
            if ln:
                try:
                    yield json.loads(ln)
                except Exception:
                    continue
    except Exception:
        return


# --- scanners ---------------------------------------------------------------
def _d_approval_backlog():
    out = []
    q = HUSTLE / "ALEX_ACTION_QUEUE.md"
    try:
        text = q.read_text(errors="ignore")
        unchecked = text.count("\n- [ ]")
        checked = text.count("\n- [x]") + text.count("\n- [X]")
        # group unchecked by staging source (## YYYY-MM-DD — SOURCE: ...)
        by_src, cur = {}, "misc"
        for ln in text.splitlines():
            if ln.startswith("## "):
                seg = ln[3:]
                seg = seg.split("—", 1)[1] if "—" in seg else seg
                cur = seg.split(":", 1)[0].strip().lower()[:40] or "misc"
            elif ln.startswith("- [ ]"):
                by_src[cur] = by_src.get(cur, 0) + 1
        if unchecked:
            top = sorted(by_src.items(), key=lambda kv: -kv[1])[:3]
            top_s = ", ".join(f"{k}×{v}" for k, v in top)
            out.append(_driver(
                "approval_backlog", "ALEX_ACTION_QUEUE", unchecked,
                f"{unchecked} staged asks unaddressed, {checked} ever cleared — top: {top_s}",
                "data/hustle/ALEX_ACTION_QUEUE.md", by_source=dict(by_src)))
    except Exception:
        pass
    fq = list(_iter_jsonl(HUSTLE / "fire_queue_pending.jsonl"))
    pend = [r for r in fq if str(r.get("status")) == "pending"]
    if pend:
        blocked = [r for r in pend if r.get("blocked_reason")]
        out.append(_driver(
            "approval_backlog", "fire_queue_pending", len(pend),
            f"{len(pend)} drafts staged for manual fire ({len(blocked)} blocked on a login/session)",
            "data/hustle/fire_queue_pending.jsonl"))
    return out


def _d_sessions():
    st = _read_json(HUSTLE / "session_status.json", {})
    out = []
    if isinstance(st, dict):
        for plat, info in st.items():
            if not isinstance(info, dict):
                continue
            status = str(info.get("status", "")).lower()
            needs = bool(info.get("human_required")) or status in ("human_required", "expired", "missing")
            if needs:
                out.append(_driver(
                    "session_reauth", f"session:{plat}", 1,
                    f"{plat} session {status or 'needs login'} — {str(info.get('detail',''))[:60]}",
                    "data/hustle/session_status.json", platform=plat, status=status))
    return out


def _d_gates():
    rows = list(_iter_jsonl(HUSTLE / "revenue_scoreboard.jsonl"))
    gates_b = (rows[-1].get("gates_blocking") if rows else None) or []
    out = []
    for g in gates_b:
        key = str(g).split(":", 1)[0].strip().lower()[:24]
        out.append(_driver("blocking_gate", f"gate:{key}", 1, str(g)[:120],
                           "data/hustle/revenue_scoreboard.jsonl"))
    return out


def _d_restart_churn(cutoff):
    heals = {}
    for row in _iter_jsonl(LOGS / "health_findings.jsonl"):
        if row.get("kind") != "heal":
            continue
        if float(row.get("ts", 0) or 0) < cutoff:
            continue
        svc = str(row.get("service") or row.get("signature") or "?")
        h = heals.setdefault(svc, {"n": 0, "unverified": 0})
        h["n"] += 1
        if row.get("verified") is False:
            h["unverified"] += 1
    out = []
    for svc, h in sorted(heals.items(), key=lambda kv: -kv[1]["n"])[:8]:
        # a service whose heals don't stick is the one that eventually needs him
        cat = "restart_churn" if h["unverified"] == 0 else "runaway_kill"
        out.append(_driver(
            cat, f"heal:{svc}", h["n"],
            f"{svc} healed {h['n']}× in-window ({h['unverified']} didn't verify)",
            "data/logs/health_findings.jsonl", unverified=h["unverified"]))
    return out


def _d_runaway(cutoff):
    kills = {}
    for row in _iter_jsonl(LOGS / "runaway_guard.incidents.jsonl"):
        if float(row.get("ts", 0) or 0) < cutoff:
            continue
        s = str(row.get("script") or row.get("cmd") or "?")[:40]
        kills[s] = kills.get(s, 0) + 1
    out = []
    for s, n in sorted(kills.items(), key=lambda kv: -kv[1])[:6]:
        if n < 2:
            continue
        out.append(_driver("runaway_kill", f"kill:{s}", n,
                           f"{s} force-killed {n}× in-window (leaking/stacking → will need a real fix)",
                           "data/logs/runaway_guard.incidents.jsonl"))
    return out


def _d_chronic_outcomes():
    data = _read_json(ROOT / "data" / "integrity" / "outcome_heal_ledger.json", {})
    out = []
    if isinstance(data, dict):
        for outcome, info in data.items():
            if not isinstance(info, dict):
                continue
            fails = int(info.get("fails", 0) or 0)
            if fails >= 10:
                out.append(_driver("chronic_outcome", f"outcome:{outcome}", fails,
                                   f"{outcome} has failed {fails}× (last good {info.get('last_good','?')})",
                                   "data/integrity/outcome_heal_ledger.json"))
    return out


def _d_pending_review():
    data = _read_json(ROOT / "data" / "work_action_learned.json", {})
    pr = data.get("pending_review") if isinstance(data, dict) else None
    if isinstance(pr, list) and pr:
        return [_driver("pending_review", "work_action_pending_review", len(pr),
                        f"{len(pr)} learned-route items awaiting your review",
                        "data/work_action_learned.json")]
    return []


def _d_halted_lanes():
    out = []
    for p in glob.glob(str(HUSTLE / "*OFF")):
        name = Path(p).name
        if name.endswith(".bak") or ".cleared" in name:
            continue
        try:
            age_d = (time.time() - Path(p).stat().st_mtime) / 86400.0
        except Exception:
            age_d = None
        out.append(_driver("halted_lane", f"off:{name}", 1,
                           f"{name} present — a lane is halted awaiting your re-arm"
                           + (f" (off {age_d:.0f}d)" if age_d is not None else ""),
                           f"data/hustle/{name}"))
    return out


def _d_display():
    """Remote-display contention — the shaper can't always tell which of two connected devices Operator
    is actually on (a hard disambiguation it punts to the ~/.remote_view_device hint), so it can hold
    the wrong device's shape and look fine (healthy process, wrong output). Surface it from the
    shaper's own log so the machine flags it instead of Operator discovering the screen wrong. Read-only."""
    log = ROOT / "logs" / "remote_link_adapter.log"
    try:
        lines = log.read_text(errors="ignore").splitlines()[-40:]
    except Exception:
        return []
    viewers, cur, errs = set(), None, 0
    for ln in lines:
        try:
            r = json.loads(ln)
        except Exception:
            if "error" in ln.lower() or "traceback" in ln.lower():
                errs += 1
            continue
        v = r.get("viewer")
        if v and str(v) != "None":
            viewers.add(v)
            cur = v
    out = []
    if len(viewers) > 1:
        out.append(_driver(
            "display_contention", "remote_display", len(viewers),
            f"{len(viewers)} remote viewers contended recently (holding '{cur}') — shaper may hold the "
            f"wrong device; fix: `remote_link_adapter.py once` or `echo <device> > ~/.remote_view_device`",
            "logs/remote_link_adapter.log", viewers=sorted(viewers), holding=cur))
    if errs >= 3:
        out.append(_driver("display_contention", "remote_display_errors", errs,
                           f"{errs} display-shaper errors in recent ticks", "logs/remote_link_adapter.log"))
    return out


def scan(window_hours: float = 168.0) -> dict:
    cutoff = time.time() - window_hours * 3600
    drivers = []
    for fn in (lambda: _d_approval_backlog(), lambda: _d_sessions(), lambda: _d_gates(),
               lambda: _d_restart_churn(cutoff), lambda: _d_runaway(cutoff),
               lambda: _d_chronic_outcomes(), lambda: _d_pending_review(), lambda: _d_halted_lanes(),
               lambda: _d_display()):
        try:
            drivers.extend(fn() or [])
        except Exception:
            continue
    drivers.sort(key=lambda d: -d["score"])
    return {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "window_hours": window_hours,
            "drivers": drivers, "total": len(drivers)}


def write(led: dict | None = None) -> dict:
    led = led or scan()
    try:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(led, indent=2, default=str))
        lines = [f"what keeps needing Operator — ranked ({led['total']} drivers, "
                 f"{led['window_hours']:.0f}h window):"]
        for i, d in enumerate(led["drivers"][:15], 1):
            lines.append(f"  {i:>2}. [{d['category']}] {d['evidence']}")
        DIGEST.write_text("\n".join(lines) + "\n")
    except Exception:
        pass
    return led


QUEUE = HUSTLE / "ALEX_ACTION_QUEUE.md"
ARCHIVE = HUSTLE / "ALEX_ACTION_QUEUE.archive.md"


def archive_stale(days: int = 14, apply: bool = False) -> dict:
    """Move unchecked '## YYYY-MM-DD — ...' blocks older than `days` out of the queue into the
    archive (reversible — nothing is deleted). Keeps checked items and anything recent/undated.
    Dry by default: returns what it WOULD move. This is the GC the queue never had."""
    import re as _re

    try:
        text = QUEUE.read_text(errors="ignore")
    except Exception as e:
        return {"error": str(e)[:120], "moved": 0}
    cutoff = time.time() - days * 86400
    # split into (preamble, blocks) on the '## ' section headers
    parts = _re.split(r"(?=^## )", text, flags=_re.M)
    keep, moved = [], []
    for p in parts:
        m = _re.match(r"## (\d{4})-(\d{2})-(\d{2})", p)
        stale = False
        if m and "- [ ]" in p and "- [x]" not in p.lower():
            try:
                y, mo, d = int(m[1]), int(m[2]), int(m[3])
                # crude epoch (UTC midnight) — good enough for a day-granularity cutoff
                ts = time.mktime((y, mo, d, 0, 0, 0, 0, 0, -1))
                stale = ts < cutoff
            except Exception:
                stale = False
        (moved if stale else keep).append(p)
    result = {"days": days, "would_move": len(moved), "kept": len([k for k in keep if k.startswith("## ")]),
              "apply": apply}
    if apply and moved:
        try:
            with ARCHIVE.open("a") as f:
                f.write(f"\n<!-- archived {time.strftime('%Y-%m-%d')} (stale > {days}d) -->\n" + "".join(moved))
            QUEUE.write_text("".join(keep))
            result["archived_to"] = str(ARCHIVE)
        except Exception as e:
            result["error"] = str(e)[:120]
    return result


def _selftest() -> int:
    led = scan()
    assert isinstance(led, dict) and "drivers" in led, "scan() must return {drivers}"
    for d in led["drivers"]:
        assert set(("category", "key", "count", "cost", "score", "evidence")) <= set(d), d
        assert isinstance(d["count"], int), d
        assert d["category"] in COST, f"unknown category {d['category']}"
    # ranked by score descending
    scores = [d["score"] for d in led["drivers"]]
    assert scores == sorted(scores, reverse=True), "drivers not rank-sorted"
    a = archive_stale(days=14, apply=False)  # dry — must not mutate the real queue
    assert isinstance(a, dict) and "would_move" in a and a["apply"] is False, a
    print(f"BABYSITTING SELFTEST OK — {led['total']} real drivers, rank-sorted, all grounded; "
          f"top: {led['drivers'][0]['category'] if led['drivers'] else '(none)'}")
    return 0


if __name__ == "__main__":
    import sys

    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    if "--archive" in sys.argv:
        days = 14
        for a in sys.argv:
            if a.startswith("--days="):
                days = int(a.split("=", 1)[1] or "14")
        print(json.dumps(archive_stale(days=days, apply="--apply" in sys.argv), indent=2))
        raise SystemExit(0)
    led = write()
    print(DIGEST.read_text() if DIGEST.exists() else json.dumps(led, indent=2, default=str))
