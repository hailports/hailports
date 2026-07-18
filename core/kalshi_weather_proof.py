"""kalshi_weather_proof.py — honestly PROVE (or disprove) the Kalshi weather-market edge.

Weather is the one Kalshi lane that plausibly has better odds than equity momentum:
NOAA/NWS publish free, keyless, well-verified next-day temperature forecasts, and
core.kalshi_event_data.estimate() already turns those into a p_yes for weather
contracts. But "plausibly better" was never actually validated. This module is the
validator, in two honest, separate parts:

  1. base_edge_estimate() — a STATISTICAL, immediate estimate: how much more accurate
     is a next-day NWS temperature forecast than a naive baseline (yesterday's temp /
     climatology), translated into an approximate probability edge on a threshold
     contract ("high > 75F"). This is available the instant you run the module — no
     waiting for markets to settle. It rests on a stated assumption chain (published
     NWS forecast-verification MAE figures + a Normal-error approximation) and is
     explicit that this is edge of the FORECAST over a NAIVE BASELINE, NOT proof of a
     tradable edge over the KALSHI MARKET PRICE — if the market already prices in the
     same public NWS forecast (likely, since it's one free HTTP call away for anyone),
     the real tradable edge could be much smaller than the forecast-accuracy edge.

  2. log_prediction() / settle_and_score() — a FORWARD calibration tracker: every time
     core.kalshi_event_data.estimate() is run on a weather market, log_prediction()
     records {ticker, predicted p_yes, market yes_price, close_time, resolution=None}
     to data/runtime/kalshi_weather_predictions.jsonl. settle_and_score() later queries
     Kalshi's public per-market endpoint for markets that have since resolved, fills in
     the realized outcome, and computes a running Brier score, a predicted-vs-realized
     calibration table, and realized edge vs the market price (closing-line value) —
     i.e. would trading estimate()'s side, sized at the market price, actually have
     made money. THIS is the only honest proof: it accrues over days/weeks as real
     Kalshi weather markets actually settle. Zero predictions logged means zero proof
     yet, and this module says so plainly rather than faking a number.

$0, keyless, stdlib + core only. No LLM calls in this module at all (estimate()'s one
local-Ollama seam lives in core.market_intel / core.kalshi_event_data, not here).
Every public function fails open: a dead network call, a malformed row, or a missing
file degrades to a neutral/empty result and is reported as such — nothing here raises
out to a caller or a cron job.

CLI: `python3 -m core.kalshi_weather_proof` prints the base-edge estimate (with the
full assumption chain and a small live NOAA sanity-check) plus the current state of
the forward calibration tracker.
"""
from __future__ import annotations

import json
import math
import os
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from core import BASE_DIR
except Exception:  # pragma: no cover - allow standalone import
    BASE_DIR = Path(os.path.expanduser("~/claude-stack"))

from core.kalshi_bot import PROD_BASE, _get_json as _kalshi_get_json
from core.kalshi_event_data import (
    CITIES as _WEATHER_CITIES,
    NOAA_UA,
    _http_json as _noaa_http_json,
    _noaa_forecast_periods,
)

PREDICTIONS_PATH = BASE_DIR / "data" / "runtime" / "kalshi_weather_predictions.jsonl"
SETTLE_CACHE_PATH = BASE_DIR / "data" / "trading" / "kalshi_weather_proof_cache.json"

MIN_RESOLVED_FOR_PROOF = 20  # forward-tracker sample size before we call the edge "proven"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# PART 1 — base-edge estimate (statistical, immediate)
# ---------------------------------------------------------------------------

# Published NWS/NDFD forecast-verification figures put next-day (day-1) max/min
# temperature forecast mean absolute error at roughly 2-3F for CONUS stations
# (NWS Techniques Development Laboratory / NDFD verification reporting; the exact
# figure moves by season/region, this is a defensible central estimate, not a
# live-measured number). Persistence ("tomorrow = today") day-ahead MAE runs
# meaningfully higher in the forecast-skill literature — call it ~1.7-2x the NWS
# figure. A 30-year-normal climatology baseline is worse again, especially in
# fast-swinging shoulder-season months.
NWS_DAY1_MAE_F = 2.6
PERSISTENCE_MAE_F = 5.3
CLIMATOLOGY_MAE_F = 7.8

