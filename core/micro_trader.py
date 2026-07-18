"""Micro trader: paper-first trading daemon with hard risk gates.

This module is intentionally boring around money. It can run forever on the
Mini, but it defaults to a local paper ledger and blocks live trading unless a
separate live arm file and environment flag are both present.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core import BASE_DIR


RUNTIME_DIR = BASE_DIR / "data" / "runtime"
LOG_DIR = BASE_DIR / "data" / "logs"
STATE_PATH = RUNTIME_DIR / "micro_trader_state.json"
STATUS_PATH = RUNTIME_DIR / "micro_trader_status.json"
TRADES_PATH = RUNTIME_DIR / "micro_trader_trades.jsonl"
KILL_PATH = RUNTIME_DIR / "micro_trader.kill"
LIVE_ARM_PATH = RUNTIME_DIR / "micro_trader_live_armed.json"
LOG_PATH = LOG_DIR / "micro_trader.jsonl"


DEFAULT_SYMBOLS = ("SPY", "QQQ", "IWM", "DIA", "GLD")
DEFAULT_STARTING_CASH = 100.0
BACKTEST_PATH = RUNTIME_DIR / "micro_trader_backtest.json"


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str))
    tmp.replace(path)


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(json.dumps(payload, sort_keys=True, default=str) + "\n")


def _read_json(path: Path, fallback: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        if path.exists():
            data = json.loads(path.read_text())
            return data if isinstance(data, dict) else (fallback or {})
    except Exception:
        pass
    return fallback or {}


def _today_key() -> str:
    return datetime.now().strftime("%Y-%m-%d")


@dataclass(frozen=True)
class TraderConfig:
    mode: str
    symbols: tuple[str, ...]
    starting_cash: float
    max_position_usd: float
    max_open_positions: int
    max_daily_loss_usd: float
    max_total_loss_usd: float
    max_trades_per_day: int
    take_profit_pct: float
    stop_loss_pct: float
    max_hold_cycles: int
    loop_interval_s: int
    live_allowed: bool

    @classmethod
    def from_env(cls) -> "TraderConfig":
        raw_symbols = os.environ.get("MICRO_TRADER_SYMBOLS", ",".join(DEFAULT_SYMBOLS))
        symbols = tuple(s.strip().upper() for s in raw_symbols.split(",") if s.strip())
        mode = str(os.environ.get("MICRO_TRADER_MODE", "simulated_paper")).strip().lower()
        if mode not in {"simulated_paper", "alpaca_paper", "live"}:
            mode = "simulated_paper"
        return cls(
            mode=mode,
            symbols=symbols or DEFAULT_SYMBOLS,
            starting_cash=float(os.environ.get("MICRO_TRADER_STARTING_CASH", DEFAULT_STARTING_CASH)),
            max_position_usd=float(os.environ.get("MICRO_TRADER_MAX_POSITION_USD", 8.0)),
            max_open_positions=int(os.environ.get("MICRO_TRADER_MAX_OPEN_POSITIONS", 3)),
            max_daily_loss_usd=float(os.environ.get("MICRO_TRADER_MAX_DAILY_LOSS_USD", 2.0)),
            max_total_loss_usd=float(os.environ.get("MICRO_TRADER_MAX_TOTAL_LOSS_USD", 20.0)),
            max_trades_per_day=int(os.environ.get("MICRO_TRADER_MAX_TRADES_PER_DAY", 6)),
            take_profit_pct=float(os.environ.get("MICRO_TRADER_TAKE_PROFIT_PCT", 0.006)),
            stop_loss_pct=float(os.environ.get("MICRO_TRADER_STOP_LOSS_PCT", 0.004)),
            max_hold_cycles=int(os.environ.get("MICRO_TRADER_MAX_HOLD_CYCLES", 48)),
            loop_interval_s=int(os.environ.get("MICRO_TRADER_LOOP_INTERVAL_S", 300)),
            live_allowed=_truthy(os.environ.get("MICRO_TRADER_ALLOW_LIVE")),
        )


def initial_state(config: TraderConfig) -> dict[str, Any]:
    return {
        "created_at": _utc_now(),
        "updated_at": _utc_now(),
        "mode": config.mode,
        "cash": round(config.starting_cash, 4),
        "starting_cash": round(config.starting_cash, 4),
        "positions": {},
        "realized_pnl": 0.0,
        "daily": {
            "date": _today_key(),
            "starting_equity": round(config.starting_cash, 4),
            "trades": 0,
            "realized_pnl": 0.0,
        },
        "prices": {},
        "price_history": {},
        "trades": [],
        "notes": [
            "simulated_paper is active by default",
            "live mode requires MICRO_TRADER_ALLOW_LIVE=1 and micro_trader_live_armed.json",
        ],
    }


def load_state(config: TraderConfig) -> dict[str, Any]:
    state = _read_json(STATE_PATH)
    if not state:
        state = initial_state(config)
    if state.get("daily", {}).get("date") != _today_key():
        equity = calculate_equity(state)
        state["daily"] = {
            "date": _today_key(),
            "starting_equity": round(equity, 4),
            "trades": 0,
            "realized_pnl": 0.0,
        }
    state.setdefault("positions", {})
    state.setdefault("prices", {})
    state.setdefault("price_history", {})
    state.setdefault("trades", [])
    return state


def save_state(state: dict[str, Any]) -> None:
    state["updated_at"] = _utc_now()
    _atomic_write_json(STATE_PATH, state)


def _seed_price(symbol: str) -> float:
    seeds = {
        "SPY": 520.0,
        "QQQ": 450.0,
        "IWM": 205.0,
        "DIA": 390.0,
        "GLD": 225.0,
        "BTCUSD": 64000.0,
        "ETHUSD": 3100.0,
    }
    return seeds.get(symbol.replace("/", ""), 100.0)


def _stable_bucket(symbol: str, bucket: int) -> random.Random:
    digest = hashlib.sha256(f"{symbol}:{bucket}".encode("utf-8")).hexdigest()
    return random.Random(int(digest[:16], 16))


def update_simulated_prices(
    state: dict[str, Any],
    config: TraderConfig,
    *,
    bucket_override: int | None = None,
) -> dict[str, float]:
    """Generate a repeatable but moving synthetic price stream."""
    bucket = int(bucket_override) if bucket_override is not None else int(time.time() // max(30, config.loop_interval_s))
    prices: dict[str, float] = {}
    state_prices = state.setdefault("prices", {})
    histories = state.setdefault("price_history", {})

    for symbol in config.symbols:
        last = float(state_prices.get(symbol, {}).get("price") or _seed_price(symbol))
        last_bucket = int(state_prices.get(symbol, {}).get("bucket") or 0)
        if bucket != last_bucket:
            rng = _stable_bucket(symbol, bucket)
            micro_noise = (rng.random() - 0.5) * 0.004
            slow_wave = math.sin(bucket / 17.0 + len(symbol)) * 0.0008
            price = max(0.01, last * (1.0 + micro_noise + slow_wave))
            state_prices[symbol] = {"price": round(price, 4), "bucket": bucket, "updated_at": _utc_now()}
        else:
            price = float(state_prices.get(symbol, {}).get("price") or last)
        hist = histories.setdefault(symbol, [])
        if not hist or int(hist[-1].get("bucket", -1)) != bucket:
            hist.append({"bucket": bucket, "price": round(price, 4), "ts": _utc_now()})
            del hist[:-160]
        prices[symbol] = round(price, 4)

    return prices


def calculate_equity(state: dict[str, Any]) -> float:
    prices = state.get("prices") or {}
    equity = float(state.get("cash") or 0.0)
    for symbol, pos in (state.get("positions") or {}).items():
        qty = float(pos.get("qty") or 0.0)
        price = float((prices.get(symbol) or {}).get("price") or pos.get("avg_price") or 0.0)
        equity += qty * price
    return round(equity, 4)


def _history_prices(state: dict[str, Any], symbol: str, limit: int = 30) -> list[float]:
    rows = (state.get("price_history") or {}).get(symbol) or []
    out: list[float] = []
    for row in rows[-limit:]:
        try:
            out.append(float(row.get("price")))
        except Exception:
            pass
    return out


def _risk_checks(state: dict[str, Any], config: TraderConfig) -> list[str]:
    reasons: list[str] = []
    if KILL_PATH.exists():
        reasons.append(f"kill switch active: {KILL_PATH}")
    if config.mode == "live":
        if not config.live_allowed:
            reasons.append("live mode blocked: MICRO_TRADER_ALLOW_LIVE is not set")
        if not LIVE_ARM_PATH.exists():
            reasons.append(f"live mode blocked: missing {LIVE_ARM_PATH}")
    daily = state.get("daily") or {}
    equity = calculate_equity(state)
    starting_equity = float(daily.get("starting_equity") or config.starting_cash)
    daily_loss = max(0.0, starting_equity - equity)
    total_loss = max(0.0, config.starting_cash - equity)
    if daily_loss >= config.max_daily_loss_usd:
        reasons.append(f"daily loss gate hit: ${daily_loss:.2f} >= ${config.max_daily_loss_usd:.2f}")
    if total_loss >= config.max_total_loss_usd:
        reasons.append(f"total loss gate hit: ${total_loss:.2f} >= ${config.max_total_loss_usd:.2f}")
    if int(daily.get("trades") or 0) >= config.max_trades_per_day:
        reasons.append(f"daily trade gate hit: {daily.get('trades')} >= {config.max_trades_per_day}")
    return reasons


def _open_positions(state: dict[str, Any]) -> int:
    return sum(1 for pos in (state.get("positions") or {}).values() if float(pos.get("qty") or 0.0) > 0)


def _buy(state: dict[str, Any], symbol: str, price: float, config: TraderConfig, reason: str) -> dict[str, Any]:
    cash = float(state.get("cash") or 0.0)
    allocation = min(config.max_position_usd, cash)
    if allocation < 1.0:
        return {"action": "hold", "symbol": symbol, "reason": "cash below $1.00"}
    qty = round(allocation / price, 8)
    state["cash"] = round(cash - (qty * price), 4)
    state.setdefault("positions", {})[symbol] = {
        "qty": qty,
        "avg_price": round(price, 4),
        "opened_at": _utc_now(),
        "opened_cycle": len(_history_prices(state, symbol, 9999)),
        "reason": reason,
    }
    trade = {
        "ts": _utc_now(),
        "side": "buy",
        "symbol": symbol,
        "qty": qty,
        "price": round(price, 4),
        "notional": round(qty * price, 4),
        "reason": reason,
        "mode": config.mode,
    }
    _record_trade(state, trade)
    return {"action": "buy", "symbol": symbol, "trade": trade}


def _sell(state: dict[str, Any], symbol: str, price: float, config: TraderConfig, reason: str) -> dict[str, Any]:
    pos = (state.get("positions") or {}).get(symbol)
    if not pos:
        return {"action": "hold", "symbol": symbol, "reason": "no position"}
    qty = float(pos.get("qty") or 0.0)
    avg = float(pos.get("avg_price") or price)
    proceeds = qty * price
    pnl = proceeds - (qty * avg)
    state["cash"] = round(float(state.get("cash") or 0.0) + proceeds, 4)
    state["positions"].pop(symbol, None)
    state["realized_pnl"] = round(float(state.get("realized_pnl") or 0.0) + pnl, 4)
    state.setdefault("daily", {})["realized_pnl"] = round(float(state.get("daily", {}).get("realized_pnl") or 0.0) + pnl, 4)
    trade = {
        "ts": _utc_now(),
        "side": "sell",
        "symbol": symbol,
        "qty": round(qty, 8),
        "price": round(price, 4),
        "notional": round(proceeds, 4),
        "pnl": round(pnl, 4),
        "return_pct": round((price / avg - 1.0) * 100, 4),
        "reason": reason,
        "mode": config.mode,
    }
    _record_trade(state, trade)
    return {"action": "sell", "symbol": symbol, "trade": trade}


def _record_trade(state: dict[str, Any], trade: dict[str, Any]) -> None:
    state.setdefault("trades", []).append(trade)
    del state["trades"][:-200]
    state.setdefault("daily", {})["trades"] = int(state.get("daily", {}).get("trades") or 0) + 1
    _append_jsonl(TRADES_PATH, trade)


def choose_action(state: dict[str, Any], config: TraderConfig, prices: dict[str, float]) -> dict[str, Any]:
    blocks = _risk_checks(state, config)
    if blocks:
        return {"action": "blocked", "reasons": blocks}

    # Exits first. Protect tiny capital before hunting.
    for symbol, pos in list((state.get("positions") or {}).items()):
        price = prices.get(symbol)
        if not price:
            continue
        avg = float(pos.get("avg_price") or price)
        ret = price / avg - 1.0
        current_cycle = len(_history_prices(state, symbol, 9999))
        age = current_cycle - int(pos.get("opened_cycle") or current_cycle)
        if ret >= config.take_profit_pct:
            return _sell(state, symbol, price, config, f"take profit at {ret * 100:.2f}%")
        if ret <= -config.stop_loss_pct:
            return _sell(state, symbol, price, config, f"stop loss at {ret * 100:.2f}%")
        if age >= config.max_hold_cycles:
            return _sell(state, symbol, price, config, f"max hold reached after {age} cycles")

    if _open_positions(state) >= config.max_open_positions:
        return {"action": "hold", "reason": "max open positions reached"}

    best: tuple[float, str, str] | None = None
    for symbol in config.symbols:
        if symbol in (state.get("positions") or {}):
            continue
        hist = _history_prices(state, symbol, 30)
        if len(hist) < 20:
            continue
        sma5 = sum(hist[-5:]) / 5
        sma20 = sum(hist[-20:]) / 20
        ret3 = hist[-1] / hist[-4] - 1.0 if len(hist) >= 4 and hist[-4] else 0.0
        trend_score = (sma5 / sma20 - 1.0) + ret3
        dip_score = (sma20 / hist[-1] - 1.0) * 0.4
        score = max(trend_score, dip_score)
        if score > 0.0018:
            reason = "momentum scalp" if trend_score >= dip_score else "small mean-reversion dip"
            if best is None or score > best[0]:
                best = (score, symbol, reason)

    if best is None:
        return {"action": "hold", "reason": "no signal above threshold"}
    _, symbol, reason = best
    return _buy(state, symbol, prices[symbol], config, reason)


def build_status(state: dict[str, Any], config: TraderConfig, decision: dict[str, Any] | None = None) -> dict[str, Any]:
    equity = calculate_equity(state)
    starting = float(state.get("starting_cash") or config.starting_cash)
    daily = state.get("daily") or {}
    status = {
        "ok": True,
        "generated_at": _utc_now(),
        "mode": config.mode,
        "live_trading_possible": bool(config.mode == "live" and config.live_allowed and LIVE_ARM_PATH.exists()),
        "kill_switch": KILL_PATH.exists(),
        "cash": round(float(state.get("cash") or 0.0), 4),
        "equity": equity,
        "starting_cash": round(starting, 4),
        "total_pnl": round(equity - starting, 4),
        "realized_pnl": round(float(state.get("realized_pnl") or 0.0), 4),
        "daily": daily,
        "positions": state.get("positions") or {},
        "prices": state.get("prices") or {},
        "risk": {
            "max_position_usd": config.max_position_usd,
            "max_daily_loss_usd": config.max_daily_loss_usd,
            "max_total_loss_usd": config.max_total_loss_usd,
            "max_trades_per_day": config.max_trades_per_day,
        },
        "decision": decision or {},
        "state_path": str(STATE_PATH),
        "trades_path": str(TRADES_PATH),
    }
    _atomic_write_json(STATUS_PATH, status)
    return status


def status_text() -> str:
    config = TraderConfig.from_env()
    state = load_state(config)
    status = build_status(state, config, state.get("last_decision") or {})
    positions = status.get("positions") or {}
    pos_lines = []
    for symbol, pos in positions.items():
        qty = float(pos.get("qty") or 0.0)
        avg = float(pos.get("avg_price") or 0.0)
        current = float((status.get("prices") or {}).get(symbol, {}).get("price") or avg)
        pnl = qty * (current - avg)
        pos_lines.append(f"- {symbol}: {qty:.6f} @ ${avg:.2f}, now ${current:.2f}, P/L ${pnl:.2f}")
    if not pos_lines:
        pos_lines.append("- none")
    decision = status.get("decision") or {}
    mode_note = "LIVE DISABLED" if not status.get("live_trading_possible") else "LIVE ARMED"
    return (
        "Micro Trader Status\n"
        f"- Mode: {status['mode']} ({mode_note})\n"
        f"- Equity: ${status['equity']:.2f} from ${status['starting_cash']:.2f}\n"
        f"- Total P/L: ${status['total_pnl']:.2f}; realized ${status['realized_pnl']:.2f}\n"
        f"- Cash: ${status['cash']:.2f}\n"
        f"- Daily trades: {status.get('daily', {}).get('trades', 0)} / {status.get('risk', {}).get('max_trades_per_day')}\n"
        f"- Kill switch: {'ON' if status.get('kill_switch') else 'off'}\n"
        "- Positions:\n"
        + "\n".join(pos_lines)
        + "\n"
        f"- Last decision: {decision.get('action', 'n/a')} {decision.get('reason', '')}".rstrip()
    )


def run_backtest(days: int = 5, *, cycles_per_day: int = 78) -> dict[str, Any]:
    """Run a compressed synthetic market simulation without touching live state.

    cycles_per_day defaults to 78, roughly one 5-minute cycle across a regular
    U.S. trading session. The synthetic stream is deterministic enough to make
    changes comparable across code edits, but still noisy enough to stress the
    gates.
    """
    config = TraderConfig.from_env()
    config = TraderConfig(
        mode="backtest",
        symbols=config.symbols,
        starting_cash=config.starting_cash,
        max_position_usd=config.max_position_usd,
        max_open_positions=config.max_open_positions,
        max_daily_loss_usd=config.max_daily_loss_usd,
        max_total_loss_usd=config.max_total_loss_usd,
        max_trades_per_day=config.max_trades_per_day,
        take_profit_pct=config.take_profit_pct,
        stop_loss_pct=config.stop_loss_pct,
        max_hold_cycles=config.max_hold_cycles,
        loop_interval_s=config.loop_interval_s,
        live_allowed=False,
    )
    state = initial_state(config)
    days = max(1, min(int(days or 1), 60))
    cycles_per_day = max(12, min(int(cycles_per_day or 78), 390))
    base_bucket = 1_900_000
    day_summaries: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []

    for day in range(days):
        day_key = f"sim-day-{day + 1}"
        state["daily"] = {
            "date": day_key,
            "starting_equity": calculate_equity(state),
            "trades": 0,
            "realized_pnl": 0.0,
        }
        for cycle in range(cycles_per_day):
            prices = update_simulated_prices(state, config, bucket_override=base_bucket + day * cycles_per_day + cycle)
            decision = choose_action(state, config, prices)
            decisions.append({
                "day": day + 1,
                "cycle": cycle + 1,
                "action": decision.get("action"),
                "symbol": decision.get("symbol"),
                "reason": decision.get("reason") or "; ".join(decision.get("reasons") or []),
            })
        equity = calculate_equity(state)
        day_summaries.append({
            "day": day + 1,
            "equity": equity,
            "cash": round(float(state.get("cash") or 0.0), 4),
            "daily_trades": int(state.get("daily", {}).get("trades") or 0),
            "daily_realized_pnl": round(float(state.get("daily", {}).get("realized_pnl") or 0.0), 4),
            "open_positions": len(state.get("positions") or {}),
        })

    # Liquidate at final simulated prices so the proof run reports clean P/L.
    final_prices = {symbol: float((state.get("prices") or {}).get(symbol, {}).get("price") or 0.0) for symbol in config.symbols}
    for symbol in list((state.get("positions") or {}).keys()):
        price = final_prices.get(symbol) or float(state["positions"][symbol].get("avg_price") or 0.0)
        if price > 0:
            _sell(state, symbol, price, config, "backtest final liquidation")

    final_equity = calculate_equity(state)
    trades = state.get("trades") or []
    wins = [t for t in trades if t.get("side") == "sell" and float(t.get("pnl") or 0.0) > 0]
    losses = [t for t in trades if t.get("side") == "sell" and float(t.get("pnl") or 0.0) < 0]
    payload = {
        "generated_at": _utc_now(),
        "days": days,
        "cycles_per_day": cycles_per_day,
        "starting_cash": config.starting_cash,
        "final_equity": final_equity,
        "net_pnl": round(final_equity - config.starting_cash, 4),
        "return_pct": round(((final_equity / config.starting_cash) - 1.0) * 100.0, 4),
        "trade_count": len(trades),
        "sell_count": len(wins) + len(losses),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round((len(wins) / max(1, len(wins) + len(losses))) * 100.0, 2),
        "day_summaries": day_summaries,
        "risk": asdict(config),
        "sample_decisions": decisions[-40:],
        "last_trades": trades[-40:],
        "important": "Synthetic backtest only. This proves plumbing and guardrails, not a durable market edge.",
    }
    _atomic_write_json(BACKTEST_PATH, payload)
    try:
        from core.test_hustle import record_micro_trader_snapshot

        record_micro_trader_snapshot(build_status(load_state(TraderConfig.from_env()), TraderConfig.from_env()), payload)
    except Exception:
        pass
    return payload


def backtest_text(days: int = 5) -> str:
    result = run_backtest(days)
    return (
        "Micro Trader Backtest\n"
        f"- Window: {result['days']} simulated trading day(s), {result['cycles_per_day']} cycles/day\n"
        f"- Starting cash: ${result['starting_cash']:.2f}\n"
        f"- Final equity: ${result['final_equity']:.2f}\n"
        f"- Net P/L: ${result['net_pnl']:.2f} ({result['return_pct']:.2f}%)\n"
        f"- Trades: {result['trade_count']}; closes: {result['sell_count']}; win rate: {result['win_rate_pct']:.1f}%\n"
        f"- Proof artifact: {BACKTEST_PATH}\n"
        "- Note: synthetic simulation proves the daemon/risk plumbing. Real edge still needs forward paper logs."
    )


def run_once() -> dict[str, Any]:
    config = TraderConfig.from_env()
    state = load_state(config)
    prices = update_simulated_prices(state, config)
    decision = choose_action(state, config, prices)
    state["last_decision"] = decision
    save_state(state)
    status = build_status(state, config, decision)
    _append_jsonl(LOG_PATH, {"ts": _utc_now(), "event": "cycle", "status": status})
    try:
        from core.test_hustle import record_micro_trader_snapshot

        backtest = _read_json(BACKTEST_PATH, {})
        record_micro_trader_snapshot(status, backtest if backtest else None)
    except Exception:
        pass
    return status


def run_loop() -> None:
    config = TraderConfig.from_env()
    while True:
        run_once()
        time.sleep(max(30, config.loop_interval_s))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the paper-first micro trader.")
    parser.add_argument("--once", action="store_true", help="run one cycle and exit")
    parser.add_argument("--status", action="store_true", help="print current status")
    parser.add_argument("--backtest-days", type=int, default=0, help="run a compressed synthetic backtest")
    parser.add_argument("--kill", action="store_true", help="activate the kill switch")
    parser.add_argument("--resume", action="store_true", help="remove the kill switch")
    args = parser.parse_args(argv)

    if args.kill:
        KILL_PATH.parent.mkdir(parents=True, exist_ok=True)
        KILL_PATH.write_text(json.dumps({"activated_at": _utc_now(), "reason": "manual kill"}))
        print(f"kill switch active: {KILL_PATH}")
        return 0
    if args.resume:
        if KILL_PATH.exists():
            KILL_PATH.unlink()
        print("kill switch cleared")
        return 0
    if args.status:
        print(status_text())
        return 0
    if args.backtest_days:
        print(backtest_text(args.backtest_days))
        return 0
    status = run_once() if args.once else None
    if args.once:
        print(json.dumps(status, indent=2, sort_keys=True, default=str))
        return 0
    run_loop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
