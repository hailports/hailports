"""Kraken Spot paper bot with live trading double-locked.

Default mode is paper. A REAL Kraken order can only ever be placed when ALL of
these are simultaneously true: KRAKEN_MODE=live, KRAKEN_ALLOW_LIVE=1, the file
data/runtime/kraken_live_armed.json exists, and KRAKEN_API_KEY + KRAKEN_API_SECRET
are set. Any single missing gate blocks live trading with a clear reason.

$0 to operate: stdlib only (urllib for Kraken REST + HMAC-SHA512 auth) plus
core.market_signals for real spot prices (Yahoo/CoinGecko/Coinbase, all free).
Paper mode still prices trades off REAL crypto spot data, never synthetic
numbers, and paper P&L subtracts realistic Kraken taker fees + slippage so the
proof stays honest.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import time
import urllib.parse
import urllib.request
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core import BASE_DIR
from core import market_signals

try:
    from core import market_intel
except Exception:  # pragma: no cover - fail open if the module can't import
    market_intel = None

try:
    from core import market_data_fusion
except Exception:  # pragma: no cover - fail open if the module can't import
    market_data_fusion = None

try:
    from core import trade_risk
except Exception:  # pragma: no cover - fail open if the module can't import
    trade_risk = None


RUNTIME = BASE_DIR / "data" / "runtime"
TRADING = BASE_DIR / "data" / "trading"
LOGS = BASE_DIR / "data" / "logs"
STATE_PATH = TRADING / "kraken_bot_state.json"
STATUS_PATH = TRADING / "kraken_bot_status.json"
TRADES_PATH = TRADING / "kraken_bot_trades.jsonl"
MARKET_CACHE_PATH = TRADING / "kraken_market_cache.json"
KILL_PATH = RUNTIME / "kraken_bot.kill"
LIVE_ARM_PATH = RUNTIME / "kraken_live_armed.json"
LOG_PATH = LOGS / "kraken_bot.jsonl"

# Small, liquid universe only. Kraken Spot is long-only (no naked shorts).
DEFAULT_PAIRS = ("BTC/USD", "ETH/USD", "SOL/USD")

# Kraken AddOrder accepts the pair "altname" — shorter than the ISO pair id
# (e.g. XXBTZUSD) but equally valid for order placement.
KRAKEN_PAIR_MAP = {
    "BTC/USD": "XBTUSD",
    "ETH/USD": "ETHUSD",
    "SOL/USD": "SOLUSD",
}

# core.market_signals symbol spelling for the same instruments (real prices).
MARKET_SIGNALS_SYMBOL_MAP = {
    "BTC/USD": "BTC-USD",
    "ETH/USD": "ETH-USD",
    "SOL/USD": "SOL-USD",
}

KRAKEN_API_BASE = "https://api.kraken.com"

# Kraken Spot's minimum order size in quote currency (USD pairs) — the hard
# floor under any research-sized notional, live or paper.
KRAKEN_MIN_ORDER_USD = 0.50


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


@dataclass(frozen=True)
class KrakenConfig:
    mode: str
    pairs: tuple[str, ...]
    starting_cash: float
    max_notional_usd: float
    max_open_positions: int
    max_daily_loss: float
    max_total_loss: float
    max_daily_trades: int
    stop_loss_pct: float
    take_profit_pct: float
    max_hold_cycles: int
    min_edge: float
    loop_interval_s: int
    taker_fee_bps: float
    slippage_bps: float
    live_allowed: bool
    kraken_api_key: str
    kraken_api_secret: str
    confluence_min_ratio: float
    rsi_overbought: float
    rsi_favor_low: float
    rsi_favor_high: float
    fng_extreme_greed_mult: float
    fng_fear_mult: float
    edge_fee_margin: float
    min_conviction: float

    @classmethod
    def from_env(cls) -> "KrakenConfig":
        raw_pairs = os.environ.get("KRAKEN_PAIRS", ",".join(DEFAULT_PAIRS))
        pairs = tuple(p.strip().upper().replace("-", "/") for p in raw_pairs.split(",") if p.strip())
        mode = str(os.environ.get("KRAKEN_MODE", "paper")).strip().lower()
        if mode not in {"paper", "live"}:
            mode = "paper"
        return cls(
            mode=mode,
            pairs=pairs or DEFAULT_PAIRS,
            starting_cash=float(os.environ.get("KRAKEN_PAPER_STARTING_CASH", 25.0)),
            max_notional_usd=float(os.environ.get("KRAKEN_MAX_NOTIONAL_USD", 8.0)),
            max_open_positions=int(os.environ.get("KRAKEN_MAX_OPEN_POSITIONS", 3)),
            max_daily_loss=float(os.environ.get("KRAKEN_MAX_DAILY_LOSS", 5.0)),
            max_total_loss=float(os.environ.get("KRAKEN_MAX_TOTAL_LOSS", 25.0)),
            max_daily_trades=int(os.environ.get("KRAKEN_MAX_DAILY_TRADES", 8)),
            stop_loss_pct=float(os.environ.get("KRAKEN_STOP_LOSS_PCT", 0.02)),
            take_profit_pct=float(os.environ.get("KRAKEN_TAKE_PROFIT_PCT", 0.03)),
            max_hold_cycles=int(os.environ.get("KRAKEN_MAX_HOLD_CYCLES", 96)),
            min_edge=float(os.environ.get("KRAKEN_MIN_EDGE", 0.004)),
            loop_interval_s=int(os.environ.get("KRAKEN_LOOP_INTERVAL_S", 900)),
            taker_fee_bps=float(os.environ.get("KRAKEN_TAKER_FEE_BPS", 40.0)),
            slippage_bps=float(os.environ.get("KRAKEN_SLIPPAGE_BPS", 15.0)),
            live_allowed=_truthy(os.environ.get("KRAKEN_ALLOW_LIVE")),
            kraken_api_key=str(os.environ.get("KRAKEN_API_KEY", "")).strip(),
            kraken_api_secret=str(os.environ.get("KRAKEN_API_SECRET", "")).strip(),
            # confluence signal thresholds (core.market_intel.technicals() factors)
            confluence_min_ratio=float(os.environ.get("KRAKEN_CONFLUENCE_MIN_RATIO", 0.6)),
            rsi_overbought=float(os.environ.get("KRAKEN_RSI_OVERBOUGHT", 70.0)),
            rsi_favor_low=float(os.environ.get("KRAKEN_RSI_FAVOR_LOW", 40.0)),
            rsi_favor_high=float(os.environ.get("KRAKEN_RSI_FAVOR_HIGH", 60.0)),
            # crypto_fear_greed() regime filter multipliers on expected_move
            fng_extreme_greed_mult=float(os.environ.get("KRAKEN_FNG_EXTREME_GREED_MULT", 0.3)),
            fng_fear_mult=float(os.environ.get("KRAKEN_FNG_FEAR_MULT", 1.15)),
            # anti-churn hard gate: expected_move must exceed round_trip_cost * this
            edge_fee_margin=float(os.environ.get("KRAKEN_EDGE_FEE_MARGIN", 1.5)),
            # market_data_fusion.fuse(pair) entry gate: min conviction for a "long" call
            min_conviction=float(os.environ.get("KRAKEN_MIN_CONVICTION", 0.6)),
        )


def initial_state(config: KrakenConfig) -> dict[str, Any]:
    return {
        "created_at": _now(),
        "updated_at": _now(),
        "mode": config.mode,
        "cash": round(config.starting_cash, 4),
        "starting_cash": round(config.starting_cash, 4),
        "cycle": 0,
        "positions": {},
        "prices": {},
        "price_history": {},
        "realized_pnl": 0.0,
        "peak_equity": round(config.starting_cash, 4),
        "daily": {"date": _today(), "starting_equity": config.starting_cash, "trades": 0, "realized_pnl": 0.0},
        "notes": [
            "paper Kraken only until KRAKEN_MODE=live, KRAKEN_ALLOW_LIVE=1, "
            "data/runtime/kraken_live_armed.json, and KRAKEN_API_KEY/KRAKEN_API_SECRET are ALL present"
        ],
    }


def load_state(config: KrakenConfig) -> dict[str, Any]:
    state = _read_json(STATE_PATH)
    if not state:
        state = initial_state(config)
    if (state.get("daily") or {}).get("date") != _today():
        state["daily"] = {"date": _today(), "starting_equity": equity(state), "trades": 0, "realized_pnl": 0.0}
    state.setdefault("positions", {})
    state.setdefault("prices", {})
    state.setdefault("price_history", {})
    state.setdefault("cycle", 0)
    state.setdefault("peak_equity", round(config.starting_cash, 4))
    return state


def save_state(state: dict[str, Any]) -> None:
    state["updated_at"] = _now()
    _write_json(STATE_PATH, state)


def _fetch_crypto(config: KrakenConfig) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Real spot prices + momentum/MA features for the configured pairs.

    Bridges to core.market_signals.fetch_prices()/compute_features() (free
    Yahoo/CoinGecko/Coinbase sources) so paper trades are priced off the real
    market, never synthetic data. Cached to a small kraken-only cache file.
    """
    cache = _read_json(MARKET_CACHE_PATH, {})
    symbols = [MARKET_SIGNALS_SYMBOL_MAP.get(p, p.replace("/", "-")) for p in config.pairs]
    bars = market_signals.fetch_prices(symbols, cache=cache, ttl_s=max(60, config.loop_interval_s))
    _write_json(MARKET_CACHE_PATH, cache)
    prices: dict[str, dict[str, Any]] = {}
    features: dict[str, dict[str, Any]] = {}
    for pair in config.pairs:
        sym = MARKET_SIGNALS_SYMBOL_MAP.get(pair, pair.replace("/", "-"))
        bar = bars.get(sym)
        if not bar:
            continue
        prices[pair] = {
            "price": round(float(bar["price"]), 6),
            "source": bar.get("source", "market_signals"),
            "updated_at": _now(),
        }
        features[pair] = market_signals.compute_features(bar.get("closes") or [])
    return prices, features