_ASSUMPTIONS = [
    f"NWS day-1 max/min temp forecast MAE assumed {NWS_DAY1_MAE_F}F — published NWS/NDFD "
    "forecast-verification reporting commonly cites ~2-3F for CONUS day-1 forecasts; "
    "not live-measured here, this is a literature-based central estimate.",
    f"Persistence (\"tomorrow = today\") day-ahead MAE assumed {PERSISTENCE_MAE_F}F — "
    "forecast-skill literature generally puts persistence error at roughly 1.7-2x the "
    "day-1 NWS forecast error.",
    f"30-year-normal climatology baseline MAE assumed {CLIMATOLOGY_MAE_F}F — worse than "
    "persistence on average, especially in shoulder-season months with fast day-to-day swings.",
    "Forecast/baseline errors are approximated as zero-mean Normal; MAE -> implied std via "
    "std = MAE * sqrt(pi/2) (the exact relation for a Normal distribution's mean absolute "
    "deviation).",
    "This is the edge of the FORECAST over a NAIVE BASELINE (how much more accurate NWS is "
    "than 'yesterday's temp' or climatology) — it is NOT proof of a tradable edge over the "
    "Kalshi MARKET PRICE. If the market already prices in the same free, public NWS forecast "
    "(likely — it's one HTTP call away for any trader), realized tradable edge could be far "
    "smaller than this forecast-accuracy edge. Part 2 (the forward tracker) is what actually "
    "measures edge vs the market price on real settled contracts.",
    "The live snapshot below is a small-n (few cities, one moment in time) sanity check that "
    "the assumption chain is in the right ballpark — it is illustrative, not a statistically "
    "powered backtest.",
    "The headline expected-edge number is the SHAKIEST link: it comes from treating the "
    "forecast-vs-naive divergence as Normal with variance = naive_variance - forecast_variance "
    "(a simplifying independence assumption), then scoring edge at a threshold placed exactly "
    "at the naive baseline's mean — the scenario construction most favorable to the forecast. "
    "Real forecast/persistence errors are correlated (both worse on volatile days) and not "
    "perfectly Gaussian, so treat the headline pp figure as a plausible upper-bound-ish "
    "back-of-envelope, not a number to size a bet on. This is exactly why Part 2 exists.",
]


def _mae_to_std(mae_f: float) -> float:
    """MAE -> implied std of a zero-mean Normal error distribution.

    For X ~ N(0, std), E[|X|] = std * sqrt(2/pi), so std = MAE * sqrt(pi/2).
    """
    return mae_f * math.sqrt(math.pi / 2)


def _norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _implied_p_yes(mean_f: float, threshold_f: float, mae_f: float, comparator: str = ">") -> float:
    """P(actual > threshold) given actual ~ N(mean_f, std implied by mae_f).

    comparator="<" flips it to P(actual < threshold) for low-temp contracts.
    """
    std = _mae_to_std(mae_f)
    if std <= 0:
        return 0.5
    z = (threshold_f - mean_f) / std
    p_above = 1.0 - _norm_cdf(z)
    return p_above if comparator == ">" else (1.0 - p_above)


def _normal_pdf(x: float, std: float) -> float:
    if std <= 0:
        return 0.0
    return math.exp(-(x * x) / (2 * std * std)) / (std * math.sqrt(2 * math.pi))


def _expected_abs_edge_pp(std_gap_f: float, mae_forecast_f: float, *, n: int = 401, span: float = 4.0) -> float:
    """E[|forecast_p_yes(D) - 0.5|] * 100, where D (how far the forecast sits from
    the naive baseline) ~ N(0, std_gap_f). Deterministic numeric integration
    (trapezoid-weighted grid), stdlib only.
    """
    if std_gap_f <= 0:
        return 0.0
    lo, hi = -span * std_gap_f, span * std_gap_f
    step = (hi - lo) / (n - 1)
    total = 0.0
    total_weight = 0.0
    for i in range(n):
        d = lo + i * step
        w = _normal_pdf(d, std_gap_f)
        p = _implied_p_yes(d, 0.0, mae_forecast_f)
        total += abs(p - 0.5) * w
        total_weight += w
    return round((total / total_weight) * 100, 2) if total_weight else 0.0


