"""Alpaca equities/ETF bot with live trading double-locked.

Three modes:
  sim   (default) internal simulator on REAL prices from core.market_signals.
        No network order calls, no API keys needed. $0.
  paper routes orders to Alpaca's REAL paper-trading endpoint
        (https://paper-api.alpaca.markets). Free, high-fidelity: real order
        matching, real fills, fake money. Requires ALPACA_API_KEY_ID +
        ALPACA_API_SECRET_KEY (Alpaca paper keys are free to generate).
  live  real money via https://api.alpaca.markets. Intentionally blocked
        unless ALL of these are true: ALPACA_MODE=live AND
        ALPACA_ALLOW_LIVE=1 AND data/runtime/alpaca_live_armed.json exists
        AND ALPACA_API_KEY_ID + ALPACA_API_SECRET_KEY are set. Any one
        missing gate = the order is refused, no exceptions.

$0 to operate: stdlib only (urllib/json/time/os/uuid/math/dataclasses/
datetime/pathlib) + core.market_signals for real prices. No pip installs,
no alpaca-py SDK, no paid data. Alpaca commissions are $0; this bot models
~5bps slippage in sim so paper/live P&L expectations stay honest.

Universe is liquid ETFs only (SPY/QQQ/XLK/XLF/XLE/XLY, intersected with
core.market_signals.EQUITY_UNIVERSE) — explicitly no single names, no OTC,
no penny stocks. Entries only fire during US regular market hours
(ET 09:30-16:00, Mon-Fri).

Strategy (evidence-based, research-calibrated):
  regime filter    long entries only fire when SPY is above its 200-day MA
                    (risk-on per core.market_signals structure regime);
                    risk-off means cash, no exceptions.
  dual momentum     sector ETFs + SPY/QQQ are ranked by ~12-week (60
                    trading-day) return (relative momentum); a name is only
                    held if that 60d return is positive (absolute momentum
                    vs a ~0 T-bill proxy). Top 1-2 held, rebalanced on an
                    ~monthly schedule to avoid churn.
  overnight sleeve  optional (ALPACA_OVERNIGHT=1), off by default: buy
                    SPY/QQQ near the close when the 20dma is sloping up.
  sizing            core.trade_risk.position_notional (quarter-Kelly),
                    capped by ALPACA_MAX_NOTIONAL_USD as a ceiling.
  circuit breakers  core.trade_risk.circuit_breaker (daily -5%, peak -18%
                    drawdown) blocks new entries; a round-trip governor
                    caps new entries per day even at $0 commission, because
                    overtrading — not fees — is what kills retail accounts.

The PDT (pattern-day-trading) round-trip cap was retired June 2026, so this
bot has no day-trade limit to respect; it stays a deliberately low-turnover
swing/position strategy anyway because the evidence favors it, not because
a rule forces it. It is not a day-trading bot and makes no return guarantees.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import time
import urllib.request
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core import BASE_DIR
from core import market_signals
from core import trade_risk


RUNTIME = BASE_DIR / "data" / "runtime"
TRADING = BASE_DIR / "data" / "trading"
STATE_PATH = TRADING / "alpaca_bot_state.json"
STATUS_PATH = TRADING / "alpaca_bot_status.json"
TRADES_PATH = TRADING / "alpaca_bot_trades.jsonl"
KILL_PATH = RUNTIME / "alpaca_bot.kill"
LIVE_ARM_PATH = RUNTIME / "alpaca_live_armed.json"

ALPACA_LIVE_BASE = "https://api.alpaca.markets"
ALPACA_PAPER_BASE = "https://paper-api.alpaca.markets"

# Liquid-ETF-only universe: broad index + sector ETFs, no single names.
STRATEGY_UNIVERSE = ("SPY", "QQQ", "XLK", "XLF", "XLE", "XLY")
DEFAULT_SYMBOLS = STRATEGY_UNIVERSE
SLIPPAGE_BPS = 5.0  # ~5bps modeled slippage in sim so paper P&L stays honest


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today() -> str:
    return datetime.now().date().isoformat()


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(json.dumps(row, sort_keys=True, default=str) + "\n")


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True, default=str))
    tmp.replace(path)


def _read_json(path: Path, fallback: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        if path.exists():
            data = json.loads(path.read_text())
            return data if isinstance(data, dict) else (fallback or {})
    except Exception:
        pass
    return fallback or {}


def _post_json(url: str, payload: dict[str, Any], headers: dict[str, str], timeout: float = 12) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    merged = {"Accept": "application/json", "Content-Type": "application/json"}
    merged.update(headers)
    req = urllib.request.Request(url, data=body, headers=merged, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as response:
        raw = response.read().decode("utf-8", errors="replace")
        return json.loads(raw) if raw else {}


def _get_json(url: str, headers: dict[str, str], timeout: float = 12) -> dict[str, Any]:
    merged = {"Accept": "application/json"}
    merged.update(headers)
    req = urllib.request.Request(url, headers=merged, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as response:
        raw = response.read().decode("utf-8", errors="replace")
        return json.loads(raw) if raw else {}


def _delete_json(url: str, headers: dict[str, str], timeout: float = 12) -> dict[str, Any]:
    merged = {"Accept": "application/json"}
    merged.update(headers)
    req = urllib.request.Request(url, headers=merged, method="DELETE")
    with urllib.request.urlopen(req, timeout=timeout) as response:
        raw = response.read().decode("utf-8", errors="replace")
        return json.loads(raw) if raw else {}


def is_market_open(dt: datetime | None = None) -> bool:
    """Simple US regular-hours check: ET 09:30-16:00, Mon-Fri. No holiday calendar."""
    now = dt or datetime.now(timezone.utc)
    # Fixed UTC-5 offset approximation (ET); good enough for a gating heuristic,
    # not for precision timing. DST means this is off by an hour part of the year.
    et_hour = (now.hour - 5) % 24
    et = now.replace(hour=et_hour)
    if et.weekday() >= 5:
        return False
    minutes = et_hour * 60 + now.minute
    return 9 * 60 + 30 <= minutes < 16 * 60


@dataclass(frozen=True)
class AlpacaConfig:
    mode: str
    symbols: tuple[str, ...]
    starting_cash: float
    max_notional_usd: float
    max_open_positions: int
    max_daily_loss: float
    max_total_loss: float
    max_daily_trades: int
    stop_loss_pct: float
    take_profit_pct: float
    loop_interval_s: int
    live_allowed: bool
    api_key_id: str
    api_secret_key: str
    # --- dual-momentum / regime / governor knobs (signal-layer only) ---
    rebalance_days: int
    momentum_lookback_days: int
    momentum_top_n: int
    max_round_trips_per_day: int
    kelly_win_prob_default: float
    kelly_payoff_default: float
    overnight_enabled: bool
    overnight_symbols: tuple[str, ...]
    overnight_window_min: int

    @classmethod
    def from_env(cls) -> "AlpacaConfig":
        # Liquid-ETF-only: intersect the requested universe with market_signals'
        # pricing coverage AND the fixed strategy universe — no single names,
        # regardless of what ALPACA_SYMBOLS is set to.
        allowed = set(market_signals.EQUITY_UNIVERSE) & set(STRATEGY_UNIVERSE)
        raw_symbols = os.environ.get("ALPACA_SYMBOLS", ",".join(STRATEGY_UNIVERSE))
        symbols = tuple(
            s.strip().upper() for s in raw_symbols.split(",")
            if s.strip() and s.strip().upper() in allowed
        )
        mode = str(os.environ.get("ALPACA_MODE", "sim")).strip().lower()
        if mode not in {"sim", "paper", "live"}:
            mode = "sim"
        raw_overnight = os.environ.get("ALPACA_OVERNIGHT_SYMBOLS", "SPY,QQQ")
        overnight_symbols = tuple(
            s.strip().upper() for s in raw_overnight.split(",")
            if s.strip() and s.strip().upper() in allowed
        )
        return cls(
            mode=mode,
            symbols=symbols or tuple(s for s in STRATEGY_UNIVERSE if s in allowed),
            starting_cash=float(os.environ.get("ALPACA_PAPER_STARTING_CASH", 25.0)),
            max_notional_usd=float(os.environ.get("ALPACA_MAX_NOTIONAL_USD", 8.0)),
            max_open_positions=int(os.environ.get("ALPACA_MAX_OPEN_POSITIONS", 3)),
            max_daily_loss=float(os.environ.get("ALPACA_MAX_DAILY_LOSS", 5.0)),
            max_total_loss=float(os.environ.get("ALPACA_MAX_TOTAL_LOSS", 25.0)),
            max_daily_trades=int(os.environ.get("ALPACA_MAX_DAILY_TRADES", 6)),
            stop_loss_pct=float(os.environ.get("ALPACA_STOP_LOSS_PCT", 0.03)),
            take_profit_pct=float(os.environ.get("ALPACA_TAKE_PROFIT_PCT", 0.05)),
            loop_interval_s=int(os.environ.get("ALPACA_LOOP_INTERVAL_S", 900)),
            live_allowed=_truthy(os.environ.get("ALPACA_ALLOW_LIVE")),
            api_key_id=str(os.environ.get("ALPACA_API_KEY_ID", "")).strip(),
            api_secret_key=str(os.environ.get("ALPACA_API_SECRET_KEY", "")).strip(),
            rebalance_days=int(os.environ.get("ALPACA_REBALANCE_DAYS", 30)),
            momentum_lookback_days=int(os.environ.get("ALPACA_MOMENTUM_LOOKBACK_DAYS", 60)),
            momentum_top_n=max(1, min(2, int(os.environ.get("ALPACA_MOMENTUM_TOP_N", 2)))),
            max_round_trips_per_day=max(0, int(os.environ.get("ALPACA_MAX_ROUND_TRIPS_PER_DAY", 1))),
            kelly_win_prob_default=float(os.environ.get("ALPACA_KELLY_WIN_PROB", 0.52)),
            kelly_payoff_default=float(os.environ.get("ALPACA_KELLY_PAYOFF_RATIO", 1.3)),
            overnight_enabled=_truthy(os.environ.get("ALPACA_OVERNIGHT", "0")),
            overnight_symbols=overnight_symbols or ("SPY", "QQQ"),
            overnight_window_min=int(os.environ.get("ALPACA_OVERNIGHT_WINDOW_MIN", 15)),
        )


def initial_state(config: AlpacaConfig) -> dict[str, Any]:
    return {
        "created_at": _now(),
        "updated_at": _now(),
        "mode": config.mode,
        "cash": round(config.starting_cash, 4),
        "starting_cash": round(config.starting_cash, 4),
        "peak_equity": round(config.starting_cash, 4),
        "positions": {},
        "prices": {},
        "realized_pnl": 0.0,
        "daily": {
            "date": _today(), "starting_equity": config.starting_cash,
            "trades": 0, "realized_pnl": 0.0, "round_trips_opened": 0,
        },
        "strategy": {"last_rebalance": None, "targets": [], "ranked": []},
        "notes": ["sim/paper only until ALPACA_MODE=live, ALPACA_ALLOW_LIVE=1 and alpaca_live_armed.json are all set"],
    }


def load_state(config: AlpacaConfig) -> dict[str, Any]:
    state = _read_json(STATE_PATH)
    if not state:
        state = initial_state(config)
    if (state.get("daily") or {}).get("date") != _today():
        state["daily"] = {
            "date": _today(), "starting_equity": equity(state),
            "trades": 0, "realized_pnl": 0.0, "round_trips_opened": 0,
        }
    state.setdefault("positions", {})
    state.setdefault("prices", {})
    state.setdefault("strategy", {"last_rebalance": None, "targets": [], "ranked": []})
    state.setdefault("peak_equity", state.get("starting_cash") or config.starting_cash)
    return state


def save_state(state: dict[str, Any]) -> None:
    state["updated_at"] = _now()
    _write_json(STATE_PATH, state)


def update_prices(state: dict[str, Any], config: AlpacaConfig) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Pull real prices/features/closes for the configured (ETF-only) universe via
    market_signals, plus the raw MarketState (for structure/regime). Also prices any
    symbol still held from a prior strategy version so it can be marked-to-market and
    exited even if it has since fallen outside the strategy universe (fail-open)."""
    market = market_signals.ingest()
    prices = market.get("prices") or {}
    features = market.get("features") or {}
    want = set(config.symbols) | set((state.get("positions") or {}).keys())
    out: dict[str, dict[str, Any]] = {}
    for sym in want:
        bar = prices.get(sym)
        if not bar or not bar.get("price"):
            continue
        out[sym] = {
            "price": float(bar["price"]),
            "features": features.get(sym) or {},
            "closes": [float(c) for c in (bar.get("closes") or [])],
            "source": bar.get("source", "yahoo"),
        }
        state.setdefault("prices", {})[sym] = {"price": out[sym]["price"], "source": out[sym]["source"], "updated_at": _now()}
    return out, market