def update_prices(state: dict[str, Any], config: KrakenConfig) -> tuple[dict[str, float], dict[str, Any]]:
    state["cycle"] = int(state.get("cycle") or 0) + 1
    prices_raw, features = _fetch_crypto(config)
    prices: dict[str, float] = {}
    for pair in config.pairs:
        row = prices_raw.get(pair)
        if not row:
            continue
        price = round(float(row["price"]), 6)
        prices[pair] = price
        state.setdefault("prices", {})[pair] = {"price": price, "source": row.get("source"), "updated_at": _now()}
        hist = state.setdefault("price_history", {}).setdefault(pair, [])
        hist.append({"cycle": state["cycle"], "price": price, "ts": _now()})
        del hist[:-240]
    state["features"] = features
    return prices, features


def _position_value(pos: dict[str, Any], current: float) -> float:
    notional = float(pos.get("notional_usd") or 0.0)
    entry = float(pos.get("entry") or current)
    if entry <= 0:
        return notional
    return notional * (current / entry)


def equity(state: dict[str, Any]) -> float:
    total = float(state.get("cash") or 0.0)
    prices = state.get("prices") or {}
    for pair, pos in (state.get("positions") or {}).items():
        current = float((prices.get(pair) or {}).get("price") or pos.get("entry") or 0.0)
        total += _position_value(pos, current)
    return round(total, 4)