def _recent_observed_high_f(lat: float, lon: float, *, timeout: float = 6) -> float | None:
    """Max observed temp (F) over the last ~48h at the nearest NWS station — a live
    persistence-baseline proxy. Fails open (returns None) on any error."""
    try:
        points = _noaa_http_json(f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}", headers=NOAA_UA, timeout=timeout)
        stations_url = ((points or {}).get("properties") or {}).get("observationStations")
        if not stations_url:
            return None
        stations = _noaa_http_json(stations_url, headers=NOAA_UA, timeout=timeout)
        features = (stations or {}).get("features") or []
        if not features:
            return None
        station_id = (features[0].get("properties") or {}).get("stationIdentifier")
        if not station_id:
            return None
        obs = _noaa_http_json(
            f"https://api.weather.gov/stations/{station_id}/observations?limit=48",
            headers=NOAA_UA, timeout=timeout,
        )
        best_c = None
        for feat in (obs or {}).get("features") or []:
            val = ((feat.get("properties") or {}).get("temperature") or {}).get("value")
            if val is None:
                continue
            if best_c is None or val > best_c:
                best_c = val
        if best_c is None:
            return None
        return round(best_c * 9.0 / 5.0 + 32.0, 1)
    except Exception:
        return None


def _live_divergence_snapshot(*, max_cities: int = 4) -> dict[str, Any]:
    """Best-effort real-time check: for a few cities, how far does today's NOAA
    day-1 forecast high sit from the max observed temp over the last ~48h (a live
    persistence proxy)? Small-n illustration, not a backtest. Fails open per-city
    and overall — a NOAA outage just yields an empty rows list.
    """
    rows: list[dict[str, Any]] = []
    try:
        for aliases, lat, lon in _WEATHER_CITIES[:max_cities]:
            city = aliases[0]
            try:
                periods = _noaa_forecast_periods(lat, lon)
                if not periods:
                    continue
                fperiod = next((p for p in periods if p.get("isDaytime")), periods[0])
                forecast_f = fperiod.get("temperature")
                unit = str(fperiod.get("temperatureUnit") or "F").upper()
                if forecast_f is None or unit != "F":
                    continue
                persistence_f = _recent_observed_high_f(lat, lon)
                if persistence_f is None:
                    continue
                divergence = round(float(forecast_f) - float(persistence_f), 1)
                edge_p = _implied_p_yes(divergence, 0.0, NWS_DAY1_MAE_F)
                rows.append({
                    "city": city,
                    "forecast_high_f": forecast_f,
                    "persistence_high_f": persistence_f,
                    "divergence_f": divergence,
                    "implied_edge_pp_if_threshold_at_persistence": round((edge_p - 0.5) * 100, 1),
                })
            except Exception:
                continue
    except Exception:
        pass
    avg_abs_edge = (
        round(sum(abs(r["implied_edge_pp_if_threshold_at_persistence"]) for r in rows) / len(rows), 1)
        if rows else None
    )
    return {
        "cities_checked": len(rows),
        "rows": rows,
        "avg_abs_edge_pp": avg_abs_edge,
        "note": "small-n live illustration only (NOAA forecast vs ~48h observed max), not a statistically powered backtest",
    }


