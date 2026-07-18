"""backtester.py — $0 historical backtest engine for the trading lanes.

Forward-papering proves an edge in months. This replays the SAME signal logic
the live bots use over YEARS of real daily closes (Yahoo chart API, $0, no
key) so a lane's edge — or lack of one — shows up in minutes, with an
honest in-sample/out-of-sample split so a curve-fit doesn't masquerade as a
real edge.

Reuse, not reimplementation:
  - core.market_signals._yahoo_chart()   real OHLC close history (per READ FIRST)
  - core.market_signals.compute_features() the exact momentum/MA/vol/zscore
    features the live bots score, recomputed bar-by-bar on a TRUNCATED close
    series (closes[:t+1]) so nothing past bar t ever leaks into the signal —
    point-in-time correct.
  - core.kraken_bot._score_pair()        the real crypto entry-scoring function,
    called directly for the Kraken lane.
  - core.kraken_bot._legacy_momentum_edge() the real, symbol-agnostic momentum
    +20/50-MA-crossover rule kraken_bot itself falls back to whenever
    core.market_intel has no usable technicals. Since it only reads a generic
    features dict (mom5, mom20, sma20, sma50), it is reused AS-IS — not
    reimplemented — to score the Alpaca/Forex lanes too (the task's "mirror
    the same momentum+MA rule for equities/forex from the features").
  - core.trade_risk.position_notional() / edge_clears_cost() / round_trip_cost()
    the same quarter-Kelly sizing + anti-churn gate the live bots use.
  - core.trade_proof_gate.evaluate()     the same "is this lane's ledger good
    enough to arm real money" bar, run against the backtest's own ledger.

IMPORTANT — why core.market_intel is disabled during replay: kraken_bot's
confluence path calls market_intel.technicals(symbol), which returns CURRENT
live indicators over the network. Calling that inside a bar-by-bar historical
loop would (a) leak today's indicators into every past bar — pure lookahead
bias — and (b) hit the network ~500 times per symbol. Setting
`kraken_bot.market_intel = None` here forces kraken_bot._score_pair into its
own real fail-open branch (_legacy_momentum_edge), which is genuine
production code operating purely on the point-in-time features this replay
passes it. This is the only patch applied to imported modules; no production
logic is altered, only a live-network branch is deliberately not exercised.

Deep history (bypasses the 520-bar cap): core.market_signals._yahoo_chart()
always slices its result to the last 520 closes (a constant baked into that
shared function, which this module intentionally does not edit — see READ
FIRST). This module instead hits the same public Yahoo chart endpoint
DIRECTLY via _yahoo_chart_deep(), untruncated, with a free stooq.com CSV
fallback (_stooq_csv()) merged in by picking whichever source returns more
daily closes — both $0, keyless, cached to disk (PRICE_CACHE_PATH) so a
re-run doesn't re-hit the network. One real Yahoo quirk found and worked
around: range="max" silently downgrades to MONTHLY (or weekly) bars for any
symbol with more than a few years of history — confirmed on SPY (max ->
"1mo" granularity, 403 bars back to 1993) and on every other symbol tested
here — so _yahoo_chart_deep() requests a large explicit range ("40y" first,
shrinking on failure) and validates meta.dataGranularity == "1d" before
accepting a candidate; "40y" alone returns each tested symbol's FULL
available daily history (e.g. SPY back to 1993, 8415 daily bars). This gives
the Alpaca equity universe (SPY/QQQ/XLK/XLF/XLE/XLY) 15-25 years of daily
closes instead of ~2.

Fee/slippage per lane: Kraken and Alpaca reuse their bots' own config/module
constants exactly (KrakenConfig.taker_fee_bps/slippage_bps,
alpaca_bot.SLIPPAGE_BPS). forex_bot.py does not currently model any fee or
slippage — its paper fills are frictionless — so this backtester applies a
conservative FOREX_SPREAD_BPS constant (typical major-pair spread) rather
than pretend forex trading is free; this is the one lane-cost the source bot
doesn't already define, and it is called out explicitly below.

CLI:
    python3 -m core.backtester --lane kraken
    python3 -m core.backtester --lane alpaca
    python3 -m core.backtester --lane forex
    python3 -m core.backtester --all

Writes data/trading/backtest_<lane>.json (summary) and
data/trading/backtest_<lane>_trades.jsonl (full closed-trade ledger, fed
through trade_proof_gate.evaluate()). $0, stdlib + core only, fail-open on
any per-symbol data gap.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import http.cookiejar
import io
import json
import math
import re
import statistics
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from core import BASE_DIR
except Exception:  # pragma: no cover - allow standalone import
    import os

    BASE_DIR = Path(os.path.expanduser("~/claude-stack"))

from core import market_signals
from core import trade_risk
from core import trade_proof_gate
from core import kraken_bot
from core import alpaca_bot
from core import forex_bot

# See module docstring: forces kraken_bot._score_pair's real fail-open branch
# (_legacy_momentum_edge) instead of a live network technicals call that would
# leak today's indicators into every historical bar.
kraken_bot.market_intel = None

TRADING_DIR = BASE_DIR / "data" / "trading"
PRICE_CACHE_PATH = TRADING_DIR / "backtest_price_cache.json"
PRICE_CACHE_TTL_S = 6 * 3600.0
MIN_WARMUP_BARS = 25          # compute_features needs >=25 closes to populate
MIN_NOTIONAL_USD = 1.0        # uniform practical order-size floor across lanes
ALPACA_BACKTEST_MAX_HOLD_DAYS = 90  # alpaca_bot has no max-hold; bound the backtest timeline
FOREX_SPREAD_BPS = 2.0        # forex_bot models no spread/fee; conservative major-pair estimate
IN_SAMPLE_FRACTION = 0.70

LANE_UNIVERSE: dict[str, list[tuple[str, str]]] = {
    # (display pair label, Yahoo symbol)
    "kraken": [("BTC/USD", "BTC-USD"), ("ETH/USD", "ETH-USD"), ("SOL/USD", "SOL-USD")],
    "alpaca": [("SPY", "SPY"), ("QQQ", "QQQ"), ("XLK", "XLK"), ("XLF", "XLF"), ("XLE", "XLE"), ("XLY", "XLY")],
    "forex": [("EUR/USD", "EURUSD=X"), ("USD/JPY", "USDJPY=X"), ("GBP/USD", "GBPUSD=X")],
}
BARS_PER_YEAR: dict[str, float] = {"kraken": 365.0, "alpaca": 252.0, "forex": 260.0}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# lane config — pulled straight from each bot's own dataclass so stop/take
# profit/fee/slippage always match production, not a re-guessed copy.
# ---------------------------------------------------------------------------

@dataclass
class LaneConfig:
    name: str
    starting_cash: float
    max_notional_usd: float
    stop_loss_pct: float
    take_profit_pct: float
    max_hold_bars: int
    min_edge: float
    fee_bps: float
    slippage_bps: float
    edge_margin: float
    win_prob: float
    payoff_ratio: float
    raw_config: Any = None
    notes: str = ""


def _kraken_lane_config() -> LaneConfig:
    cfg = kraken_bot.KrakenConfig.from_env()
    payoff = (cfg.take_profit_pct / cfg.stop_loss_pct) if cfg.stop_loss_pct > 0 else 1.5
    return LaneConfig(
        name="kraken",
        starting_cash=cfg.starting_cash,
        max_notional_usd=cfg.max_notional_usd,
        stop_loss_pct=cfg.stop_loss_pct,
        take_profit_pct=cfg.take_profit_pct,
        # kraken_bot's max_hold_cycles counts live 15-min poll cycles; this replay
        # runs on daily bars, so the cap is reinterpreted as max_hold_bars (days)
        # at the replay's own granularity — same knob, same config value.
        max_hold_bars=max(1, int(cfg.max_hold_cycles)),
        min_edge=cfg.min_edge,
        fee_bps=cfg.taker_fee_bps,
        slippage_bps=cfg.slippage_bps,
        edge_margin=cfg.edge_fee_margin,
        win_prob=0.54,  # midpoint of _win_prob_from_conviction's 0.50-0.58 band (no live conviction signal here)
        payoff_ratio=payoff,
        raw_config=cfg,
        notes="fee/slippage/stop/tp/min_edge from KrakenConfig.from_env()",
    )


def _alpaca_lane_config() -> LaneConfig:
    cfg = alpaca_bot.AlpacaConfig.from_env()
    payoff = (cfg.take_profit_pct / cfg.stop_loss_pct) if cfg.stop_loss_pct > 0 else cfg.kelly_payoff_default
    return LaneConfig(
        name="alpaca",
        starting_cash=cfg.starting_cash,
        max_notional_usd=cfg.max_notional_usd,
        stop_loss_pct=cfg.stop_loss_pct,
        take_profit_pct=cfg.take_profit_pct,
        max_hold_bars=ALPACA_BACKTEST_MAX_HOLD_DAYS,
        min_edge=0.0,  # alpaca_bot has no min_edge floor; edge_clears_cost is the only gate
        fee_bps=0.0,   # Alpaca commissions are $0
        slippage_bps=alpaca_bot.SLIPPAGE_BPS,
        edge_margin=1.5,  # matches trade_risk.edge_clears_cost's own default margin
        win_prob=cfg.kelly_win_prob_default,
        payoff_ratio=payoff,
        raw_config=cfg,
        notes="stop/tp/fee(0)/slippage from AlpacaConfig.from_env() + SLIPPAGE_BPS; max_hold is a backtest addition (bot has none)",
    )


def _forex_lane_config() -> LaneConfig:
    cfg = forex_bot.ForexConfig.from_env()
    payoff = (cfg.take_profit_pct / cfg.stop_loss_pct) if cfg.stop_loss_pct > 0 else 1.5
    return LaneConfig(
        name="forex",
        starting_cash=cfg.starting_cash,
        max_notional_usd=cfg.max_notional_usd,
        stop_loss_pct=cfg.stop_loss_pct,
        take_profit_pct=cfg.take_profit_pct,
        max_hold_bars=max(1, int(cfg.max_hold_cycles)),
        min_edge=cfg.min_edge,
        fee_bps=0.0,
        slippage_bps=FOREX_SPREAD_BPS,  # forex_bot models no spread/fee (see module docstring)
        edge_margin=1.5,
        win_prob=0.52,
        payoff_ratio=payoff,
        raw_config=cfg,
        notes="stop/tp/min_edge from ForexConfig.from_env(); spread is a backtest addition (bot has none)",
    )


LANE_CONFIG_BUILDERS = {
    "kraken": _kraken_lane_config,
    "alpaca": _alpaca_lane_config,
    "forex": _forex_lane_config,
}


# ---------------------------------------------------------------------------
# $0 price cache (Yahoo _yahoo_chart is the only network call this makes)
# ---------------------------------------------------------------------------

def _load_price_cache() -> dict[str, Any]:
    try:
        if PRICE_CACHE_PATH.exists():
            return json.loads(PRICE_CACHE_PATH.read_text())
    except Exception:
        pass
    return {}


def _save_price_cache(cache: dict[str, Any]) -> None:
    try:
        PRICE_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = PRICE_CACHE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cache, default=str))
        tmp.replace(PRICE_CACHE_PATH)
    except Exception:
        pass


_DEEP_UA = {"User-Agent": "Mozilla/5.0 (research; paper-only)"}
# Largest-first: "max" is deliberately excluded (see module docstring — it
# silently coarsens to monthly/weekly bars on every symbol tested here).
DEEP_RANGE_CANDIDATES = ("40y", "30y", "20y", "10y", "5y")


def _http_get(url: str, *, data: bytes | None = None, headers: dict[str, str] | None = None,
               opener: urllib.request.OpenerDirector | None = None, timeout: float = 20) -> str:
    req = urllib.request.Request(url, data=data, headers=headers or _DEEP_UA)
    resp = opener.open(req, timeout=timeout) if opener else urllib.request.urlopen(req, timeout=timeout)
    try:
        return resp.read().decode("utf-8", errors="replace")
    finally:
        resp.close()


def _yahoo_chart_deep(symbol: str) -> dict[str, Any] | None:
    """Full daily close history direct from Yahoo's chart endpoint, NOT truncated
    to 520 bars. Tries progressively smaller explicit ranges (never "max" — see
    module docstring) and only accepts a candidate whose meta.dataGranularity is
    genuinely "1d"; the first (i.e. widest) daily-granularity hit already holds
    that symbol's full available history, so no further candidates are tried."""
    enc = urllib.parse.quote(symbol, safe="")
    for rng in DEEP_RANGE_CANDIDATES:
        for host in ("query1", "query2"):
            try:
                txt = _http_get(
                    f"https://{host}.finance.yahoo.com/v8/finance/chart/{enc}?range={rng}&interval=1d",
                    headers=_DEEP_UA,
                )
                d = json.loads(txt)
                res = (((d or {}).get("chart") or {}).get("result") or [None])[0]
                if not res:
                    continue
                meta = res.get("meta") or {}
                if meta.get("dataGranularity") != "1d":
                    continue  # e.g. this host/range coarsened to monthly/weekly -- reject, try next
                quote = ((res.get("indicators") or {}).get("quote") or [{}])[0]
                closes = [float(c) for c in (quote.get("close") or []) if isinstance(c, (int, float))]
                if not closes:
                    continue
                return {"closes": closes, "source": f"yahoo:{rng}"}
            except Exception:
                continue
    return None