def equity(state: dict[str, Any]) -> float:
    total = float(state.get("cash") or 0.0)
    prices = state.get("prices") or {}
    for sym, pos in (state.get("positions") or {}).items():
        current = float((prices.get(sym) or {}).get("price") or pos.get("entry") or 0.0)
        total += _position_value(pos, current)
    return round(total, 4)


def _position_value(pos: dict[str, Any], current: float) -> float:
    notional = float(pos.get("notional_usd") or 0.0)
    entry = float(pos.get("entry") or current)
    side = 1.0 if pos.get("side") == "long" else -1.0
    pnl = notional * side * ((current / entry) - 1.0)
    return notional + pnl


def _risk_blocks(state: dict[str, Any], config: AlpacaConfig) -> list[str]:
    blocks: list[str] = []
    if KILL_PATH.exists():
        blocks.append(f"kill switch active: {KILL_PATH}")
    daily = state.get("daily") or {}
    if int(daily.get("trades") or 0) >= config.max_daily_trades:
        blocks.append(f"daily trade cap hit: {daily.get('trades')} >= {config.max_daily_trades}")
    current_equity = equity(state)
    daily_loss = max(0.0, float(daily.get("starting_equity") or config.starting_cash) - current_equity)
    total_loss = max(0.0, config.starting_cash - current_equity)
    if daily_loss >= config.max_daily_loss:
        blocks.append(f"daily loss gate hit: ${daily_loss:.2f} >= ${config.max_daily_loss:.2f}")
    if total_loss >= config.max_total_loss:
        blocks.append(f"total loss gate hit: ${total_loss:.2f} >= ${config.max_total_loss:.2f}")
    # research-calibrated circuit breakers (core.trade_risk): daily -5% / peak -18%
    blocks.extend(trade_risk.circuit_breaker(
        equity=current_equity,
        day_start_equity=float(daily.get("starting_equity") or config.starting_cash),
        peak_equity=float(state.get("peak_equity") or config.starting_cash),
    ))
    round_trips_opened = int(daily.get("round_trips_opened") or 0)
    if round_trips_opened >= config.max_round_trips_per_day:
        blocks.append(
            f"round-trip governor: {round_trips_opened} new entr{'y' if round_trips_opened == 1 else 'ies'} "
            f"today >= {config.max_round_trips_per_day} — $0 commission doesn't mean overtrading is free"
        )
    if len(state.get("positions") or {}) >= config.max_open_positions:
        blocks.append(f"open position cap hit: {len(state.get('positions') or {})} >= {config.max_open_positions}")
    if not is_market_open():
        blocks.append("market closed: entries only during US regular hours (ET 09:30-16:00 Mon-Fri)")
    if config.mode == "live":
        if not config.live_allowed:
            blocks.append("live blocked: ALPACA_ALLOW_LIVE is not set")
        if not LIVE_ARM_PATH.exists():
            blocks.append(f"live blocked: missing {LIVE_ARM_PATH}")
        if not config.api_key_id or not config.api_secret_key:
            blocks.append("live blocked: ALPACA_API_KEY_ID/ALPACA_API_SECRET_KEY not configured")
    if config.mode == "paper":
        if not config.api_key_id or not config.api_secret_key:
            blocks.append("paper order blocked: ALPACA_API_KEY_ID/ALPACA_API_SECRET_KEY not configured (falling back to internal sim)")
    return blocks