def base_edge_estimate() -> dict[str, Any]:
    """The honest, immediate answer to 'does the forecast-accuracy edge plausibly
    exist': a scenario table + one headline number, fully derived from a stated
    assumption chain, plus a small live NOAA sanity check. Never raises.
    """
    try:
        std_forecast = _mae_to_std(NWS_DAY1_MAE_F)
        std_persistence = _mae_to_std(PERSISTENCE_MAE_F)
        std_climatology = _mae_to_std(CLIMATOLOGY_MAE_F)

        scenarios = []
        for divergence in (0.0, 2.0, 4.0, 6.0, 8.0, 12.0):
            forecast_p = _implied_p_yes(divergence, 0.0, NWS_DAY1_MAE_F)
            scenarios.append({
                "forecast_minus_naive_f": divergence,
                "naive_p_yes": 0.5,
                "forecast_p_yes": round(forecast_p, 3),
                "edge_pp": round((forecast_p - 0.5) * 100, 1),
            })

        gap_vs_persistence = math.sqrt(max(std_persistence ** 2 - std_forecast ** 2, 0.0))
        gap_vs_climatology = math.sqrt(max(std_climatology ** 2 - std_forecast ** 2, 0.0))
        expected_edge_vs_persistence_pp = _expected_abs_edge_pp(gap_vs_persistence, NWS_DAY1_MAE_F)
        expected_edge_vs_climatology_pp = _expected_abs_edge_pp(gap_vs_climatology, NWS_DAY1_MAE_F)

        live = _live_divergence_snapshot()

        return {
            "assumptions": _ASSUMPTIONS,
            "nws_day1_mae_f": NWS_DAY1_MAE_F,
            "persistence_mae_f": PERSISTENCE_MAE_F,
            "climatology_mae_f": CLIMATOLOGY_MAE_F,
            "implied_std_forecast_f": round(std_forecast, 2),
            "implied_std_persistence_f": round(std_persistence, 2),
            "implied_std_climatology_f": round(std_climatology, 2),
            "scenarios_vs_persistence": scenarios,
            "expected_edge_vs_persistence_pp": expected_edge_vs_persistence_pp,
            "expected_edge_vs_climatology_pp": expected_edge_vs_climatology_pp,
            "headline": (
                f"~{expected_edge_vs_persistence_pp}pp back-of-envelope (upper-bound-ish, see caveats) "
                f"probability edge of the NWS day-1 forecast over a persistence (\"yesterday's temp\") "
                f"baseline; ~{expected_edge_vs_climatology_pp}pp over a climatology baseline. This is "
                "forecast accuracy edge over a naive baseline, NOT proven edge over the Kalshi market "
                "price — see assumptions/caveats and Part 2's forward tracker."
            ),
            "live_snapshot": live,
            "generated_at": _now(),
        }
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}", "generated_at": _now()}


# ---------------------------------------------------------------------------
# PART 2 — forward calibration tracker
# ---------------------------------------------------------------------------

def _load_predictions() -> list[dict[str, Any]]:
    if not PREDICTIONS_PATH.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in PREDICTIONS_PATH.read_text(errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows


def _rewrite_predictions(rows: list[dict[str, Any]]) -> None:
    PREDICTIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PREDICTIONS_PATH.with_suffix(".jsonl.tmp")
    text = "\n".join(json.dumps(r, default=str) for r in rows)
    tmp.write_text(text + ("\n" if rows else ""))
    tmp.replace(PREDICTIONS_PATH)


def log_prediction(market: dict[str, Any], estimate: dict[str, Any]) -> dict[str, Any]:
    """Record one forward prediction for a weather market: {ticker, predicted p_yes,
    market yes_price, close_time, resolution=None} appended/upserted into
    data/runtime/kalshi_weather_predictions.jsonl.

    `market` is a Kalshi-shaped market dict; `estimate` is the output of
    core.kalshi_event_data.estimate(market). Upserts (by ticker, while
    resolution is still None) instead of appending duplicate snapshots of the
    same still-open market, so calibration stats later score one prediction
    per market rather than over-weighting frequently-re-estimated tickers.
    Fails open: never raises, returns {"logged": False, "reason": ...} on any
    problem instead.
    """
    try:
        market = market or {}
        estimate = estimate or {}
        category = str(estimate.get("category") or market.get("category") or "").lower()
        if category and "weather" not in category:
            return {"logged": False, "reason": f"not a weather market (category={category!r})"}

        ticker = market.get("ticker") or estimate.get("ticker")
        if not ticker:
            return {"logged": False, "reason": "no ticker on market/estimate"}
        ticker = str(ticker)

        p_yes = estimate.get("p_yes")
        if p_yes is None:
            return {"logged": False, "reason": "estimate has no p_yes"}

        p_market = estimate.get("p_market")
        if p_market is None:
            yes_ask = market.get("yes_ask")
            if yes_ask is not None:
                try:
                    p_market = round(float(yes_ask) / 100.0, 4)
                except Exception:
                    p_market = None

        close_time = market.get("close_time") or market.get("expiration_time") or market.get("expected_expiration_time")

        rows = _load_predictions()
        existing_idx = next(
            (i for i, r in enumerate(rows) if r.get("ticker") == ticker and r.get("resolution") is None),
            None,
        )

        row = {
            "ts": _now(),
            "ticker": ticker,
            "title": market.get("title") or estimate.get("question"),
            "category": estimate.get("category") or market.get("category"),
            "predicted_p_yes": float(p_yes),
            "market_yes_price": p_market,
            "close_time": close_time,
            "confidence": estimate.get("confidence"),
            "edge_after_fee_at_log": estimate.get("edge_after_fee"),
            "side_at_log": estimate.get("side"),
            "data_sources_used": estimate.get("data_sources_used"),
            "resolution": None,
            "resolved_at": None,
        }

        if existing_idx is not None:
            row["first_logged_at"] = rows[existing_idx].get("first_logged_at") or rows[existing_idx].get("ts")
            rows[existing_idx] = row
            updated = True
        else:
            row["first_logged_at"] = row["ts"]
            rows.append(row)
            updated = False

        _rewrite_predictions(rows)
        return {"logged": True, "updated_existing": updated, "row": row}
    except Exception as exc:
        return {"logged": False, "reason": f"{type(exc).__name__}: {exc}"}


def _load_settle_cache() -> dict[str, Any]:
    try:
        if SETTLE_CACHE_PATH.exists():
            return json.loads(SETTLE_CACHE_PATH.read_text())
    except Exception:
        pass
    return {}


def _save_settle_cache(cache: dict[str, Any]) -> None:
    try:
        SETTLE_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = SETTLE_CACHE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cache, default=str))
        tmp.replace(SETTLE_CACHE_PATH)
    except Exception:
        pass


