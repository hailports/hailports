"""market_data_fusion.py — $0, keyless, max-coverage market microstructure +
signal fusion for the Kraken trading bot (core/kraken_bot.py).

This is the FUSION layer that sits on top of the two existing signal modules
instead of re-deriving anything they already compute:

  core/market_intel.py    -> technicals() (RSI/MACD/ATR/Bollinger/multi-tf
                              momentum on daily Yahoo OHLCV) + crypto_fear_greed()
  core/market_signals.py  -> real spot prices, FRED macro, VIX/yield-curve regime

New here (not in either module above):
  - Kraken public REST (Depth/Ticker/OHLC, no auth): bid-ask spread in bps,
    order-book imbalance, 24h volume/VWAP/high/low, intraday (1h candle)
    momentum + volume confirmation.
  - Multi-source spot price cross-check (Kraken + CoinGecko + Coinbase) with
    a divergence flag that discounts conviction when sources disagree.
  - Best-effort funding-rate / open-interest positioning read from Binance's
    keyless public futures endpoints (bonus family — never required, skipped
    cleanly if geo-blocked or the symbol isn't listed).

  fuse(symbol)          -> {conviction 0-1, direction long/flat, spread_bps,
                            book_imbalance, use_maker, regime, vol_regime,
                            reasons, price, sources}
  fuse_all(symbols)     -> {symbol: fuse(symbol)}

CONFLUENCE RULE: conviction requires agreement across >=3 of 4 CORE_FAMILIES
(trend, momentum, microstructure, regime) — a single strong factor never
produces a "long" call by itself. See _combine() below.

$0 / KEYLESS / FAIL-OPEN (HARD): every network call here is TTL-cached in
data/trading/market_data_fusion_cache.json and wrapped so a dead/blocked
source degrades to a neutral/unavailable reading — this module NEVER raises.
The trading loop must never stall on a signal fetch.
"""
from __future__ import annotations

import json
import math
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

try:
    from core import BASE_DIR
except Exception:  # pragma: no cover - allow standalone import
    BASE_DIR = Path(os.path.expanduser("~/claude-stack"))

try:
    from core import market_intel
except Exception:  # pragma: no cover - fail open if the module can't import
    market_intel = None

try:
    from core import market_signals
except Exception:  # pragma: no cover - fail open if the module can't import
    market_signals = None

CACHE_PATH = BASE_DIR / "data" / "trading" / "market_data_fusion_cache.json"
UA = {"User-Agent": "Mozilla/5.0 (research; paper-only)"}

KRAKEN_API = "https://api.kraken.com/0/public"
BINANCE_FAPI = "https://fapi.binance.com"

# fuse(symbol) uses core.market_intel/market_signals spelling ("BTC-USD").
# Maps to Kraken altname pair codes, CoinGecko ids, and Binance USDT-margined
# futures symbols. Symbols outside this map fall back to _guess_codes() —
# best-effort, fails open per-source if the guess is wrong.
SYMBOL_MAP: dict[str, dict[str, str | None]] = {
    "BTC-USD": {"kraken": "XBTUSD", "coingecko": "bitcoin", "binance": "BTCUSDT"},
    "ETH-USD": {"kraken": "ETHUSD", "coingecko": "ethereum", "binance": "ETHUSDT"},
    "SOL-USD": {"kraken": "SOLUSD", "coingecko": "solana", "binance": "SOLUSDT"},
    "XRP-USD": {"kraken": "XRPUSD", "coingecko": "ripple", "binance": "XRPUSDT"},
    "ADA-USD": {"kraken": "ADAUSD", "coingecko": "cardano", "binance": "ADAUSDT"},
    "DOGE-USD": {"kraken": "DOGEUSD", "coingecko": "dogecoin", "binance": "DOGEUSDT"},
    "DOT-USD": {"kraken": "DOTUSD", "coingecko": "polkadot", "binance": "DOTUSDT"},
    "AVAX-USD": {"kraken": "AVAXUSD", "coingecko": "avalanche-2", "binance": "AVAXUSDT"},
    "LINK-USD": {"kraken": "LINKUSD", "coingecko": "chainlink", "binance": "LINKUSDT"},
    "LTC-USD": {"kraken": "LTCUSD", "coingecko": "litecoin", "binance": "LTCUSDT"},
    "MATIC-USD": {"kraken": "MATICUSD", "coingecko": "matic-network", "binance": "MATICUSDT"},
    "ATOM-USD": {"kraken": "ATOMUSD", "coingecko": "cosmos", "binance": "ATOMUSDT"},
}