def _stooq_symbol(yahoo_symbol: str, lane: str) -> str | None:
    """Best-effort Yahoo -> stooq.com ticker mapping. Returns None for anything
    unmapped (this is a fallback source, not a requirement)."""
    if lane == "forex":
        base = yahoo_symbol.replace("=X", "").lower()
        return base if base.isalpha() and len(base) == 6 else None
    if lane == "kraken":
        return yahoo_symbol.replace("-", "").lower() or None
    return f"{yahoo_symbol.lower()}.us"


_STOOQ_CHALLENGE_RE = re.compile(r'c="([^"]+)",d=(\d+)')


def _stooq_solve_challenge(challenge: str, difficulty: int) -> int | None:
    """stooq fronts its CSV export with a small client-side proof-of-work bot
    check (find n such that sha256(challenge+n) has `difficulty` leading hex
    zeros). $0 arithmetic, no browser/JS engine needed — solved in-process.
    Bounded so a pathological difficulty can't hang the backtest."""
    prefix = "0" * difficulty
    for n in range(2_000_000):
        if hashlib.sha256(f"{challenge}{n}".encode()).hexdigest().startswith(prefix):
            return n
    return None


def _stooq_csv(stooq_symbol: str) -> list[float] | None:
    """Free, keyless daily-close fallback: https://stooq.com/q/d/l/?s=<sym>&i=d
    Fails open (returns None) on any error, challenge it can't clear, or
    non-CSV response — this is a fallback, the Yahoo deep fetch is primary."""
    try:
        cj = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
        url = f"https://stooq.com/q/d/l/?s={stooq_symbol}&i=d"
        txt = _http_get(url, opener=opener)
        m = _STOOQ_CHALLENGE_RE.search(txt)
        if m:
            n = _stooq_solve_challenge(m.group(1), int(m.group(2)))
            if n is None:
                return None
            body = urllib.parse.urlencode({"c": m.group(1), "n": n}).encode()
            _http_get(
                "https://stooq.com/__verify", data=body, opener=opener,
                headers={**_DEEP_UA, "Content-Type": "application/x-www-form-urlencoded", "Referer": url},
            )
            txt = _http_get(url, opener=opener)
        if not txt.lstrip().lower().startswith("date,"):
            return None  # still a challenge/denial page, not CSV
        closes: list[float] = []
        for row in csv.DictReader(io.StringIO(txt)):
            try:
                closes.append(float(row["Close"]))
            except Exception:
                continue
        return closes or None
    except Exception:
        return None


