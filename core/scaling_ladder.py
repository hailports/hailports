"""scaling_ladder.py — the "then SCALE" engine: how much real money a PROVEN lane should hold.

A lane earns the right to go live via core.trade_proof_gate (paper record clears the bar) and
tools/trading_lanes.py `arm <lane>`. This module answers the NEXT question: once real money is
flowing, how big should the stake actually be — and does the lane keep earning that size? It
walks a fixed ladder of stakes:

    25 -> 50 -> 100 -> 250 -> 500 -> 1000

promoting one rung when the lane's LIVE (mode == "live", real-money) round-trip record keeps
proving the edge, and pulling back one rung (or to the floor) the instant that record decays —
the auto-pullback so a stale edge can't bleed the scaled-up capital.

Rules (all evaluated off the lane's LIVE closed-trade ledger only — paper/sim rows never count):
  PROMOTE one rung, ALL of:
    - proven to scale (prefers core.trade_proof_gate.proven_to_scale if a sibling has landed it,
      else falls back to trade_proof_gate.evaluate — guarded via getattr, works either way)
    - >= N_PROMOTE (15) live round-trips accumulated since the last PROMOTION, net profitable
    - live max-drawdown since the last ladder step < 15%
    - rolling live expectancy (last M=10 live trades) > 0
  DEMOTE one rung (or to the floor if already there), EITHER (checked first — capital
  protection outranks scaling up):
    - rolling live expectancy (last M=10 live trades) < 0
    - drawdown from the lane's all-time peak equity > 20%
  Otherwise HOLD.

This module ONLY recommends and persists its own bookkeeping in
data/trading/scaling_ladder_state.json (current rung, trade/window anchors, peak equity, a short
promote/demote log). It never writes env vars, arm files, or touches a broker — actually raising
a live stake stays a human/arm-gated step, same doctrine as trade_proof_gate: it prints the exact
command an operator would run to apply the change.

$0. stdlib + core.trade_proof_gate + tools.trading_lanes (single-source-of-truth LANES registry).
Fail-open: any lane that can't be read cleanly recommends HOLD with the reason spelled out.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import trade_proof_gate as gate  # noqa: E402
from tools.trading_lanes import LANES, BASE_DIR  # noqa: E402

LADDER: list[int] = [25, 50, 100, 250, 500, 1000]
FLOOR_RUNG = 0
TOP_RUNG = len(LADDER) - 1

N_PROMOTE = 15            # min profitable-net LIVE round-trips since the last promotion to earn a step up
M_ROLLING = 10            # rolling window (# of LIVE trades) for the expectancy check, both directions
PROMOTE_MAX_DD = 0.15     # live drawdown since the last ladder step must stay under this to promote
DEMOTE_PEAK_DD = 0.20     # live drawdown from all-time peak equity that forces a demotion

STATE_PATH = BASE_DIR / "data" / "trading" / "scaling_ladder_state.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_state() -> dict[str, Any]:
    try:
        if STATE_PATH.exists():
            return json.loads(STATE_PATH.read_text())
    except Exception:
        pass
    return {}


def _save_state(state: dict[str, Any]) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state, indent=2, default=str))
    except Exception:
        pass  # fail-open: a bookkeeping write failure must never break a recommendation


def _default_lane_state() -> dict[str, Any]:
    return {
        "rung": FLOOR_RUNG,
        "trades_at_rung": 0,
        "peak_equity": None,
        "equity_at_last_step": None,
        "last_step_ts": None,
        "last_promote_ts": None,
        "last_step_action": "init",
        "log": [],
    }


def _read_json(path: Path) -> dict[str, Any]:
    try:
        if path.exists():
            return json.loads(path.read_text())
    except Exception:
        pass
    return {}


def _load_live_closed(trades_path: Path) -> list[dict[str, Any]]:
    """Closed round-trips from a lane's REAL-money ledger only (mode == "live"), same
    synthetic-price exclusion as trade_proof_gate. Sorted oldest -> newest."""
    out: list[dict[str, Any]] = []
    if not trades_path.exists():
        return out
    for line in trades_path.read_text(errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        if str(row.get("mode") or "").lower() != "live":
            continue
        event = str(row.get("event") or row.get("status") or "")
        pnl = row.get("pnl")
        if pnl is None or event in {"open", "paper_order", "paper_open"}:
            continue
        if str(row.get("price_source") or row.get("source") or "").lower() in {"synthetic", "seed", "stub"}:
            continue
        try:
            row["_pnl"] = float(pnl)
        except Exception:
            continue
        row["_ts"] = str(row.get("ts") or "")
        out.append(row)
    out.sort(key=lambda r: r["_ts"])
    return out


def _is_proven_to_scale(reg: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    """Prefer proven_to_scale() if a sibling agent has landed it on trade_proof_gate; fall back
    to the standing evaluate() paper-proof gate. Guarded so this works whether or not it exists."""
    trades_path = BASE_DIR / reg["trades"]
    fn = getattr(gate, "proven_to_scale", None)
    if callable(fn):
        try:
            res = fn(trades_path)
            if isinstance(res, dict) and "proven" in res:
                return bool(res["proven"]), res
            if isinstance(res, dict) and "proven_to_scale" in res:
                return bool(res["proven_to_scale"]), res
            if isinstance(res, bool):
                return res, {"proven": res, "reasons": ["proven_to_scale()"]}
        except Exception:
            pass  # not wired yet / signature mismatch -> fall back below
    res = gate.evaluate(trades_path)
    return bool(res.get("proven")), res


def _expectancy(trades: list[dict[str, Any]]) -> float:
    n = len(trades)
    if n == 0:
        return 0.0
    wins = [t for t in trades if t["_pnl"] > 0]
    losses = [t for t in trades if t["_pnl"] <= 0]
    win_rate = len(wins) / n
    avg_win = (sum(t["_pnl"] for t in wins) / len(wins)) if wins else 0.0
    avg_loss = (abs(sum(t["_pnl"] for t in losses)) / len(losses)) if losses else 0.0
    return round((win_rate * avg_win) - ((1 - win_rate) * avg_loss), 4)


def _drawdown_curve(trades: list[dict[str, Any]], baseline: float) -> float:
    """Max peak-to-trough drawdown (fraction) walking `trades` in order from `baseline` equity."""
    equity = baseline
    peak = baseline
    max_dd = 0.0
    for t in trades:
        equity += t["_pnl"]
        peak = max(peak, equity)
        if peak > 0:
            max_dd = max(max_dd, (peak - equity) / peak)
    return round(max_dd, 4)


def _lane_equity_now(reg: dict[str, Any], live_trades: list[dict[str, Any]], baseline: float) -> float:
    st = _read_json(BASE_DIR / reg["status"])
    equity = st.get("equity", st.get("cash"))
    if isinstance(equity, (int, float)):
        return float(equity)
    # fallback: reconstruct from the live ledger itself if the status file has nothing usable
    return round(baseline + sum(t["_pnl"] for t in live_trades), 4)


def _stake_env_hint(reg: dict[str, Any]) -> str:
    prefix = reg.get("mode_env", "").rsplit("_MODE", 1)[0] or "LANE"
    return f"{prefix}_PAPER_STARTING_CASH"


def recommend(lane: str) -> dict[str, Any]:
    reg = LANES.get(lane)
    if not reg:
        return {"lane": lane, "action": "hold", "reasons": [f"unknown lane '{lane}', choose from {list(LANES)}"],
                "current_stake": None, "target_stake": None}

    state = _load_state()
    entry = state.setdefault(lane, _default_lane_state())
    rung = int(entry.get("rung", FLOOR_RUNG))
    rung = min(max(rung, FLOOR_RUNG), TOP_RUNG)  # clamp against a hand-edited/corrupt state file
    current_stake = LADDER[rung]

    live_trades = _load_live_closed(BASE_DIR / reg["trades"])
    proven, proof_res = _is_proven_to_scale(reg)

    since_promote_ts = entry.get("last_promote_ts")
    since_promotion = [t for t in live_trades if since_promote_ts is None or t["_ts"] > since_promote_ts]
    n_since_promotion = len(since_promotion)
    net_since_promotion = round(sum(t["_pnl"] for t in since_promotion), 4)

    since_step_ts = entry.get("last_step_ts")
    since_step = [t for t in live_trades if since_step_ts is None or t["_ts"] > since_step_ts]
    baseline = float(entry.get("equity_at_last_step") or current_stake)
    dd_since_step = _drawdown_curve(since_step, baseline)

    rolling = live_trades[-M_ROLLING:]
    expectancy_rolling = _expectancy(rolling)

    equity_now = _lane_equity_now(reg, live_trades, baseline)
    peak_equity = entry.get("peak_equity")
    peak_equity = equity_now if peak_equity is None else max(float(peak_equity), equity_now)
    peak_dd = round((peak_equity - equity_now) / peak_equity, 4) if peak_equity > 0 else 0.0

    # capital protection outranks scaling up: check demote triggers before promote eligibility
    demote_reasons: list[str] = []
    if rolling and expectancy_rolling < 0:
        demote_reasons.append(f"rolling live expectancy over last {len(rolling)} live trades is negative ({expectancy_rolling})")
    if peak_dd > DEMOTE_PEAK_DD:
        demote_reasons.append(f"live drawdown from peak equity is {peak_dd:.1%} (breach > {DEMOTE_PEAK_DD:.0%})")

    promote_checks = {
        "proven to scale": proven,
        f">= {N_PROMOTE} live round-trips since last promotion (have {n_since_promotion})": n_since_promotion >= N_PROMOTE,
        "net profitable over that window": net_since_promotion > 0,
        f"live drawdown since last step < {PROMOTE_MAX_DD:.0%} (is {dd_since_step:.1%})": dd_since_step < PROMOTE_MAX_DD,
        f"rolling live expectancy > 0 (is {expectancy_rolling})": expectancy_rolling > 0,
    }

    action = "hold"
    target_stake = current_stake
    reasons: list[str] = []
    new_rung = rung

    if demote_reasons:
        action = "demote"
        if rung > FLOOR_RUNG:
            new_rung = rung - 1
            target_stake = LADDER[new_rung]
        else:
            target_stake = LADDER[FLOOR_RUNG]
            demote_reasons.append(f"already at the floor stake (${LADDER[FLOOR_RUNG]}) — cannot demote further")
        reasons = demote_reasons
    elif all(promote_checks.values()):
        if rung < TOP_RUNG:
            action = "promote"
            new_rung = rung + 1
            target_stake = LADDER[new_rung]
            reasons = [f"cleared: {k}" for k in promote_checks]
        else:
            reasons = [f"already at the top rung (${LADDER[TOP_RUNG]}) — no higher rung to promote to"]
    else:
        if not proven:
            reasons.append("lane is not proven to scale yet: " + "; ".join(proof_res.get("reasons", ["not proven"])))
        else:
            reasons = [f"blocked: {k}" for k, ok in promote_checks.items() if not ok]
        if reg.get("settles_at_event") and not live_trades:
            reasons.append("this lane settles at event resolution — round-trip P&L tracking isn't wired yet, ladder can't promote")

    command = None
    if action in {"promote", "demote"} and target_stake != current_stake:
        env_var = _stake_env_hint(reg)
        command = (f"python3 tools/trading_lanes.py proof {lane}   # re-confirm, then set {env_var}={target_stake} "
                   f"in data/secrets/trading_accounts.env, fund the {lane} account to match, restart the lane loop")

    # persist bookkeeping only — never touches env/arm/broker state
    entry["trades_at_rung"] = n_since_promotion
    entry["peak_equity"] = round(peak_equity, 4)
    if action in {"promote", "demote"} and target_stake != current_stake:
        entry["log"] = (entry.get("log") or [])[-19:] + [{
            "ts": _now(), "action": action, "from_rung": rung, "to_rung": new_rung,
            "from_stake": current_stake, "to_stake": LADDER[new_rung], "reasons": reasons,
        }]
        entry["rung"] = new_rung
        entry["equity_at_last_step"] = equity_now
        entry["last_step_ts"] = _now()
        if action == "promote":
            entry["last_promote_ts"] = entry["last_step_ts"]
        entry["last_step_action"] = action
    state[lane] = entry
    _save_state(state)

    return {
        "lane": lane,
        "action": action,
        "current_stake": current_stake,
        "target_stake": target_stake,
        "reasons": reasons,
        "proven_to_scale": proven,
        "live_round_trips_since_promotion": n_since_promotion,
        "net_pnl_since_promotion": net_since_promotion,
        "drawdown_since_step": dd_since_step,
        "rolling_expectancy": expectancy_rolling,
        "rolling_window": len(rolling),
        "equity_now": equity_now,
        "peak_equity": round(peak_equity, 4),
        "drawdown_from_peak": peak_dd,
        "command": command,
    }


def recommend_all() -> dict[str, dict[str, Any]]:
    return {lane: recommend(lane) for lane in LANES}


def _print(res: dict[str, Any]) -> None:
    lane = res["lane"]
    if res.get("current_stake") is None:
        print(f"[{lane}] {res['reasons'][0]}")
        return
    print(f"[{lane}] action={res['action'].upper():7} current_stake=${res['current_stake']}  target_stake=${res['target_stake']}")
    print(f"  proven_to_scale={res['proven_to_scale']}  live_round_trips_since_promotion={res['live_round_trips_since_promotion']}  "
          f"net_since_promotion=${res['net_pnl_since_promotion']}  dd_since_step={res['drawdown_since_step']:.1%}  "
          f"rolling_expectancy={res['rolling_expectancy']} (n={res['rolling_window']})  "
          f"equity=${res['equity_now']}  peak=${res['peak_equity']}  dd_from_peak={res['drawdown_from_peak']:.1%}")
    for r in res["reasons"]:
        print(f"  - {r}")
    if res.get("command"):
        print(f"  operator command: {res['command']}")


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Scaling ladder — how much real money a PROVEN lane should hold. Recommend-only.")
    ap.add_argument("--lane", default=None, help="single lane (kalshi/forex/kraken/alpaca); default: all")
    args = ap.parse_args()

    print(f"SCALING LADDER {LADDER} — recommend-only, never moves money or env ($0, stdlib+core)\n")
    lanes = [args.lane] if args.lane else list(LANES.keys())
    for ln in lanes:
        _print(recommend(ln))
        print()