def _regime(market: dict[str, Any]) -> tuple[bool, str]:
    """Risk-on iff SPY > its 200dma, per core.market_signals structure regime."""
    structure = market.get("structure") or {}
    features = market.get("features") or {}
    spy_feats = features.get("SPY") or {}
    if "spy_above_200dma" in structure:
        risk_on = bool(structure.get("spy_above_200dma"))
    else:
        risk_on = bool(spy_feats.get("above_200dma"))
    return risk_on, str(structure.get("regime") or "unknown")


def _dual_momentum_rank(prices: dict[str, dict[str, Any]], config: AlpacaConfig) -> list[dict[str, Any]]:
    """Rank the configured ETF universe by ~12-week (60 trading-day) return."""
    lookback = max(5, config.momentum_lookback_days)
    ranked: list[dict[str, Any]] = []
    for sym in config.symbols:
        row = prices.get(sym)
        if not row:
            continue
        closes = row.get("closes") or []
        if len(closes) <= lookback:
            continue
        base = closes[-1 - lookback]
        if base <= 0:
            continue
        mom60 = (closes[-1] / base) - 1.0
        ranked.append({"symbol": sym, "price": row["price"], "mom60": round(mom60, 6)})
    ranked.sort(key=lambda r: r["mom60"], reverse=True)
    return ranked