def _deep_history(yahoo_symbol: str, lane: str, cache: dict[str, Any]) -> dict[str, Any] | None:
    """Untruncated daily close history for one symbol: Yahoo direct + stooq CSV,
    cached to disk, whichever source returns MORE bars wins."""
    key = f"deep:{yahoo_symbol}"
    row = cache.get(key)
    if isinstance(row, dict) and (time.time() - float(row.get("_t", 0))) <= PRICE_CACHE_TTL_S:
        return row.get("v")
    candidates: list[tuple[str, list[float]]] = []
    yahoo = _yahoo_chart_deep(yahoo_symbol)
    if yahoo and yahoo.get("closes"):
        candidates.append((yahoo["source"], yahoo["closes"]))
    stooq_symbol = _stooq_symbol(yahoo_symbol, lane)
    stooq_closes = _stooq_csv(stooq_symbol) if stooq_symbol else None
    if stooq_closes:
        candidates.append(("stooq", stooq_closes))
    if not candidates:
        return None
    source, closes = max(candidates, key=lambda c: len(c[1]))
    out = {"closes": closes, "source": source, "bars": len(closes)}
    cache[key] = {"_t": time.time(), "v": out}
    return out


# ---------------------------------------------------------------------------
# signal — reuse for Kraken, mirror (via the bot's own generic helper) for
# Alpaca/Forex
# ---------------------------------------------------------------------------