def _settle_cache_get(cache: dict[str, Any], key: str, ttl_s: float) -> Any:
    import time
    row = cache.get(key)
    if not isinstance(row, dict):
        return None
    if (time.time() - float(row.get("_t", 0))) > ttl_s:
        return None
    return row.get("v")


def _settle_cache_put(cache: dict[str, Any], key: str, value: Any) -> None:
    import time
    cache[key] = {"_t": time.time(), "v": value}


def _kalshi_market_status_result(ticker: str, *, ttl_s: float = 1800) -> tuple[str | None, str | None]:
    """(status, result) for a Kalshi ticker via the public GET /markets/{ticker}
    endpoint (no auth needed for read-only market data). Fails open -> (None, None).
    """
    try:
        cache = _load_settle_cache()
        key = f"result:{ticker}"
        hit = _settle_cache_get(cache, key, ttl_s)
        if hit is not None:
            return tuple(hit)  # type: ignore[return-value]
        data = _kalshi_get_json(f"{PROD_BASE}/markets/{urllib.parse.quote(ticker)}", timeout=8)
        m = data.get("market") if isinstance(data, dict) else None
        if not isinstance(m, dict):
            m = data if isinstance(data, dict) else {}
        status = str(m.get("status") or "").strip().lower()
        result = str(m.get("result") or "").strip().lower()
        _settle_cache_put(cache, key, [status, result])
        _save_settle_cache(cache)
        return status, result
    except Exception:
        return None, None