# TTLs (seconds) — order book/ticker refresh fast, OHLC/positioning slower.
DEPTH_TTL_S = 90
TICKER_TTL_S = 90
OHLC_TTL_S = 900
XCHECK_TTL_S = 300
POSITIONING_TTL_S = 900
REGIME_TTL_S = 1800

# When the top-of-book spread exceeds ~this many bps, crossing it as a taker
# gives up more edge than Kraken's maker/taker fee differential (roughly
# 10bps at the low-volume tier) — past this point a passive limit order
# meaningfully beats a market/taker order.
MAKER_SPREAD_THRESHOLD_BPS = 10.0
DIVERGENCE_FLAG_BPS = 25.0  # cross-source price disagreement worth discounting
WIDE_SPREAD_FLAG_BPS = 50.0  # illiquid book worth discounting

CORE_FAMILIES = ("trend", "momentum", "microstructure", "regime")
CONFLUENCE_MIN_AVAIL = 3     # need at least 3 of the 4 core families to have data
CONFLUENCE_MIN_RATIO = 0.75  # and >=75% of those available must agree bullish


# ---------------------------------------------------------------------------
# low-level fetch + TTL cache (mirrors core/market_intel.py + market_signals.py)
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_cache() -> dict[str, Any]:
    try:
        if CACHE_PATH.exists():
            return json.loads(CACHE_PATH.read_text())
    except Exception:
        pass
    return {}


def _save_cache(cache: dict[str, Any]) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cache, default=str))
        tmp.replace(CACHE_PATH)
    except Exception:
        pass


def _cache_get(cache: dict[str, Any], key: str, ttl_s: float) -> Any:
    row = cache.get(key)
    if not isinstance(row, dict):
        return None
    if (time.time() - float(row.get("_t", 0))) > ttl_s:
        return None
    return row.get("v")


def _cache_put(cache: dict[str, Any], key: str, value: Any) -> None:
    cache[key] = {"_t": time.time(), "v": value}


def _cached(cache: dict[str, Any], key: str, ttl_s: float, fn: Callable[[], Any]) -> Any:
    """Fetch-or-cache wrapper: never raises, a failing fn() just returns None
    (cache miss stays a miss, nothing is written)."""
    hit = _cache_get(cache, key, ttl_s)
    if hit is not None:
        return hit
    try:
        val = fn()
    except Exception:
        val = None
    if val is not None:
        _cache_put(cache, key, val)
    return val


def _http_text(url: str, timeout: float = 10) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _http_json(url: str, timeout: float = 10) -> Any:
    return json.loads(_http_text(url, timeout))


def _guess_codes(symbol: str) -> dict[str, str | None]:
    """Best-effort code guess for a symbol outside SYMBOL_MAP. Fails open
    per-source (a wrong guess just means that source errors/skips)."""
    base, _, quote = symbol.partition("-")
    base = base.upper()
    quote = (quote or "USD").upper()
    kraken_base = "XBT" if base == "BTC" else base
    return {
        "kraken": f"{kraken_base}{quote}",
        "coingecko": None,  # no safe generic guess for CoinGecko's slug ids
        "binance": f"{base}USDT" if quote == "USD" else None,
    }


def _symbol_codes(symbol: str) -> dict[str, str | None]:
    return SYMBOL_MAP.get(symbol) or _guess_codes(symbol)