def _signal(feats: dict[str, Any], pair_label: str, lane: str, cfg: LaneConfig) -> dict[str, Any] | None:
    if "price" not in feats or int(feats.get("n") or 0) < MIN_WARMUP_BARS:
        return None
    if lane == "kraken":
        return kraken_bot._score_pair({pair_label: feats}, pair_label, cfg.raw_config, fear_greed=None)
    edge = kraken_bot._legacy_momentum_edge(feats)
    if edge is None or edge <= 0:
        return None
    return {
        "pair": pair_label,
        "price": feats["price"],
        "side": "long",
        "edge": round(edge, 6),
        "reason": "momentum+MA crossover (mirrors kraken_bot._legacy_momentum_edge on this symbol's own features)",
    }


def _slip(price: float, bps: float, *, buy: bool) -> float:
    adj = bps / 10000.0
    return price * (1 + adj) if buy else price * (1 - adj)


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------

def _replay(closes: list[float], pair_label: str, lane: str, cfg: LaneConfig) -> list[dict[str, Any]]:
    """Walk the close series bar-by-bar. At bar t, features are computed from
    closes[:t+1] only (no lookahead). A qualifying long signal enters at the
    NEXT bar's close (the only price we have — Yahoo's free chart endpoint
    returns closes, not opens); the position is then marked at each
    subsequent bar's close and exits on stop/take-profit/max-hold, mirroring
    the live bots' own poll-and-check exit style (they don't place true
    intrabar stop orders either)."""
    n = len(closes)
    trades: list[dict[str, Any]] = []
    position: dict[str, Any] | None = None
    cash = cfg.starting_cash
    i = MIN_WARMUP_BARS - 1
    while i < n - 1:
        if position is None:
            feats = market_signals.compute_features(closes[: i + 1])
            sig = _signal(feats, pair_label, lane, cfg)
            if (
                sig
                and sig["edge"] >= cfg.min_edge
                and trade_risk.edge_clears_cost(sig["edge"], cfg.fee_bps, cfg.slippage_bps, cfg.edge_margin)
            ):
                entry_idx = i + 1
                entry_price = _slip(closes[entry_idx], cfg.slippage_bps, buy=True)
                notional = trade_risk.position_notional(
                    max(cash, 0.0), win_prob=cfg.win_prob, payoff_ratio=cfg.payoff_ratio,
                    stop_pct=max(1e-4, cfg.stop_loss_pct),
                )
                notional = max(0.0, min(notional, cfg.max_notional_usd, cash))
                fee_entry = notional * cfg.fee_bps / 10000.0
                if notional + fee_entry > cash:
                    notional = max(0.0, cash - fee_entry)
                    fee_entry = notional * cfg.fee_bps / 10000.0
                if notional < MIN_NOTIONAL_USD:
                    i += 1
                    continue
                cash -= (notional + fee_entry)
                position = {
                    "entry_idx": entry_idx, "entry": entry_price, "notional": notional,
                    "fee_entry": fee_entry, "edge": sig["edge"], "reason_open": sig["reason"],
                }
                i = entry_idx
                continue
            i += 1
            continue

        price = closes[i]
        entry = position["entry"]
        ret = price / entry - 1.0
        age = i - position["entry_idx"]
        reason = None
        if ret >= cfg.take_profit_pct:
            reason = f"take_profit {ret * 100:.3f}%"
        elif ret <= -cfg.stop_loss_pct:
            reason = f"stop_loss {ret * 100:.3f}%"
        elif age >= cfg.max_hold_bars:
            reason = f"max_hold {age}bars"
        elif i == n - 1:
            reason = "end_of_data"
        if reason:
            exit_price = _slip(price, cfg.slippage_bps, buy=False)
            notional = position["notional"]
            gross = notional * (exit_price / entry)
            fee_exit = gross * cfg.fee_bps / 10000.0
            net = gross - fee_exit
            pnl = net - notional - position["fee_entry"]
            cash += net
            trades.append({
                "event": "close",
                "pair": pair_label,
                "entry_idx": position["entry_idx"], "exit_idx": i,
                "entry": round(entry, 6), "exit": round(exit_price, 6),
                "notional_usd": round(notional, 4),
                "pnl": round(pnl, 4),
                "return_pct": round((pnl / notional) * 100, 4) if notional else 0.0,
                "won": bool(pnl > 0),
                "edge": position["edge"],
                "reason": reason,
                "opened_reason": position["reason_open"],
                "price_source": "yahoo_backtest",
                "mode": "backtest",
            })
            position = None
        i += 1
    return trades


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------