def _should_rebalance(strat: dict[str, Any], config: AlpacaConfig) -> bool:
    last = strat.get("last_rebalance")
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
    except Exception:
        return True
    elapsed_days = (datetime.now(timezone.utc) - last_dt).total_seconds() / 86400.0
    return elapsed_days >= config.rebalance_days


def _sma20_slope_up(closes: list[float]) -> bool:
    """Overnight-sleeve trigger: is the 20dma sloping up day-over-day?"""
    if len(closes) < 21:
        return False
    sma_now = sum(closes[-20:]) / 20.0
    sma_prev = sum(closes[-21:-1]) / 20.0
    return sma_now > sma_prev


def _near_close(window_min: int, dt: datetime | None = None) -> bool:
    now = dt or datetime.now(timezone.utc)
    if not is_market_open(now):
        return False
    et_hour = (now.hour - 5) % 24
    minutes = et_hour * 60 + now.minute
    return (16 * 60) - minutes <= max(0, window_min)


def _estimated_edge(config: AlpacaConfig) -> tuple[float, float]:
    """Win-prob/payoff-ratio for Kelly sizing, learned from this bot's own closed
    trades once there's a real sample (>=5 closes); conservative env-configurable
    defaults otherwise. Fail-open on any parse error — never blocks sizing."""
    try:
        rows = []
        if TRADES_PATH.exists():
            for line in TRADES_PATH.read_text().splitlines():
                row = json.loads(line)
                if row.get("event") == "close":
                    rows.append(row)
        if len(rows) >= 5:
            wins = [r for r in rows if float(r.get("pnl") or 0) > 0]
            losses = [r for r in rows if float(r.get("pnl") or 0) <= 0]
            win_prob = len(wins) / len(rows)
            avg_win = (sum(float(r.get("pnl") or 0) for r in wins) / len(wins)) if wins else 0.0
            avg_loss = abs(sum(float(r.get("pnl") or 0) for r in losses) / len(losses)) if losses else 0.0
            payoff = (avg_win / avg_loss) if avg_loss > 0 else config.kelly_payoff_default
            return max(0.0, min(1.0, win_prob)), max(0.1, payoff)
    except Exception:
        pass
    return config.kelly_win_prob_default, config.kelly_payoff_default


def _sized_notional(state: dict[str, Any], config: AlpacaConfig) -> float:
    """Quarter-Kelly dollar sizing (core.trade_risk.position_notional), capped by
    ALPACA_MAX_NOTIONAL_USD and available cash as ceilings."""
    win_prob, payoff_ratio = _estimated_edge(config)
    eq = equity(state)
    try:
        notional = trade_risk.position_notional(
            eq, win_prob=win_prob, payoff_ratio=payoff_ratio,
            stop_pct=max(1e-4, config.stop_loss_pct),
        )
    except Exception:
        notional = config.max_notional_usd
    return round(min(notional, config.max_notional_usd, float(state.get("cash") or 0.0)), 4)


