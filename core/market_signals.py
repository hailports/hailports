"""market_signals.py — modular, $0, pluggable market + economic signal ingest.

This is the SIGNAL half of the paper market-opportunity engine (see
agents/markets_paper_trader.py). It pulls "literally everything" we can get for
free and folds it into one MarketState the conviction model can score:

  prices        real OHLC for a tradeable universe (equities/ETFs/indices/crypto)
                via the public Yahoo Finance chart API ($0, no key); crypto falls
                back to CoinGecko / Coinbase spot.
  macro         FRED CSV (no key): 10y/2y yields (curve slope), CPI, unemployment,
                fed funds. Cached 6h.
  structure     derived market-structure read: SPY trend vs 200dma, VIX level +
                regime, universe breadth (% above 50dma), crude sector rotation.
  sentiment     a $0 fear/greed proxy built from VIX + index momentum + breadth.
                (A paid news/social feed would attach at the documented seam below;
                we do NOT spend on data.)

Everything is fetched through a TTL cache (data/trading/markets_cache.json) so a
15-minute trading loop never hammers a source — $0-data discipline.

Providers are pluggable: PROVIDERS is an ordered list of callables; add one and it
shows up in MarketState["providers"]. compute_features() turns a close series into
the per-symbol features the model reads (trend, momentum, z-score, realized vol).

NOTE: nothing here trades or spends. It only reads public data.
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

CACHE_PATH = BASE_DIR / "data" / "trading" / "markets_cache.json"
UA = {"User-Agent": "Mozilla/5.0 (research; paper-only)"}

# Tradeable universe (liquid, free to price). Indices/VIX are read-only context.
EQUITY_UNIVERSE = (
    "SPY", "QQQ", "IWM", "DIA",                                  # broad index ETFs
    "XLK", "XLF", "XLE", "XLV", "XLY", "XLP", "XLI", "XLU", "XLB", "XLRE", "XLC",  # sectors
    "TLT", "GLD",                                                # bonds, gold
    "AAPL", "MSFT", "NVDA", "AMZN",                              # megacap singles
)
CRYPTO_UNIVERSE = ("BTC-USD", "ETH-USD")
CONTEXT_SYMBOLS = ("^VIX", "^TNX", "^IRX", "^FVX", "^TYX")  # never traded; regime + rates context
SECTOR_ETFS = ("XLK", "XLF", "XLE", "XLV", "XLY", "XLP", "XLI", "XLU", "XLB", "XLRE", "XLC")

# FRED is best-effort enrichment only (CPI/jobs) — rates+curve come from Yahoo so a
# slow/blocked FRED can never stall the trading loop. Keyed name -> FRED series id.
FRED_SERIES = {
    "cpi": "CPIAUCSL",      # CPI index (compute yoy)
    "unrate": "UNRATE",     # unemployment
    "fedfunds": "FEDFUNDS", # effective fed funds
}

# ---------------------------------------------------------------------------
# low-level fetch + cache
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


def _http_text(url: str, timeout: float = 15) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _http_json(url: str, timeout: float = 15) -> Any:
    return json.loads(_http_text(url, timeout))


# ---------------------------------------------------------------------------
# price provider (Yahoo chart API primary; crypto fallbacks)
# ---------------------------------------------------------------------------

def _yahoo_chart(symbol: str, rng: str = "1y", interval: str = "1d") -> dict[str, Any] | None:
    enc = urllib.parse.quote(symbol, safe="")
    for host in ("query1", "query2"):
        try:
            d = _http_json(
                f"https://{host}.finance.yahoo.com/v8/finance/chart/{enc}"
                f"?range={rng}&interval={interval}"
            )
            res = (((d or {}).get("chart") or {}).get("result") or [None])[0]
            if not res:
                continue
            meta = res.get("meta") or {}
            quote = ((res.get("indicators") or {}).get("quote") or [{}])[0]
            closes = [c for c in (quote.get("close") or []) if isinstance(c, (int, float))]
            if not closes:
                continue
            return {
                "price": float(meta.get("regularMarketPrice") or closes[-1]),
                "prev_close": float(meta.get("chartPreviousClose") or (closes[-2] if len(closes) > 1 else closes[-1])),
                "closes": [float(c) for c in closes][-520:],
                "currency": meta.get("currency") or "USD",
                "source": "yahoo",
            }
        except Exception:
            continue
    return None


def _coingecko_spot(symbol: str) -> dict[str, Any] | None:
    ids = {"BTC-USD": "bitcoin", "ETH-USD": "ethereum"}
    cid = ids.get(symbol)
    if not cid:
        return None
    try:
        d = _http_json(
            f"https://api.coingecko.com/api/v3/simple/price?ids={cid}"
            f"&vs_currencies=usd&include_24hr_change=true"
        )
        px = float(d[cid]["usd"])
        chg = float(d[cid].get("usd_24h_change") or 0.0) / 100.0
        prev = px / (1 + chg) if (1 + chg) else px
        return {"price": px, "prev_close": prev, "closes": [prev, px], "currency": "USD", "source": "coingecko"}
    except Exception:
        return None


def _coinbase_spot(symbol: str) -> dict[str, Any] | None:
    pair = symbol if "-" in symbol else f"{symbol}-USD"
    try:
        d = _http_json(f"https://api.coinbase.com/v2/prices/{pair}/spot")
        px = float(d["data"]["amount"])
        return {"price": px, "prev_close": px, "closes": [px], "currency": "USD", "source": "coinbase"}
    except Exception:
        return None


def fetch_prices(symbols: list[str], *, cache: dict[str, Any], ttl_s: float = 600) -> dict[str, dict[str, Any]]:
    """Real prices + recent close history for each symbol. Cached, $0."""
    out: dict[str, dict[str, Any]] = {}
    for sym in symbols:
        key = f"px:{sym}"
        hit = _cache_get(cache, key, ttl_s)
        if hit:
            out[sym] = hit
            continue
        bar = _yahoo_chart(sym)
        if bar is None and sym in CRYPTO_UNIVERSE:
            bar = _coingecko_spot(sym) or _coinbase_spot(sym)
        if bar is None:
            # last-known fallback keeps the loop alive when a source blips
            stale = (cache.get(key) or {}).get("v")
            if stale:
                stale = dict(stale, source=str(stale.get("source")) + "/stale")
                out[sym] = stale
            continue
        bar["ts"] = _now()
        _cache_put(cache, key, bar)
        out[sym] = bar
    return out


# ---------------------------------------------------------------------------
# per-symbol features (reused by the conviction model)
# ---------------------------------------------------------------------------

def _sma(xs: list[float], n: int) -> float | None:
    if len(xs) < n:
        return None
    return sum(xs[-n:]) / n


def compute_features(closes: list[float]) -> dict[str, Any]:
    """Turn a close series into the features the model scores."""
    f: dict[str, Any] = {"n": len(closes)}
    if len(closes) < 25:
        return f
    px = closes[-1]
    sma20 = _sma(closes, 20)
    sma50 = _sma(closes, 50)
    sma200 = _sma(closes, 200)
    f["price"] = px
    f["sma20"], f["sma50"], f["sma200"] = sma20, sma50, sma200
    f["mom20"] = (px / closes[-21] - 1.0) if len(closes) > 21 else 0.0
    f["mom5"] = (px / closes[-6] - 1.0) if len(closes) > 6 else 0.0
    f["ret1"] = (px / closes[-2] - 1.0) if len(closes) > 2 else 0.0
    f["above_50dma"] = bool(sma50 and px > sma50)
    f["above_200dma"] = bool(sma200 and px > sma200)
    f["trend"] = bool(sma20 and sma50 and sma20 > sma50)
    # z-score of price vs 20d mean (mean-reversion overlay)
    window = closes[-20:]
    mean = sum(window) / len(window)
    var = sum((c - mean) ** 2 for c in window) / len(window)
    sd = math.sqrt(var) if var > 0 else 0.0
    f["zscore20"] = (px - mean) / sd if sd else 0.0
    # annualized realized vol from daily log returns (used for sizing + stops)
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes)) if closes[i - 1] > 0]
    rets = rets[-20:]
    if rets:
        m = sum(rets) / len(rets)
        v = sum((r - m) ** 2 for r in rets) / len(rets)
        daily_vol = math.sqrt(v)
    else:
        daily_vol = 0.0
    f["daily_vol"] = daily_vol
    f["ann_vol"] = daily_vol * math.sqrt(252)
    return f


# ---------------------------------------------------------------------------
# providers: macro / structure / sentiment
# ---------------------------------------------------------------------------

def _fred_csv(series_id: str, n: int = 30) -> list[tuple[str, float]]:
    # constrain the download to a recent window — the full series (since 1962)
    # is hundreds of KB; cosd (observation start) keeps it small. Short timeout:
    # FRED is enrichment only, never on the critical path.
    start = (datetime.now(timezone.utc).date().replace(year=datetime.now(timezone.utc).year - 3)).isoformat()
    txt = _http_text(
        f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}&cosd={start}",
        timeout=8,
    )
    rows: list[tuple[str, float]] = []
    for line in txt.splitlines()[1:]:
        parts = line.split(",")
        if len(parts) < 2:
            continue
        try:
            rows.append((parts[0], float(parts[1])))
        except ValueError:
            continue  # FRED uses "." for missing
    return rows[-n:]


def provide_macro(state: dict[str, Any], cache: dict[str, Any]) -> dict[str, Any]:
    hit = _cache_get(cache, "macro", ttl_s=6 * 3600)
    if hit:
        return hit
    prices = state.get("prices") or {}
    macro: dict[str, Any] = {}
    # rates + curve from Yahoo Treasury yield indices (reliable, already priced)
    rate_map = {"us10y": "^TNX", "us3m": "^IRX", "us5y": "^FVX", "us30y": "^TYX"}
    for name, sym in rate_map.items():
        px = (prices.get(sym) or {}).get("price")
        if px is not None:
            macro[name] = round(float(px), 3)
    if "us10y" in macro and "us3m" in macro:
        macro["yield_curve_10y_3m"] = round(macro["us10y"] - macro["us3m"], 3)
        macro["curve_inverted"] = macro["yield_curve_10y_3m"] < 0
    macro["rates_source"] = "yahoo"
    # best-effort FRED enrichment (CPI/jobs); never blocks — short timeout, swallowed
    for name, sid in FRED_SERIES.items():
        try:
            rows = _fred_csv(sid, n=16)
            if rows:
                macro[name] = rows[-1][1]
                if name == "cpi" and len(rows) > 13:
                    macro["cpi_yoy"] = round((rows[-1][1] / rows[-13][1] - 1.0) * 100, 2)
        except Exception:
            continue
    _cache_put(cache, "macro", macro)
    return macro


def provide_structure(state: dict[str, Any], cache: dict[str, Any]) -> dict[str, Any]:
    prices = state.get("prices") or {}
    feats = state.get("features") or {}
    struct: dict[str, Any] = {}
    spy = feats.get("SPY") or {}
    struct["spy_above_200dma"] = bool(spy.get("above_200dma"))
    struct["spy_trend"] = bool(spy.get("trend"))
    struct["spy_mom20"] = round(float(spy.get("mom20") or 0.0), 4)
    vix = (prices.get("^VIX") or {}).get("price")
    struct["vix"] = round(float(vix), 2) if vix else None
    if struct["vix"] is not None:
        struct["vix_regime"] = (
            "calm" if struct["vix"] < 16 else "normal" if struct["vix"] < 22
            else "elevated" if struct["vix"] < 30 else "stressed"
        )
    # breadth: % of equity universe above its 50dma
    eq = [s for s in EQUITY_UNIVERSE if s in feats and feats[s].get("n", 0) >= 50]
    if eq:
        above = sum(1 for s in eq if feats[s].get("above_50dma"))
        struct["breadth_pct_above_50dma"] = round(100 * above / len(eq), 1)
    # crude sector rotation: rank sector ETFs by 20d momentum
    sect = [(s, float(feats[s].get("mom20") or 0.0)) for s in SECTOR_ETFS if s in feats]
    sect.sort(key=lambda r: r[1], reverse=True)
    struct["sector_leaders"] = [s for s, _ in sect[:3]]
    struct["sector_laggards"] = [s for s, _ in sect[-3:]]
    # overall regime
    risk_on = struct.get("spy_above_200dma") and (struct.get("vix") or 99) < 24 and (struct.get("breadth_pct_above_50dma") or 0) >= 50
    risk_off = (not struct.get("spy_above_200dma")) or (struct.get("vix") or 0) >= 28 or (struct.get("breadth_pct_above_50dma") or 100) < 35
    struct["regime"] = "risk_on" if risk_on else "risk_off" if risk_off else "neutral"
    return struct


def provide_sentiment(state: dict[str, Any], cache: dict[str, Any]) -> dict[str, Any]:
    """$0 fear/greed proxy (0=extreme fear, 100=extreme greed).

    Seam: a paid news/social sentiment feed would be folded in here. We do NOT
    spend on data, so this is derived from price-based risk appetite only.
    """
    struct = state.get("structure") or {}
    comps: list[float] = []
    vix = struct.get("vix")
    if vix is not None:
        comps.append(max(0.0, min(100.0, 100 - (vix - 10) * (100 / 30))))  # vix 10->100, 40->0
    breadth = struct.get("breadth_pct_above_50dma")
    if breadth is not None:
        comps.append(float(breadth))
    spy_mom = struct.get("spy_mom20")
    if spy_mom is not None:
        comps.append(max(0.0, min(100.0, 50 + spy_mom * 1000)))  # +5% -> 100, -5% -> 0
    score = round(sum(comps) / len(comps), 1) if comps else 50.0
    label = (
        "extreme_fear" if score < 25 else "fear" if score < 45
        else "neutral" if score < 55 else "greed" if score < 75 else "extreme_greed"
    )
    return {"fear_greed": score, "label": label, "components": len(comps), "source": "$0_price_derived"}


# ordered, pluggable provider registry (each gets the state-so-far + cache)
def provide_smart_money(state: dict[str, Any], cache: dict[str, Any]) -> dict[str, Any]:
    """On-chain smart-money flow (Solana, $0). Lazy import so a tracker issue can
    never take down the signal pipeline the bots depend on."""
    from core import smart_money_tracker as smt

    return smt.provide_smart_money(state, cache)


PROVIDERS: list[tuple[str, Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]]] = [
    ("macro", provide_macro),
    ("structure", provide_structure),
    ("sentiment", provide_sentiment),
    ("smart_money", provide_smart_money),
]


# ---------------------------------------------------------------------------
# top-level ingest
# ---------------------------------------------------------------------------

def tradeable_symbols() -> list[str]:
    return list(EQUITY_UNIVERSE) + list(CRYPTO_UNIVERSE)


def ingest(*, price_ttl_s: float = 600) -> dict[str, Any]:
    """Pull everything into one MarketState dict. $0, cached."""
    cache = _load_cache()
    all_syms = list(EQUITY_UNIVERSE) + list(CRYPTO_UNIVERSE) + list(CONTEXT_SYMBOLS)
    prices = fetch_prices(all_syms, cache=cache, ttl_s=price_ttl_s)
    features = {s: compute_features(b.get("closes") or []) for s, b in prices.items()}
    state: dict[str, Any] = {
        "ts": _now(),
        "prices": prices,
        "features": features,
        "providers": [],
    }
    for name, fn in PROVIDERS:
        try:
            state[name] = fn(state, cache)
            state["providers"].append(name)
        except Exception as exc:
            state[name] = {"error": f"{type(exc).__name__}: {exc}"}
    _save_cache(cache)
    state["coverage"] = {
        "priced_symbols": sum(1 for s in tradeable_symbols() if s in prices),
        "tradeable_total": len(tradeable_symbols()),
        "have_macro": bool(state.get("macro") and not state["macro"].get("error")),
    }
    return state


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="market signal ingest ($0, paper-research only)")
    ap.add_argument("--ttl", type=float, default=0, help="price cache TTL seconds (0 = always fresh)")
    args = ap.parse_args()
    st = ingest(price_ttl_s=args.ttl)
    slim = {
        "ts": st["ts"],
        "coverage": st["coverage"],
        "macro": st.get("macro"),
        "structure": st.get("structure"),
        "sentiment": st.get("sentiment"),
        "sample_features": {k: st["features"].get(k) for k in ("SPY", "BTC-USD") if k in st["features"]},
    }
    print(json.dumps(slim, indent=2, default=str))