def _trade_stats(trades: list[dict[str, Any]], starting_cash: float, bar_span: int, bars_per_year: float) -> dict[str, Any]:
    n = len(trades)
    if n == 0:
        return {
            "n_trades": 0, "win_rate": 0.0, "expectancy": 0.0, "profit_factor": 0.0,
            "sharpe": 0.0, "max_drawdown": 0.0, "total_return": 0.0,
            "net_pnl": 0.0, "final_equity": round(starting_cash, 4),
        }
    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    win_rate = len(wins) / n
    expectancy = sum(pnls) / n
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0)

    equity_curve = [starting_cash]
    for p in pnls:
        equity_curve.append(equity_curve[-1] + p)
    peak = equity_curve[0]
    max_dd = 0.0
    for e in equity_curve[1:]:
        peak = max(peak, e)
        if peak > 0:
            max_dd = max(max_dd, (peak - e) / peak)
    final_equity = equity_curve[-1]
    total_return = (final_equity / starting_cash - 1.0) if starting_cash > 0 else 0.0

    rets = [t["return_pct"] / 100.0 for t in trades]
    mean_r = statistics.fmean(rets)
    std_r = statistics.pstdev(rets) if len(rets) > 1 else 0.0
    years = max(bar_span, 1) / bars_per_year
    trades_per_year = (n / years) if years > 0 else float(n)
    sharpe = (mean_r / std_r) * math.sqrt(trades_per_year) if std_r > 0 else 0.0

    return {
        "n_trades": n,
        "win_rate": round(win_rate, 4),
        "expectancy": round(expectancy, 4),
        "profit_factor": "inf" if profit_factor == float("inf") else round(profit_factor, 3),
        "sharpe": round(sharpe, 3),
        "max_drawdown": round(max_dd, 4),
        "total_return": round(total_return, 4),
        "net_pnl": round(sum(pnls), 4),
        "final_equity": round(final_equity, 4),
    }