# ---------------------------------------------------------------------------
# 1. Kraken public REST — Depth (spread + book imbalance), Ticker (24h
#    volume/VWAP/high/low), OHLC (intraday momentum + volume confirmation)
# ---------------------------------------------------------------------------

def _kraken_public(endpoint: str, params: dict[str, Any], timeout: float = 10) -> Any:
    try:
        qs = urllib.parse.urlencode(params)
        data = _http_json(f"{KRAKEN_API}/{endpoint}?{qs}", timeout=timeout)
        if not isinstance(data, dict) or data.get("error"):
            return None
        result = data.get("result") or {}
        keys = [k for k in result if k != "last"]  # OHLC also returns a "last" cursor key
        return result[keys[0]] if keys else None
    except Exception:
        return None


def _kraken_depth(pair_code: str, count: int = 10) -> dict[str, Any] | None:
    raw = _kraken_public("Depth", {"pair": pair_code, "count": count})
    if not isinstance(raw, dict):
        return None
    bids = raw.get("bids") or []
    asks = raw.get("asks") or []
    if not bids or not asks:
        return None
    try:
        best_bid, best_ask = float(bids[0][0]), float(asks[0][0])
        mid = (best_bid + best_ask) / 2
        if mid <= 0:
            return None
        bidvol = sum(float(b[1]) for b in bids)
        askvol = sum(float(a[1]) for a in asks)
        imbalance = bidvol / (bidvol + askvol) if (bidvol + askvol) else 0.5
        return {
            "best_bid": best_bid, "best_ask": best_ask, "mid": round(mid, 6),
            "spread_bps": round((best_ask - best_bid) / mid * 10000, 3),
            "book_imbalance": round(imbalance, 4),
            "bidvol": round(bidvol, 6), "askvol": round(askvol, 6),
        }
    except Exception:
        return None


def _kraken_ticker(pair_code: str) -> dict[str, Any] | None:
    raw = _kraken_public("Ticker", {"pair": pair_code})
    if not isinstance(raw, dict):
        return None
    try:
        return {
            "last": float(raw["c"][0]),
            "vwap24h": float(raw["p"][1]),
            "vol24h": float(raw["v"][1]),
            "high24h": float(raw["h"][1]),
            "low24h": float(raw["l"][1]),
        }
    except Exception:
        return None


def _kraken_ohlc(pair_code: str, interval: int = 60, hours_back: int = 12) -> dict[str, Any] | None:
    since = int(time.time()) - hours_back * 3600
    raw = _kraken_public("OHLC", {"pair": pair_code, "interval": interval, "since": since})
    if not isinstance(raw, list) or not raw:
        return None
    try:
        closes = [float(r[4]) for r in raw]
        volumes = [float(r[6]) for r in raw]
        return {"closes": closes, "volumes": volumes, "interval_min": interval, "n": len(closes)}
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 2. multi-source spot price cross-check — Kraken (already fetched) +
#    CoinGecko + Coinbase. Reuses core.market_signals' fetchers, does not
#    re-implement them.
# ---------------------------------------------------------------------------

def _coingecko_simple_price(cg_id: str, timeout: float = 8) -> float | None:
    d = _http_json(f"https://api.coingecko.com/api/v3/simple/price?ids={cg_id}&vs_currencies=usd", timeout=timeout)
    return float(d[cg_id]["usd"])


