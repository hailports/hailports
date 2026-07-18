"""Fill fidelity: do REAL live fills match what the paper model assumed?

A trading lane's "proven" edge comes from paper P&L, which subtracts a
*modeled* slippage_bps + taker_fee_bps round-trip cost (see core/kraken_bot.py
KrakenConfig). Once a lane goes live, the only honest check that the paper
proof still holds is comparing that modeled cost against what REAL fills
actually cost -- if live execution is worse than modeled, the paper edge was
overstated and scaling on it is scaling a hole.

Each lane's *_bot_trades.jsonl ledger rows (open + close events) carry, when
mode=="live":
  - quote_price  -- the reference price the signal was priced/decided at
  - entry/exit   -- the REAL fill price returned by the exchange
  - entry_fee_usd / exit_fee_usd (or lane-equivalent fee field) -- REAL fee
Only rows that carry both a fill price and a reference price can yield an
actual-slippage number; only rows that carry an explicit fee field can yield
an actual-fee number. Rows missing those fields are silently skipped rather
than treated as zero-cost -- "not tracked" is not the same as "free".

Modeled cost per lane comes straight from that lane's own config/module
(KrakenConfig.slippage_bps/taker_fee_bps, alpaca_bot.SLIPPAGE_BPS, ...) so this
module never re-guesses a number the bot itself already owns. Lanes that don't
model any cost yet (forex/kalshi/micro_trader/markets_paper/polymarket_paper as
of writing) get modeled=0 -- meaning any real cost on those lanes shows up as
100% drift, which is the correct, conservative read.

$0, stdlib + core only. Read-only: never writes to a ledger or bot state.
Fails open on every per-lane/per-row error so a single bad ledger can't take
the report down -- an error surfaces as flag_reason text with fidelity_ok=True
(never silently blocks, but never claims a live-invalidated edge either).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Callable

from core import BASE_DIR

TRADING = BASE_DIR / "data" / "trading"
RUNTIME = BASE_DIR / "data" / "runtime"

# "last K live trades" rolling window, and the drift multiple that trips the flag.
DEFAULT_WINDOW = 50
DEFAULT_MARGIN = 1.5

REF_FIELDS = ("quote_price", "ref_price")
FEE_FIELDS = ("entry_fee_usd", "exit_fee_usd", "fees_paid", "fee_usd")
NOTIONAL_FIELDS = ("notional_usd", "notional", "cost_dollars")


def _modeled_kraken() -> tuple[float, float]:
    """(slippage_bps, taker_fee_bps) the paper model currently assumes."""
    try:
        from core.kraken_bot import KrakenConfig

        cfg = KrakenConfig.from_env()
        return float(cfg.slippage_bps), float(cfg.taker_fee_bps)
    except Exception:
        return 0.0, 0.0


def _modeled_alpaca() -> tuple[float, float]:
    try:
        from core import alpaca_bot

        return float(getattr(alpaca_bot, "SLIPPAGE_BPS", 0.0)), 0.0
    except Exception:
        return 0.0, 0.0


def _modeled_zero() -> tuple[float, float]:
    # Lanes that don't model slippage/fee at all yet -- any real cost is 100% drift.
    return 0.0, 0.0


LANES: dict[str, dict[str, Any]] = {
    "kraken": {"path": TRADING / "kraken_bot_trades.jsonl", "modeled": _modeled_kraken},
    "alpaca": {"path": TRADING / "alpaca_bot_trades.jsonl", "modeled": _modeled_alpaca},
    "forex": {"path": RUNTIME / "forex_bot_trades.jsonl", "modeled": _modeled_zero},
    "kalshi": {"path": RUNTIME / "kalshi_bot_trades.jsonl", "modeled": _modeled_zero},
    "micro_trader": {"path": RUNTIME / "micro_trader_trades.jsonl", "modeled": _modeled_zero},
    "markets_paper": {"path": TRADING / "markets_paper_trades.jsonl", "modeled": _modeled_zero},
    "polymarket_paper": {"path": TRADING / "polymarket_paper_trades.jsonl", "modeled": _modeled_zero},
}


def _empty_report(lane: str, *, flag_reason: str | None = None) -> dict[str, Any]:
    return {
        "lane": lane,
        "n_live_trades": 0,
        "avg_modeled_slip_bps": None,
        "avg_actual_slip_bps": None,
        "avg_modeled_fee": None,
        "avg_actual_fee": None,
        "cost_drift_ratio": None,
        "fidelity_ok": True,
        "flag_reason": flag_reason,
    }


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if isinstance(row, dict):
                rows.append(row)
    except Exception:
        return rows
    return rows


def _is_live(row: dict[str, Any]) -> bool:
    return str(row.get("mode") or "").strip().lower() == "live"


def _num(row: dict[str, Any], fields: tuple[str, ...]) -> float | None:
    for f in fields:
        v = row.get(f)
        if v is None:
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return None


def _fill_price(row: dict[str, Any]) -> float | None:
    """The REAL fill on this row: exit for a close event, entry for an open one."""
    event = str(row.get("event") or "").strip().lower()
    if event == "close":
        return _num(row, ("exit", "entry"))
    return _num(row, ("entry", "exit"))


def _lane_report(lane: str, cfg: dict[str, Any], k: int, margin: float) -> dict[str, Any]:
    path: Path = cfg["path"]
    modeled_fn: Callable[[], tuple[float, float]] = cfg["modeled"]
    rows = _read_jsonl(path)
    live_rows = [r for r in rows if _is_live(r)]
    n_live = len(live_rows)
    if n_live == 0:
        return _empty_report(lane)

    window = sorted(live_rows, key=lambda r: str(r.get("ts") or ""))[-max(1, k):]
    modeled_slip_bps, modeled_fee_bps = modeled_fn()

    slip_actuals: list[float] = []
    for row in window:
        ref = _num(row, REF_FIELDS)
        fill = _fill_price(row)
        if ref is None or fill is None or ref == 0:
            continue
        slip_actuals.append(abs(fill - ref) / ref * 10000.0)

    fee_actuals: list[float] = []
    fee_notionals: list[float] = []
    for row in window:
        fee = _num(row, FEE_FIELDS)
        if fee is None:
            continue
        fee_actuals.append(fee)
        notional = _num(row, NOTIONAL_FIELDS)
        if notional:
            fee_notionals.append(notional)

    avg_actual_slip_bps = (sum(slip_actuals) / len(slip_actuals)) if slip_actuals else None
    avg_actual_fee = (sum(fee_actuals) / len(fee_actuals)) if fee_actuals else None
    avg_notional = (sum(fee_notionals) / len(fee_notionals)) if fee_notionals else None
    avg_modeled_fee = (avg_notional * modeled_fee_bps / 10000.0) if avg_notional is not None else None

    have_signal = bool(slip_actuals or fee_actuals)
    if not have_signal:
        return {
            "lane": lane,
            "n_live_trades": n_live,
            "avg_modeled_slip_bps": round(modeled_slip_bps, 4),
            "avg_actual_slip_bps": None,
            "avg_modeled_fee": None,
            "avg_actual_fee": None,
            "cost_drift_ratio": None,
            "fidelity_ok": True,
            "flag_reason": (
                f"{lane}: {n_live} live trade(s) but none carry a comparable "
                "quote_price/entry or fee field -- nothing to validate against yet"
            ),
        }

    # Round-trip cost = entry + exit leg, same 2x(fee+slip) convention the bots'
    # own edge gates already use (e.g. kraken_bot._edge_gate) -- keeps the drift
    # comparison on the identical basis the paper proof itself was gated on.
    actual_fee_bps = (avg_actual_fee / avg_notional * 10000.0) if (avg_actual_fee is not None and avg_notional) else 0.0
    actual_slip_component = avg_actual_slip_bps if avg_actual_slip_bps is not None else 0.0
    modeled_round_trip_bps = 2.0 * (modeled_fee_bps + modeled_slip_bps)
    actual_round_trip_bps = 2.0 * (actual_fee_bps + actual_slip_component)

    if modeled_round_trip_bps <= 0:
        fidelity_ok = actual_round_trip_bps <= 0
        cost_drift_ratio = None if fidelity_ok else float("inf")
        flag_reason = None
        if not fidelity_ok:
            flag_reason = (
                f"{lane}: paper model assumes 0bps round-trip cost but real fills over the "
                f"last {len(window)} live trade(s) show {actual_round_trip_bps:.2f}bps -- "
                "the paper proof never accounted for this cost at all, pause scaling"
            )
    else:
        cost_drift_ratio = round(actual_round_trip_bps / modeled_round_trip_bps, 4)
        fidelity_ok = cost_drift_ratio <= margin
        flag_reason = None
        if not fidelity_ok:
            flag_reason = (
                f"{lane}: actual round-trip cost {actual_round_trip_bps:.2f}bps is "
                f"{cost_drift_ratio:.2f}x modeled {modeled_round_trip_bps:.2f}bps "
                f"(> {margin}x over last {len(window)} live trades) -- paper edge is "
                "overstated by real execution, pause scaling"
            )

    return {
        "lane": lane,
        "n_live_trades": n_live,
        "avg_modeled_slip_bps": round(modeled_slip_bps, 4),
        "avg_actual_slip_bps": round(avg_actual_slip_bps, 4) if avg_actual_slip_bps is not None else None,
        "avg_modeled_fee": round(avg_modeled_fee, 6) if avg_modeled_fee is not None else None,
        "avg_actual_fee": round(avg_actual_fee, 6) if avg_actual_fee is not None else None,
        "cost_drift_ratio": cost_drift_ratio,
        "fidelity_ok": fidelity_ok,
        "flag_reason": flag_reason,
    }


def report(lane: str, *, k: int = DEFAULT_WINDOW, margin: float = DEFAULT_MARGIN) -> dict[str, Any]:
    cfg = LANES.get(lane)
    if cfg is None:
        return _empty_report(lane, flag_reason=f"unknown lane {lane!r} -- no ledger registered in fill_fidelity.LANES")
    try:
        return _lane_report(lane, cfg, k, margin)
    except Exception as exc:  # fail-open: a bad ledger row must never crash the report
        return _empty_report(lane, flag_reason=f"{lane}: fill_fidelity error ({exc}) -- treated as no signal")


def report_all(*, k: int = DEFAULT_WINDOW, margin: float = DEFAULT_MARGIN) -> dict[str, dict[str, Any]]:
    return {name: report(name, k=k, margin=margin) for name in LANES}


def _fmt(value: float | None, fmt: str = "{:.2f}") -> str:
    return fmt.format(value) if value is not None else "-"


def _print_table(rows: list[dict[str, Any]]) -> None:
    header = (
        f"{'lane':<17} {'live_n':>7} {'mdl_slip':>9} {'act_slip':>9} "
        f"{'mdl_fee':>9} {'act_fee':>9} {'drift_x':>8}  ok  flag"
    )
    print(header)
    print("-" * len(header))
    for r in rows:
        print(
            f"{r['lane']:<17} {r['n_live_trades']:>7} "
            f"{_fmt(r['avg_modeled_slip_bps']):>9} {_fmt(r['avg_actual_slip_bps']):>9} "
            f"{_fmt(r['avg_modeled_fee'], '{:.4f}'):>9} {_fmt(r['avg_actual_fee'], '{:.4f}'):>9} "
            f"{_fmt(r['cost_drift_ratio']):>8}  {'Y' if r['fidelity_ok'] else 'N':>2}  "
            f"{r['flag_reason'] or ''}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Live-fill vs paper-model fidelity check (read-only, $0).")
    parser.add_argument("--lane", default=None, help="report a single lane (default: all registered lanes)")
    parser.add_argument("--k", type=int, default=DEFAULT_WINDOW, help="rolling window of most-recent live trades")
    parser.add_argument(
        "--margin", type=float, default=DEFAULT_MARGIN,
        help="flag when actual round-trip cost exceeds modeled cost by this multiple",
    )
    args = parser.parse_args(argv)

    if args.lane:
        rows = [report(args.lane, k=args.k, margin=args.margin)]
    else:
        rows = list(report_all(k=args.k, margin=args.margin).values())

    _print_table(rows)
    flagged = [r for r in rows if not r["fidelity_ok"]]
    if flagged:
        print(f"\n{len(flagged)} lane(s) flagged -- live fills are worse than the paper model assumed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