def _risk_blocks(state: dict[str, Any], config: KrakenConfig) -> list[str]:
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
    if trade_risk is not None:
        try:
            peak_equity = float(state.get("peak_equity") or config.starting_cash)
            day_start_equity = float(daily.get("starting_equity") or config.starting_cash)
            for reason in trade_risk.circuit_breaker(
                equity=current_equity, day_start_equity=day_start_equity, peak_equity=peak_equity,
            ):
                blocks.append(f"circuit breaker: {reason}")
        except Exception:
            pass  # fail open — never let a risk-math error block a status/close cycle
    if len(state.get("positions") or {}) >= config.max_open_positions:
        blocks.append(f"open position cap hit: {len(state.get('positions') or {})} >= {config.max_open_positions}")
    if config.mode == "live":
        if not config.live_allowed:
            blocks.append("live blocked: KRAKEN_ALLOW_LIVE is not set")
        if not LIVE_ARM_PATH.exists():
            blocks.append(f"live blocked: missing {LIVE_ARM_PATH}")
        if not config.kraken_api_key or not config.kraken_api_secret:
            blocks.append("live blocked: KRAKEN_API_KEY/KRAKEN_API_SECRET not configured")
    return blocks


def _legacy_momentum_edge(feat: dict[str, Any]) -> float | None:
    """Original 5d-momentum + 20/50-MA-crossover score. Kept as the fail-open
    fallback whenever core.market_intel has no usable technicals for a pair."""
    mom5 = float(feat.get("mom5") or 0.0)
    mom20 = float(feat.get("mom20") or 0.0)
    sma20, sma50 = feat.get("sma20"), feat.get("sma50")
    ma_cross = ((sma20 / sma50) - 1.0) if sma20 and sma50 else 0.0
    raw_edge = (mom5 * 0.6) + (ma_cross * 0.4)
    if raw_edge <= 0:
        # Kraken Spot is long-only here; a non-positive edge has no entry.
        return None
    return raw_edge + abs(mom20) * 0.1


def _confluence_votes(feat: dict[str, Any], tech: dict[str, Any], config: KrakenConfig) -> tuple[list[int], dict[str, Any]]:
    """+1/-1 votes from independent technical factors; 0/omitted = no opinion.

    Factors: 20/50 MA trend, multi-timeframe momentum (5/20/60d), RSI (favor
    40-60, reject >70 overbought), MACD histogram sign, price-vs-Bollinger-mid.
    """
    votes: list[int] = []
    detail: dict[str, Any] = {}

    sma20, sma50 = feat.get("sma20"), feat.get("sma50")
    if sma20 and sma50:
        v = 1 if sma20 > sma50 else -1
        votes.append(v)
        detail["trend_20_50ma"] = v

    momentum = (tech or {}).get("momentum") or {}
    for label in ("m5", "m20", "m60"):
        m = momentum.get(label)
        if m is not None:
            v = 1 if m > 0 else -1
            votes.append(v)
            detail[f"mom_{label}"] = m

    rsi = (tech or {}).get("rsi14")
    if rsi is not None:
        detail["rsi14"] = rsi
        if rsi > config.rsi_overbought:
            votes.append(-1)  # overbought -> reject, mean-reversion risk
        elif config.rsi_favor_low <= rsi <= config.rsi_favor_high:
            votes.append(1)  # sweet spot: room to run, not yet overbought
        # else: RSI outside both bands is a genuine "no opinion" -> no vote

    hist = ((tech or {}).get("macd") or {}).get("hist")
    if hist is not None:
        v = 1 if hist > 0 else -1
        votes.append(v)
        detail["macd_hist"] = hist

    boll = (tech or {}).get("bollinger") or {}
    mid, tprice = boll.get("mid"), (tech or {}).get("price")
    if mid and tprice:
        v = 1 if tprice > mid else -1
        votes.append(v)
        detail["above_boll_mid"] = v

    return votes, detail


def _expected_move(feat: dict[str, Any], tech: dict[str, Any], ratio: float, legacy_edge: float | None) -> float:
    """Blended expected % move (fraction) the edge-vs-cost gate compares against
    round-trip cost. Confluence-weighted average of the multi-timeframe momentum
    magnitudes when technicals are available; falls back to the legacy edge."""
    momentum = (tech or {}).get("momentum") or {}
    mags = [abs(momentum[k]) / 100.0 for k in ("m5", "m20", "m60") if momentum.get(k) is not None]
    if mags:
        base = sum(mags) / len(mags)
    elif legacy_edge is not None:
        base = abs(legacy_edge)
    else:
        base = abs(float(feat.get("mom5") or 0.0))
    return base * ratio


