#!/usr/bin/env python3
"""paper_trader_backtest.py — replay the PAPER engine over REAL price history.

This is the evidence/validation harness for agents/markets_paper_trader.py. It
steps the tradeable EQUITY universe's real ~1y daily history (Yahoo, $0, cached)
through the SAME model + the SAME simulated-fill code the live engine uses
(score_candidate, size_position, paper_open/paper_close), so it exercises the
full close path — stops, targets, time-exits — and produces real closed trades
on real prices. From those it computes the same performance + conviction
calibration statistics the live --report produces.

Why it exists: the forward live loop proves ingest->score->execute->mark, but
closed-trade stats + calibration only accrue as positions actually exit over
time. This replays history so the close/performance/calibration machinery is
proven NOW, on real prices — and doubles as a quick backtest of the strategy.

It is explicitly a HISTORICAL BACKTEST, not forward paper: results are modeled
on past prices and written to a SEPARATE ledger so the live forward track record
stays clean. Crypto is excluded (different trading calendar); the live engine
trades it forward. This is a directional sanity check, not proof of a live edge —
that needs weeks of forward paper (see the engine docstring).

  PYTHONPATH=. .venv/bin/python tools/paper_trader_backtest.py            # run + print metrics
  PYTHONPATH=. .venv/bin/python tools/paper_trader_backtest.py --keep-ledger
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from core import market_signals as sig
from core.market_signals import EQUITY_UNIVERSE, SECTOR_ETFS, compute_features
from agents import markets_paper_trader as eng

BT_TRADES = eng.DATA / "markets_paper_backtest_trades.jsonl"
BT_RESULT = eng.DATA / "markets_paper_backtest_result.json"
WARMUP = 205   # need ~200 bars for the 200-day MA


def _historical_structure(feats_by_sym: dict[str, dict], vix: float | None) -> dict[str, Any]:
    """Reconstruct the regime/breadth read for a single historical day from that
    day's feature slice — mirrors core.market_signals.provide_structure."""
    spy = feats_by_sym.get("SPY") or {}
    eq = [s for s in EQUITY_UNIVERSE if (feats_by_sym.get(s) or {}).get("n", 0) >= 50]
    breadth = (100 * sum(1 for s in eq if feats_by_sym[s].get("above_50dma")) / len(eq)) if eq else 50.0
    risk_on = spy.get("above_200dma") and (vix or 99) < 24 and breadth >= 50
    risk_off = (not spy.get("above_200dma")) or (vix or 0) >= 28 or breadth < 35
    regime = "risk_on" if risk_on else "risk_off" if risk_off else "neutral"
    return {"structure": {"regime": regime, "vix": vix, "breadth_pct_above_50dma": round(breadth, 1)}}


