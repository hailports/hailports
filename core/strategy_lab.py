"""strategy_lab.py — $0 strategy-DISCOVERY harness: battery-test many distinct
edges over deep history, keep only the ones that are real, combine the
uncorrelated survivors into a portfolio.

Why this exists: core/backtester.py proves (or disproves) the ONE signal each
live bot already runs. This module answers a different question — of many
genuinely different strategy TYPES, which ones have a real edge at all, and
do the survivors' return streams move together (redundant) or independently
(a robust basket)? One thin edge is fragile (regime shift kills it); a basket
of uncorrelated thin edges is the actual survivable portfolio.

Reuse, not reimplementation (per READ FIRST):
  - core.backtester._http_get / _DEEP_UA / DEEP_RANGE_CANDIDATES / _stooq_symbol /
    _stooq_solve_challenge / _STOOQ_CHALLENGE_RE — the exact same $0/keyless Yahoo
    chart + stooq CSV (proof-of-work-solved) network primitives backtester.py uses
    for deep (untruncated) daily-close history. Importing core.backtester also
    re-applies its `kraken_bot.market_intel = None` patch (see that module's
    docstring) so market_signals-derived signals never leak a live network call.
  - core.backtester._slip                — identical bps slippage adjustment.
  - core.market_signals.compute_features — the exact point-in-time feature calc
    (sma20/50/200, mom5/mom20, zscore20, vol) used by strategy #1 (baseline).
  - core.kraken_bot._legacy_momentum_edge — the real, symbol-agnostic momentum
    +20/50-MA-crossover rule the live bots fall back to; reused AS-IS as strategy
    #1's signal so the "baseline" here is genuine production logic, not a new guess.
  - core.trade_proof_gate.evaluate / evaluate_oos — the SAME statistical bar (t-stat
    significance, profit factor, expectancy, net P&L) and the SAME chronological
    walk-forward out-of-sample check used to arm real money on the live lanes. A
    strategy here "survives" under the identical math, not a bespoke one.
  - core.trade_risk.kelly_fraction        — the same quarter-Kelly sizing math, used
    ONLY at the final portfolio-construction step (each survivor's own win_rate /
    avg_win / avg_loss -> its own quarter-Kelly weight).

One departure from backtester.py's fetcher, by necessity: _yahoo_chart_deep /
_stooq_csv there return closes ONLY (no dates), which is fine for a single-symbol
lane replay. Strategy #4 (cross-sectional rotation) and #6 (turn-of-month
seasonality) are inherently CALENDAR-aware — #6 needs real month boundaries, #4
needs a common trading-day clock to rank multiple symbols on the SAME date. So
this module adds thin *_dated() wrappers around the same low-level primitives
that also carry each bar's real date through — same endpoints, same
proof-of-work/caching approach, extended only to keep the (date, close) pairing
that backtester.py's use case didn't need.

The 6 strategies (long-only, universe below), each reduced to an enter/exit
signal pair evaluated bar-by-bar on closes[:t+1] only (no lookahead; fills at
the NEXT bar, mirroring backtester.py's own replay convention):
  1. Time-series momentum (baseline)   — kraken_bot._legacy_momentum_edge > 0
  2. Mean-reversion                    — RSI(2) < 10 buy, RSI(2) > 50 or 10d exit
  3. Bollinger reversion               — close < lower(20,2sigma) buy, close >= mid exit
  4. Cross-sectional momentum          — rank universe by 60d return EVERY bar,
                                          hold top-2, rotate only on rank dropout
  5. Volatility breakout (Donchian)    — close > 20d high buy, close < 10d low exit
  6. Turn-of-month seasonality         — long last 1 + first 3 trading days/month

Transaction costs: $0 commission (equities), 5bps slippage each way (fee_bps=0,
slippage_bps=5), applied via the same _slip() every fill uses. A strategy that
only "works" before costs isn't a strategy — it's noise; this is why the mean-
reversion and breakout strategies in particular tend to die here.

Trade sizing during the discovery backtest is a FLAT $10,000 notional per trade
(not a compounding equity curve) — deliberate, so expectancy/PF/Sharpe compare
strategies apples-to-apples without one strategy's path-dependent sizing
inflating or deflating its own numbers. Real position sizing (quarter-Kelly,
per the task) is only applied once at the very end, to the SURVIVING portfolio.

Survivor bar: significant (per trade_proof_gate, t-stat >= 1.66, n >= 30) AND
holds_oos (its own out-of-sample 30% slice, split chronologically by real trade
dates, still shows positive expectancy and PF >= 1.0). Most strategies here will
NOT clear this bar — that is the expected, useful result, not a bug.

Portfolio construction: for survivors only, build a Pearson correlation matrix
of each strategy's DAILY return stream (reconstructed from its own closed
trades' held-symbol daily price returns, aligned by real calendar date), then
greedily pick the highest-OOS-expectancy survivor, add the next survivor only
if |corr| < 0.5 with everything already picked, repeat. Each pick gets its own
quarter-Kelly weight from ITS OWN win_rate/avg_win/avg_loss.

$0, keyless (Yahoo + stooq), stdlib + core only.

CLI:
    python3 -m core.strategy_lab [--refresh]

Writes data/trading/strategy_lab.json (summary) and one
data/trading/strategy_lab_<strategy_id>_trades.jsonl closed-trade ledger per
strategy (fed through trade_proof_gate, same as the live lanes).
"""
from __future__ import annotations

