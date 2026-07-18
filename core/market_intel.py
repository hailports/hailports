"""market_intel.py — $0, keyless, LOCAL-LLM-only history/trend/prediction enrichment.

This is the DEEP-READ half that sits on top of core/market_signals.py (which
does the base price/macro/structure/sentiment ingest). Everything here is
free, keyless, and stdlib-only — no pip installs beyond what core/ already
uses, no paid APIs, no API keys.

  technicals(symbol)          extended TA on Yahoo OHLCV: RSI14, MACD, ATR14,
                               Bollinger %B, 52w high/low proximity, volume
                               trend, multi-timeframe momentum (5/20/60/120d).
  crypto_fear_greed()         alternative.me Fear & Greed index ($0, no key).
  headlines(symbol, limit)    Yahoo Finance RSS titles for a symbol ($0, no key).
  macro_events()              a couple more free FRED CSV series (initial
                               jobless claims, consumer sentiment) beyond what
                               market_signals already pulls.
  event_probability(q, ctx)   the ONE place an LLM adds edge: a LOCAL Ollama
                               call that turns a Kalshi-style yes/no question
                               + headline context into a calibrated
                               probability. Local-only, hard-coded to
                               localhost — see the OLLAMA_URL note below.
  intel(symbols)               bundles all of the above for a symbol list.

$0 / LOCAL-ONLY LLM RULE (HARD): this stack has been bled by paid-LLM leaks
before (OpenRouter auto-top-up incidents). event_probability() and every
helper under it talk ONLY to a hard-coded http://localhost:11434 Ollama
endpoint — never core.llm_router's paid-escalation path, never
core.free_llm_pool, never OpenRouter, never any remote host. If Ollama isn't
running or the call fails for any reason, the function returns a neutral
fallback and never raises.

Every network call below is TTL-cached in data/trading/market_intel_cache.json
and FAILS OPEN: a blocked/slow source returns neutral/empty, never an
exception, so a trading loop can never stall on this module.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from core import BASE_DIR
except Exception:  # pragma: no cover - allow standalone import
    BASE_DIR = Path(os.path.expanduser("~/claude-stack"))

CACHE_PATH = BASE_DIR / "data" / "trading" / "market_intel_cache.json"
UA = {"User-Agent": "Mozilla/5.0 (research; paper-only)"}

# LOCAL ONLY. Hard-coded to localhost so this module can never be pointed at
# a remote/paid provider by config drift. Do not parameterize this from an
# env var or settings file — that's exactly the kind of seam that turned into
# the OpenRouter bleed. See core/local_client.py for the same convention.
OLLAMA_URL = "http://localhost:11434"

# extra $0 FRED series beyond what market_signals already pulls (cpi/unrate/fedfunds)
FRED_EXTRA_SERIES = {
    "initial_claims": "ICSA",      # weekly jobless claims — good event cadence
    "consumer_sentiment": "UMCSENT",  # U. Michigan sentiment, monthly
}


# ---------------------------------------------------------------------------
# low-level fetch + TTL cache (mirrors core/market_signals.py conventions)
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
# 1. extended technicals — Yahoo OHLCV (replicates market_signals' fetcher,
#    extended with high/low/volume for ATR + volume-trend)
# ---------------------------------------------------------------------------

def _yahoo_ohlcv(symbol: str, rng: str = "1y", interval: str = "1d") -> dict[str, Any] | None:
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
            closes_raw = quote.get("close") or []
            highs_raw = quote.get("high") or []
            lows_raw = quote.get("low") or []
            vol_raw = quote.get("volume") or []
            idx = [i for i, c in enumerate(closes_raw) if isinstance(c, (int, float))]
            if not idx:
                continue
            closes = [float(closes_raw[i]) for i in idx]
            highs = [
                float(highs_raw[i]) if i < len(highs_raw) and isinstance(highs_raw[i], (int, float)) else closes[j]
                for j, i in enumerate(idx)
            ]
            lows = [
                float(lows_raw[i]) if i < len(lows_raw) and isinstance(lows_raw[i], (int, float)) else closes[j]
                for j, i in enumerate(idx)
            ]
            volumes = [
                float(vol_raw[i]) if i < len(vol_raw) and isinstance(vol_raw[i], (int, float)) else 0.0
                for j, i in enumerate(idx)
            ]
            return {
                "price": float(meta.get("regularMarketPrice") or closes[-1]),
                "closes": closes[-520:],
                "highs": highs[-520:],
                "lows": lows[-520:],
                "volumes": volumes[-520:],
                "currency": meta.get("currency") or "USD",
                "source": "yahoo",
            }
        except Exception:
            continue
    return None


def _rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _ema_full(values: list[float], period: int) -> list[float | None]:
    """EMA series, same length as `values`, leading (period-1) entries None."""
    if len(values) < period:
        return [None] * len(values)
    k = 2.0 / (period + 1)
    out: list[float | None] = [None] * (period - 1)
    prev = sum(values[:period]) / period
    out.append(prev)
    for v in values[period:]:
        prev = v * k + prev * (1 - k)
        out.append(prev)
    return out


def _macd(closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> dict[str, Any] | None:
    if len(closes) < slow + signal:
        return None
    ema_fast = _ema_full(closes, fast)
    ema_slow = _ema_full(closes, slow)
    macd_vals = [f - s for f, s in zip(ema_fast, ema_slow) if f is not None and s is not None]
    if len(macd_vals) < signal:
        return None
    signal_series = _ema_full(macd_vals, signal)
    signal_val = next((v for v in reversed(signal_series) if v is not None), None)
    macd_val = macd_vals[-1]
    if macd_val is None or signal_val is None:
        return None
    hist = macd_val - signal_val
    return {"line": round(macd_val, 4), "signal": round(signal_val, 4), "hist": round(hist, 4)}


def _atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float | None:
    n = len(closes)
    if n < period + 1:
        return None
    trs = []
    for i in range(1, n):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        trs.append(tr)
    atr = sum(trs[:period]) / period
    for t in trs[period:]:
        atr = (atr * (period - 1) + t) / period
    return atr


def _bollinger(closes: list[float], period: int = 20, k: float = 2.0) -> dict[str, Any] | None:
    if len(closes) < period:
        return None
    window = closes[-period:]
    mid = sum(window) / period
    var = sum((c - mid) ** 2 for c in window) / period
    sd = math.sqrt(var)
    upper = mid + k * sd
    lower = mid - k * sd
    px = closes[-1]
    pct_b = (px - lower) / (upper - lower) if upper != lower else 0.5
    position = (
        "overbought" if pct_b > 1 else "oversold" if pct_b < 0
        else "upper_half" if pct_b > 0.5 else "lower_half"
    )
    return {"mid": round(mid, 4), "upper": round(upper, 4), "lower": round(lower, 4),
            "pct_b": round(pct_b, 4), "position": position}


def _week52(closes: list[float]) -> dict[str, Any]:
    window = closes[-252:] if len(closes) >= 252 else closes
    hi, lo, px = max(window), min(window), closes[-1]
    return {
        "high": round(hi, 4), "low": round(lo, 4),
        "pct_from_high": round((px / hi - 1) * 100, 2) if hi else None,
        "pct_from_low": round((px / lo - 1) * 100, 2) if lo else None,
    }


def _volume_trend(volumes: list[float]) -> dict[str, Any] | None:
    vols = [v for v in volumes if isinstance(v, (int, float))]
    if len(vols) < 20:
        return None
    avg5 = sum(vols[-5:]) / 5
    avg20 = sum(vols[-20:]) / 20
    ratio = avg5 / avg20 if avg20 else 1.0
    trend = "rising" if ratio > 1.15 else "falling" if ratio < 0.85 else "flat"
    return {"avg5": round(avg5, 0), "avg20": round(avg20, 0), "ratio": round(ratio, 3), "trend": trend}


def _momentum(closes: list[float]) -> dict[str, Any]:
    px = closes[-1]
    out: dict[str, Any] = {}
    for label, n in (("m5", 5), ("m20", 20), ("m60", 60), ("m120", 120)):
        out[label] = round((px / closes[-n - 1] - 1) * 100, 2) if len(closes) > n else None
    return out


def technicals(symbol: str, *, ttl_s: float = 900) -> dict[str, Any]:
    """Extended per-symbol technicals from free Yahoo OHLCV. Fails open."""
    try:
        cache = _load_cache()
        key = f"ohlcv:{symbol}"
        bar = _cache_get(cache, key, ttl_s)
        if bar is None:
            bar = _yahoo_ohlcv(symbol)
            if bar is not None:
                _cache_put(cache, key, bar)
                _save_cache(cache)
        if not bar or not bar.get("closes"):
            return {"symbol": symbol, "available": False, "source": None}

        closes = bar["closes"]
        highs = bar.get("highs") or closes
        lows = bar.get("lows") or closes
        volumes = bar.get("volumes") or []

        out: dict[str, Any] = {
            "symbol": symbol, "available": True, "source": bar.get("source", "yahoo"),
            "n": len(closes), "price": round(closes[-1], 4),
        }
        try:
            r = _rsi(closes)
            out["rsi14"] = round(r, 2) if r is not None else None
        except Exception:
            out["rsi14"] = None
        try:
            out["macd"] = _macd(closes)
        except Exception:
            out["macd"] = None
        try:
            a = _atr(highs, lows, closes)
            out["atr14"] = round(a, 4) if a is not None else None
            out["atr14_pct"] = round(a / closes[-1] * 100, 3) if a is not None and closes[-1] else None
        except Exception:
            out["atr14"], out["atr14_pct"] = None, None
        try:
            out["bollinger"] = _bollinger(closes)
        except Exception:
            out["bollinger"] = None
        try:
            out["week52"] = _week52(closes)
        except Exception:
            out["week52"] = None
        try:
            out["volume_trend"] = _volume_trend(volumes)
        except Exception:
            out["volume_trend"] = None
        try:
            out["momentum"] = _momentum(closes)
        except Exception:
            out["momentum"] = None
        return out
    except Exception as exc:
        return {"symbol": symbol, "available": False, "source": None, "error": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------------------
# 2. crypto fear & greed — alternative.me ($0, no key)
# ---------------------------------------------------------------------------

def crypto_fear_greed(*, ttl_s: float = 3600) -> dict[str, Any]:
    neutral = {"value": 50, "classification": "neutral", "source": None, "available": False}
    try:
        cache = _load_cache()
        hit = _cache_get(cache, "fng", ttl_s)
        if hit is not None:
            return hit
        out = neutral
        d = _http_json("https://api.alternative.me/fng/?limit=1&format=json", timeout=8)
        row = ((d or {}).get("data") or [None])[0]
        if row:
            out = {
                "value": int(row.get("value")),
                "classification": str(row.get("value_classification") or "").lower(),
                "timestamp": row.get("timestamp"),
                "source": "alternative.me",
                "available": True,
            }
        _cache_put(cache, "fng", out)
        _save_cache(cache)
        return out
    except Exception:
        return neutral


# ---------------------------------------------------------------------------
# 3. free news/RSS headlines — Yahoo Finance RSS ($0, no key)
# ---------------------------------------------------------------------------

_CDATA_RE = re.compile(r"^<!\[CDATA\[(.*?)\]\]>$", re.S)


def headlines(symbol: str, limit: int = 10, *, ttl_s: float = 1800) -> list[str]:
    try:
        cache = _load_cache()
        key = f"news:{symbol}"
        hit = _cache_get(cache, key, ttl_s)
        if hit is not None:
            return list(hit)[:limit]
        out: list[str] = []
        enc = urllib.parse.quote(symbol, safe="")
        txt = _http_text(
            f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={enc}&region=US&lang=en-US",
            timeout=8,
        )
        titles = re.findall(r"<title>(.*?)</title>", txt, flags=re.S)
        for t in titles[1:]:  # first <title> is the channel title, not a headline
            clean = t.strip()
            m = _CDATA_RE.match(clean)
            if m:
                clean = m.group(1).strip()
            clean = clean.replace("&amp;", "&").replace("&#39;", "'").replace("&quot;", '"')
            if clean:
                out.append(clean)
            if len(out) >= max(limit, 25):
                break
        _cache_put(cache, key, out)
        _save_cache(cache)
        return out[:limit]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# 4. macro/event awareness — a couple more free FRED CSV series ($0, no key)
# ---------------------------------------------------------------------------

def _fred_csv(series_id: str, n: int = 30, timeout: float = 8) -> list[tuple[str, float]]:
    start = (datetime.now(timezone.utc).date().replace(year=datetime.now(timezone.utc).year - 3)).isoformat()
    txt = _http_text(
        f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}&cosd={start}",
        timeout=timeout,
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


def macro_events(*, ttl_s: float = 6 * 3600) -> dict[str, Any]:
    try:
        cache = _load_cache()
        hit = _cache_get(cache, "macro_events", ttl_s)
        if hit is not None:
            return hit
        out: dict[str, Any] = {}
        for name, sid in FRED_EXTRA_SERIES.items():
            try:
                rows = _fred_csv(sid, n=8)
                if rows:
                    out[name] = rows[-1][1]
                    out[f"{name}_asof"] = rows[-1][0]
                    if len(rows) > 1:
                        out[f"{name}_chg"] = round(out[name] - rows[-2][1], 3)
            except Exception:
                continue
        out["source"] = "fred_csv" if any(k in out for k in FRED_EXTRA_SERIES) else None
        out["available"] = bool(out.get("source"))
        _cache_put(cache, "macro_events", out)
        _save_cache(cache)
        return out
    except Exception:
        return {"source": None, "available": False}


# ---------------------------------------------------------------------------
# 5. event_probability — the ONE LLM seam, LOCAL Ollama ONLY, $0
# ---------------------------------------------------------------------------

def _local_model_name() -> str:
    try:
        from core.constants import LOCAL_MODEL
        from core.local_model_registry import get_active_local_model
        return get_active_local_model(LOCAL_MODEL)
    except Exception:
        return "qwen2.5:7b"  # matches core/constants.py DEFAULT_LOCAL_MODEL


def _ollama_alive(timeout: float = 2.0) -> bool:
    try:
        req = urllib.request.Request(f"{OLLAMA_URL}/api/version", headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def _ollama_generate_local(prompt: str, *, system: str | None = None, max_tokens: int = 220, timeout: float = 30) -> str | None:
    """LOCAL-ONLY Ollama call — hard-coded to localhost. Never touches a paid API.

    Returns None (never raises) on any failure: Ollama not running, network
    error, timeout, bad response — all fail open.
    """
    if not _ollama_alive():
        return None
    body = {
        "model": _local_model_name(),
        "prompt": prompt,
        "stream": False,
        "options": {"num_predict": max_tokens, "temperature": 0.2},
    }
    if system:
        body["system"] = system
    try:
        req = urllib.request.Request(
            f"{OLLAMA_URL}/api/generate",
            data=json.dumps(body).encode("utf-8"),
            headers={**UA, "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        text = str(data.get("response") or "").strip()
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
        return text or None
    except Exception:
        return None


def event_probability(question: str, context: str = "", *, ttl_s: float = 1800) -> dict[str, Any]:
    """Calibrated yes/no probability for a Kalshi-style event question.

    LOCAL Ollama only ($0). If the question is empty, Ollama is unreachable,
    or the response can't be parsed, returns a neutral 50/50 fallback —
    never raises, never calls a remote/paid provider.
    """
    neutral = {"p": 0.5, "confidence": "low", "rationale": "local LLM unavailable — neutral prior", "source": "neutral_fallback"}
    q = str(question or "").strip()
    if not q:
        return neutral
    try:
        cache = _load_cache()
        key = "evtprob:" + hashlib.sha1((q + "||" + str(context or "")).encode("utf-8")).hexdigest()
        hit = _cache_get(cache, key, ttl_s)
        if hit is not None:
            return hit

        prompt = (
            "You are a calibrated forecaster estimating the probability of a yes/no event contract.\n"
            f"Question: {q}\n"
            f"Context (recent headlines/data, may be empty): {str(context or '')[:2000]}\n\n"
            "Reply with ONLY a JSON object, no prose, no markdown fences: "
            '{"p": <float 0.0-1.0, probability the answer is YES>, '
            '"confidence": "low"|"medium"|"high", "rationale": "<one short sentence>"}'
        )
        raw = _ollama_generate_local(prompt, system="You output strict JSON only, nothing else.", max_tokens=200, timeout=30)
        result = neutral
        if raw:
            m = re.search(r"\{.*\}", raw, flags=re.S)
            if m:
                try:
                    parsed = json.loads(m.group(0))
                    p = max(0.0, min(1.0, float(parsed.get("p"))))
                    conf = str(parsed.get("confidence") or "low").lower()
                    if conf not in {"low", "medium", "high"}:
                        conf = "low"
                    rationale = str(parsed.get("rationale") or "")[:400]
                    result = {
                        "p": round(p, 3), "confidence": conf, "rationale": rationale,
                        "source": f"local_ollama:{_local_model_name()}",
                    }
                except Exception:
                    result = neutral
        _cache_put(cache, key, result)
        _save_cache(cache)
        return result
    except Exception:
        return neutral


# ---------------------------------------------------------------------------
# aggregate
# ---------------------------------------------------------------------------

def intel(symbols: list[str]) -> dict[str, Any]:
    """Bundle technicals + fear/greed + headlines + macro events for a symbol list. $0, fails open."""
    syms = list(symbols or [])
    out: dict[str, Any] = {"ts": _now()}
    try:
        out["technicals"] = {s: technicals(s) for s in syms}
    except Exception:
        out["technicals"] = {}
    try:
        out["crypto_fear_greed"] = crypto_fear_greed()
    except Exception:
        out["crypto_fear_greed"] = {"available": False}
    try:
        out["headlines"] = {s: headlines(s, limit=5) for s in syms}
    except Exception:
        out["headlines"] = {}
    try:
        out["macro_events"] = macro_events()
    except Exception:
        out["macro_events"] = {"available": False}
    return out


if __name__ == "__main__":
    syms = ["BTC-USD", "SPY"]
    data = intel(syms)
    print(json.dumps(data, indent=2, default=str))

    heads = (data.get("headlines") or {}).get("SPY") or []
    example_q = "Will the S&P 500 (SPY) close higher than its current level by the end of this week?"
    prob = event_probability(example_q, context="; ".join(heads[:5]))
    print(json.dumps({"event_question": example_q, "event_probability": prob}, indent=2, default=str))