def _enter_position(state: dict[str, Any], config: AlpacaConfig, signal: dict[str, Any], notional: float) -> dict[str, Any]:
    if config.mode in {"paper", "live"} and config.api_key_id and config.api_secret_key:
        broker = _alpaca_order(config, signal, notional)
        if broker.get("status") == "blocked":
            return broker
        # Record the ACTUAL fill, not the modeled price: poll the order we just
        # placed for filled_avg_price/filled_qty (falls back to modeled if the
        # broker hasn't reported a fill yet -- never blocks the entry on this).
        order_id = broker.get("id") if isinstance(broker, dict) else None
        fill = _alpaca_order_fill(config, order_id) if order_id else {}
        fill_price = fill.get("filled_avg_price")
        fill_qty = fill.get("filled_qty")
        actual_notional = (fill_qty * fill_price) if (fill_price and fill_qty) else None
        result = _sim_open(
            state, config, signal, notional_override=notional,
            fill_price_override=fill_price, actual_notional_override=actual_notional,
        )
        return result | {"broker_response": broker, "broker_order_id": order_id}
    return _sim_open(state, config, signal, notional_override=notional)


def _sim_open(
    state: dict[str, Any], config: AlpacaConfig, signal: dict[str, Any],
    notional_override: float | None = None, fill_price_override: float | None = None,
    actual_notional_override: float | None = None,
) -> dict[str, Any]:
    cash = float(state.get("cash") or 0.0)
    base_notional = notional_override if notional_override is not None else config.max_notional_usd
    notional = min(base_notional, config.max_notional_usd, cash)
    if notional < 1:
        return {"status": "blocked", "reason": "cash below $1 or sized notional below $1"}
    sym = signal["symbol"]
    broker_fill = bool(fill_price_override and fill_price_override > 0)
    if broker_fill:
        fill_price = fill_price_override
        if actual_notional_override and actual_notional_override > 0:
            notional = round(min(actual_notional_override, cash), 4)
    else:
        fill_price = signal["price"] * (1 + SLIPPAGE_BPS / 10000.0 * (1 if signal["side"] == "long" else -1))
    state["cash"] = round(cash - notional, 4)
    trade = {
        "id": f"alpacasim-{uuid.uuid4().hex[:10]}",
        "ts": _now(),
        "symbol": sym,
        "side": signal["side"],
        "entry": round(fill_price, 4),
        "notional_usd": round(notional, 4),
        "edge": signal["edge"],
        "reason": signal["reason"],
        "mode": config.mode,
        "fill_source": "broker" if broker_fill else "modeled",
    }
    state.setdefault("positions", {})[sym] = trade
    daily = state.setdefault("daily", {})
    daily["trades"] = int(daily.get("trades") or 0) + 1
    daily["round_trips_opened"] = int(daily.get("round_trips_opened") or 0) + 1
    _append_jsonl(TRADES_PATH, {"event": "open", **trade})
    return {"status": f"{config.mode}_opened", "trade": trade}


def _sim_close(state: dict[str, Any], config: AlpacaConfig, sym: str, price: float, reason: str) -> dict[str, Any]:
    pos = (state.get("positions") or {}).pop(sym, None)
    if not pos:
        return {"status": "hold", "reason": "no position"}
    fill_price = price * (1 - SLIPPAGE_BPS / 10000.0 * (1 if pos.get("side") == "long" else -1))
    value = _position_value(pos, fill_price)
    pnl = value - float(pos.get("notional_usd") or 0.0)
    state["cash"] = round(float(state.get("cash") or 0.0) + value, 4)
    state["realized_pnl"] = round(float(state.get("realized_pnl") or 0.0) + pnl, 4)
    state.setdefault("daily", {})["realized_pnl"] = round(float(state.get("daily", {}).get("realized_pnl") or 0.0) + pnl, 4)
    state.setdefault("daily", {})["trades"] = int(state.get("daily", {}).get("trades") or 0) + 1
    trade = {
        "id": pos.get("id"),
        "ts": _now(),
        "symbol": sym,
        "side": pos.get("side"),
        "entry": pos.get("entry"),
        "exit": round(fill_price, 4),
        "notional_usd": pos.get("notional_usd"),
        "pnl": round(pnl, 4),
        "return_pct": round((pnl / max(1e-9, float(pos.get("notional_usd") or 0.0))) * 100, 4),
        "reason": reason,
        "mode": config.mode,
    }
    _append_jsonl(TRADES_PATH, {"event": "close", **trade})
    return {"status": f"{config.mode}_closed", "trade": trade}


def _alpaca_order(config: AlpacaConfig, signal: dict[str, Any], notional: float | None = None) -> dict[str, Any]:
    """POST a market order to Alpaca (paper or live base URL). Re-checks gates."""
    blocks = _risk_blocks({"daily": {}, "positions": {}}, config)
    hard_blocks = [b for b in blocks if b.startswith("live") or b.startswith("paper order blocked")]
    if config.mode == "live" and hard_blocks:
        return {"status": "blocked", "reasons": hard_blocks}
    if config.mode == "paper" and not (config.api_key_id and config.api_secret_key):
        return {"status": "blocked", "reasons": hard_blocks or ["paper order blocked: missing API keys"]}
    if config.mode not in {"paper", "live"}:
        return {"status": "blocked", "reasons": ["_alpaca_order called outside paper/live mode"]}
    base = ALPACA_LIVE_BASE if config.mode == "live" else ALPACA_PAPER_BASE
    base_notional = notional if notional is not None else config.max_notional_usd
    notional = round(min(base_notional, config.max_notional_usd, float(config.starting_cash)), 2)
    payload = {
        "symbol": signal["symbol"],
        "notional": str(notional),
        "side": "buy" if signal["side"] == "long" else "sell",
        "type": "market",
        "time_in_force": "day",
    }
    headers = {
        "APCA-API-KEY-ID": config.api_key_id,
        "APCA-API-SECRET-KEY": config.api_secret_key,
    }
    return _post_json(f"{base}/v2/orders", payload, headers)