def _price_cross_check(
    symbol: str, codes: dict[str, str | None], cache: dict[str, Any], kraken_price: float | None,
    *, ttl_s: float = XCHECK_TTL_S,
) -> dict[str, Any]:
    prices: dict[str, float] = {}
    if kraken_price:
        prices["kraken"] = kraken_price
    if market_signals is not None:
        cg = _cached(cache, f"xcheck:cg_ms:{symbol}", ttl_s, lambda: market_signals._coingecko_spot(symbol))
        if cg:
            prices["coingecko"] = cg["price"]
        elif codes.get("coingecko"):
            cg2 = _cached(cache, f"xcheck:cg_own:{symbol}", ttl_s, lambda: _coingecko_simple_price(codes["coingecko"]))
            if cg2:
                prices["coingecko"] = cg2
        cb = _cached(cache, f"xcheck:cb:{symbol}", ttl_s, lambda: market_signals._coinbase_spot(symbol))
        if cb:
            prices["coinbase"] = cb["price"]
    if len(prices) < 2:
        return {"prices": prices, "divergence_bps": None, "flag": False}
    vals = list(prices.values())
    mean = sum(vals) / len(vals)
    divergence = (max(vals) - min(vals)) / mean * 10000 if mean else 0.0
    return {"prices": prices, "divergence_bps": round(divergence, 2), "flag": divergence > DIVERGENCE_FLAG_BPS}


# ---------------------------------------------------------------------------
# 3. positioning (bonus, best-effort) — Binance keyless public futures data.
#    Skips cleanly (never required for confluence) if blocked/unlisted.
# ---------------------------------------------------------------------------

def _binance_funding(binance_symbol: str, timeout: float = 8) -> dict[str, Any] | None:
    d = _http_json(f"{BINANCE_FAPI}/fapi/v1/premiumIndex?symbol={binance_symbol}", timeout=timeout)
    if not isinstance(d, dict) or "lastFundingRate" not in d:
        return None
    return {"funding_rate": float(d["lastFundingRate"]), "mark_price": float(d.get("markPrice") or 0.0)}


def _binance_oi_trend(binance_symbol: str, timeout: float = 8) -> dict[str, Any] | None:
    d = _http_json(
        f"{BINANCE_FAPI}/futures/data/openInterestHist?symbol={binance_symbol}&period=1h&limit=6",
        timeout=timeout,
    )
    if not isinstance(d, list) or len(d) < 2:
        return None
    vals = [float(r["sumOpenInterest"]) for r in d]
    chg = (vals[-1] / vals[0] - 1.0) if vals[0] else 0.0
    return {"oi_now": vals[-1], "oi_chg_pct_6h": round(chg * 100, 3)}


# ---------------------------------------------------------------------------
# 4. macro/vix regime — bridges to core.market_signals.provide_macro() /
#    provide_structure() directly (their real bucketing logic, not reimplemented)
#    on a minimal 4-symbol set instead of the full ~29-symbol universe, so a
#    fuse() call doesn't pay for the whole equity/sector fetch every time.
#    Tradeoff: breadth_pct_above_50dma degrades to "is SPY above its own
#    50dma" (0/100) rather than true cross-market breadth — acceptable for a
#    crypto-signal regime filter, and still fails open either way.
# ---------------------------------------------------------------------------

def _lightweight_regime(*, ttl_s: float = REGIME_TTL_S) -> dict[str, Any]:
    neutral = {"regime": None, "vix": None, "vix_regime": None, "available": False}
    if market_signals is None:
        return neutral
    try:
        cache = market_signals._load_cache()
        hit = market_signals._cache_get(cache, "fusion:regime", ttl_s)
        if hit is not None:
            return hit
        syms = ["SPY", "^VIX", "^TNX", "^IRX"]
        prices = market_signals.fetch_prices(syms, cache=cache, ttl_s=max(600, ttl_s))
        features = {s: market_signals.compute_features(b.get("closes") or []) for s, b in prices.items()}
        state = {"prices": prices, "features": features}
        macro = market_signals.provide_macro(state, cache)
        structure = market_signals.provide_structure(state, cache)
        out = {
            "regime": structure.get("regime"),
            "vix": structure.get("vix"),
            "vix_regime": structure.get("vix_regime"),
            "spy_trend": structure.get("spy_trend"),
            "yield_curve_10y_3m": macro.get("yield_curve_10y_3m"),
            "curve_inverted": macro.get("curve_inverted"),
            "available": bool(structure.get("regime")),
        }
        market_signals._cache_put(cache, "fusion:regime", out)
        market_signals._save_cache(cache)
        return out
    except Exception:
        return neutral


