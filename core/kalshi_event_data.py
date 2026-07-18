"""kalshi_event_data.py — $0, keyless, LOCAL-LLM-only edge engine for Kalshi markets.

Niche Kalshi contracts (weather, small econ prints, low-volume "general" events)
get almost no professional attention because there's no money in building a
paid data pipeline for a $20 position. This module is the asymmetric-edge
play: pull every FREE, keyless signal relevant to a market's category, fuse it
through core.market_intel.event_probability() (LOCAL Ollama only), and compare
the resulting probability to the market's own implied price net of Kalshi's
trading fee.

Sources (all $0, all keyless):
  1. Weather   — api.weather.gov (NOAA), keyless. Static lat/lon table for the
                 major US cities Kalshi actually lists weather contracts on;
                 pulls the 7-day /forecast for the resolution date.
  2. Economic  — FRED CSV via core.market_signals._fred_csv (reused, not
                 re-implemented) for CPI/unemployment/jobless-claims/fed-funds/
                 GDP/payrolls/consumer-sentiment trend context.
  3. General   — Google News RSS (fallback: Yahoo News RSS) keyword search,
                 same title/CDATA-parsing shape as core.market_intel.headlines().
  4. Microstructure — Kalshi's own public GET /markets/{ticker} (no auth for
                 read-only market data; base URL reused from core.kalshi_bot).
  5. Fusion    — core.market_intel.event_probability(question, context): the
                 ONE LLM seam, hard-coded LOCAL Ollama only. Never touches
                 OpenRouter / free_llm_pool / any paid or remote provider.

Everything below is TTL-cached in data/trading/kalshi_event_data_cache.json and
FAILS OPEN: a dead source (NOAA down, FRED blocked, no news hit, Ollama not
running, malformed market dict) degrades that one signal to "unavailable" and
the pipeline keeps going — estimate() itself never raises.

estimate(market) -> {
  p_yes, p_market, edge, edge_after_fee, fee_dollars, confidence, category,
  data_sources_used, kelly_fraction, side, liquidity_ok, recommend, ...
}
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import urllib.parse
import urllib.request
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    from core import BASE_DIR
except Exception:  # pragma: no cover - allow standalone import
    BASE_DIR = Path(os.path.expanduser("~/claude-stack"))

from core import market_intel
from core.market_signals import _fred_csv as _fred_csv_series
from core.kalshi_bot import PROD_BASE as KALSHI_PUBLIC_BASE

CACHE_PATH = BASE_DIR / "data" / "trading" / "kalshi_event_data_cache.json"
UA = {"User-Agent": "Mozilla/5.0 (research; paper-only)"}
# NOAA asks API consumers to identify themselves in the UA (reduces block risk).
NOAA_UA = {
    "User-Agent": "claude-stack-kalshi-research/1.0 (contact: user@example.com)",
    "Accept": "application/geo+json",
}

# tunables — deliberately conservative given the probability comes from one
# local-LLM call, not an ensemble.
MIN_EDGE_AFTER_FEE = 0.05     # 5pp net-of-fee edge before we call it real
MIN_LIQUIDITY = 50            # min(volume, liquidity/open_interest) contracts
MAX_SPREAD_CENTS = 12
MIN_HOURS_TO_CLOSE = 0.5      # don't recommend contracts about to expire
KELLY_SCALE = 0.5             # half-Kelly: single local-LLM estimate, not an ensemble
KELLY_CAP = 0.20              # hard cap regardless of computed edge


# ---------------------------------------------------------------------------
# low-level fetch + TTL cache (mirrors core/market_intel.py conventions)
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


def _http_text(url: str, headers: dict[str, str] | None = None, timeout: float = 10) -> str:
    req = urllib.request.Request(url, headers=headers or UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _http_json(url: str, headers: dict[str, str] | None = None, timeout: float = 10) -> Any:
    return json.loads(_http_text(url, headers=headers, timeout=timeout))


# ---------------------------------------------------------------------------
# category inference
# ---------------------------------------------------------------------------

_WEATHER_HINTS = ("weather", "temperature", "temp ", "precip", "rain", "snow", "°f", "degrees f", "high temp", "low temp")
_ECON_HINTS = (
    "cpi", "inflation", "consumer price", "jobs report", "unemployment", "nonfarm", "payroll",
    "federal reserve", "fomc", "interest rate", "gdp", "jobless claims", "consumer sentiment",
    "producer price", "ppi", "fed rate", "rate hike", "rate cut",
)


def _market_text(market: dict[str, Any]) -> str:
    return " ".join(str(market.get(k) or "") for k in ("title", "subtitle", "question", "ticker"))


def _infer_category(market: dict[str, Any]) -> str:
    text = _market_text(market).lower()
    if any(h in text for h in _WEATHER_HINTS):
        return "weather"
    if any(h in text for h in _ECON_HINTS):
        return "economic"
    return "general"


# ---------------------------------------------------------------------------
# 1. weather — NOAA / api.weather.gov (keyless)
# ---------------------------------------------------------------------------

# static lat/lon for the major US cities Kalshi actually lists weather
# contracts on, plus a few common extras. word-boundary matched against the
# market title so "la" doesn't false-hit inside "atlanta" etc.
CITIES: list[tuple[tuple[str, ...], float, float]] = [
    (("new york city", "new york", "nyc", "knyc", "central park"), 40.7829, -73.9654),
    (("chicago", "kmdw", "midway"), 41.7868, -87.7522),
    (("austin", "kaus"), 30.1975, -97.6664),
    (("denver", "kden"), 39.8561, -104.6737),
    (("los angeles", "klax"), 33.9425, -118.4081),
    (("miami", "kmia"), 25.7959, -80.2870),
    (("philadelphia", "philly", "kphl"), 39.8729, -75.2437),
    (("houston", "khou"), 29.9902, -95.3368),
    (("phoenix", "kphx"), 33.4342, -112.0116),
    (("seattle", "ksea"), 47.4502, -122.3088),
    (("san francisco", "ksfo"), 37.6213, -122.3790),
    (("portland", "kpdx"), 45.5898, -122.5951),
    (("atlanta", "katl"), 33.6407, -84.4277),
    (("dallas", "kdfw"), 32.8998, -97.0403),
    (("minneapolis", "kmsp"), 44.8848, -93.2223),
    (("detroit", "kdtw"), 42.2124, -83.3534),
    (("boston", "kbos"), 42.3656, -71.0096),
    (("washington dc", "washington", "kdca"), 38.8512, -77.0402),
    (("las vegas", "vegas", "klas"), 36.0840, -115.1537),
]


def _match_city(text: str) -> tuple[str, float, float] | None:
    t = text.lower()
    for aliases, lat, lon in CITIES:
        for alias in aliases:
            if re.search(rf"\b{re.escape(alias)}\b", t):
                return aliases[0], lat, lon
    return None


def _resolution_date(market: dict[str, Any]) -> date | None:
    for key in ("event_date", "resolution_date", "resolution_time", "close_time", "expiration_time", "expected_expiration_time"):
        raw = market.get(key)
        if not raw:
            continue
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).date()
        except Exception:
            continue
    return None


def _hours_to_close(market: dict[str, Any]) -> float | None:
    for key in ("close_time", "expiration_time", "expected_expiration_time"):
        raw = market.get(key)
        if not raw:
            continue
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return round((dt - datetime.now(timezone.utc)).total_seconds() / 3600.0, 2)
        except Exception:
            continue
    return None


def _noaa_forecast_periods(lat: float, lon: float, *, ttl_s: float = 1800) -> list[dict[str, Any]] | None:
    try:
        cache = _load_cache()
        key = f"noaa:{round(lat, 2)},{round(lon, 2)}"
        hit = _cache_get(cache, key, ttl_s)
        if hit is not None:
            return hit
        points = _http_json(f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}", headers=NOAA_UA, timeout=10)
        forecast_url = ((points or {}).get("properties") or {}).get("forecast")
        if not forecast_url:
            return None
        fdata = _http_json(forecast_url, headers=NOAA_UA, timeout=10)
        periods = ((fdata or {}).get("properties") or {}).get("periods") or []
        if periods:
            _cache_put(cache, key, periods)
            _save_cache(cache)
        return periods or None
    except Exception:
        return None


def weather_signal(market: dict[str, Any]) -> dict[str, Any]:
    """NOAA forecast (high/low temp, precip prob) for the market's city+date. Fails open."""
    neutral = {"available": False, "source": None}
    try:
        match = _match_city(_market_text(market))
        if not match:
            return neutral
        city, lat, lon = match
        periods = _noaa_forecast_periods(lat, lon)
        if not periods:
            return {"available": False, "source": None, "city": city}
        target = _resolution_date(market)
        chosen, exact = None, False
        for p in periods:
            try:
                p_date = datetime.fromisoformat(str(p.get("startTime")).replace("Z", "+00:00")).date()
            except Exception:
                continue
            if target and p_date == target and p.get("isDaytime"):
                chosen, exact = p, True
                break
        if chosen is None:
            chosen = next((p for p in periods if p.get("isDaytime")), periods[0] if periods else None)
        if not chosen:
            return {"available": False, "source": None, "city": city}
        precip = (chosen.get("probabilityOfPrecipitation") or {}).get("value")
        out = {
            "available": True,
            "source": "noaa_weather.gov",
            "city": city,
            "period_name": chosen.get("name"),
            "date_matched_exact": exact,
            "temperature": chosen.get("temperature"),
            "temperature_unit": chosen.get("temperatureUnit"),
            "precip_probability_pct": precip,
            "short_forecast": chosen.get("shortForecast"),
        }
        out["context"] = (
            f"NOAA forecast for {city.title()} ({chosen.get('name')}, "
            f"{'exact date match' if exact else 'nearest available period'}): "
            f"{chosen.get('temperature')}°{chosen.get('temperatureUnit')}, "
            f"precip chance {precip if precip is not None else 'n/a'}%, {chosen.get('shortForecast')}."
        )
        return out
    except Exception as exc:
        return {"available": False, "source": None, "error": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------------------
# 2. economic — FRED via core.market_signals._fred_csv (reused, not rebuilt)
# ---------------------------------------------------------------------------

ECON_SERIES: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (("cpi", "inflation", "consumer price"), "CPIAUCSL", "CPI (all urban consumers, index)"),
    (("jobless claims", "initial claims"), "ICSA", "initial jobless claims"),
    (("unemployment", "jobless rate"), "UNRATE", "unemployment rate"),
    (("federal reserve", "fomc", "fed rate", "interest rate", "rate hike", "rate cut"), "FEDFUNDS", "effective fed funds rate"),
    (("consumer sentiment", "umich", "michigan sentiment"), "UMCSENT", "U. Michigan consumer sentiment"),
    (("nonfarm", "payroll", "jobs report"), "PAYEMS", "nonfarm payrolls"),
    (("gdp", "gross domestic product"), "GDP", "nominal GDP"),
)