def _alpaca_order_fill(config: AlpacaConfig, order_id: str, *, attempts: int = 5, delay_s: float = 0.5) -> dict[str, Any]:
    """Poll GET {base}/v2/orders/{id} for the ACTUAL fill (avg price / qty), briefly
    retrying since a market order can take a beat to report filled_avg_price. Never
    raises -- returns {} / {"error": ...} on failure so callers fail open to the
    modeled/market price rather than blocking."""
    if not order_id:
        return {}
    base = ALPACA_LIVE_BASE if config.mode == "live" else ALPACA_PAPER_BASE
    headers = {
        "APCA-API-KEY-ID": config.api_key_id,
        "APCA-API-SECRET-KEY": config.api_secret_key,
    }
    last: dict[str, Any] = {}
    for i in range(max(1, attempts)):
        try:
            order = _get_json(f"{base}/v2/orders/{order_id}", headers)
            filled_avg_price = order.get("filled_avg_price")
            filled_qty = order.get("filled_qty")
            last = {
                "filled_avg_price": float(filled_avg_price) if filled_avg_price else None,
                "filled_qty": float(filled_qty) if filled_qty else None,
                "status": order.get("status"),
            }
            if last["filled_avg_price"]:
                return last
        except Exception as exc:
            last = {"error": str(exc)}
        if i < attempts - 1:
            time.sleep(delay_s)
    return last


def _alpaca_close(state: dict[str, Any], config: AlpacaConfig, sym: str, price: float, reason: str) -> dict[str, Any]:
    """Liquidate a REAL Alpaca position via the close-position endpoint, then book
    the ACTUAL exit fill (poll the resulting order for filled_avg_price, falling
    back to the passed market price if it hasn't reported one yet). Same P&L
    bookkeeping as _sim_close.

    Fail-open on any error placing the close itself: the position is NOT removed
    from state, so it's retried next cycle instead of being silently marked flat
    while real shares are still held. Re-checks the same live-only safety gates
    (ALLOW_LIVE / arm file / keys) that entries do -- never weakened, never bypassed."""
    pos = (state.get("positions") or {}).get(sym)
    if not pos:
        return {"status": "hold", "reason": "no position"}
    if config.mode == "live":
        live_blocks = [b for b in _risk_blocks({"daily": {}, "positions": {}}, config) if b.startswith("live")]
        if live_blocks:
            return {"status": "close_blocked", "reasons": live_blocks, "symbol": sym}
    base = ALPACA_LIVE_BASE if config.mode == "live" else ALPACA_PAPER_BASE
    headers = {
        "APCA-API-KEY-ID": config.api_key_id,
        "APCA-API-SECRET-KEY": config.api_secret_key,
    }
    try:
        resp = _delete_json(f"{base}/v2/positions/{sym}", headers)
    except Exception as exc:
        return {
            "status": "close_error",
            "reason": f"close-position call failed, position retained for retry: {exc}",
            "symbol": sym,
        }
    order_id = resp.get("id") if isinstance(resp, dict) else None
    fill = _alpaca_order_fill(config, order_id) if order_id else {}
    exit_price = fill.get("filled_avg_price") or price
    # The DELETE call above did not raise, so the broker confirmed the close order
    # -- the real position is gone at the broker even if the fill-price poll
    # errored, so it must come off state too now (falls back to market price).
    pos = (state.get("positions") or {}).pop(sym)
    value = _position_value(pos, exit_price)
    pnl = value - float(pos.get("notional_usd") or 0.0)
    state["cash"] = round(float(state.get("cash") or 0.0) + value, 4)
    state["realized_pnl"] = round(float(state.get("realized_pnl") or 0.0) + pnl, 4)
    state.setdefault("daily", {})["realized_pnl"] = round(float(state.get("daily", {}).get("realized_pnl") or 0.0) + pnl, 4)
    state.setdefault("daily", {})["trades"] = int(state.get("daily", {}).get("trades") or 0) + 1
    trade = {
        "id": pos.get("id"),
        "broker_order_id": order_id,
        "ts": _now(),
        "symbol": sym,
        "side": pos.get("side"),
        "entry": pos.get("entry"),
        "exit": round(exit_price, 4),
        "notional_usd": pos.get("notional_usd"),
        "pnl": round(pnl, 4),
        "return_pct": round((pnl / max(1e-9, float(pos.get("notional_usd") or 0.0))) * 100, 4),
        "reason": reason,
        "mode": config.mode,
        "fill_source": "broker" if fill.get("filled_avg_price") else "market_price_fallback",
    }
    if fill.get("error"):
        trade["fill_error"] = fill["error"]
    _append_jsonl(TRADES_PATH, {"event": "close", **trade})
    return {"status": f"{config.mode}_closed", "trade": trade}