# ---------------------------------------------------------------------------
# 5. factor families — each returns {avail, vote (-1/0/1), strength (0-1),
#    subs: {factor_name: raw_value}}. vote is the family's own majority of
#    independent sub-factors; strength is how unanimous that family was.
# ---------------------------------------------------------------------------

def _family_from_votes(avail: bool, votes: list[int], subs: dict[str, Any]) -> dict[str, Any]:
    if not votes:
        return {"avail": avail, "vote": 0, "strength": 0.0, "subs": subs}
    positive = sum(1 for v in votes if v > 0)
    ratio = positive / len(votes)
    vote = 1 if ratio > 0.5 else (-1 if ratio < 0.5 else 0)
    strength = abs(ratio - 0.5) * 2  # 0 when split 50/50, 1 when unanimous
    return {"avail": True, "vote": vote, "strength": round(strength, 3), "subs": subs}


def _trend_family(tech: dict[str, Any] | None) -> dict[str, Any]:
    """Daily-scale trend: 52w-high/low proximity, Bollinger position, 60/120d momentum."""
    subs: dict[str, Any] = {}
    votes: list[int] = []
    if not tech or not tech.get("available"):
        return {"avail": False, "vote": 0, "strength": 0.0, "subs": subs}
    week52 = tech.get("week52") or {}
    pfh, pfl = week52.get("pct_from_high"), week52.get("pct_from_low")
    if pfh is not None and pfl is not None:
        if pfh > -10:
            subs["near_52w_high"] = pfh
            votes.append(1)
        elif pfl < 10:
            subs["near_52w_low"] = pfl
            votes.append(-1)
    boll_pos = (tech.get("bollinger") or {}).get("position")
    if boll_pos in ("overbought", "upper_half"):
        subs["bollinger_position"] = boll_pos
        votes.append(1)
    elif boll_pos in ("oversold", "lower_half"):
        subs["bollinger_position"] = boll_pos
        votes.append(-1)
    momentum = tech.get("momentum") or {}
    for lbl in ("m60", "m120"):
        v = momentum.get(lbl)
        if v is not None:
            subs[f"mom_{lbl}_pct"] = v
            votes.append(1 if v > 0 else -1)
    return _family_from_votes(True, votes, subs)


def _momentum_family(tech: dict[str, Any] | None, ohlc: dict[str, Any] | None) -> dict[str, Any]:
    """Shorter-horizon momentum: RSI band, MACD hist, 5/20d momentum (daily,
    from market_intel) plus Kraken 1h-candle 4h/12h momentum (intraday, new)."""
    subs: dict[str, Any] = {}
    votes: list[int] = []
    if tech and tech.get("available"):
        rsi = tech.get("rsi14")
        if rsi is not None:
            subs["rsi14"] = rsi
            if rsi > 70:
                votes.append(-1)
            elif 40 <= rsi <= 60:
                votes.append(1)
        macd_hist = ((tech.get("macd") or {}) or {}).get("hist")
        if macd_hist is not None:
            subs["macd_hist"] = macd_hist
            votes.append(1 if macd_hist > 0 else -1)
        momentum = tech.get("momentum") or {}
        for lbl in ("m5", "m20"):
            v = momentum.get(lbl)
            if v is not None:
                subs[f"mom_{lbl}_pct"] = v
                votes.append(1 if v > 0 else -1)
    if ohlc and ohlc.get("n", 0) >= 5:
        closes = ohlc["closes"]
        try:
            mom4h = closes[-1] / closes[-5] - 1.0
            subs["kraken_mom_4h_pct"] = round(mom4h * 100, 3)
            votes.append(1 if mom4h > 0 else -1)
        except Exception:
            pass
        try:
            mom12h = closes[-1] / closes[0] - 1.0
            subs["kraken_mom_12h_pct"] = round(mom12h * 100, 3)
            votes.append(1 if mom12h > 0 else -1)
        except Exception:
            pass
    avail = bool((tech and tech.get("available")) or (ohlc and ohlc.get("n", 0) >= 5))
    return _family_from_votes(avail, votes, subs)


