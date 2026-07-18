"""trade_proof_gate.py — honest "don't risk real money until the paper record proves an edge" gate.

A lane cannot be armed for real money (see tools/trading_lanes.py `arm <lane>`) until its
PAPER ledger clears this bar. This is the only honest version of "nearly guaranteed to
profit": you do not fund a lane with a real dollar until its OWN simulated track record,
run against REAL prices, shows the edge is actually there — not hoped-for.

Bar to clear (all must hold):
  - MIN_CLOSED closed round-trip trades  (enough sample to not be luck)
  - expectancy per trade > 0             (the average trade makes money after modeled fees)
  - profit factor >= MIN_PROFIT_FACTOR   (gross wins outweigh gross losses with margin)
  - net paper P&L > 0                    (the account actually grew)

`proven` above is the arm-a-lane bar. `proven_strict` adds a one-sample t-test on the
per-trade P&L series so "expectancy > 0" isn't just a lucky run, and `proven_to_scale`
additionally requires the edge to survive an out-of-sample split (see `evaluate_oos`)
before MORE real money goes into an already-armed lane.

$0, stdlib only. Reads the same close-event JSONL the bots already write.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

MIN_CLOSED = 30
MIN_CLOSED_STRICT = 100  # bar for putting MORE real money into an already-armed lane
MIN_PROFIT_FACTOR = 1.1
SIGNIFICANCE_T = 1.66  # one-sided t-stat threshold, ~p<0.05


def _load_closed(path: Path) -> list[dict[str, Any]]:
    """Every ledger row that represents a *closed* trade with a realized pnl."""
    out: list[dict[str, Any]] = []
    if not path.exists():
        return out
    for line in path.read_text(errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        event = str(row.get("event") or row.get("status") or "")
        pnl = row.get("pnl")
        # a close is any row that carries a realized pnl (open events have none)
        if pnl is None or event in {"open", "paper_order", "paper_open"}:
            continue
        # never let synthetic/simulated-price fills count toward a real-money proof
        if str(row.get("price_source") or row.get("source") or "").lower() in {"synthetic", "seed", "stub"}:
            continue
        try:
            row["_pnl"] = float(pnl)
        except Exception:
            continue
        out.append(row)
    return out


def _t_stat(pnls: list[float]) -> float:
    """One-sample t-stat for H0: mean per-trade pnl <= 0. Higher = less likely the
    observed expectancy is luck. inf when every trade has the identical (nonzero) pnl."""
    n = len(pnls)
    if n < 2:
        return 0.0
    mean = sum(pnls) / n
    var = sum((x - mean) ** 2 for x in pnls) / (n - 1)
    std = math.sqrt(var)
    if std <= 0:
        return float("inf") if mean > 0 else 0.0
    return mean / (std / math.sqrt(n))


def _compute_metrics(closed: list[dict[str, Any]], min_closed: int, min_profit_factor: float) -> dict[str, Any]:
    """Core proof-bar math, shared by evaluate() and evaluate_oos() so in-sample /
    out-of-sample slices are scored with identical logic."""
    n = len(closed)
    wins = [t for t in closed if (t.get("won") if "won" in t else t["_pnl"] > 0)]
    losses = [t for t in closed if not (t.get("won") if "won" in t else t["_pnl"] > 0)]
    gross_win = sum(t["_pnl"] for t in wins)
    gross_loss = abs(sum(t["_pnl"] for t in losses))
    net = round(gross_win - gross_loss, 4)
    win_rate = round(len(wins) / n, 4) if n else 0.0
    avg_win = round(gross_win / len(wins), 4) if wins else 0.0
    avg_loss = round(gross_loss / len(losses), 4) if losses else 0.0
    expectancy = round((win_rate * avg_win) - ((1 - win_rate) * avg_loss), 4) if n else 0.0
    profit_factor = round(gross_win / gross_loss, 3) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0)

    t_stat = _t_stat([t["_pnl"] for t in closed])
    significant = bool(n >= min_closed and (t_stat == float("inf") or t_stat >= SIGNIFICANCE_T))

    reasons: list[str] = []
    if n < min_closed:
        reasons.append(f"need >= {min_closed} closed paper trades, have {n}")
    if n and expectancy <= 0:
        reasons.append(f"expectancy per trade must be > 0, is {expectancy}")
    if n and profit_factor != float("inf") and profit_factor < min_profit_factor:
        reasons.append(f"profit factor must be >= {min_profit_factor}, is {profit_factor}")
    if n and net <= 0:
        reasons.append(f"net paper P&L must be > 0, is {net}")
    proven = (n >= min_closed) and not reasons
    proven_strict = bool(proven and significant)

    return {
        "proven": bool(proven),
        "proven_strict": proven_strict,
        "reasons": reasons or (["clears the bar"] if proven else ["no closed trades yet"]),
        "n_closed": n,
        "win_rate": win_rate,
        "expectancy": expectancy,
        "profit_factor": "inf" if profit_factor == float("inf") else profit_factor,
        "net_pnl": net,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "t_stat": "inf" if t_stat == float("inf") else round(t_stat, 3),
        "significant": significant,
    }


def evaluate(trades_path: str | Path, *, min_closed: int = MIN_CLOSED,
             min_profit_factor: float = MIN_PROFIT_FACTOR) -> dict[str, Any]:
    path = Path(trades_path)
    closed = _load_closed(path)
    result = _compute_metrics(closed, min_closed, min_profit_factor)
    result["trades_path"] = str(path)
    result["thresholds"] = {
        "min_closed": min_closed,
        "min_profit_factor": min_profit_factor,
        "significance_t": SIGNIFICANCE_T,
    }
    return result


def evaluate_oos(trades_path: str | Path, split: float = 0.7, *, min_closed: int = MIN_CLOSED,
                  min_profit_factor: float = MIN_PROFIT_FACTOR) -> dict[str, Any]:
    """Chronological in-sample / out-of-sample split — the real overfit check. A lane
    that only 'proves' itself in-sample and falls apart out-of-sample was curve-fit to
    its own history, not actually edged. holds_oos requires the out-of-sample slice to
    still show expectancy > 0 and profit_factor >= 1.0 on its own."""
    path = Path(trades_path)
    closed = _load_closed(path)
    ordered = sorted(closed, key=lambda r: str(r.get("ts") or r.get("closed_at") or r.get("timestamp") or ""))
    n = len(ordered)
    split_idx = int(n * split)
    in_sample = _compute_metrics(ordered[:split_idx], min_closed, min_profit_factor)
    out_of_sample = _compute_metrics(ordered[split_idx:], min_closed, min_profit_factor)

    pf = out_of_sample["profit_factor"]
    pf_ok = pf == "inf" or (isinstance(pf, (int, float)) and pf >= 1.0)
    holds_oos = bool(out_of_sample["expectancy"] > 0 and pf_ok)

    return {
        "trades_path": str(path),
        "split": split,
        "n_total": n,
        "in_sample": in_sample,
        "out_of_sample": out_of_sample,
        "holds_oos": holds_oos,
    }


def calibration(trades_path: str | Path) -> dict[str, Any] | None:
    """Brier score + predicted-vs-realized win-rate table for closed trades that carry a
    `win_prob` prediction (see agents/markets_paper_trader's conviction calibration).
    Returns None when no rows carry a prediction — nothing to calibrate."""
    path = Path(trades_path)
    closed = _load_closed(path)
    rated = [t for t in closed if t.get("win_prob") is not None]
    if not rated:
        return None

    def _won(t: dict[str, Any]) -> bool:
        return bool(t.get("won")) if "won" in t else t["_pnl"] > 0

    brier = sum((float(t["win_prob"]) - (1.0 if _won(t) else 0.0)) ** 2 for t in rated) / len(rated)

    bands = [(0.0, 0.5), (0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 1.01)]
    table: list[dict[str, Any]] = []
    for lo, hi in bands:
        bucket = [t for t in rated if lo <= float(t["win_prob"]) < hi]
        if not bucket:
            continue
        bwins = sum(1 for t in bucket if _won(t))
        table.append({
            "band": f"{lo:.2f}-{hi:.2f}",
            "n": len(bucket),
            "predicted_win_prob": round(sum(float(t["win_prob"]) for t in bucket) / len(bucket), 3),
            "realized_win_rate": round(bwins / len(bucket), 3),
        })

    return {
        "trades_path": str(path),
        "n_rated": len(rated),
        "brier_score": round(brier, 4),
        "table": table,
    }


def proven_to_scale(trades_path: str | Path) -> dict[str, Any]:
    """The bar for putting MORE real money into an already-armed lane: strict
    statistical significance on a big-enough sample, AND the edge survives an
    out-of-sample split (not curve-fit to its own history)."""
    path = Path(trades_path)
    res = evaluate(trades_path)
    oos = evaluate_oos(trades_path)

    reasons: list[str] = []
    if not res["proven_strict"]:
        reasons.append("not proven_strict (fails the base proof bar or significance test)")
    if not oos["holds_oos"]:
        reasons.append("edge does not hold out-of-sample (may be curve-fit)")
    if res["n_closed"] < MIN_CLOSED_STRICT:
        reasons.append(f"need >= {MIN_CLOSED_STRICT} closed trades to scale, have {res['n_closed']}")
    ready = bool(res["proven_strict"] and oos["holds_oos"] and res["n_closed"] >= MIN_CLOSED_STRICT)

    return {
        "trades_path": str(path),
        "ready_to_scale": ready,
        "reasons": reasons or ["clears the scale-up bar"],
        "n_closed": res["n_closed"],
        "min_required": MIN_CLOSED_STRICT,
        "evaluate": res,
        "oos": oos,
    }


def verdict_text(lane: str, res: dict[str, Any]) -> str:
    tag = "PROVEN ✓ (eligible to arm)" if res["proven"] else "NOT PROVEN — real money stays locked"
    lines = [
        f"[{lane}] {tag}",
        f"  closed={res['n_closed']} win_rate={res['win_rate']} expectancy={res['expectancy']} "
        f"pf={res['profit_factor']} net=${res['net_pnl']}",
    ]
    if "t_stat" in res:
        lines.append(
            f"  t_stat={res['t_stat']} significant={res.get('significant')} "
            f"proven_strict={res.get('proven_strict')}"
        )
    if not res["proven"]:
        for r in res["reasons"]:
            lines.append(f"  - {r}")
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Trade proof gate — is a lane's paper record good enough to risk real money?")
    ap.add_argument("trades_path", help="path to a lane's *_trades.jsonl / *_signals.jsonl")
    ap.add_argument("--lane", default="lane")
    ap.add_argument("--min-closed", type=int, default=MIN_CLOSED)
    args = ap.parse_args()
    res = evaluate(args.trades_path, min_closed=args.min_closed)
    print(verdict_text(args.lane, res))
    print(json.dumps(res, indent=2, default=str))