def _close_position(state: dict[str, Any], config: AlpacaConfig, sym: str, price: float, reason: str) -> dict[str, Any]:
    """Exit dispatcher used by every close in run_once (stop, take-profit, rotation):
    paper/live with API keys configured places a REAL close order via _alpaca_close;
    everything else (sim mode, or paper/live missing keys) uses the internal simulator."""
    if config.mode in {"paper", "live"} and config.api_key_id and config.api_secret_key:
        return _alpaca_close(state, config, sym, price, reason)
    return _sim_close(state, config, sym, price, reason)


def run_once() -> dict[str, Any]:
    config = AlpacaConfig.from_env()
    state = load_state(config)
    prices, market = update_prices(state, config)
    risk_on, regime_label = _regime(market)
    state["regime"] = {"risk_on": risk_on, "label": regime_label}
    action: dict[str, Any] = {"status": "hold", "reason": "no qualifying signal"}

    # 1) Exits first (swing: no forced same-day close, only stop/take-profit).
    #    This is a pure risk-cut and is never blocked by the entry gates below.
    for sym, pos in list((state.get("positions") or {}).items()):
        row = prices.get(sym)
        if not row:
            continue
        price = row["price"]
        entry = float(pos.get("entry") or price)
        side = 1.0 if pos.get("side") == "long" else -1.0
        ret = side * ((price / entry) - 1.0)
        if ret >= config.take_profit_pct:
            action = _close_position(state, config, sym, price, f"take profit {ret * 100:.3f}%")
            break
        if ret <= -config.stop_loss_pct:
            action = _close_position(state, config, sym, price, f"stop loss {ret * 100:.3f}%")
            break

    # Peak-equity bookkeeping feeds the trade_risk drawdown circuit breaker.
    state["peak_equity"] = max(float(state.get("peak_equity") or config.starting_cash), equity(state))

    # 2) Dual-momentum targets: recompute on an ~monthly schedule only (avoid churn).
    strat = state.setdefault("strategy", {"last_rebalance": None, "targets": [], "ranked": []})
    if action.get("status") == "hold":
        if _should_rebalance(strat, config) or not strat.get("targets"):
            ranked = _dual_momentum_rank(prices, config)
            eligible = [r for r in ranked if r["mom60"] > 0.0]  # absolute momentum vs ~0 T-bill proxy
            strat["ranked"] = ranked
            strat["targets"] = [r["symbol"] for r in eligible[:config.momentum_top_n]]
            strat["last_rebalance"] = _now()
        targets = strat.get("targets") or []
        # Regime filter gates execution, not the momentum ranking itself: risk-off
        # always drains to cash even mid-window; risk-on rebuilds from the stored picks.
        effective_targets = targets if risk_on else []
        state["last_signal"] = {
            "targets": targets, "effective_targets": effective_targets,
            "ranked": strat.get("ranked"), "regime": regime_label, "risk_on": risk_on,
        }

        # 2a) Rotate out of anything no longer a target (rank dropout, momentum turned
        #     negative, or regime flipped risk-off).
        for sym in list((state.get("positions") or {}).keys()):
            if sym not in effective_targets:
                row = prices.get(sym)
                if row:
                    reason = (
                        "regime filter: SPY below 200dma, moving to cash" if not risk_on
                        else "dual-momentum rebalance: rotated out (rank dropout / momentum turned negative)"
                    )
                    action = _close_position(state, config, sym, row["price"], reason)
                break

    # 3) New entries: regime gate -> risk/circuit-breaker/governor gates -> momentum
    #    pick -> optional overnight sleeve. At most one new entry per cycle.
    if action.get("status") == "hold":
        if not risk_on:
            action = {"status": "hold", "reason": f"regime filter: SPY below 200dma (regime={regime_label}) — long entries disabled, cash only"}
        else:
            blocks = _risk_blocks(state, config)
            if blocks:
                action = {"status": "blocked", "reasons": blocks}
            else:
                targets = strat.get("targets") or []
                held = state.get("positions") or {}
                candidate_sym = next((s for s in targets if s not in held), None)
                if candidate_sym:
                    row = prices.get(candidate_sym)
                    rank_row = next((r for r in (strat.get("ranked") or []) if r.get("symbol") == candidate_sym), None)
                    mom60 = float(rank_row["mom60"]) if rank_row else 0.0
                    if row and trade_risk.edge_clears_cost(abs(mom60), fee_bps=0.0, slippage_bps=SLIPPAGE_BPS):
                        signal = {
                            "symbol": candidate_sym,
                            "price": row["price"],
                            "side": "long",
                            "edge": round(mom60, 6),
                            "reason": f"dual-momentum sector rotation: 60td return {mom60 * 100:.2f}% (risk-on, top {config.momentum_top_n})",
                        }
                        notional = _sized_notional(state, config)
                        if notional >= 1:
                            action = _enter_position(state, config, signal, notional)
                elif config.overnight_enabled and _near_close(config.overnight_window_min):
                    for sym in config.overnight_symbols:
                        if sym in held:
                            continue
                        row = prices.get(sym)
                        if not row or not _sma20_slope_up(row.get("closes") or []):
                            continue
                        signal = {
                            "symbol": sym, "price": row["price"], "side": "long", "edge": 0.0,
                            "reason": "overnight sleeve: 20dma sloping up near close (ALPACA_OVERNIGHT)",
                        }
                        notional = _sized_notional(state, config)
                        if notional >= 1:
                            action = _enter_position(state, config, signal, notional)
                        break

    state["last_action"] = action
    save_state(state)
    status = build_status(state, config, action=action)
    return status