def run_backtest(*, keep_ledger: bool = False) -> dict[str, Any]:
    eng.assert_paper_only()
    cfg = eng.Config.from_env()
    BT_TRADES.unlink(missing_ok=True)

    # fetch ~2y directly (longer than the live 1y) so the 200-bar warmup still
    # leaves a meaningful (~1y) test window. Direct calls, not the live cache.
    universe = list(EQUITY_UNIVERSE)
    bars = {s: (sig._yahoo_chart(s, rng="2y") or {}) for s in universe + ["^VIX"]}
    closes = {s: (bars.get(s) or {}).get("closes") or [] for s in universe}
    vix_closes = (bars.get("^VIX") or {}).get("closes") or []
    closes = {s: c for s, c in closes.items() if len(c) > WARMUP + 5}
    if "SPY" not in closes:
        return {"error": "insufficient SPY history for backtest"}
    L = min(len(c) for c in closes.values())
    closes = {s: c[-L:] for s, c in closes.items()}
    vix_closes = vix_closes[-L:] if len(vix_closes) >= L else []

    # fresh in-memory portfolio (NOT the live state); reuse engine fill/sizing/risk math
    st: dict[str, Any] = {
        "cash": cfg.starting_cash, "starting_cash": cfg.starting_cash,
        "positions": {}, "realized_pnl": 0.0, "peak_equity": cfg.starting_cash,
        "last_prices": {}, "daily": {"date": "bt", "starting_equity": cfg.starting_cash, "new_trades": 0},
    }
    eqs: list[float] = []
    base_day = datetime.now(timezone.utc) - timedelta(days=L)

    for t in range(WARMUP, L):
        day_ts = (base_day + timedelta(days=t)).isoformat()
        price_t = {s: closes[s][t] for s in closes}
        st["last_prices"] = dict(price_t)
        feats = {s: compute_features(closes[s][: t + 1]) for s in closes}
        vix = vix_closes[t] if t < len(vix_closes) else None
        market = _historical_structure(feats, vix)

        # reset the per-day loss governor (matches live daily reset)
        st["daily"] = {"date": day_ts[:10], "starting_equity": eng.equity(st), "new_trades": 0}

        # 1) exits first (stop / target / time)
        for symx, pos in list(st["positions"].items()):
            px = price_t.get(symx)
            if px is None:
                continue
            side = pos["side"]
            hit_stop = (px <= pos["stop"]) if side == "long" else (px >= pos["stop"])
            hit_target = (px >= pos["target"]) if side == "long" else (px <= pos["target"])
            held = t - int(pos.get("opened_bar") or t)
            reason = "stop" if hit_stop else "target" if hit_target else ("time-exit" if held >= cfg.max_hold_days else None)
            if reason:
                eng.paper_close(st, symx, px, reason, cfg, trades_path=BT_TRADES, ts=day_ts)

        # 2) entries (gated exactly like live)
        if not eng.risk_blocks(st, cfg):
            cands = []
            for s in closes:
                if s in st["positions"]:
                    continue
                c = eng.score_candidate(s, feats[s], market, cfg)
                if c and c["conviction"] >= cfg.min_conviction and c["reward_risk"] >= cfg.min_reward_risk:
                    cands.append(c)
            cands.sort(key=lambda c: c["conviction"], reverse=True)
            opened = 0
            for c in cands:
                if opened >= cfg.max_new_per_cycle or len(st["positions"]) >= cfg.max_open_positions:
                    break
                notional = eng.size_position(c, st, cfg)
                if notional < max(50.0, eng.equity(st) * 0.005):
                    continue
                pos = eng.paper_open(st, c, notional, cfg, trades_path=BT_TRADES, ts=day_ts)
                pos["opened_bar"] = t
                opened += 1

        e = eng.equity(st)
        st["peak_equity"] = max(st["peak_equity"], e)
        eqs.append(e)

    # liquidate remaining at the last bar for a complete closed-trade record
    last = {s: closes[s][L - 1] for s in closes}
    st["last_prices"] = dict(last)
    for symx in list(st["positions"]):
        eng.paper_close(st, symx, last[symx], "backtest-end", cfg, trades_path=BT_TRADES,
                        ts=(base_day + timedelta(days=L)).isoformat())
    eqs.append(eng.equity(st))

    closed = [json.loads(x) for x in BT_TRADES.read_text().splitlines() if '"event": "close"' in x or '"event":"close"' in x]
    metrics = eng.performance_metrics(
        closed, eqs, starting_cash=cfg.starting_cash, current_equity=eng.equity(st),
        cash=float(st["cash"]), realized_pnl=float(st["realized_pnl"]),
        open_positions=0, gross_exp=0.0,
    )
    result = {
        "kind": "HISTORICAL_BACKTEST (paper, modeled on past real prices — not forward paper, not a live edge)",
        "paper_only": True,
        "universe": "equity/ETF only (crypto is forward-live only)",
        "bars_per_symbol": L, "symbols": len(closes), "warmup_bars": WARMUP,
        **metrics,
    }
    eng._write_json(BT_RESULT, result)
    if not keep_ledger:
        BT_TRADES.unlink(missing_ok=True)
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="Replay the PAPER engine over real price history.")
    ap.add_argument("--keep-ledger", action="store_true", help="keep the backtest trade ledger")
    args = ap.parse_args()
    res = run_backtest(keep_ledger=args.keep_ledger)
    print(json.dumps(res, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