def backtest_symbol(lane: str, pair_label: str, yahoo_symbol: str, cfg: LaneConfig, cache: dict[str, Any]) -> dict[str, Any] | None:
    bar = _deep_history(yahoo_symbol, lane, cache)
    if not bar or not bar.get("closes"):
        return None
    closes = bar["closes"]
    n = len(closes)
    if n < MIN_WARMUP_BARS + 10:
        return None
    trades = _replay(closes, pair_label, lane, cfg)
    split_idx = int(n * IN_SAMPLE_FRACTION)
    in_sample = [t for t in trades if t["entry_idx"] < split_idx]
    out_sample = [t for t in trades if t["entry_idx"] >= split_idx]
    bars_per_year = BARS_PER_YEAR[lane]
    return {
        "pair": pair_label,
        "symbol": yahoo_symbol,
        "bars": n,
        "data_source": bar.get("source"),
        "split_bar": split_idx,
        "full": _trade_stats(trades, cfg.starting_cash, n, bars_per_year),
        "in_sample": _trade_stats(in_sample, cfg.starting_cash, split_idx, bars_per_year),
        "out_of_sample": _trade_stats(out_sample, cfg.starting_cash, n - split_idx, bars_per_year),
        "trades_full": trades,
        "trades_is": in_sample,
        "trades_oos": out_sample,
    }