def settle_and_score(*, ttl_s: float = 1800) -> dict[str, Any]:
    """Query Kalshi for any logged-but-unresolved market that has since settled,
    fill in its realized outcome, then score the whole resolved set: running
    Brier score (ours vs the market's own price as a naive predictor),
    a predicted-vs-realized calibration table, average edge claimed at log time,
    and average realized edge per contract (closing-line value) — the actual
    proof, or disproof, of the estimate() pipeline on real settled weather
    markets. Accrues forward: with 0 resolved predictions this honestly reports
    "no proof yet" rather than fabricating a number. Never raises.
    """
    try:
        rows = _load_predictions()
        base = {
            "predictions_path": str(PREDICTIONS_PATH),
            "n_logged": len(rows),
        }
        if not rows:
            base.update({
                "n_resolved": 0, "n_pending": 0, "resolved_this_run": 0,
                "brier_score": None, "market_brier_score": None, "calibration_table": [],
                "avg_claimed_edge_pp": None, "avg_realized_edge_per_contract": None,
                "min_resolved_for_proof": MIN_RESOLVED_FOR_PROOF,
                "proven": False,
                "reason": "no predictions logged yet — call log_prediction() on weather-market estimate() "
                          "output, then re-run settle_and_score() after markets close",
                "generated_at": _now(),
            })
            return base

        resolved_now = 0
        seen: dict[str, tuple[str | None, str | None]] = {}
        for row in rows:
            if row.get("resolution") in {"yes", "no"}:
                continue
            ticker = row.get("ticker")
            if not ticker:
                continue
            if ticker not in seen:
                seen[ticker] = _kalshi_market_status_result(ticker, ttl_s=ttl_s)
            status, result = seen[ticker]
            if status in {"finalized", "settled"} and result in {"yes", "no"}:
                row["resolution"] = result
                row["resolved_at"] = _now()
                resolved_now += 1

        if resolved_now:
            _rewrite_predictions(rows)

        resolved_rows = [
            r for r in rows
            if r.get("resolution") in {"yes", "no"} and r.get("predicted_p_yes") is not None
        ]
        n_resolved = len(resolved_rows)
        n_pending = len(rows) - n_resolved
        base.update({"n_resolved": n_resolved, "n_pending": n_pending, "resolved_this_run": resolved_now})

        if n_resolved == 0:
            base.update({
                "brier_score": None, "market_brier_score": None, "calibration_table": [],
                "avg_claimed_edge_pp": None, "avg_realized_edge_per_contract": None,
                "min_resolved_for_proof": MIN_RESOLVED_FOR_PROOF,
                "proven": False,
                "reason": f"0 of {len(rows)} logged predictions have settled yet — check back after their close_time",
                "generated_at": _now(),
            })
            return base

        def _won(r: dict[str, Any]) -> float:
            return 1.0 if r["resolution"] == "yes" else 0.0

        brier = sum((float(r["predicted_p_yes"]) - _won(r)) ** 2 for r in resolved_rows) / n_resolved

        market_rows = [r for r in resolved_rows if r.get("market_yes_price") is not None]
        market_brier = (
            sum((float(r["market_yes_price"]) - _won(r)) ** 2 for r in market_rows) / len(market_rows)
            if market_rows else None
        )

        bands = [(0.0, 0.3), (0.3, 0.45), (0.45, 0.55), (0.55, 0.7), (0.7, 1.01)]
        table: list[dict[str, Any]] = []
        for lo, hi in bands:
            bucket = [r for r in resolved_rows if lo <= float(r["predicted_p_yes"]) < hi]
            if not bucket:
                continue
            table.append({
                "band": f"{lo:.2f}-{hi:.2f}",
                "n": len(bucket),
                "predicted_avg": round(sum(float(r["predicted_p_yes"]) for r in bucket) / len(bucket), 3),
                "realized_rate": round(sum(_won(r) for r in bucket) / len(bucket), 3),
            })

        claimed_edges = [float(r["predicted_p_yes"]) - float(r["market_yes_price"]) for r in market_rows]
        avg_claimed_edge_pp = round((sum(claimed_edges) / len(claimed_edges)) * 100, 2) if claimed_edges else None

        # realized edge / closing-line value: would trading estimate()'s side at the
        # logged market price actually have made money, per contract?
        realized_pnls = []
        for r in market_rows:
            p_yes = float(r["predicted_p_yes"])
            p_mkt = float(r["market_yes_price"])
            won = _won(r)
            side = "yes" if p_yes >= p_mkt else "no"
            cost = p_mkt if side == "yes" else (1.0 - p_mkt)
            payoff = 1.0 if ((side == "yes" and won == 1.0) or (side == "no" and won == 0.0)) else 0.0
            realized_pnls.append(payoff - cost)
        avg_realized_edge_per_contract = round(sum(realized_pnls) / len(realized_pnls), 4) if realized_pnls else None

        proven = bool(
            n_resolved >= MIN_RESOLVED_FOR_PROOF
            and avg_realized_edge_per_contract is not None
            and avg_realized_edge_per_contract > 0
            and (market_brier is None or brier < market_brier)
        )
        reason = (
            "clears the forward-proof bar: enough settled samples, positive realized "
            "edge per contract, and better-calibrated (lower Brier) than the market price itself"
            if proven else
            f"not proven yet — need >= {MIN_RESOLVED_FOR_PROOF} resolved predictions "
            f"(have {n_resolved}), positive avg_realized_edge_per_contract, and brier < market_brier"
        )

        base.update({
            "brier_score": round(brier, 4),
            "market_brier_score": round(market_brier, 4) if market_brier is not None else None,
            "calibration_table": table,
            "avg_claimed_edge_pp": avg_claimed_edge_pp,
            "avg_realized_edge_per_contract": avg_realized_edge_per_contract,
            "min_resolved_for_proof": MIN_RESOLVED_FOR_PROOF,
            "proven": proven,
            "reason": reason,
            "generated_at": _now(),
        })
        return base
    except Exception as exc:
        return {
            "predictions_path": str(PREDICTIONS_PATH), "n_logged": 0, "n_resolved": 0,
            "proven": False, "error": f"{type(exc).__name__}: {exc}", "generated_at": _now(),
        }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_report() -> None:
    base = base_edge_estimate()
    print("=" * 78)
    print("KALSHI WEATHER EDGE — PART 1: base-edge estimate (statistical, immediate)")
    print("=" * 78)
    if base.get("error"):
        print(f"ERROR: {base['error']}")
    else:
        print(base["headline"])
        print()
        print(f"NWS day-1 MAE assumed:    {base['nws_day1_mae_f']}F -> implied std {base['implied_std_forecast_f']}F")
        print(f"Persistence MAE assumed:  {base['persistence_mae_f']}F -> implied std {base['implied_std_persistence_f']}F")
        print(f"Climatology MAE assumed:  {base['climatology_mae_f']}F -> implied std {base['implied_std_climatology_f']}F")
        print()
        print("Scenario table (naive p_yes fixed at 0.5 by construction; edge_pp = forecast_p_yes - 0.5):")
        for s in base["scenarios_vs_persistence"]:
            print(f"  forecast-naive divergence={s['forecast_minus_naive_f']:>5.1f}F   "
                  f"forecast_p_yes={s['forecast_p_yes']:<6} edge={s['edge_pp']:+.1f}pp")
        print()
        live = base.get("live_snapshot") or {}
        print(f"Live NOAA sanity-check ({live.get('cities_checked', 0)} cities, right now):")
        for r in live.get("rows", []):
            print(f"  {r['city']:<16} forecast={r['forecast_high_f']:>5}F  persistence(~48h max)={r['persistence_high_f']:>5}F  "
                  f"divergence={r['divergence_f']:+.1f}F  implied_edge={r['implied_edge_pp_if_threshold_at_persistence']:+.1f}pp")
        if live.get("avg_abs_edge_pp") is not None:
            print(f"  avg |implied edge| across checked cities: {live['avg_abs_edge_pp']}pp")
        elif not live.get("rows"):
            print("  (no cities resolved live right now — NOAA unreachable or no daytime period matched; base estimate above stands on its own)")
        print()
        print("Assumptions / caveats:")
        for a in base["assumptions"]:
            print(f"  - {a}")

    print()
    print("=" * 78)
    print("KALSHI WEATHER EDGE — PART 2: forward calibration tracker (current state)")
    print("=" * 78)
    score = settle_and_score()
    if score.get("error"):
        print(f"ERROR: {score['error']}")
    print(f"predictions file: {score.get('predictions_path')}")
    print(f"logged={score.get('n_logged', 0)}  resolved={score.get('n_resolved', 0)}  "
          f"pending={score.get('n_pending', 0)}  resolved_this_run={score.get('resolved_this_run', 0)}")
    if score.get("n_resolved", 0) == 0:
        print(f"  {score.get('reason')}")
    else:
        print(f"  brier_score={score['brier_score']}   market_brier_score={score['market_brier_score']}")
        print(f"  avg_claimed_edge_at_log={score['avg_claimed_edge_pp']}pp   "
              f"avg_realized_edge_per_contract=${score['avg_realized_edge_per_contract']}")
        print("  calibration table (predicted vs realized):")
        for row in score["calibration_table"]:
            print(f"    band={row['band']:<11} n={row['n']:<4} predicted_avg={row['predicted_avg']:<6} realized_rate={row['realized_rate']}")
        print(f"  proven={score['proven']}: {score['reason']}")


if __name__ == "__main__":
    _print_report()