import argparse
import bisect
import csv
import http.cookiejar
import io
import json
import math
import statistics
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

try:
    from core import BASE_DIR
except Exception:  # pragma: no cover - allow standalone import
    import os

    BASE_DIR = Path(os.path.expanduser("~/claude-stack"))

from core import market_signals
from core import trade_risk
from core import trade_proof_gate
from core import kraken_bot
from core.backtester import (
    _http_get,
    _DEEP_UA,
    DEEP_RANGE_CANDIDATES,
    _stooq_symbol,
    _stooq_solve_challenge,
    _STOOQ_CHALLENGE_RE,
    _slip,
)
# importing core.backtester above already sets kraken_bot.market_intel = None
# (see that module's docstring) — forces _legacy_momentum_edge's real fail-open
# branch instead of a live network technicals call that would leak lookahead.

TRADING_DIR = BASE_DIR / "data" / "trading"
PRICE_CACHE_PATH = TRADING_DIR / "strategy_lab_price_cache.json"
PRICE_CACHE_TTL_S = 6 * 3600.0
IN_SAMPLE_FRACTION = 0.70
WARMUP = 25            # single-symbol strategies: matches backtester.MIN_WARMUP_BARS
CROSS_WARMUP = 70       # cross-sectional needs a 60d lookback + buffer
CROSS_LOOKBACK = 60
CROSS_TOP_K = 2
CORR_THRESHOLD = 0.5    # |corr| below this = "uncorrelated enough" to combine