def _microstructure_family(depth: dict[str, Any] | None, ohlc: dict[str, Any] | None) -> dict[str, Any]:
    """Order-book imbalance + Kraken intraday volume confirming (or not) the
    recent price move. This is the family that reads live execution quality,
    not just price history."""
    subs: dict[str, Any] = {}
    votes: list[int] = []
    if depth:
        imb = depth.get("book_imbalance")
        if imb is not None:
            subs["book_imbalance"] = imb
            if imb > 0.55:
                votes.append(1)
            elif imb < 0.45:
                votes.append(-1)
    if ohlc and ohlc.get("n", 0) >= 4:
        vols, closes = ohlc["volumes"], ohlc["closes"]
        half = len(vols) // 2
        if half >= 1:
            avg1 = sum(vols[:half]) / half
            avg2 = sum(vols[half:]) / (len(vols) - half)
            price_chg = closes[-1] - closes[0]
            if avg2 > avg1 * 1.1 and price_chg != 0:
                v = 1 if price_chg > 0 else -1
                subs["volume_confirms_move"] = v
                votes.append(v)
    avail = bool(depth or (ohlc and ohlc.get("n", 0) >= 4))
    return _family_from_votes(avail, votes, subs)


def _regime_family(fng: dict[str, Any] | None, regime: dict[str, Any] | None) -> dict[str, Any]:
    """Crypto fear/greed (contrarian) + macro risk_on/risk_off + VIX regime."""
    subs: dict[str, Any] = {}
    votes: list[int] = []
    if fng and fng.get("available"):
        cls = str(fng.get("classification") or "")
        subs["fear_greed"] = f"{fng.get('value')} ({cls})"
        if cls in ("extreme fear", "fear"):
            votes.append(1)
        elif cls in ("extreme greed", "greed"):
            votes.append(-1)
    if regime and regime.get("available"):
        r = regime.get("regime")
        if r:
            subs["macro_regime"] = r
            if r == "risk_on":
                votes.append(1)
            elif r == "risk_off":
                votes.append(-1)
        vr = regime.get("vix_regime")
        if vr:
            subs["vix_regime"] = vr
            if vr in ("calm", "normal"):
                votes.append(1)
            elif vr in ("elevated", "stressed"):
                votes.append(-1)
    avail = bool((fng and fng.get("available")) or (regime and regime.get("available")))
    return _family_from_votes(avail, votes, subs)


def _positioning_family(funding: dict[str, Any] | None, oi_trend: dict[str, Any] | None) -> dict[str, Any]:
    """Bonus family (never required for confluence): Binance funding rate as
    a contrarian crowding signal; OI trend reported for context only."""
    subs: dict[str, Any] = {}
    votes: list[int] = []
    if funding:
        fr = funding.get("funding_rate")
        if fr is not None:
            subs["funding_rate_pct_8h"] = round(fr * 100, 4)
            if fr > 0.0004:
                votes.append(-1)  # crowded long -> contrarian caution
            elif fr < -0.0004:
                votes.append(1)  # crowded short -> contrarian bullish
    if oi_trend and oi_trend.get("oi_chg_pct_6h") is not None:
        subs["oi_chg_pct_6h"] = oi_trend["oi_chg_pct_6h"]
    avail = bool(funding or oi_trend)
    return _family_from_votes(avail, votes, subs)


def _vol_regime(tech: dict[str, Any] | None, ohlc: dict[str, Any] | None) -> str:
    atr_pct = (tech or {}).get("atr14_pct")
    if atr_pct is not None:
        return "low" if atr_pct < 2.0 else "normal" if atr_pct < 5.0 else "high"
    if ohlc and ohlc.get("n", 0) >= 6:
        closes = ohlc["closes"]
        rets = [closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes)) if closes[i - 1]]
        if rets:
            mean = sum(rets) / len(rets)
            sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / len(rets))
            hourly_pct = sd * 100
            return "low" if hourly_pct < 0.3 else "normal" if hourly_pct < 0.8 else "high"
    return "unknown"