def econ_signal(market: dict[str, Any], *, ttl_s: float = 6 * 3600) -> dict[str, Any]:
    """Recent trend for the FRED series implied by the market title. Fails open."""
    neutral = {"available": False, "source": None}
    try:
        text = _market_text(market).lower()
        match = next(((sid, label) for kws, sid, label in ECON_SERIES if any(kw in text for kw in kws)), None)
        if not match:
            return neutral
        series_id, label = match
        cache = _load_cache()
        key = f"fred:{series_id}"
        rows = _cache_get(cache, key, ttl_s)
        if rows is None:
            rows = _fred_csv_series(series_id, n=13)
            if rows:
                _cache_put(cache, key, rows)
                _save_cache(cache)
        if not rows:
            return {"available": False, "source": None, "series_id": series_id}
        latest_asof, latest_val = rows[-1]
        prev_val = rows[-2][1] if len(rows) > 1 else None
        yoy_val = rows[-13][1] if len(rows) >= 13 else None
        chg = round(latest_val - prev_val, 4) if prev_val is not None else None
        yoy_chg_pct = round((latest_val / yoy_val - 1) * 100, 2) if yoy_val else None
        out = {
            "available": True,
            "source": "fred_csv",
            "series_id": series_id,
            "label": label,
            "latest_value": latest_val,
            "latest_asof": latest_asof,
            "change_from_prior": chg,
            "yoy_change_pct": yoy_chg_pct,
        }
        out["context"] = (
            f"FRED {label} ({series_id}): latest {latest_val} as of {latest_asof}"
            + (f", change {chg:+.3f} from prior reading" if chg is not None else "")
            + (f", {yoy_chg_pct:+.2f}% YoY" if yoy_chg_pct is not None else "")
            + "."
        )
        return out
    except Exception as exc:
        return {"available": False, "source": None, "error": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------------------
# 3. general/news — Google News RSS, Yahoo News RSS fallback (both keyless)
# ---------------------------------------------------------------------------

_CDATA_RE = re.compile(r"^<!\[CDATA\[(.*?)\]\]>$", re.S)


def _rss_titles(url: str, limit: int, *, timeout: float = 8) -> list[str]:
    txt = _http_text(url, timeout=timeout)
    out: list[str] = []
    for t in re.findall(r"<title>(.*?)</title>", txt, flags=re.S)[1:]:
        clean = t.strip()
        m = _CDATA_RE.match(clean)
        if m:
            clean = m.group(1).strip()
        clean = clean.replace("&amp;", "&").replace("&#39;", "'").replace("&quot;", '"')
        if clean:
            out.append(clean)
        if len(out) >= limit:
            break
    return out


def news_signal(market: dict[str, Any], *, limit: int = 6, ttl_s: float = 1800) -> dict[str, Any]:
    """Keyword headlines relevant to the market question. Fails open, $0."""
    neutral = {"available": False, "source": None, "headlines": []}
    try:
        query = str(market.get("title") or market.get("subtitle") or market.get("question") or "").strip()
        if not query:
            return neutral
        cache = _load_cache()
        key = "news:" + hashlib.sha1(query.encode("utf-8")).hexdigest()
        hit = _cache_get(cache, key, ttl_s)
        if hit is not None:
            return hit
        q = urllib.parse.quote(query[:200])
        out = dict(neutral)
        try:
            titles = _rss_titles(f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en", limit)
            if titles:
                out = {"available": True, "source": "google_news_rss", "headlines": titles}
        except Exception:
            pass
        if not out.get("available"):
            try:
                titles = _rss_titles(f"https://news.search.yahoo.com/rss?p={q}", limit)
                if titles:
                    out = {"available": True, "source": "yahoo_news_rss", "headlines": titles}
            except Exception:
                pass
        _cache_put(cache, key, out)
        _save_cache(cache)
        return out
    except Exception as exc:
        return {"available": False, "source": None, "headlines": [], "error": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------------------
# 4. microstructure — Kalshi's own public market-data API (no auth needed)
# ---------------------------------------------------------------------------

def kalshi_microstructure(market: dict[str, Any], *, ttl_s: float = 90) -> dict[str, Any]:
    """Live volume/liquidity/spread from Kalshi's public GET /markets/{ticker}.

    Falls back to whatever the caller already passed in `market` (yes_ask,
    yes_bid, volume, liquidity) if the ticker is missing or the API call
    fails — never raises.
    """
    ticker = str(market.get("ticker") or "").strip()
    fallback = {
        "available": market.get("yes_ask") is not None,
        "source": "input_market" if market.get("yes_ask") is not None else None,
        "yes_ask_cents": market.get("yes_ask"),
        "yes_bid_cents": market.get("yes_bid"),
        "volume": market.get("volume") or market.get("volume_24h"),
        "liquidity": market.get("liquidity") or market.get("open_interest"),
    }
    if not ticker:
        return fallback
    try:
        cache = _load_cache()
        key = f"kmicro:{ticker}"
        hit = _cache_get(cache, key, ttl_s)
        if hit is not None:
            return hit
        data = _http_json(f"{KALSHI_PUBLIC_BASE}/markets/{urllib.parse.quote(ticker)}", timeout=8)
        m = data.get("market") if isinstance(data, dict) else None
        if not isinstance(m, dict):
            return fallback
        out = {
            "available": True,
            "source": "kalshi_public_api",
            "yes_ask_cents": m.get("yes_ask", fallback["yes_ask_cents"]),
            "yes_bid_cents": m.get("yes_bid", fallback["yes_bid_cents"]),
            "volume": m.get("volume") or m.get("volume_24h") or fallback["volume"],
            "liquidity": m.get("liquidity") or m.get("open_interest") or fallback["liquidity"],
            "open_interest": m.get("open_interest"),
            "close_time": m.get("close_time"),
            "status": m.get("status"),
        }
        _cache_put(cache, key, out)
        _save_cache(cache)
        return out
    except Exception:
        return fallback


# ---------------------------------------------------------------------------
# fee / Kelly math
# ---------------------------------------------------------------------------

def _kalshi_fee_dollars(price_dollars: float) -> float:
    """Kalshi's standard per-contract taker fee: ceil(0.07 * P * (1-P) * 100) cents.

    P is the contract price in dollars (0..1). Approximates Kalshi's published
    fee schedule for the general market tier; close enough for an edge filter.
    """
    p = max(0.0, min(1.0, price_dollars))
    fee_cents = math.ceil(0.07 * p * (1 - p) * 100 - 1e-9)
    return max(0, fee_cents) / 100.0


def _kelly_fraction_from_edge(p_true: float, cost: float) -> float:
    """Binary-contract Kelly f* = (p - cost) / (1 - cost), half-Kelly scaled + capped.

    cost is the effective price paid per contract (market price + fee) for the
    side being bought; payout on a win is $1/contract. Scaled to half-Kelly and
    hard-capped because p_true comes from a single local-LLM call, not an
    ensemble — full Kelly on a noisy point estimate is how bankrolls die.
    """
    if cost <= 0 or cost >= 1:
        return 0.0
    raw = (p_true - cost) / (1 - cost)
    return round(max(0.0, min(KELLY_CAP, raw * KELLY_SCALE)), 4)


# ---------------------------------------------------------------------------
# fusion + top-level estimate()
# ---------------------------------------------------------------------------

def estimate(market: dict[str, Any]) -> dict[str, Any]:
    """Gather category-relevant free data, fuse via local Ollama, score edge vs market.

    Never raises — any internal failure degrades to a neutral, non-recommended
    estimate with the error noted.
    """
    try:
        return _estimate_inner(market or {})
    except Exception as exc:
        return {
            "ticker": (market or {}).get("ticker"),
            "p_yes": 0.5, "p_market": None, "edge": None, "edge_after_fee": None,
            "confidence": "low", "category": None, "data_sources_used": [],
            "kelly_fraction": 0.0, "side": None, "liquidity_ok": False,
            "recommend": False, "error": f"{type(exc).__name__}: {exc}",
            "generated_at": _now(),
        }


def _estimate_inner(market: dict[str, Any]) -> dict[str, Any]:
    title = str(market.get("title") or market.get("subtitle") or market.get("question") or "").strip()

    # real Kalshi category labels ("Economics", "Politics", "Weather", ...)
    # don't equal our internal "weather"/"economic" branch names, so match by
    # substring on the label first, then fall back to title-keyword inference
    # (which also catches an econ/weather question filed under an unrelated
    # or missing category label).
    category_label = str(market.get("category") or "").strip().lower()
    text_kind = _infer_category(market)
    if "weather" in category_label:
        category_kind = "weather"
    elif any(h in category_label for h in ("econ", "financ")):
        category_kind = "economic"
    else:
        category_kind = text_kind
    category = category_label or category_kind

    sources: list[str] = []
    context_parts: list[str] = []

    micro = kalshi_microstructure(market)
    if micro.get("available"):
        sources.append(micro.get("source") or "kalshi_public_api")
        context_parts.append(
            f"Market microstructure: yes_ask={micro.get('yes_ask_cents')}c yes_bid={micro.get('yes_bid_cents')}c "
            f"volume={micro.get('volume')} liquidity={micro.get('liquidity')}."
        )

    weather, econ = {"available": False}, {"available": False}
    if category_kind == "weather":
        weather = weather_signal(market)
        if weather.get("available"):
            sources.append(weather.get("source"))
            context_parts.append(weather.get("context") or "")
    elif category_kind == "economic":
        econ = econ_signal(market)
        if econ.get("available"):
            sources.append(econ.get("source") or "fred_csv")
            context_parts.append(econ.get("context") or "")

    news = news_signal(market)
    if news.get("available"):
        sources.append(news.get("source"))
        heads = news.get("headlines") or []
        if heads:
            context_parts.append("Recent headlines: " + "; ".join(heads[:6]))

    context = " ".join(p for p in context_parts if p).strip()
    question = title or str(market.get("ticker") or "unspecified Kalshi event")
    prob = market_intel.event_probability(question, context=context)
    p_yes = float(prob.get("p", 0.5))
    confidence = str(prob.get("confidence") or "low")
    sources.append(prob.get("source") or "neutral_fallback")

    # a category with no category-specific source is a weaker call regardless
    # of how confident the LLM sounded — cap it.
    if category_kind in {"weather", "economic"} and not (weather.get("available") or econ.get("available")):
        confidence = "low"

    yes_ask_cents = micro.get("yes_ask_cents") if micro.get("yes_ask_cents") is not None else market.get("yes_ask")
    yes_bid_cents = micro.get("yes_bid_cents") if micro.get("yes_bid_cents") is not None else market.get("yes_bid")
    p_market = round(float(yes_ask_cents) / 100.0, 4) if yes_ask_cents is not None else None

    fee_dollars = _kalshi_fee_dollars(p_market) if p_market is not None else 0.0
    edge = round(p_yes - p_market, 4) if p_market is not None else None
    edge_after_fee = round(edge - fee_dollars, 4) if edge is not None else None

    spread_cents = int(yes_ask_cents) - int(yes_bid_cents) if (yes_ask_cents is not None and yes_bid_cents is not None) else None
    volume = float(micro.get("volume") or market.get("volume") or 0.0)
    liquidity_metric = float(micro.get("liquidity") or market.get("liquidity") or market.get("open_interest") or 0.0)
    hours_to_close = _hours_to_close(market)
    if hours_to_close is None:
        hours_to_close = _hours_to_close(micro)

    liquidity_ok = bool(
        max(volume, liquidity_metric) >= MIN_LIQUIDITY
        and (spread_cents is None or spread_cents <= MAX_SPREAD_CENTS)
        and (hours_to_close is None or hours_to_close >= MIN_HOURS_TO_CLOSE)
    )

    side, kelly = None, 0.0
    if p_market is not None and edge_after_fee is not None:
        if edge_after_fee > 0:
            side = "yes"
            kelly = _kelly_fraction_from_edge(p_yes, min(0.999, p_market + fee_dollars))
        elif edge_after_fee < 0:
            side = "no"
            kelly = _kelly_fraction_from_edge(1.0 - p_yes, min(0.999, (1.0 - p_market) + fee_dollars))

    recommend = bool(
        p_market is not None
        and edge_after_fee is not None
        and abs(edge_after_fee) >= MIN_EDGE_AFTER_FEE
        and liquidity_ok
    )

    return {
        "ticker": market.get("ticker"),
        "question": question,
        "category": category,
        "p_yes": round(p_yes, 4),
        "p_market": p_market,
        "edge": edge,
        "fee_dollars": round(fee_dollars, 4) if p_market is not None else None,
        "edge_after_fee": edge_after_fee,
        "confidence": confidence,
        "side": side,
        "kelly_fraction": kelly,
        "liquidity_ok": liquidity_ok,
        "spread_cents": spread_cents,
        "hours_to_close": hours_to_close,
        "recommend": recommend,
        "data_sources_used": [s for s in dict.fromkeys(sources) if s],
        "rationale": prob.get("rationale"),
        "generated_at": _now(),
    }


if __name__ == "__main__":
    tomorrow = datetime.now(timezone.utc) + timedelta(days=1)
    examples = [
        {
            "ticker": "HIGHNY-EX-T85",
            "title": "Will the high temperature in New York City exceed 85F tomorrow?",
            "category": "Weather",
            "yes_ask": 42, "yes_bid": 38, "volume": 4200, "liquidity": 2600,
            "close_time": tomorrow.isoformat(),
        },
        {
            "ticker": "CPIYOY-EX",
            "title": "Will next month's CPI year-over-year print come in above 3.0%?",
            "category": "Economics",
            "yes_ask": 55, "yes_bid": 51, "volume": 8000, "liquidity": 5000,
            "close_time": (datetime.now(timezone.utc) + timedelta(days=10)).isoformat(),
        },
        {
            "ticker": "GENERIC-EX",
            "title": "Will a new bipartisan AI regulation bill pass the US Senate this month?",
            "category": "Politics",
            "yes_ask": 20, "yes_bid": 15, "volume": 300, "liquidity": 150,
            "close_time": (datetime.now(timezone.utc) + timedelta(days=5)).isoformat(),
        },
    ]
    for mkt in examples:
        print(f"=== {mkt['ticker']} ({mkt['category']}) ===")
        print(json.dumps(estimate(mkt), indent=2, default=str))
        print()