UNIVERSE: list[tuple[str, str]] = [
    ("SPY", "SPY"), ("QQQ", "QQQ"), ("XLK", "XLK"), ("XLF", "XLF"),
    ("XLE", "XLE"), ("XLY", "XLY"), ("AAPL", "AAPL"), ("MSFT", "MSFT"), ("NVDA", "NVDA"),
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# $0 dated deep-history fetch (Yahoo direct + stooq CSV, whichever has more
# bars wins) — same endpoints/proof-of-work as core.backtester, extended to
# also carry each bar's real calendar date (needed by strategies #4 and #6).
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


def _yahoo_chart_deep_dated(symbol: str) -> dict[str, Any] | None:
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
                    continue
                ts_list = res.get("timestamp") or []
                quote = ((res.get("indicators") or {}).get("quote") or [{}])[0]
                close_list = quote.get("close") or []
                dates: list[str] = []
                closes: list[float] = []
                for ts, c in zip(ts_list, close_list):
                    if isinstance(c, (int, float)) and ts:
                        dates.append(datetime.fromtimestamp(int(ts), tz=timezone.utc).date().isoformat())
                        closes.append(float(c))
                if not closes:
                    continue
                return {"dates": dates, "closes": closes, "source": f"yahoo:{rng}"}
            except Exception:
                continue
    return None


def _stooq_csv_dated(stooq_symbol: str) -> dict[str, Any] | None:
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
            return None
        dates: list[str] = []
        closes: list[float] = []
        for row in csv.DictReader(io.StringIO(txt)):
            try:
                dates.append(row["Date"])
                closes.append(float(row["Close"]))
            except Exception:
                continue
        return {"dates": dates, "closes": closes, "source": "stooq"} if closes else None
    except Exception:
        return None


def _deep_history_dated(symbol: str, cache: dict[str, Any]) -> dict[str, Any] | None:
    key = f"deep:{symbol}"
    row = cache.get(key)
    if isinstance(row, dict) and (time.time() - float(row.get("_t", 0))) <= PRICE_CACHE_TTL_S:
        return row.get("v")
    candidates: list[dict[str, Any]] = []
    yahoo = _yahoo_chart_deep_dated(symbol)
    if yahoo:
        candidates.append(yahoo)
    stooq_symbol = _stooq_symbol(symbol, "alpaca")  # equities suffix mapping (not forex/kraken)
    stooq = _stooq_csv_dated(stooq_symbol) if stooq_symbol else None
    if stooq:
        candidates.append(stooq)
    if not candidates:
        return None
    best = max(candidates, key=lambda c: len(c["closes"]))
    # belt-and-suspenders: guarantee strictly ascending chronological order
    pairs = sorted(zip(best["dates"], best["closes"]), key=lambda p: p[0])
    dates = [p[0] for p in pairs]
    closes = [p[1] for p in pairs]
    out = {"dates": dates, "closes": closes, "source": best["source"], "bars": len(closes)}
    cache[key] = {"_t": time.time(), "v": out}
    return out


# ---------------------------------------------------------------------------
# indicators — small, point-in-time (index <= t only), O(period) per bar
# ---------------------------------------------------------------------------

def _rsi(closes: list[float], t: int, period: int) -> float | None:
    if t < period:
        return None
    gains = losses = 0.0
    for i in range(t - period + 1, t + 1):
        chg = closes[i] - closes[i - 1]
        if chg > 0:
            gains += chg
        else:
            losses += -chg
    avg_gain, avg_loss = gains / period, losses / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _bollinger(closes: list[float], t: int, period: int, num_std: float) -> tuple[float, float, float] | None:
    if t < period - 1:
        return None
    window = closes[t - period + 1: t + 1]
    mean = sum(window) / period
    var = sum((c - mean) ** 2 for c in window) / period
    sd = math.sqrt(var)
    return mean, mean + num_std * sd, mean - num_std * sd  # mid, upper, lower


def _donchian(closes: list[float], t: int, period: int) -> tuple[float, float] | None:
    """Highest/lowest close over the `period` bars PRIOR to t (excludes t itself,
    so a breakout is measured against the past, not against itself)."""
    if t < period:
        return None
    window = closes[t - period: t]
    return max(window), min(window)


def _turn_of_month_window(dates: list[str]) -> list[bool]:
    """True on the last trading day of each month AND the first 3 trading days
    of the following month. Pure calendar structure (no price data) — computing
    this from the full `dates` array up front is not lookahead: on any given day
    we already know today's date and can mechanically tell whether TOMORROW is
    in the window without knowing tomorrow's price."""
    by_month: dict[str, list[int]] = {}
    for i, d in enumerate(dates):
        by_month.setdefault(d[:7], []).append(i)
    months = sorted(by_month)
    window = [False] * len(dates)
    for mi, ym in enumerate(months):
        idxs = by_month[ym]
        window[idxs[-1]] = True
        if mi + 1 < len(months):
            for i in by_month[months[mi + 1]][:3]:
                window[i] = True
    return window


# ---------------------------------------------------------------------------
# strategy signal functions — enter_fn(closes, t) / exit_fn(closes, entry_idx, t)
# ---------------------------------------------------------------------------

def _enter_momentum(closes: list[float], t: int) -> bool:
    feats = market_signals.compute_features(closes[: t + 1])
    if "price" not in feats:
        return False
    edge = kraken_bot._legacy_momentum_edge(feats)
    return edge is not None and edge > 0


def _exit_momentum(closes: list[float], entry_idx: int, t: int) -> bool:
    feats = market_signals.compute_features(closes[: t + 1])
    if "price" not in feats:
        return True
    edge = kraken_bot._legacy_momentum_edge(feats)
    return edge is None or edge <= 0


def _enter_rsi2(closes: list[float], t: int) -> bool:
    r = _rsi(closes, t, 2)
    return r is not None and r < 10.0


def _exit_rsi2(closes: list[float], entry_idx: int, t: int) -> bool:
    r = _rsi(closes, t, 2)
    return r is not None and r > 50.0


def _enter_bollinger(closes: list[float], t: int) -> bool:
    b = _bollinger(closes, t, 20, 2.0)
    return b is not None and closes[t] < b[2]


def _exit_bollinger(closes: list[float], entry_idx: int, t: int) -> bool:
    b = _bollinger(closes, t, 20, 2.0)
    return b is not None and closes[t] >= b[0]


def _enter_donchian(closes: list[float], t: int) -> bool:
    d = _donchian(closes, t, 20)
    return d is not None and closes[t] > d[0]


def _exit_donchian(closes: list[float], entry_idx: int, t: int) -> bool:
    d = _donchian(closes, t, 10)
    return d is not None and closes[t] < d[1]


def _make_tom_fns(window: list[bool]) -> tuple[Callable[[list[float], int], bool], Callable[[list[float], int, int], bool]]:
    def enter(closes: list[float], t: int) -> bool:
        nxt = t + 1
        return nxt < len(window) and window[nxt]

    def exit_(closes: list[float], entry_idx: int, t: int) -> bool:
        return not (t < len(window) and window[t])

    return enter, exit_


# ---------------------------------------------------------------------------
# generic single-symbol bar-by-bar replay — mirrors backtester._replay's
# no-lookahead / next-bar-fill / same-bar-exit convention and _slip() reuse.
# ---------------------------------------------------------------------------

@dataclass
class ReplayConfig:
    slippage_bps: float = 5.0
    fee_bps: float = 0.0        # equities: $0 commission (per task)
    max_hold_bars: int = 60     # safety cap so a stalled signal can't hold forever
    stop_loss_pct: float = 0.15 # safety stop bounding a single trade's downside
    notional_usd: float = 10_000.0


def _replay_single(symbol: str, dates: list[str], closes: list[float], *,
                    enter_fn: Callable[[list[float], int], bool],
                    exit_fn: Callable[[list[float], int, int], bool],
                    cfg: ReplayConfig, strategy_id: str) -> list[dict[str, Any]]:
    n = len(closes)
    trades: list[dict[str, Any]] = []
    position: dict[str, Any] | None = None
    i = WARMUP - 1
    while i < n - 1:
        if position is None:
            if enter_fn(closes, i):
                entry_idx = i + 1
                entry_price = _slip(closes[entry_idx], cfg.slippage_bps, buy=True)
                position = {"entry_idx": entry_idx, "entry": entry_price}
                i = entry_idx
                continue
            i += 1
            continue

        price = closes[i]
        entry = position["entry"]
        ret = price / entry - 1.0
        age = i - position["entry_idx"]
        reason = None
        if ret <= -cfg.stop_loss_pct:
            reason = f"stop_loss {ret * 100:.2f}%"
        elif age >= cfg.max_hold_bars:
            reason = f"max_hold {age}bars"
        elif exit_fn(closes, position["entry_idx"], i):
            reason = "signal_exit"
        elif i == n - 1:
            reason = "end_of_data"
        if reason:
            exit_price = _slip(price, cfg.slippage_bps, buy=False)
            notional = cfg.notional_usd
            fee_entry = notional * cfg.fee_bps / 10000.0
            gross = notional * (exit_price / entry)
            fee_exit = gross * cfg.fee_bps / 10000.0
            net = gross - fee_exit
            pnl = net - notional - fee_entry
            trades.append({
                "event": "close", "strategy": strategy_id, "pair": symbol,
                "entry_idx": position["entry_idx"], "exit_idx": i,
                "entry": round(entry, 6), "exit": round(exit_price, 6),
                "notional_usd": round(notional, 2), "pnl": round(pnl, 4),
                "return_pct": round((pnl / notional) * 100, 4) if notional else 0.0,
                "won": bool(pnl > 0), "reason": reason,
                "date_entry": dates[position["entry_idx"]], "date_exit": dates[i],
                "ts": dates[i],
                "price_source": "yahoo_backtest", "mode": "backtest",
            })
            position = None
        i += 1
    return trades


# ---------------------------------------------------------------------------
# cross-sectional momentum: separate multi-symbol engine. Ranks the WHOLE
# universe by trailing 60d return on EVERY master-calendar bar; holds the
# top-2; rotates a slot only when its held symbol drops out of the top-2
# (daily rank checks, but turnover only on membership change, not every day).
# ---------------------------------------------------------------------------

def _nearest_idx(date_to_idx: dict[str, int], dates_sorted: list[str], target: str) -> int | None:
    if target in date_to_idx:
        return date_to_idx[target]
    pos = bisect.bisect_left(dates_sorted, target)
    if pos < len(dates_sorted):
        return date_to_idx[dates_sorted[pos]]
    return None


def _cross_sectional_trades(universe_data: dict[str, dict[str, Any]], cfg: ReplayConfig,
                             *, lookback: int = CROSS_LOOKBACK, top_k: int = CROSS_TOP_K,
                             warmup: int = CROSS_WARMUP) -> list[dict[str, Any]]:
    if not universe_data:
        return []
    master_sym = "SPY" if "SPY" in universe_data else max(universe_data, key=lambda s: len(universe_data[s]["closes"]))
    master_dates = universe_data[master_sym]["dates"]
    idx_maps = {s: {d: i for i, d in enumerate(v["dates"])} for s, v in universe_data.items()}
    dates_by_sym = {s: v["dates"] for s, v in universe_data.items()}
    n = len(master_dates)
    trades: list[dict[str, Any]] = []
    held: dict[str, dict[str, Any]] = {}

    def _close_out(sym: str, exit_j: int, reason: str) -> None:
        pos = held.pop(sym)
        cl = universe_data[sym]["closes"]
        dl = dates_by_sym[sym]
        if exit_j <= pos["entry_idx"]:
            return
        exit_price = _slip(cl[exit_j], cfg.slippage_bps, buy=False)
        notional = cfg.notional_usd / top_k
        gross = notional * (exit_price / pos["entry"])
        pnl = gross - notional
        trades.append({
            "event": "close", "strategy": "cross_sectional_mom", "pair": sym,
            "entry_idx": pos["entry_idx"], "exit_idx": exit_j,
            "entry": round(pos["entry"], 6), "exit": round(exit_price, 6),
            "notional_usd": round(notional, 2), "pnl": round(pnl, 4),
            "return_pct": round((pnl / notional) * 100, 4) if notional else 0.0,
            "won": bool(pnl > 0), "reason": reason,
            "date_entry": dl[pos["entry_idx"]], "date_exit": dl[exit_j], "ts": dl[exit_j],
            "price_source": "yahoo_backtest", "mode": "backtest",
        })

    for t in range(warmup, n - 1):
        today = master_dates[t]
        scored: list[tuple[str, float, int]] = []
        for sym, data in universe_data.items():
            dmap = idx_maps[sym]
            j = dmap.get(today) if today in dmap else _nearest_idx(dmap, dates_by_sym[sym], today)
            if j is None or j < lookback:
                continue
            cl = data["closes"]
            ret60 = cl[j] / cl[j - lookback] - 1.0
            scored.append((sym, ret60, j))
        if not scored:
            continue
        scored.sort(key=lambda r: r[1], reverse=True)
        desired = {sym for sym, _, _ in scored[:top_k]}

        nxt_date = master_dates[t + 1]
        for sym in list(held):
            if sym not in desired:
                dmap = idx_maps[sym]
                ej = dmap.get(nxt_date) if nxt_date in dmap else _nearest_idx(dmap, dates_by_sym[sym], nxt_date)
                if ej is not None:
                    _close_out(sym, ej, "rank_dropout")
                else:
                    held.pop(sym, None)

        for sym, _ret60, _j in scored[:top_k]:
            if sym in held:
                continue
            dmap = idx_maps[sym]
            ej = dmap.get(nxt_date) if nxt_date in dmap else _nearest_idx(dmap, dates_by_sym[sym], nxt_date)
            if ej is None:
                continue
            cl = universe_data[sym]["closes"]
            entry_price = _slip(cl[ej], cfg.slippage_bps, buy=True)
            held[sym] = {"entry_idx": ej, "entry": entry_price}

    for sym in list(held):
        cl = universe_data[sym]["closes"]
        last_j = len(cl) - 1
        _close_out(sym, last_j, "end_of_data")

    return trades


# ---------------------------------------------------------------------------
# stats: Sharpe (trade_proof_gate gives n/expectancy/PF/t-stat/significance;
# it doesn't compute Sharpe, so this fills that one gap using REAL elapsed
# calendar time between first/last trade date — more accurate than a bar-count
# approximation now that real dates are available).
# ---------------------------------------------------------------------------

def _annualized_sharpe(trades: list[dict[str, Any]]) -> float:
    if len(trades) < 2:
        return 0.0
    rets = [t["return_pct"] / 100.0 for t in trades]
    mean_r = statistics.fmean(rets)
    std_r = statistics.pstdev(rets)
    if std_r <= 0:
        return 0.0
    ts_dates = sorted(t["ts"] for t in trades if t.get("ts"))
    if len(ts_dates) >= 2:
        d0 = datetime.fromisoformat(ts_dates[0])
        d1 = datetime.fromisoformat(ts_dates[-1])
        years = max((d1 - d0).days / 365.25, 0.25)
    else:
        years = 1.0
    trades_per_year = len(trades) / years
    return round((mean_r / std_r) * math.sqrt(trades_per_year), 3)


def _strategy_daily_returns(trades: list[dict[str, Any]], universe_data: dict[str, dict[str, Any]]) -> dict[str, float]:
    """Reconstruct a strategy's own daily return stream: for each day it was
    holding a symbol, that symbol's realized daily close-to-close return
    (averaged across symbols on days with >1 concurrent position)."""
    daily: dict[str, list[float]] = {}
    for tr in trades:
        sym = tr["pair"]
        data = universe_data.get(sym)
        if not data:
            continue
        closes, dates = data["closes"], data["dates"]
        lo, hi = tr["entry_idx"] + 1, min(tr["exit_idx"], len(closes) - 1)
        for i in range(lo, hi + 1):
            r = closes[i] / closes[i - 1] - 1.0
            daily.setdefault(dates[i], []).append(r)
    return {d: sum(v) / len(v) for d, v in daily.items()}


def _pearson(a: dict[str, float], b: dict[str, float]) -> float | None:
    common = sorted(set(a) & set(b))
    if len(common) < 10:
        return None
    xs = [a[d] for d in common]
    ys = [b[d] for d in common]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx <= 0 or vy <= 0:
        return 0.0
    return round(cov / math.sqrt(vx * vy), 4)


# ---------------------------------------------------------------------------
# strategy registry
# ---------------------------------------------------------------------------

STRATEGY_SPECS: list[dict[str, Any]] = [
    {"id": "ts_momentum", "label": "1. Time-series momentum (baseline: mom5 + 20/50 MA-cross, reused from kraken_bot)",
     "kind": "single", "enter": _enter_momentum, "exit": _exit_momentum,
     "cfg": ReplayConfig(max_hold_bars=90, stop_loss_pct=0.15)},
    {"id": "rsi2_meanrev", "label": "2. Mean-reversion: RSI(2) < 10 buy, RSI(2) > 50 or 10d exit",
     "kind": "single", "enter": _enter_rsi2, "exit": _exit_rsi2,
     "cfg": ReplayConfig(max_hold_bars=10, stop_loss_pct=0.10)},
    {"id": "bollinger_reversion", "label": "3. Bollinger(20, 2sigma) reversion: buy < lower band, exit >= mid",
     "kind": "single", "enter": _enter_bollinger, "exit": _exit_bollinger,
     "cfg": ReplayConfig(max_hold_bars=30, stop_loss_pct=0.12)},
    {"id": "cross_sectional_mom", "label": "4. Cross-sectional momentum: rank universe by 60d return, hold top-2",
     "kind": "cross", "cfg": ReplayConfig(slippage_bps=5.0, fee_bps=0.0, notional_usd=10_000.0)},
    {"id": "vol_breakout_donchian", "label": "5. Volatility breakout: Donchian 20d high buy, 10d low trail exit",
     "kind": "single", "enter": _enter_donchian, "exit": _exit_donchian,
     "cfg": ReplayConfig(max_hold_bars=180, stop_loss_pct=0.20)},
    {"id": "turn_of_month", "label": "6. Turn-of-month seasonality: long last 1 + first 3 trading days/month",
     "kind": "single_tom", "cfg": ReplayConfig(max_hold_bars=6, stop_loss_pct=0.08)},
]


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------

def _fetch_universe(*, refresh: bool) -> tuple[dict[str, dict[str, Any]], list[str]]:
    cache: dict[str, Any] = {} if refresh else _load_price_cache()
    universe_data: dict[str, dict[str, Any]] = {}
    skipped: list[str] = []
    for _label, sym in UNIVERSE:
        bar = _deep_history_dated(sym, cache)
        if not bar or len(bar["closes"]) < WARMUP + 30:
            skipped.append(sym)  # fail-open: e.g. a megacap symbol that isn't priceable
            continue
        universe_data[sym] = bar
    _save_price_cache(cache)
    return universe_data, skipped


def _run_strategy(spec: dict[str, Any], universe_data: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    sid, kind, cfg = spec["id"], spec["kind"], spec["cfg"]
    trades: list[dict[str, Any]] = []
    if kind == "cross":
        trades = _cross_sectional_trades(universe_data, cfg)
    elif kind == "single_tom":
        for sym, data in universe_data.items():
            window = _turn_of_month_window(data["dates"])
            enter_fn, exit_fn = _make_tom_fns(window)
            trades.extend(_replay_single(sym, data["dates"], data["closes"],
                                          enter_fn=enter_fn, exit_fn=exit_fn, cfg=cfg, strategy_id=sid))
    else:
        for sym, data in universe_data.items():
            trades.extend(_replay_single(sym, data["dates"], data["closes"],
                                          enter_fn=spec["enter"], exit_fn=spec["exit"], cfg=cfg, strategy_id=sid))
    return trades


def _recommend_portfolio(survivors: dict[str, dict[str, Any]], corr: dict[tuple[str, str], float | None],
                          threshold: float = CORR_THRESHOLD) -> dict[str, Any]:
    if not survivors:
        return {"selected": [], "quarter_kelly_weights_raw": {}, "quarter_kelly_weights_normalized": {},
                "note": "zero strategies survived significance + out-of-sample — nothing to combine. "
                        "stay in paper on all 6 and re-run after more history / a regime change."}

    def _oos_expectancy(r: dict[str, Any]) -> float:
        e = r["oos"]["out_of_sample"]["expectancy"]
        return float(e) if isinstance(e, (int, float)) else -1e9

    ranked = sorted(survivors.values(), key=_oos_expectancy, reverse=True)
    chosen: list[str] = []
    for r in ranked:
        sid = r["id"]
        ok = True
        for c in chosen:
            val = corr.get((sid, c), corr.get((c, sid)))
            if val is not None and abs(val) >= threshold:
                ok = False
                break
        if ok:
            chosen.append(sid)

    weights_raw: dict[str, float] = {}
    for sid in chosen:
        p = survivors[sid]["proof"]
        win_rate = float(p.get("win_rate") or 0.0)
        avg_win = float(p.get("avg_win") or 0.0)
        avg_loss = float(p.get("avg_loss") or 0.0)
        payoff = (avg_win / avg_loss) if avg_loss > 0 else 1.5
        weights_raw[sid] = round(trade_risk.kelly_fraction(win_rate, payoff, fraction=0.25), 4)
    total = sum(weights_raw.values())
    weights_norm = {sid: round(w / total, 4) for sid, w in weights_raw.items()} if total > 0 else {}

    return {
        "selected": chosen,
        "correlation_threshold": threshold,
        "quarter_kelly_weights_raw": weights_raw,
        "quarter_kelly_weights_normalized": weights_norm,
        "note": (f"{len(chosen)} of {len(survivors)} survivor(s) selected as mutually uncorrelated "
                 f"(|corr| < {threshold}); weights are each survivor's OWN quarter-Kelly fraction "
                 f"from its own win_rate/avg_win/avg_loss."),
    }


def run_lab(*, refresh: bool = False) -> dict[str, Any]:
    universe_data, skipped = _fetch_universe(refresh=refresh)
    results: dict[str, dict[str, Any]] = {}

    for spec in STRATEGY_SPECS:
        sid = spec["id"]
        trades = _run_strategy(spec, universe_data)
        ledger_path = TRADING_DIR / f"strategy_lab_{sid}_trades.jsonl"
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with ledger_path.open("w") as fh:
            for tr in trades:
                fh.write(json.dumps(tr, default=str) + "\n")
        proof = trade_proof_gate.evaluate(ledger_path)
        oos = trade_proof_gate.evaluate_oos(ledger_path, split=IN_SAMPLE_FRACTION)
        sharpe = _annualized_sharpe(trades)
        survivor = bool(proof.get("significant") and oos.get("holds_oos"))
        results[sid] = {
            "id": sid, "label": spec["label"],
            "n_symbols": len({t["pair"] for t in trades}),
            "proof": proof, "oos": oos, "sharpe": sharpe, "survivor": survivor,
            "ledger_path": str(ledger_path), "trades": trades,
        }

    survivors = {sid: r for sid, r in results.items() if r["survivor"]}
    daily_by_survivor = {sid: _strategy_daily_returns(r["trades"], universe_data) for sid, r in survivors.items()}
    corr: dict[tuple[str, str], float | None] = {}
    ids = list(survivors)
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            a, b = ids[i], ids[j]
            corr[(a, b)] = _pearson(daily_by_survivor[a], daily_by_survivor[b])

    portfolio = _recommend_portfolio(survivors, corr)

    out = {
        "generated_at": _now(),
        "universe_requested": [s for _, s in UNIVERSE],
        "universe_used": sorted(universe_data),
        "skipped_symbols": skipped,
        "in_sample_fraction": IN_SAMPLE_FRACTION,
        "fee_bps": 0.0, "slippage_bps": 5.0, "notional_per_trade_usd": 10_000.0,
        "strategies": {sid: {k: v for k, v in r.items() if k != "trades"} for sid, r in results.items()},
        "survivors": list(survivors),
        "correlation_matrix": {f"{a}|{b}": c for (a, b), c in corr.items()},
        "portfolio_recommendation": portfolio,
    }
    out_path = TRADING_DIR / "strategy_lab.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2, default=str))
    out["_out_path"] = str(out_path)
    return out


# ---------------------------------------------------------------------------
# CLI / printed report
# ---------------------------------------------------------------------------

def _verdict_block(r: dict[str, Any]) -> str:
    p, oos = r["proof"], r["oos"]
    tag = "SURVIVOR" if r["survivor"] else "died"
    lines = [
        f"[{tag}] {r['label']}",
        f"  symbols={r['n_symbols']} n_trades={p['n_closed']} win_rate={p['win_rate']} "
        f"expectancy=${p['expectancy']} pf={p['profit_factor']} sharpe={r['sharpe']} "
        f"t_stat={p['t_stat']} significant={p['significant']} proven_strict={p['proven_strict']} "
        f"holds_oos={oos['holds_oos']}",
    ]
    if not r["survivor"]:
        why: list[str] = []
        if not p["significant"]:
            why.append(f"not statistically significant (t_stat={p['t_stat']} < 1.66 or n<30)")
        if p["n_closed"] and p["expectancy"] <= 0:
            why.append(f"negative expectancy (${p['expectancy']}/trade after costs)")
        if not oos["holds_oos"]:
            oe = oos["out_of_sample"]["expectancy"]
            on = oos["out_of_sample"]["n_closed"]
            why.append(f"fails out-of-sample (oos n={on} expectancy=${oe}) — likely curve-fit to in-sample history")
        lines.append("  why it died: " + ("; ".join(why) if why else "unknown"))
    return "\n".join(lines)


def _format_report(out: dict[str, Any]) -> str:
    lines = [f"=== strategy_lab ({out['generated_at']}) ===",
             f"universe: {out['universe_used']} (skipped: {out['skipped_symbols'] or 'none'})",
             f"costs: fee_bps=0 slippage_bps=5, notional/trade=${out['notional_per_trade_usd']:,.0f}, "
             f"in-sample/out-of-sample split={out['in_sample_fraction']}",
             ""]
    for sid in [s["id"] for s in STRATEGY_SPECS]:
        r = out["strategies"][sid]
        lines.append(_verdict_block(r))
        lines.append("")

    survivors = out["survivors"]
    lines.append(f"--- SURVIVORS: {survivors or 'NONE'} ---")
    if len(survivors) >= 2:
        lines.append("\ncorrelation matrix (daily return streams, Pearson):")
        header = "".ljust(24) + "".join(s.ljust(22) for s in survivors)
        lines.append(header)
        for a in survivors:
            row = a.ljust(24)
            for b in survivors:
                if a == b:
                    row += "1.0000".ljust(22)
                else:
                    v = out["correlation_matrix"].get(f"{a}|{b}", out["correlation_matrix"].get(f"{b}|{a}"))
                    row += (f"{v:.4f}" if isinstance(v, (int, float)) else "n/a").ljust(22)
            lines.append(row)

    pr = out["portfolio_recommendation"]
    lines.append("\n--- PORTFOLIO RECOMMENDATION ---")
    lines.append(pr["note"])
    if pr["selected"]:
        lines.append(f"combine: {pr['selected']}")
        lines.append(f"quarter-Kelly weights (raw): {pr['quarter_kelly_weights_raw']}")
        lines.append(f"quarter-Kelly weights (normalized to 100%): {pr['quarter_kelly_weights_normalized']}")

    lines.append(f"\nwritten: {out.get('_out_path')}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="strategy_lab — battery-test distinct strategy types over deep history, "
                    "keep only the statistically real out-of-sample survivors, combine the "
                    "uncorrelated ones into a portfolio. $0/keyless.")
    ap.add_argument("--refresh", action="store_true", help="bypass the price cache and refetch deep history")
    args = ap.parse_args(argv)
    out = run_lab(refresh=args.refresh)
    print(_format_report(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