# ---------------------------------------------------------------------------
# 6. confluence combine — this is the conviction gate. A single family voting
#    strongly is NOT enough: needs >=3 of the 4 core families available, and
#    >=75% of those available agreeing bullish, before direction can be "long".
# ---------------------------------------------------------------------------

def _combine(
    families: dict[str, dict[str, Any]], *, divergence_flag: bool, wide_spread_flag: bool,
) -> tuple[str, float, list[dict[str, Any]]]:
    reasons: list[dict[str, Any]] = []
    for fam_name in (*CORE_FAMILIES, "positioning"):
        fam = families.get(fam_name) or {}
        for factor, value in (fam.get("subs") or {}).items():
            reasons.append({"family": fam_name, "factor": factor, "value": value})

    avail_core = [f for f in CORE_FAMILIES if (families.get(f) or {}).get("avail")]
    if len(avail_core) < CONFLUENCE_MIN_AVAIL:
        reasons.append({
            "family": "confluence", "factor": "data_coverage",
            "value": f"only {len(avail_core)}/{len(CORE_FAMILIES)} core families available — below confluence bar",
        })
        return "flat", 0.0, reasons

    agree = sum(1 for f in avail_core if families[f]["vote"] == 1)
    ratio = agree / len(avail_core)
    avg_strength = sum(families[f]["strength"] for f in avail_core) / len(avail_core)
    reasons.append({
        "family": "confluence", "factor": "agreement",
        "value": f"{agree}/{len(avail_core)} core families bullish (ratio {ratio:.2f}, need >={CONFLUENCE_MIN_RATIO})",
    })

    quality_mult = 1.0
    if divergence_flag:
        quality_mult *= 0.7
        reasons.append({"family": "quality", "factor": "price_cross_source_divergence", "value": "flagged — conviction discounted"})
    if wide_spread_flag:
        quality_mult *= 0.85
        reasons.append({"family": "quality", "factor": "wide_spread", "value": "illiquid book — conviction discounted"})

    positioning_bonus = 0.0
    posf = families.get("positioning") or {}
    if ratio >= CONFLUENCE_MIN_RATIO and posf.get("avail") and posf.get("vote") == 1:
        positioning_bonus = 0.05
        reasons.append({"family": "positioning", "factor": "bonus", "value": "positioning family also agrees bullish (+0.05)"})

    if ratio >= CONFLUENCE_MIN_RATIO:
        direction = "long"
        conviction = ratio * avg_strength * quality_mult + positioning_bonus
    else:
        direction = "flat"
        conviction = ratio * avg_strength * quality_mult * 0.4  # informational only, not actionable
    return direction, round(max(0.0, min(1.0, conviction)), 3), reasons


# ---------------------------------------------------------------------------
# top-level: fuse() / fuse_all()
# ---------------------------------------------------------------------------