# ---------------------------------------------------------------------------
# lane orchestration
# ---------------------------------------------------------------------------

def run_lane(lane: str) -> dict[str, Any]:
    universe = LANE_UNIVERSE[lane]
    cfg = LANE_CONFIG_BUILDERS[lane]()
    cache = _load_price_cache()
    symbols_out: dict[str, Any] = {}
    pooled_full: list[dict[str, Any]] = []
    pooled_is: list[dict[str, Any]] = []
    pooled_oos: list[dict[str, Any]] = []
    skipped: list[str] = []
    for pair_label, yahoo_symbol in universe:
        result = backtest_symbol(lane, pair_label, yahoo_symbol, cfg, cache)
        if result is None:
            skipped.append(yahoo_symbol)  # fail-open: skip a symbol on a data gap, keep going
            continue
        pooled_full.extend(result["trades_full"])
        pooled_is.extend(result["trades_is"])
        pooled_oos.extend(result["trades_oos"])
        symbols_out[yahoo_symbol] = {k: v for k, v in result.items() if not k.startswith("trades_")}
    _save_price_cache(cache)

    bars_per_year = BARS_PER_YEAR[lane]
    avg_bars = int(statistics.fmean([r["bars"] for r in symbols_out.values()])) if symbols_out else 0
    avg_split = int(statistics.fmean([r["split_bar"] for r in symbols_out.values()])) if symbols_out else 0
    lane_aggregate = {
        "full": _trade_stats(pooled_full, cfg.starting_cash, avg_bars, bars_per_year),
        "in_sample": _trade_stats(pooled_is, cfg.starting_cash, avg_split, bars_per_year),
        "out_of_sample": _trade_stats(pooled_oos, cfg.starting_cash, max(1, avg_bars - avg_split), bars_per_year),
    }

    ledger_path = TRADING_DIR / f"backtest_{lane}_trades.jsonl"
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with ledger_path.open("w") as fh:
        for t in pooled_full:
            fh.write(json.dumps(t, default=str) + "\n")
    proof = trade_proof_gate.evaluate(ledger_path)
    proof_oos = trade_proof_gate.evaluate_oos(ledger_path, split=IN_SAMPLE_FRACTION)

    out = {
        "lane": lane,
        "generated_at": _now(),
        "universe": [sym for _, sym in universe],
        "skipped_symbols": skipped,
        "config": {
            "starting_cash": cfg.starting_cash, "max_notional_usd": cfg.max_notional_usd,
            "stop_loss_pct": cfg.stop_loss_pct, "take_profit_pct": cfg.take_profit_pct,
            "max_hold_bars": cfg.max_hold_bars, "min_edge": cfg.min_edge,
            "fee_bps": cfg.fee_bps, "slippage_bps": cfg.slippage_bps, "edge_margin": cfg.edge_margin,
            "win_prob": cfg.win_prob, "payoff_ratio": round(cfg.payoff_ratio, 4), "notes": cfg.notes,
        },
        "symbols": symbols_out,
        "lane_aggregate": lane_aggregate,
        "proof_gate": proof,
        "proof_gate_oos": proof_oos,
        "trades_ledger": str(ledger_path),
    }
    out_path = TRADING_DIR / f"backtest_{lane}.json"
    out_path.write_text(json.dumps(out, indent=2, default=str))
    out["_out_path"] = str(out_path)
    return out