def _score_pair(
    features: dict[str, Any],
    pair: str,
    config: KrakenConfig,
    fear_greed: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Multi-factor confluence score using core.market_intel.technicals(): trend
    (20/50 MA), 5/20/60d momentum, RSI, MACD histogram, and Bollinger position —
    only emits a long signal when a MAJORITY of applicable factors agree, and
    applies a crypto_fear_greed() regime filter (discount in extreme greed,
    boost in fear). Fails open to the legacy momentum/MA score whenever
    market_intel is unavailable or returns no usable technicals for the pair —
    never crashes the loop. Kraken Spot is long-only here.
    """
    feat = (features or {}).get(pair) or {}
    if int(feat.get("n") or 0) < 25 or "price" not in feat:
        return None
    price = float(feat["price"])
    legacy_edge = _legacy_momentum_edge(feat)

    tech: dict[str, Any] = {}
    if market_intel is not None:
        try:
            symbol = MARKET_SIGNALS_SYMBOL_MAP.get(pair, pair.replace("/", "-"))
            t = market_intel.technicals(symbol)
            if isinstance(t, dict) and t.get("available"):
                tech = t
        except Exception:
            tech = {}

    reason_bits: list[str] = []
    detail: dict[str, Any] = {}
    if tech:
        votes, detail = _confluence_votes(feat, tech, config)
        if not votes:
            # technicals came back but every factor was a no-opinion — fall back.
            if legacy_edge is None:
                return None
            edge = legacy_edge
            reason_bits.append("legacy momentum+MA (no confluence factors available)")
        else:
            positive = sum(1 for v in votes if v > 0)
            ratio = positive / len(votes)
            if ratio < config.confluence_min_ratio:
                return None  # no majority confluence -> no entry (anti-churn)
            edge = _expected_move(feat, tech, ratio, legacy_edge)
            reason_bits.append(f"confluence {positive}/{len(votes)} factors agree")
    else:
        # market_intel neutral/unavailable (fail-open) -> original behavior.
        if legacy_edge is None:
            return None
        edge = legacy_edge
        reason_bits.append("legacy 5d momentum + 20/50 MA crossover (market_intel unavailable)")

    if edge is None or edge <= 0:
        return None

    fg = fear_greed or {}
    classification = str(fg.get("classification") or "").strip().lower()
    if classification == "extreme greed":
        edge *= config.fng_extreme_greed_mult
        reason_bits.append(f"fear/greed=extreme greed (x{config.fng_extreme_greed_mult})")
        if edge <= 0:
            return None
    elif classification in {"fear", "extreme fear"}:
        edge *= config.fng_fear_mult
        reason_bits.append(f"fear/greed={classification} (x{config.fng_fear_mult})")

    return {
        "pair": pair,
        "price": price,
        "side": "long",
        "edge": round(edge, 6),
        "reason": "; ".join(reason_bits),
        "factors": detail,
    }


def _edge_gate(signal: dict[str, Any], config: KrakenConfig) -> tuple[bool, float, float]:
    """Anti-churn hard gate: refuse an entry unless its expected_move clears
    round-trip cost (2x taker fee + 2x slippage) by config.edge_fee_margin.
    This is the mechanism that stops marginal trades from just feeding fees."""
    expected_move = float(signal.get("edge") or 0.0)
    round_trip_cost = 2.0 * (config.taker_fee_bps + config.slippage_bps) / 10000.0
    required_move = round_trip_cost * config.edge_fee_margin
    return expected_move > required_move, expected_move, required_move


def _fuse_pair(pair: str) -> dict[str, Any]:
    """Bridge to market_data_fusion.fuse() using its BTC-USD/ETH-USD spelling.
    Never raises — a dead/unavailable/errored fusion layer returns {} so the
    entry gate in run_once() fails open to legacy edge-only behavior."""
    if market_data_fusion is None:
        return {}
    try:
        symbol = MARKET_SIGNALS_SYMBOL_MAP.get(pair, pair.replace("/", "-"))
        result = market_data_fusion.fuse(symbol)
        return result if isinstance(result, dict) else {}
    except Exception:
        return {}


def _fee_usd(notional: float, config: KrakenConfig) -> float:
    return notional * (config.taker_fee_bps / 10000.0)


def _slip_price(price: float, config: KrakenConfig, *, buy: bool) -> float:
    adj = config.slippage_bps / 10000.0
    return price * (1 + adj) if buy else price * (1 - adj)


def _win_prob_from_conviction(conviction: float) -> float:
    """Conservative conviction(0-1)->win_prob map for trade_risk.position_notional:
    0.50-0.58, never claiming more edge than a confluence signal actually earns."""
    c = max(0.0, min(1.0, conviction))
    return round(max(0.50, min(0.58, 0.50 + 0.08 * c)), 4)


def _target_notional(state: dict[str, Any], config: KrakenConfig, conviction: float) -> float:
    """Quarter-Kelly position size via trade_risk.position_notional(), ceilinged by
    the existing max_notional_usd/cash caps. Fails open to the flat max_notional_usd
    cap (the pre-existing sizing) if trade_risk is unavailable or errors."""
    cash = float(state.get("cash") or 0.0)
    notional = config.max_notional_usd
    if trade_risk is not None:
        try:
            eq = equity(state)
            win_prob = _win_prob_from_conviction(conviction)
            payoff_ratio = (config.take_profit_pct / config.stop_loss_pct) if config.stop_loss_pct > 0 else 1.5
            sized = trade_risk.position_notional(
                eq, win_prob=win_prob, payoff_ratio=payoff_ratio, stop_pct=config.stop_loss_pct,
            )
            if sized > 0:
                notional = sized
        except Exception:
            pass  # fail open to config.max_notional_usd
    return round(min(notional, config.max_notional_usd, cash), 4)


def _paper_open(state: dict[str, Any], config: KrakenConfig, signal: dict[str, Any]) -> dict[str, Any]:
    cash = float(state.get("cash") or 0.0)
    notional = _target_notional(state, config, float(signal.get("conviction") or 0.0))
    if notional < KRAKEN_MIN_ORDER_USD:
        return {"status": "blocked", "reason": f"sized notional ${notional:.2f} below Kraken ${KRAKEN_MIN_ORDER_USD:.2f} min order"}
    exec_price = _slip_price(signal["price"], config, buy=True)
    fee = _fee_usd(notional, config)
    if notional + fee > cash:
        notional = max(0.0, cash - fee)
        fee = _fee_usd(notional, config)
    if notional < KRAKEN_MIN_ORDER_USD:
        return {"status": "blocked", "reason": "cash below Kraken min order after fees"}
    pair = signal["pair"]
    state["cash"] = round(cash - (notional + fee), 4)
    trade = {
        "id": f"krknpaper-{uuid.uuid4().hex[:10]}",
        "ts": _now(),
        "pair": pair,
        "side": "long",
        "entry": round(exec_price, 6),
        "quote_price": signal["price"],
        "notional_usd": round(notional, 4),
        "entry_fee_usd": round(fee, 4),
        "edge": signal["edge"],
        "reason": signal["reason"],
        "mode": config.mode,
        "opened_cycle": int(state.get("cycle") or 0),
    }
    state.setdefault("positions", {})[pair] = trade
    state.setdefault("daily", {})["trades"] = int(state.get("daily", {}).get("trades") or 0) + 1
    _append_jsonl(TRADES_PATH, {"event": "open", **trade})
    return {"status": "paper_opened", "trade": trade}


def _paper_close(state: dict[str, Any], config: KrakenConfig, pair: str, price: float, reason: str) -> dict[str, Any]:
    pos = (state.get("positions") or {}).pop(pair, None)
    if not pos:
        return {"status": "hold", "reason": "no position"}
    entry = float(pos.get("entry") or price)
    notional = float(pos.get("notional_usd") or 0.0)
    exec_price = _slip_price(price, config, buy=False)
    gross_value = notional * (exec_price / entry) if entry else notional
    exit_fee = _fee_usd(gross_value, config)
    net_proceeds = gross_value - exit_fee
    entry_fee = float(pos.get("entry_fee_usd") or 0.0)
    pnl = net_proceeds - notional - entry_fee
    state["cash"] = round(float(state.get("cash") or 0.0) + net_proceeds, 4)
    state["realized_pnl"] = round(float(state.get("realized_pnl") or 0.0) + pnl, 4)
    state.setdefault("daily", {})["realized_pnl"] = round(float(state.get("daily", {}).get("realized_pnl") or 0.0) + pnl, 4)
    state.setdefault("daily", {})["trades"] = int(state.get("daily", {}).get("trades") or 0) + 1
    trade = {
        "id": pos.get("id"),
        "ts": _now(),
        "pair": pair,
        "side": pos.get("side"),
        "entry": entry,
        "exit": round(exec_price, 6),
        "notional_usd": notional,
        "entry_fee_usd": round(entry_fee, 4),
        "exit_fee_usd": round(exit_fee, 4),
        "pnl": round(pnl, 4),
        "return_pct": round((pnl / max(1e-9, notional)) * 100, 4),
        "reason": reason,
        "mode": config.mode,
    }
    _append_jsonl(TRADES_PATH, {"event": "close", **trade})
    return {"status": "paper_closed", "trade": trade}


def _kraken_sign(path: str, data: dict[str, str], secret_b64: str, nonce: str) -> str:
    postdata = urllib.parse.urlencode(data)
    sha256_digest = hashlib.sha256((nonce + postdata).encode()).digest()
    message = path.encode() + sha256_digest
    secret = base64.b64decode(secret_b64)
    signature = hmac.new(secret, message, hashlib.sha512)
    return base64.b64encode(signature.digest()).decode()


def _kraken_private(config: KrakenConfig, path: str, data: dict[str, str]) -> dict[str, Any]:
    """Signed Kraken private POST ($0). Reused for order placement + fill queries."""
    payload = dict(data)
    payload["nonce"] = str(int(time.time() * 1000))
    headers = {
        "API-Key": config.kraken_api_key,
        "API-Sign": _kraken_sign(path, payload, config.kraken_api_secret, payload["nonce"]),
        "Content-Type": "application/x-www-form-urlencoded",
    }
    body = urllib.parse.urlencode(payload).encode()
    req = urllib.request.Request(f"{KRAKEN_API_BASE}{path}", data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=20) as response:
        raw = response.read().decode("utf-8", errors="replace")
        return json.loads(raw) if raw else {}


def _kraken_add_order(
    config: KrakenConfig, pair: str, side: str, volume: float,
    *, ordertype: str = "market", price: float | None = None, oflags: str | None = None,
) -> dict[str, Any]:
    """Place a REAL Kraken order (market by default; pass ordertype="limit" +
    price + oflags="post" for a post-only maker entry). Re-checks every live
    gate first and refuses to fire if any is missing, even if called out of band."""
    live_blocks = [b for b in _risk_blocks({"daily": {}, "positions": {}}, config) if b.startswith("live")]
    if live_blocks:
        return {"error": live_blocks}
    if volume <= 0:
        return {"error": ["computed volume is zero"]}
    pair_code = KRAKEN_PAIR_MAP.get(pair, pair.replace("/", ""))
    data = {"ordertype": ordertype, "type": side, "volume": f"{volume:.8f}", "pair": pair_code}
    if price is not None:
        data["price"] = f"{price:.10f}".rstrip("0").rstrip(".")
    if oflags:
        data["oflags"] = oflags
    return _kraken_private(config, "/0/private/AddOrder", data)


def _kraken_cancel(config: KrakenConfig, txid: str) -> dict[str, Any]:
    """Cancel a REAL open Kraken order — used to pull an unfilled post-only maker
    entry so it never sits resting past its cycle. Re-checks live gates first,
    same discipline as _kraken_add_order."""
    live_blocks = [b for b in _risk_blocks({"daily": {}, "positions": {}}, config) if b.startswith("live")]
    if live_blocks:
        return {"error": live_blocks}
    try:
        return _kraken_private(config, "/0/private/CancelOrder", {"txid": txid})
    except Exception as exc:
        return {"error": str(exc)}


def _kraken_query_order(config: KrakenConfig, txid: str) -> dict[str, Any]:
    """Actual executed fill (avg price / executed vol / fee / cost) for a placed order."""
    try:
        d = _kraken_private(config, "/0/private/QueryOrders", {"txid": txid})
        o = (d.get("result") or {}).get(txid) or {}
        return {"price": float(o.get("price") or 0.0), "vol_exec": float(o.get("vol_exec") or 0.0),
                "fee": float(o.get("fee") or 0.0), "cost": float(o.get("cost") or 0.0), "status": o.get("status")}
    except Exception as exc:
        return {"error": str(exc)}


def _price_decimals(price: float) -> int:
    """Conservative price-tick guess for Kraken USD pairs without an extra
    AssetPairs metadata call — sized for the BTC/ETH/SOL universe this bot trades."""
    if price >= 100:
        return 2
    if price >= 1:
        return 4
    return 8


def _record_live_open(
    state: dict[str, Any], pair: str, txid: str | None, entry: float, vol: float,
    fee: float, cost: float, signal: dict[str, Any],
) -> dict[str, Any]:
    """Shared fill-recording tail for both the taker and maker live-open paths —
    books the real fill exactly once, however the order got filled."""
    cash = float(state.get("cash") or 0.0)
    state["cash"] = round(cash - (cost + fee), 4)
    trade = {
        "id": f"krknlive-{txid}", "txid": txid, "ts": _now(), "pair": pair, "side": "long",
        "entry": round(entry, 6), "quote_price": signal.get("price"), "volume": round(vol, 10),
        "notional_usd": round(cost, 4), "entry_fee_usd": round(fee, 4),
        "edge": signal.get("edge"), "reason": signal.get("reason"), "mode": "live",
        "opened_cycle": int(state.get("cycle") or 0),
    }
    state.setdefault("positions", {})[pair] = trade
    state.setdefault("daily", {})["trades"] = int(state.get("daily", {}).get("trades") or 0) + 1
    _append_jsonl(TRADES_PATH, {"event": "open", **trade})
    return {"status": "live_opened", "txid": txid, "trade": trade}


def _live_open_taker(
    state: dict[str, Any], config: KrakenConfig, pair: str, price: float, target: float, signal: dict[str, Any],
) -> dict[str, Any]:
    """Existing market/taker path — used for tight spreads or whenever maker
    execution data (fusion/depth) isn't available (fail-open)."""
    resp = _kraken_add_order(config, pair, "buy", round(target / price, 8))
    if not isinstance(resp, dict) or resp.get("error"):
        return {"status": "order_error", "resp": resp}
    txid = ((resp.get("result") or {}).get("txid") or [None])[0]
    fill = _kraken_query_order(config, txid) if txid else {}
    entry = float(fill.get("price") or _slip_price(price, config, buy=True))
    vol = float(fill.get("vol_exec") or (target / price))
    fee = float(fill.get("fee") or _fee_usd(target, config))
    cost = float(fill.get("cost") or (vol * entry))
    return _record_live_open(state, pair, txid, entry, vol, fee, cost, signal)


def _live_open_maker(
    state: dict[str, Any], config: KrakenConfig, pair: str, target: float, signal: dict[str, Any], depth: dict[str, Any],
) -> dict[str, Any]:
    """Post-only limit entry at the best bid (the fee lever: avoids the taker
    side of the spread entirely). Places -> polls the fill ONCE -> if it isn't
    filled within this cycle, cancels and records NO position (retry next cycle)."""
    best_bid = float(depth.get("best_bid") or 0.0)
    if best_bid <= 0:
        return {"status": "blocked", "reason": "no valid best_bid for maker order"}
    limit_price = round(best_bid, _price_decimals(best_bid))
    volume = round(target / limit_price, 8)
    resp = _kraken_add_order(config, pair, "buy", volume, ordertype="limit", price=limit_price, oflags="post")
    if not isinstance(resp, dict) or resp.get("error"):
        return {"status": "order_error", "resp": resp}
    txid = ((resp.get("result") or {}).get("txid") or [None])[0]
    if not txid:
        return {"status": "order_error", "reason": "no txid returned for maker order", "resp": resp}
    fill = _kraken_query_order(config, txid)
    vol_exec = float(fill.get("vol_exec") or 0.0)
    filled = str(fill.get("status") or "").lower() == "closed" and vol_exec > 0
    if not filled:
        cancel_resp = _kraken_cancel(config, txid)
        _append_jsonl(LOG_PATH, {
            "ts": _now(), "event": "maker_order_cancelled", "pair": pair, "txid": txid,
            "limit_price": limit_price, "fill": fill, "cancel_resp": cancel_resp,
        })
        return {
            "status": "blocked",
            "reason": "maker limit order unfilled within cycle, cancelled, retry next cycle",
            "txid": txid,
        }
    entry = float(fill.get("price") or limit_price)
    fee = float(fill.get("fee") or _fee_usd(target, config))
    cost = float(fill.get("cost") or (vol_exec * entry))
    return _record_live_open(state, pair, txid, entry, vol_exec, fee, cost, signal)


def _live_open(state: dict[str, Any], config: KrakenConfig, signal: dict[str, Any]) -> dict[str, Any]:
    """Place a REAL buy, then RECORD the position from the real fill — so the bot
    tracks the holding, manages its exit, and never re-buys what it already owns.
    Uses a post-only maker limit order at the best bid when market_data_fusion
    flagged use_maker (spread wide enough that crossing it as a taker gives up
    more than the maker/taker fee differential); otherwise falls back to the
    existing market/taker order (also the fail-open path if fusion/depth errors)."""
    pair = signal["pair"]
    price = float(signal.get("price") or 0.0)
    if price <= 0:
        return {"status": "blocked", "reason": "no valid price"}
    target = _target_notional(state, config, float(signal.get("conviction") or 0.0))
    if target < KRAKEN_MIN_ORDER_USD:
        return {"status": "blocked", "reason": f"sized notional ${target:.2f} below Kraken ${KRAKEN_MIN_ORDER_USD:.2f} min order"}

    fusion = signal.get("fusion") or {}
    depth = None
    if fusion.get("use_maker") and market_data_fusion is not None:
        try:
            pair_code = KRAKEN_PAIR_MAP.get(pair, pair.replace("/", ""))
            depth = market_data_fusion._kraken_depth(pair_code)
        except Exception:
            depth = None

    if depth and depth.get("best_bid"):
        return _live_open_maker(state, config, pair, target, signal, depth)
    return _live_open_taker(state, config, pair, price, target, signal)


def _live_close(state: dict[str, Any], config: KrakenConfig, pair: str, price: float, reason: str) -> dict[str, Any]:
    """Place a REAL sell of the exact held volume, then book realized P&L."""
    pos = (state.get("positions") or {}).get(pair)
    if not pos:
        return {"status": "hold", "reason": "no position"}
    volume = float(pos.get("volume") or 0.0)
    if volume <= 0:
        return {"status": "order_error", "reason": "position has no recorded volume to sell"}
    resp = _kraken_add_order(config, pair, "sell", volume)
    if not isinstance(resp, dict) or resp.get("error"):
        return {"status": "order_error", "resp": resp}
    txid = ((resp.get("result") or {}).get("txid") or [None])[0]
    fill = _kraken_query_order(config, txid) if txid else {}
    exit_price = float(fill.get("price") or _slip_price(price, config, buy=False))
    proceeds = float(fill.get("cost") or (volume * exit_price))
    exit_fee = float(fill.get("fee") or _fee_usd(proceeds, config))
    net = proceeds - exit_fee
    notional = float(pos.get("notional_usd") or 0.0)
    entry_fee = float(pos.get("entry_fee_usd") or 0.0)
    pnl = round(net - notional - entry_fee, 4)
    (state.get("positions") or {}).pop(pair, None)
    state["cash"] = round(float(state.get("cash") or 0.0) + net, 4)
    state["realized_pnl"] = round(float(state.get("realized_pnl") or 0.0) + pnl, 4)
    state.setdefault("daily", {})["realized_pnl"] = round(float(state.get("daily", {}).get("realized_pnl") or 0.0) + pnl, 4)
    state.setdefault("daily", {})["trades"] = int(state.get("daily", {}).get("trades") or 0) + 1
    trade = {
        "id": pos.get("id"), "txid": txid, "ts": _now(), "pair": pair, "side": "long",
        "entry": pos.get("entry"), "exit": round(exit_price, 6), "volume": volume,
        "notional_usd": notional, "exit_fee_usd": round(exit_fee, 4),
        "pnl": pnl, "return_pct": round((pnl / max(1e-9, notional)) * 100, 4),
        "won": bool(pnl > 0), "reason": reason, "mode": "live", "price_source": "kraken",
    }
    _append_jsonl(TRADES_PATH, {"event": "close", **trade})
    return {"status": "live_closed", "txid": txid, "trade": trade}


def run_once() -> dict[str, Any]:
    config = KrakenConfig.from_env()
    state = load_state(config)
    prices, features = update_prices(state, config)
    state["peak_equity"] = round(max(float(state.get("peak_equity") or 0.0), equity(state)), 4)
    action: dict[str, Any] = {"status": "hold", "reason": "no qualifying signal"}

    # Exits first — run even when the kill switch blocks NEW entries, and place a
    # REAL sell in live mode (never a paper close on a real holding).
    close_fn = _live_close if config.mode == "live" else _paper_close
    for pair, pos in list((state.get("positions") or {}).items()):
        price = prices.get(pair)
        if not price:
            continue
        entry = float(pos.get("entry") or price)
        ret = (price / entry) - 1.0 if entry else 0.0
        age = int(state.get("cycle") or 0) - int(pos.get("opened_cycle") or 0)
        if ret >= config.take_profit_pct:
            action = close_fn(state, config, pair, price, f"take profit {ret * 100:.3f}%")
            break
        if ret <= -config.stop_loss_pct:
            action = close_fn(state, config, pair, price, f"stop loss {ret * 100:.3f}%")
            break
        if age >= config.max_hold_cycles:
            action = close_fn(state, config, pair, price, f"max hold {age} cycles")
            break

    if action.get("status") == "hold":
        blocks = _risk_blocks(state, config)
        if blocks:
            action = {"status": "blocked", "reasons": blocks}
        else:
            fear_greed: dict[str, Any] = {}
            if market_intel is not None:
                try:
                    fear_greed = market_intel.crypto_fear_greed()
                except Exception:
                    fear_greed = {}
            signals = [
                s for s in (_score_pair(features, p, config, fear_greed) for p in config.pairs)
                if s and s["pair"] not in state.get("positions", {})
            ]
            signals.sort(key=lambda row: row["edge"], reverse=True)
            best = signals[0] if signals else None
            state["last_signal"] = best or {}
            if best and best["edge"] >= config.min_edge:
                edge_ok, expected_move, required_move = _edge_gate(best, config)
                if not edge_ok:
                    reason = (
                        f"edge-gate reject {best['pair']}: expected_move={expected_move:.5f} "
                        f"<= round_trip_cost*margin={required_move:.5f} "
                        f"(fee_bps={config.taker_fee_bps}, slip_bps={config.slippage_bps}, margin={config.edge_fee_margin})"
                    )
                    action = {"status": "blocked", "reason": reason}
                    _append_jsonl(LOG_PATH, {
                        "ts": _now(), "event": "edge_gate_reject", "pair": best["pair"],
                        "expected_move": expected_move, "required_move": required_move,
                        "edge_fee_margin": config.edge_fee_margin,
                    })
                else:
                    # market_data_fusion.fuse() entry gate: only enter on a confluence-backed
                    # long call. fusion == {} (module unavailable/errored) fails open to the
                    # legacy edge-only behavior above.
                    fusion = _fuse_pair(best["pair"])
                    conviction = float(fusion.get("conviction") or 0.0)
                    direction = fusion.get("direction")
                    fusion_ok = (not fusion) or (direction == "long" and conviction >= config.min_conviction)
                    if not fusion_ok:
                        reason = (
                            f"fusion-gate reject {best['pair']}: direction={direction} "
                            f"conviction={conviction:.3f} < min_conviction={config.min_conviction}"
                        )
                        action = {"status": "blocked", "reason": reason}
                        _append_jsonl(LOG_PATH, {
                            "ts": _now(), "event": "fusion_gate_reject", "pair": best["pair"],
                            "direction": direction, "conviction": conviction,
                            "min_conviction": config.min_conviction,
                        })
                    else:
                        best["conviction"] = conviction
                        best["fusion"] = fusion
                        action = _live_open(state, config, best) if config.mode == "live" else _paper_open(state, config, best)

    state["last_action"] = action
    save_state(state)
    status = build_status(state, config, action=action)
    _append_jsonl(LOG_PATH, {"ts": _now(), "event": "cycle", "status": status})
    return status


def build_status(state: dict[str, Any], config: KrakenConfig, *, action: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = {
        "ok": True,
        "generated_at": _now(),
        "mode": config.mode,
        "exchange": "kraken",
        "cash": round(float(state.get("cash") or 0.0), 4),
        "equity": equity(state),
        "starting_cash": round(float(state.get("starting_cash") or config.starting_cash), 4),
        "realized_pnl": round(float(state.get("realized_pnl") or 0.0), 4),
        "positions": state.get("positions") or {},
        "daily": state.get("daily") or {},
        "prices": state.get("prices") or {},
        "last_signal": state.get("last_signal") or {},
        "last_action": action or state.get("last_action") or {},
        "risk": asdict(config) | {"kraken_api_key": bool(config.kraken_api_key), "kraken_api_secret": bool(config.kraken_api_secret)},
        "live_trading_possible": bool(
            config.mode == "live"
            and config.live_allowed
            and LIVE_ARM_PATH.exists()
            and config.kraken_api_key
            and config.kraken_api_secret
        ),
        "kill_switch": KILL_PATH.exists(),
        "important": "Kraken remains paper-only until MODE=live + ALLOW_LIVE=1 + kraken_live_armed.json + API key/secret are ALL present.",
    }
    _write_json(STATUS_PATH, payload)
    return payload


def status_text() -> str:
    config = KrakenConfig.from_env()
    state = load_state(config)
    status = build_status(state, config)
    signal = status.get("last_signal") or {}
    action = status.get("last_action") or {}
    return (
        "Kraken Bot Status\n"
        f"- Mode: {status['mode']} ({'LIVE ARMED' if status['live_trading_possible'] else 'live disabled'})\n"
        f"- Equity: ${status['equity']:.2f} from ${status['starting_cash']:.2f}; cash ${status['cash']:.2f}\n"
        f"- Realized P/L: ${status['realized_pnl']:.2f}; positions: {len(status.get('positions') or {})}\n"
        f"- Daily trades: {status.get('daily', {}).get('trades', 0)}\n"
        f"- Last signal: {signal.get('pair', 'none')} {signal.get('side', '')} edge={signal.get('edge', 'n/a')}\n"
        f"- Last action: {action.get('status', 'none')}"
    )


def report_text() -> str:
    closes: list[dict[str, Any]] = []
    if TRADES_PATH.exists():
        for line in TRADES_PATH.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if row.get("event") == "close":
                closes.append(row)
    n = len(closes)
    if n == 0:
        return "Kraken Bot Report\n- No closed trades yet."
    pnls = [float(r.get("pnl") or 0.0) for r in closes]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    total_pnl = sum(pnls)
    win_rate = (len(wins) / n) * 100
    expectancy = total_pnl / n
    avg_win = (sum(wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(losses) / len(losses)) if losses else 0.0
    return (
        "Kraken Bot Report\n"
        f"- Closed trades: {n}\n"
        f"- Win rate: {win_rate:.1f}% ({len(wins)}/{n})\n"
        f"- Expectancy per trade: ${expectancy:.4f}\n"
        f"- Avg win: ${avg_win:.4f}; avg loss: ${avg_loss:.4f}\n"
        f"- Total realized P/L: ${total_pnl:.4f}\n"
        "- No guarantees: past paper/live fills do not predict future results."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Kraken Spot paper/live-gated bot.")
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
    if args.status:
        print(status_text())
        return 0
    if args.report:
        print(report_text())
        return 0
    if args.once:
        print(json.dumps(run_once(), indent=2, sort_keys=True, default=str))
        return 0
    while True:
        run_once()
        time.sleep(max(60, KrakenConfig.from_env().loop_interval_s))


if __name__ == "__main__":
    raise SystemExit(main())