def fuse(symbol: str) -> dict[str, Any]:
    """High-conviction long/flat signal + execution/cost read for one pair.

    Fuses: Kraken order-book microstructure (spread/imbalance) + Kraken
    intraday OHLC, multi-source price cross-check, core.market_intel
    technicals + crypto_fear_greed, core.market_signals macro/VIX regime, and
    a best-effort Binance funding/OI positioning bonus. Never raises — any
    dead source degrades that factor to unavailable/neutral.
    """
    ts = _now()
    try:
        cache = _load_cache()
        codes = _symbol_codes(symbol)
        kraken_code = codes.get("kraken")

        depth = _cached(cache, f"depth:{symbol}", DEPTH_TTL_S, lambda: _kraken_depth(kraken_code)) if kraken_code else None
        ticker = _cached(cache, f"ticker:{symbol}", TICKER_TTL_S, lambda: _kraken_ticker(kraken_code)) if kraken_code else None
        ohlc = _cached(cache, f"ohlc:{symbol}", OHLC_TTL_S, lambda: _kraken_ohlc(kraken_code)) if kraken_code else None

        xcheck = _price_cross_check(symbol, codes, cache, (ticker or {}).get("last"))

        tech: dict[str, Any] = {}
        fng: dict[str, Any] = {}
        if market_intel is not None:
            try:
                tech = market_intel.technicals(symbol) or {}
            except Exception:
                tech = {}
            try:
                fng = market_intel.crypto_fear_greed() or {}
            except Exception:
                fng = {}
        regime = _lightweight_regime()

        funding = None
        oi_trend = None
        bsym = codes.get("binance")
        if bsym:
            funding = _cached(cache, f"funding:{symbol}", POSITIONING_TTL_S, lambda: _binance_funding(bsym))
            oi_trend = _cached(cache, f"oitrend:{symbol}", POSITIONING_TTL_S, lambda: _binance_oi_trend(bsym))

        _save_cache(cache)

        families = {
            "trend": _trend_family(tech),
            "momentum": _momentum_family(tech, ohlc),
            "microstructure": _microstructure_family(depth, ohlc),
            "regime": _regime_family(fng, regime),
            "positioning": _positioning_family(funding, oi_trend),
        }
        wide_spread_flag = bool(depth and (depth.get("spread_bps") or 0) > WIDE_SPREAD_FLAG_BPS)
        direction, conviction, reasons = _combine(
            families, divergence_flag=bool(xcheck.get("flag")), wide_spread_flag=wide_spread_flag,
        )

        spread_bps = (depth or {}).get("spread_bps")
        book_imbalance = (depth or {}).get("book_imbalance")
        use_maker = bool(spread_bps is not None and spread_bps >= MAKER_SPREAD_THRESHOLD_BPS)

        price = (ticker or {}).get("last")
        if price is None and xcheck.get("prices"):
            price = next(iter(xcheck["prices"].values()), None)
        if price is None:
            price = tech.get("price")

        return {
            "symbol": symbol, "ts": ts, "price": price,
            "conviction": conviction, "direction": direction,
            "spread_bps": spread_bps, "book_imbalance": book_imbalance,
            "use_maker": use_maker,
            "regime": (regime or {}).get("regime"),
            "vol_regime": _vol_regime(tech, ohlc),
            "reasons": reasons,
            "sources": {
                "kraken_depth": bool(depth), "kraken_ticker": bool(ticker), "kraken_ohlc": bool(ohlc),
                "market_intel_technicals": bool(tech.get("available")),
                "crypto_fear_greed": bool(fng.get("available")),
                "macro_regime": bool((regime or {}).get("available")),
                "price_cross_check": xcheck.get("prices") or {},
                "binance_funding": bool(funding), "binance_oi_trend": bool(oi_trend),
            },
        }
    except Exception as exc:
        return {
            "symbol": symbol, "ts": ts, "price": None,
            "conviction": 0.0, "direction": "flat",
            "spread_bps": None, "book_imbalance": None, "use_maker": False,
            "regime": None, "vol_regime": "unknown",
            "reasons": [{"family": "error", "factor": "exception", "value": f"{type(exc).__name__}: {exc}"}],
            "sources": {},
        }


def fuse_all(symbols: list[str]) -> dict[str, Any]:
    """fuse() for a list of pairs. Each symbol is independent — one bad
    symbol/source never takes the rest down."""
    out: dict[str, Any] = {}
    for sym in symbols or []:
        try:
            out[sym] = fuse(sym)
        except Exception as exc:  # pragma: no cover - fuse() already fails open
            out[sym] = {
                "symbol": sym, "conviction": 0.0, "direction": "flat",
                "reasons": [{"family": "error", "factor": "exception", "value": f"{type(exc).__name__}: {exc}"}],
            }
    return out


if __name__ == "__main__":
    for sym in ("BTC-USD", "ETH-USD", "SOL-USD"):
        print(json.dumps(fuse(sym), indent=2, default=str))