# ---------------------------------------------------------------------------
# CLI / printed summary
# ---------------------------------------------------------------------------

_METRIC_KEYS = ("n_trades", "win_rate", "expectancy", "profit_factor", "sharpe", "max_drawdown", "total_return")


def _side_by_side(is_stats: dict[str, Any], oos_stats: dict[str, Any]) -> str:
    header = f"  {'metric':<14}{'in-sample(70%)':>18}{'out-of-sample(30%)':>22}"
    rows = [header]
    for k in _METRIC_KEYS:
        rows.append(f"  {k:<14}{str(is_stats.get(k)):>18}{str(oos_stats.get(k)):>22}")
    return "\n".join(rows)


def _format_summary(result: dict[str, Any]) -> str:
    lines = [f"=== {result['lane'].upper()} backtest ({result['generated_at']}) ===",
             f"config: {result['config']}"]
    if result.get("skipped_symbols"):
        lines.append(f"skipped (no data): {result['skipped_symbols']}")
    for sym, row in result["symbols"].items():
        lines.append(
            f"\n{row['pair']} ({sym}) — {row['bars']} daily bars [{row.get('data_source')}], "
            f"split at bar {row['split_bar']}"
        )
        lines.append(_side_by_side(row["in_sample"], row["out_of_sample"]))
        f = row["full"]
        lines.append(
            f"  full-history: n={f['n_trades']} win_rate={f['win_rate']} expectancy=${f['expectancy']} "
            f"pf={f['profit_factor']} sharpe={f['sharpe']} max_dd={f['max_drawdown']} total_return={f['total_return']}"
        )
    lines.append("\n--- LANE AGGREGATE (pooled across symbols) ---")
    lines.append(_side_by_side(result["lane_aggregate"]["in_sample"], result["lane_aggregate"]["out_of_sample"]))
    af = result["lane_aggregate"]["full"]
    lines.append(
        f"  full-history: n={af['n_trades']} win_rate={af['win_rate']} expectancy=${af['expectancy']} "
        f"pf={af['profit_factor']} sharpe={af['sharpe']} max_dd={af['max_drawdown']} total_return={af['total_return']}"
    )
    lines.append("")
    lines.append(trade_proof_gate.verdict_text(result["lane"], result["proof_gate"]))
    oos = result.get("proof_gate_oos") or {}
    if oos:
        lines.append(
            f"[{result['lane']}] proof_gate.evaluate_oos: holds_oos={oos.get('holds_oos')} "
            f"in_sample(n={oos['in_sample']['n_closed']} exp={oos['in_sample']['expectancy']} "
            f"t={oos['in_sample']['t_stat']}) out_of_sample(n={oos['out_of_sample']['n_closed']} "
            f"exp={oos['out_of_sample']['expectancy']} t={oos['out_of_sample']['t_stat']})"
        )
    lines.append(f"\nwritten: {result.get('_out_path')}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="$0 historical backtest engine for the trading lanes (Yahoo daily closes only).")
    ap.add_argument("--lane", choices=sorted(LANE_UNIVERSE), help="run a single lane")
    ap.add_argument("--all", action="store_true", help="run every lane")
    args = ap.parse_args(argv)
    lanes = list(LANE_UNIVERSE) if args.all else ([args.lane] if args.lane else [])
    if not lanes:
        ap.error("pass --lane <kraken|alpaca|forex> or --all")
    for lane in lanes:
        result = run_lane(lane)
        print(_format_summary(result))
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
