"""cortex.sensors — SENSE: fold every real outcome signal into one machine-state.

Reuses signals that already exist (and today feed only dashboards): the revenue
scoreboard, the strategist's situation brief, the health ledger's chronic-failure
signal, the site funnel rates, and — when track A is present — the portfolio effort
state. Everything is fail-soft: a missing/broken source contributes nothing rather
than raising. The output is the grounded input to DIAGNOSE.
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from core.cortex import gates

ROOT = gates.ROOT
HUSTLE = gates.HUSTLE

SCOREBOARD = HUSTLE / "revenue_scoreboard.jsonl"
FUNNEL_RATES = HUSTLE / "funnel_rates.json"
CHRONIC = ROOT / "data" / "runtime" / "chronic_failures.json"


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(errors="ignore"))
    except Exception:
        return default


def _tail_jsonl(path: Path, n: int) -> list:
    try:
        rows = [ln for ln in path.read_text(errors="ignore").splitlines() if ln.strip()]
        out = []
        for ln in rows[-n:]:
            try:
                out.append(json.loads(ln))
            except Exception:
                continue
        return out
    except Exception:
        return []


def _scoreboard(n: int = 6) -> dict:
    rows = _tail_jsonl(SCOREBOARD, n)
    if not rows:
        return {"rows": 0}
    latest = rows[-1]
    stripe = latest.get("stripe") or {}
    funnel = latest.get("funnel") or {}
    leads = latest.get("leads") or {}
    return {
        "rows": len(rows),
        "latest_ts": latest.get("ts"),
        "stripe": {k: stripe.get(k) for k in ("revenue_cents", "revenue_usd", "orders", "charges") if k in stripe},
        "funnel": {k: funnel.get(k) for k in list(funnel)[:12]},
        "leads": {k: leads.get(k) for k in list(leads)[:12]},
        "gates_blocking": latest.get("gates_blocking"),
        "trend_revenue_cents": [
            (r.get("stripe") or {}).get("revenue_cents") for r in rows if isinstance(r.get("stripe"), dict)
        ],
    }


def _strategist_brief() -> dict:
    try:
        from core.revenue_strategist_loop import build_brief

        return build_brief()
    except Exception as e:
        return {"error": f"strategist brief unavailable: {str(e)[:120]}"}


# A persisted chronic snapshot older than this is untrustworthy — it can name long-resolved
# issues (a stale chronic_failures.json once made the loop chase a 3-week-dead anthropic finding).
_CHRONIC_STALE_HOURS = 6.0


def _chronic() -> dict:
    """Prefer the LIVE, window-bounded recompute over the persisted snapshot. A stale snapshot
    reports resolved problems as active, so the fallback is only trusted when fresh; otherwise the
    loop sees zero chronics rather than a false one. Fail-soft throughout."""
    try:
        from core.health_ledger import chronic

        return {"source": "health_ledger.chronic()", "items": chronic() or [], "fresh": True}
    except Exception as live_err:
        try:
            age_h = (time.time() - CHRONIC.stat().st_mtime) / 3600.0
        except Exception:
            age_h = None
        data = _read_json(CHRONIC, None)
        if isinstance(data, dict) and data and age_h is not None and age_h <= _CHRONIC_STALE_HOURS:
            items = data.get("chronic") or data.get("items") or data
            return {"source": "chronic_failures.json", "fresh": True, "age_hours": round(age_h, 1),
                    "items": items if isinstance(items, list) else []}
        # live recompute unavailable AND no fresh snapshot -> report nothing (never a stale false chronic)
        return {"source": "unavailable_or_stale", "items": [], "fresh": False,
                "age_hours": round(age_h, 1) if age_h is not None else None,
                "error": str(live_err)[:120]}


def _portfolio() -> dict:
    try:
        from core.cortex import portfolio  # track A (built by the extensions workflow)

        return portfolio.read_portfolio_state()
    except Exception:
        return {"available": False}


def _babysitting() -> dict:
    """The ranked 'what keeps needing Operator' drivers — so the loop works the real intervention
    list instead of guessing. Compact (top 8) to keep the state small. Fail-soft."""
    try:
        from core.cortex import babysitting

        led = babysitting.scan()
        return {"total": led["total"], "top": led["drivers"][:8]}
    except Exception:
        return {"available": False}


def _git_dirty() -> int:
    try:
        out = subprocess.run(
            ["git", "-C", str(ROOT), "status", "--porcelain"],
            capture_output=True, text=True, timeout=15,
        )
        return len([ln for ln in out.stdout.splitlines() if ln.strip()])
    except Exception:
        return -1


def sense() -> dict:
    """Build the unified machine-state. Pure read; never mutates, never raises."""
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "arming": gates.arming(),
        "scoreboard": _scoreboard(),
        "strategist": _strategist_brief(),
        "chronic_failures": _chronic(),
        "funnel_rates": (_read_json(FUNNEL_RATES, {}) or {}).get("overall", _read_json(FUNNEL_RATES, {})),
        "portfolio": _portfolio(),
        "babysitting": _babysitting(),
        "git_dirty_files": _git_dirty(),
    }


def grounding_facts(state: dict) -> set:
    """Every real number in the machine-state, as strings — so a proposed change's
    rationale can be checked against it (a rationale citing a number NOT here is
    ungrounded / fabricated). Reuses the strategist's extractor when importable."""
    try:
        from core.revenue_strategist_loop import _grounding_facts

        return _grounding_facts(state)
    except Exception:
        import re

        facts: set = set()

        def walk(v):
            if isinstance(v, dict):
                for x in v.values():
                    walk(x)
            elif isinstance(v, (list, tuple)):
                for x in v:
                    walk(x)
            elif isinstance(v, bool):
                return
            elif isinstance(v, (int, float)):
                facts.add(str(v))
            elif isinstance(v, str):
                for m in re.findall(r"\d+\.?\d*", v):
                    facts.add(m)

        walk(state)
        return {f for f in facts if f and f != "0.0"}


def _selftest() -> int:
    st = sense()
    assert isinstance(st, dict), "sense() must return a dict"
    for key in ("scoreboard", "strategist", "chronic_failures", "arming", "portfolio", "babysitting"):
        assert key in st, f"sense() missing key {key!r}"
    facts = grounding_facts(st)
    assert isinstance(facts, set), "grounding_facts must return a set"
    ch = st["chronic_failures"]
    assert ch["source"] in ("health_ledger.chronic()", "chronic_failures.json", "unavailable_or_stale"), ch
    # never surface a persisted snapshot unless it is fresh (guards the stale-chronic trap)
    assert ch["source"] != "chronic_failures.json" or ch.get("fresh") is True, "trusted a stale snapshot"
    print(f"SENSORS SELFTEST OK — {len(facts)} grounded facts; chronic src={ch['source']}; "
          f"scoreboard rows={st['scoreboard'].get('rows')}; "
          f"chronic items={len(st['chronic_failures'].get('items', []))}; "
          f"git_dirty={st['git_dirty_files']}")
    return 0


if __name__ == "__main__":
    import sys

    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    print(json.dumps(sense(), indent=2, default=str)[:6000])