def build_status(state: dict[str, Any], config: AlpacaConfig, *, action: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = {
        "ok": True,
        "generated_at": _now(),
        "mode": config.mode,
        "cash": round(float(state.get("cash") or 0.0), 4),
        "equity": equity(state),
        "starting_cash": round(float(state.get("starting_cash") or config.starting_cash), 4),
        "realized_pnl": round(float(state.get("realized_pnl") or 0.0), 4),
        "positions": state.get("positions") or {},
        "daily": state.get("daily") or {},
        "prices": state.get("prices") or {},
        "regime": state.get("regime") or {},
        "strategy": state.get("strategy") or {},
        "peak_equity": round(float(state.get("peak_equity") or config.starting_cash), 4),
        "last_signal": state.get("last_signal") or {},
        "last_action": action or state.get("last_action") or {},
        "risk": asdict(config) | {"api_key_id": bool(config.api_key_id), "api_secret_key": bool(config.api_secret_key)},
        "market_open": is_market_open(),
        "live_trading_possible": bool(
            config.mode == "live"
            and config.live_allowed
            and LIVE_ARM_PATH.exists()
            and config.api_key_id
            and config.api_secret_key
        ),
        "kill_switch": KILL_PATH.exists(),
        "important": "Alpaca trading stays sim/paper until ALPACA_MODE=live, ALPACA_ALLOW_LIVE=1, alpaca_live_armed.json, and both API keys are all present.",
    }
    _write_json(STATUS_PATH, payload)
    return payload


def status_text() -> str:
    config = AlpacaConfig.from_env()
    state = load_state(config)
    status = build_status(state, config)
    signal = status.get("last_signal") or {}
    action = status.get("last_action") or {}
    regime = status.get("regime") or {}
    daily = status.get("daily") or {}
    return (
        "Alpaca Bot Status\n"
        f"- Mode: {status['mode']} ({'LIVE ARMED' if status['live_trading_possible'] else 'live disabled'})\n"
        f"- Market open: {status['market_open']}\n"
        f"- Regime: {'risk-on' if regime.get('risk_on') else 'risk-off'} ({regime.get('label', 'unknown')})\n"
        f"- Equity: ${status['equity']:.2f} from ${status['starting_cash']:.2f}; cash ${status['cash']:.2f}; peak ${status.get('peak_equity', status['starting_cash']):.2f}\n"
        f"- Realized P/L: ${status['realized_pnl']:.2f}; positions: {len(status.get('positions') or {})}\n"
        f"- Daily trades: {daily.get('trades', 0)}; round-trips opened: {daily.get('round_trips_opened', 0)}/{config.max_round_trips_per_day}\n"
        f"- Dual-momentum targets: {signal.get('effective_targets') or signal.get('targets') or 'none'}\n"
        f"- Last action: {action.get('status', 'none')}"
    )


def report_text() -> str:
    rows: list[dict[str, Any]] = []
    if TRADES_PATH.exists():
        for line in TRADES_PATH.read_text().splitlines():
            try:
                row = json.loads(line)
            except Exception:
                continue
            if row.get("event") == "close":
                rows.append(row)
    if not rows:
        return "Alpaca Bot Report\n- No closed trades yet."
    wins = [r for r in rows if float(r.get("pnl") or 0) > 0]
    losses = [r for r in rows if float(r.get("pnl") or 0) <= 0]
    total_pnl = sum(float(r.get("pnl") or 0) for r in rows)
    win_rate = len(wins) / len(rows) if rows else 0.0
    avg_win = sum(float(r.get("pnl") or 0) for r in wins) / len(wins) if wins else 0.0
    avg_loss = sum(float(r.get("pnl") or 0) for r in losses) / len(losses) if losses else 0.0
    expectancy = win_rate * avg_win + (1 - win_rate) * avg_loss
    return (
        "Alpaca Bot Report\n"
        f"- Closed trades: {len(rows)}\n"
        f"- Win rate: {win_rate * 100:.1f}% ({len(wins)}W / {len(losses)}L)\n"
        f"- Avg win: ${avg_win:.4f}; avg loss: ${avg_loss:.4f}\n"
        f"- Expectancy per trade: ${expectancy:.4f}\n"
        f"- Total realized P/L: ${total_pnl:.4f}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Alpaca equities/ETF sim/paper/live-gated swing bot.")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--kill", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--report", action="store_true")
    args = parser.parse_args(argv)
    if args.kill:
        KILL_PATH.parent.mkdir(parents=True, exist_ok=True)
        KILL_PATH.write_text(json.dumps({"activated_at": _now()}))
        print(f"kill switch active: {KILL_PATH}")
        return 0
    if args.resume:
        if KILL_PATH.exists():
            KILL_PATH.unlink()
        print("kill switch cleared")
        return 0
    if args.report:
        print(report_text())
        return 0
    if args.status:
        print(status_text())
        return 0
    if args.once:
        print(json.dumps(run_once(), indent=2, sort_keys=True, default=str))
        return 0
    while True:
        run_once()
        time.sleep(max(60, AlpacaConfig.from_env().loop_interval_s))


if __name__ == "__main__":
    raise SystemExit(main())
