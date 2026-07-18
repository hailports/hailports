"""Kalshi event-trading bot with Bovada read-only odds bridge.

Default behavior is paper/research only. Live Kalshi orders require all gates:
  1. KALSHI_MODE=live
  2. KALSHI_ALLOW_LIVE=1
  3. data/runtime/kalshi_live_armed.json exists
  4. KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH are configured

Bovada support is deliberately read-only. Bovada's public terms prohibit bots
and automated play through their site, so this module can ingest licensed odds
feeds for comparison/research but never places Bovada wagers.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core import BASE_DIR


RUNTIME = BASE_DIR / "data" / "runtime"
LOGS = BASE_DIR / "data" / "logs"
STATE_PATH = RUNTIME / "kalshi_bot_state.json"
STATUS_PATH = RUNTIME / "kalshi_bot_status.json"
SIGNALS_PATH = RUNTIME / "kalshi_bot_signals.jsonl"
BOVADA_PATH = RUNTIME / "bovada_odds_snapshot.json"
TEST_SOURCES_PATH = BASE_DIR / "data" / "hustle" / "test_sources.json"
KILL_PATH = RUNTIME / "kalshi_bot.kill"
LIVE_ARM_PATH = RUNTIME / "kalshi_live_armed.json"
LOG_PATH = LOGS / "kalshi_bot.jsonl"

PROD_BASE = "https://external-api.kalshi.com/trade-api/v2"
DEMO_BASE = "https://external-api.demo.kalshi.co/trade-api/v2"


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(json.dumps(payload, sort_keys=True, default=str) + "\n")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str))
    tmp.replace(path)


def _read_json(path: Path, fallback: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        if path.exists():
            data = json.loads(path.read_text())
            return data if isinstance(data, dict) else (fallback or {})
    except Exception:
        pass
    return fallback or {}


def _get_json(url: str, headers: dict[str, str] | None = None, timeout: float = 12) -> dict[str, Any]:
    req = urllib.request.Request(url, headers=headers or {"Accept": "application/json"}, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as response:
        raw = response.read().decode("utf-8", errors="replace")
        return json.loads(raw) if raw else {}


def _post_json(url: str, payload: dict[str, Any], headers: dict[str, str] | None = None, timeout: float = 12) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    merged = {"Accept": "application/json", "Content-Type": "application/json"}
    merged.update(headers or {})
    req = urllib.request.Request(url, data=body, headers=merged, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as response:
        raw = response.read().decode("utf-8", errors="replace")
        return json.loads(raw) if raw else {}


@dataclass(frozen=True)
class KalshiConfig:
    mode: str
    env: str
    base_url: str
    starting_cash: float
    max_order_cost: float
    max_daily_loss: float
    max_open_positions: int
    max_daily_orders: int
    min_edge: float
    live_allowed: bool
    api_key_id: str
    private_key_path: str
    bovada_provider_url: str
    bovada_api_key: str
    research_urls: tuple[str, ...]

    @classmethod
    def from_env(cls) -> "KalshiConfig":
        env = str(os.environ.get("KALSHI_ENV", "demo")).strip().lower()
        base_url = PROD_BASE if env == "prod" else DEMO_BASE
        mode = str(os.environ.get("KALSHI_MODE", "paper")).strip().lower()
        if mode not in {"paper", "live"}:
            mode = "paper"
        env_sources = tuple(
            item.strip()
            for item in str(
                os.environ.get("KALSHI_RESEARCH_URLS")
                or os.environ.get("SPORTS_EDGE_SOURCE_URLS")
                or ""
            ).split(",")
            if item.strip()
        )
        file_sources = _read_source_urls()
        return cls(
            mode=mode,
            env=env,
            base_url=os.environ.get("KALSHI_BASE_URL", base_url).rstrip("/"),
            starting_cash=float(os.environ.get("KALSHI_PAPER_STARTING_CASH", 100.0)),
            max_order_cost=float(os.environ.get("KALSHI_MAX_ORDER_COST", 3.0)),
            max_daily_loss=float(os.environ.get("KALSHI_MAX_DAILY_LOSS", 5.0)),
            max_open_positions=int(os.environ.get("KALSHI_MAX_OPEN_POSITIONS", 10)),
            max_daily_orders=int(os.environ.get("KALSHI_MAX_DAILY_ORDERS", 10)),
            min_edge=float(os.environ.get("KALSHI_MIN_EDGE", 0.08)),
            live_allowed=_truthy(os.environ.get("KALSHI_ALLOW_LIVE")),
            api_key_id=str(os.environ.get("KALSHI_API_KEY_ID", "")).strip(),
            private_key_path=str(os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")).strip(),
            bovada_provider_url=str(os.environ.get("BOVADA_ODDS_API_URL", "")).strip() or _read_bovada_provider_from_file(),
            bovada_api_key=str(os.environ.get("BOVADA_ODDS_API_KEY", "")).strip(),
            research_urls=env_sources or file_sources,
        )


def _read_source_urls() -> tuple[str, ...]:
    try:
        data = _read_json(TEST_SOURCES_PATH, {})
        raw = data.get("sports_research_urls") or data.get("kalshi_research_urls") or data.get("sources") or []
        urls: list[str] = []
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, str):
                    url = item.strip()
                    enabled = True
                elif isinstance(item, dict):
                    url = str(item.get("url") or "").strip()
                    enabled = item.get("enabled", True) is not False
                else:
                    continue
                if enabled and url.startswith(("http://", "https://")):
                    urls.append(url)
        return tuple(urls[:12])
    except Exception:
        return tuple()


def _read_bovada_provider_from_file() -> str:
    try:
        data = _read_json(TEST_SOURCES_PATH, {})
        return str(data.get("bovada_odds_api_url") or "").strip()
    except Exception:
        return ""


def _initial_state(config: KalshiConfig) -> dict[str, Any]:
    return {
        "created_at": _utc_now(),
        "updated_at": _utc_now(),
        "mode": config.mode,
        "env": config.env,
        "cash": round(config.starting_cash, 4),
        "starting_cash": round(config.starting_cash, 4),
        "positions": {},
        "signals": [],
        "daily": {"date": datetime.now().date().isoformat(), "orders": 0, "paper_spend": 0.0, "realized_pnl": 0.0},
        "notes": ["paper mode only until live gates are explicitly armed"],
    }


def load_state(config: KalshiConfig) -> dict[str, Any]:
    state = _read_json(STATE_PATH)
    if not state:
        state = _initial_state(config)
    today = datetime.now().date().isoformat()
    if (state.get("daily") or {}).get("date") != today:
        state["daily"] = {"date": today, "orders": 0, "paper_spend": 0.0, "realized_pnl": 0.0}
    state.setdefault("positions", {})
    state.setdefault("signals", [])
    return state


def save_state(state: dict[str, Any]) -> None:
    state["updated_at"] = _utc_now()
    _write_json(STATE_PATH, state)


def fetch_markets(config: KalshiConfig, *, limit: int = 80) -> tuple[list[dict[str, Any]], str]:
    query = urllib.parse.urlencode({"limit": max(1, min(limit, 200)), "status": "open"})
    url = f"{config.base_url}/markets?{query}"
    try:
        data = _get_json(url)
        markets = data.get("markets") if isinstance(data, dict) else []
        if isinstance(markets, list):
            return markets, "kalshi_public_api"
    except Exception as exc:
        return _synthetic_markets(), f"synthetic_fallback:{exc}"
    return _synthetic_markets(), "synthetic_fallback:empty"


def _synthetic_markets() -> list[dict[str, Any]]:
    bucket = int(time.time() // 900)
    markets: list[dict[str, Any]] = []
    titles = [
        "Will BTC close above a key level today?",
        "Will the high temperature exceed forecast?",
        "Will a major index close green today?",
        "Will an NBA favorite win tonight?",
        "Will a government data release beat consensus?",
    ]
    for idx, title in enumerate(titles):
        seed = int(hashlib.sha256(f"{bucket}:{idx}".encode()).hexdigest()[:10], 16)
        yes_ask = 8 + (seed % 70)
        yes_bid = max(1, yes_ask - (2 + seed % 8))
        markets.append({
            "ticker": f"SIM-{bucket}-{idx}",
            "title": title,
            "yes_bid": yes_bid,
            "yes_ask": yes_ask,
            "volume": 2000 + seed % 15000,
            "liquidity": 1000 + seed % 8000,
            "status": "open",
        })
    return markets


def _as_cents(value: Any) -> int | None:
    if value is None:
        return None
    try:
        if isinstance(value, str) and "." in value:
            return int(round(float(value) * 100))
        return int(round(float(value)))
    except Exception:
        return None


def fetch_research_sources(config: KalshiConfig) -> dict[str, Any]:
    """Fetch configured online research/handicapper/odds feeds read-only."""
    rows: list[dict[str, Any]] = []
    for url in config.research_urls[:12]:
        try:
            data = _get_json(url, timeout=10)
            text = json.dumps(data, default=str)[:12000]
            rows.append({"url": url, "ok": True, "chars": len(text), "text": text})
        except Exception as exc:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html,text/plain,application/rss+xml"})
                with urllib.request.urlopen(req, timeout=10) as response:
                    raw = response.read(12000).decode("utf-8", errors="replace")
                rows.append({"url": url, "ok": True, "chars": len(raw), "text": raw})
            except Exception as exc2:
                rows.append({"url": url, "ok": False, "error": f"{exc}; {exc2}", "text": ""})
    return {
        "enabled": bool(config.research_urls),
        "sources": [{k: v for k, v in row.items() if k != "text"} for row in rows],
        "text": "\n\n".join(row.get("text") or "" for row in rows if row.get("ok"))[:50000],
        "updated_at": _utc_now(),
        "mode": "read_only",
    }


def _research_overlap(title: str, research: dict[str, Any] | None) -> tuple[float, list[str]]:
    text = str((research or {}).get("text") or "").lower()
    if not text:
        return 0.0, []
    raw_words = [w.strip(".,:;!?()[]{}\"'").lower() for w in str(title or "").split()]
    words = [w for w in raw_words if len(w) >= 4 and w not in {"will", "above", "below", "today", "this", "that", "with"}]
    hits = []
    for word in words[:20]:
        if word in text and word not in hits:
            hits.append(word)
    score = min(0.05, len(hits) * 0.008)
    return round(score, 4), hits[:8]


def score_market(market: dict[str, Any], research: dict[str, Any] | None = None) -> dict[str, Any] | None:
    ticker = str(market.get("ticker") or "").strip()
    title = str(market.get("title") or market.get("event_title") or "").strip()
    yes_ask = _as_cents(market.get("yes_ask") or market.get("yes_ask_dollars"))
    yes_bid = _as_cents(market.get("yes_bid") or market.get("yes_bid_dollars"))
    if not ticker or yes_ask is None or yes_ask <= 0 or yes_ask >= 100:
        return None
    volume = float(market.get("volume") or market.get("volume_24h") or 0.0)
    liquidity = float(market.get("liquidity") or market.get("open_interest") or 0.0)
    spread = max(1, yes_ask - (yes_bid or max(1, yes_ask - 8)))
    price = yes_ask / 100.0
    liquidity_score = min(0.08, (volume + liquidity) / 250000.0)
    cheap_convexity = 0.06 if 0.03 <= price <= 0.20 else 0.0
    spread_penalty = min(0.12, spread / 100.0)
    research_score, research_terms = _research_overlap(title, research)
    edge = round(liquidity_score + cheap_convexity + research_score - spread_penalty, 4)
    return {
        "ticker": ticker,
        "title": title,
        "yes_ask_cents": yes_ask,
        "yes_bid_cents": yes_bid,
        "spread_cents": spread,
        "price": round(price, 4),
        "volume": volume,
        "liquidity": liquidity,
        "edge_score": edge,
        "research_score": research_score,
        "research_terms": research_terms,
        "reason": "cheap convexity + liquidity" if cheap_convexity else "liquidity/spread scan",
        "raw": {k: market.get(k) for k in ("ticker", "title", "yes_bid", "yes_ask", "volume", "liquidity", "status")},
    }


def fetch_bovada_snapshot(config: KalshiConfig) -> dict[str, Any]:
    if not config.bovada_provider_url:
        payload = {
            "enabled": False,
            "mode": "read_only",
            "reason": "BOVADA_ODDS_API_URL not configured; no scraping and no live Bovada wagering",
            "updated_at": _utc_now(),
        }
        _write_json(BOVADA_PATH, payload)
        return payload
    headers = {"Accept": "application/json"}
    if config.bovada_api_key:
        headers["Authorization"] = f"Bearer {config.bovada_api_key}"
    try:
        data = _get_json(config.bovada_provider_url, headers=headers, timeout=15)
        items = data.get("data") or data.get("events") or data.get("odds") or []
        count = len(items) if isinstance(items, list) else 1
        payload = {
            "enabled": True,
            "mode": "read_only",
            "provider_url": config.bovada_provider_url,
            "items": count,
            "updated_at": _utc_now(),
            "note": "odds feed only; no Bovada account automation or wagering",
        }
    except Exception as exc:
        payload = {"enabled": True, "mode": "read_only", "error": str(exc), "updated_at": _utc_now()}
    _write_json(BOVADA_PATH, payload)
    return payload


def _risk_blocks(state: dict[str, Any], config: KalshiConfig) -> list[str]:
    blocks: list[str] = []
    if KILL_PATH.exists():
        blocks.append(f"kill switch active: {KILL_PATH}")
    daily = state.get("daily") or {}
    if int(daily.get("orders") or 0) >= config.max_daily_orders:
        blocks.append(f"daily order cap hit: {daily.get('orders')} >= {config.max_daily_orders}")
    if float(daily.get("paper_spend") or 0.0) >= config.max_daily_loss:
        blocks.append(f"daily paper risk cap hit: ${daily.get('paper_spend'):.2f} >= ${config.max_daily_loss:.2f}")
    if len(state.get("positions") or {}) >= config.max_open_positions:
        blocks.append(f"open position cap hit: {len(state.get('positions') or {})} >= {config.max_open_positions}")
    if config.mode == "live":
        if not config.live_allowed:
            blocks.append("live blocked: KALSHI_ALLOW_LIVE is not set")
        if not LIVE_ARM_PATH.exists():
            blocks.append(f"live blocked: missing {LIVE_ARM_PATH}")
        if not config.api_key_id or not config.private_key_path:
            blocks.append("live blocked: Kalshi key id/private key path not configured")
    return blocks


def paper_order(state: dict[str, Any], config: KalshiConfig, signal: dict[str, Any]) -> dict[str, Any]:
    cost = min(config.max_order_cost, max(1.0, float(signal.get("price") or 0.0) * 1.0))
    if float(state.get("cash") or 0.0) < cost:
        return {"status": "blocked", "reason": "paper cash too low"}
    order = {
        "id": f"kpaper-{uuid.uuid4().hex[:10]}",
        "ts": _utc_now(),
        "ticker": signal["ticker"],
        "title": signal.get("title"),
        "side": "yes",
        "contracts": 1,
        "price": signal.get("price"),
        "paper_cost": round(cost, 4),
        "edge_score": signal.get("edge_score"),
        "status": "paper_open",
        "reason": signal.get("reason"),
    }
    state["cash"] = round(float(state.get("cash") or 0.0) - cost, 4)
    state.setdefault("positions", {})[order["ticker"]] = order
    state.setdefault("daily", {})["orders"] = int(state.get("daily", {}).get("orders") or 0) + 1
    state.setdefault("daily", {})["paper_spend"] = round(float(state.get("daily", {}).get("paper_spend") or 0.0) + cost, 4)
    _append_jsonl(SIGNALS_PATH, {"event": "paper_order", **order})
    return {"status": "paper_ordered", "order": order}


def _load_private_key(path: str):
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives import serialization

    with open(path, "rb") as handle:
        return serialization.load_pem_private_key(handle.read(), password=None, backend=default_backend())


def _kalshi_headers(config: KalshiConfig, method: str, path: str) -> dict[str, str]:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    private_key = _load_private_key(config.private_key_path)
    timestamp = str(int(time.time() * 1000))
    sign_path = path.split("?")[0]
    message = f"{timestamp}{method.upper()}{sign_path}".encode("utf-8")
    signature = private_key.sign(
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": config.api_key_id,
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("utf-8"),
    }


def live_order(config: KalshiConfig, signal: dict[str, Any]) -> dict[str, Any]:
    # Caller must still run _risk_blocks; this guard is duplicated by design.
    blocks = _risk_blocks({"daily": {}, "positions": {}}, config)
    live_blocks = [b for b in blocks if b.startswith("live")]
    if live_blocks:
        return {"status": "blocked", "reasons": live_blocks}
    path = "/portfolio/events/orders"
    payload = {
        "ticker": signal["ticker"],
        "client_order_id": f"mini-{uuid.uuid4().hex}",
        "side": "bid",
        "count": "1.00",
        "price": f"{float(signal['price']):.4f}",
        "time_in_force": "fill_or_kill",
        "self_trade_prevention_type": "taker_at_cross",
        "cancel_order_on_pause": True,
        "post_only": False,
    }
    headers = _kalshi_headers(config, "POST", f"/trade-api/v2{path}")
    return _post_json(f"{config.base_url}{path}", payload, headers=headers)


def run_once() -> dict[str, Any]:
    config = KalshiConfig.from_env()
    state = load_state(config)
    markets, market_source = fetch_markets(config)
    bovada = fetch_bovada_snapshot(config)
    research = fetch_research_sources(config)
    scored = [s for s in (score_market(m, research) for m in markets) if s]
    scored.sort(key=lambda row: float(row.get("edge_score") or 0.0), reverse=True)
    best = scored[0] if scored else None
    blocks = _risk_blocks(state, config)
    action: dict[str, Any] = {"status": "hold", "reason": "no qualifying market"}
    if blocks:
        action = {"status": "blocked", "reasons": blocks}
    elif best and float(best.get("edge_score") or 0.0) >= config.min_edge:
        if config.mode == "live":
            action = live_order(config, best)
        else:
            action = paper_order(state, config, best)
    state["last_market_source"] = market_source
    state["last_signal"] = best or {}
    state["last_action"] = action
    state["last_scan_at"] = _utc_now()
    state.setdefault("signals", []).append(best or {"status": "none", "ts": _utc_now()})
    del state["signals"][:-100]
    save_state(state)
    status = build_status(state, config, markets=scored[:10], bovada=bovada, research=research, action=action)
    try:
        from core.test_hustle import record_kalshi_snapshot

        record_kalshi_snapshot(status)
    except Exception:
        pass
    _append_jsonl(LOG_PATH, {"ts": _utc_now(), "event": "cycle", "status": status})
    return status


def build_status(
    state: dict[str, Any],
    config: KalshiConfig,
    *,
    markets: list[dict[str, Any]] | None = None,
    bovada: dict[str, Any] | None = None,
    research: dict[str, Any] | None = None,
    action: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "ok": True,
        "generated_at": _utc_now(),
        "mode": config.mode,
        "env": config.env,
        "base_url": config.base_url,
        "cash": round(float(state.get("cash") or 0.0), 4),
        "starting_cash": round(float(state.get("starting_cash") or config.starting_cash), 4),
        "paper_exposure": round(sum(float(pos.get("paper_cost") or 0.0) for pos in (state.get("positions") or {}).values()), 4),
        "positions": state.get("positions") or {},
        "daily": state.get("daily") or {},
        "risk": asdict(config) | {"api_key_id": bool(config.api_key_id), "private_key_path": bool(config.private_key_path), "bovada_api_key": bool(config.bovada_api_key)},
        "live_trading_possible": bool(config.mode == "live" and config.live_allowed and LIVE_ARM_PATH.exists()),
        "kill_switch": KILL_PATH.exists(),
        "last_market_source": state.get("last_market_source"),
        "last_signal": state.get("last_signal") or {},
        "last_action": action or state.get("last_action") or {},
        "top_markets": markets or [],
        "bovada": bovada if bovada is not None else _read_json(BOVADA_PATH),
        "research": research or {"enabled": bool(config.research_urls), "sources": [], "mode": "read_only"},
        "important": "Bovada is read-only odds ingestion only; no automated Bovada wagering.",
    }
    _write_json(STATUS_PATH, payload)
    return payload


def status_text() -> str:
    config = KalshiConfig.from_env()
    state = load_state(config)
    status = build_status(state, config)
    signal = status.get("last_signal") or {}
    action = status.get("last_action") or {}
    bovada = status.get("bovada") or {}
    research = status.get("research") or {}
    return (
        "Kalshi Bot Status\n"
        f"- Mode: {status['mode']} / {status['env']} ({'LIVE ARMED' if status['live_trading_possible'] else 'live disabled'})\n"
        f"- Paper cash: ${status['cash']:.2f}; exposure: ${status['paper_exposure']:.2f}\n"
        f"- Positions: {len(status.get('positions') or {})}; daily orders: {status.get('daily', {}).get('orders', 0)}\n"
        f"- Last source: {status.get('last_market_source') or 'none'}\n"
        f"- Last signal: {signal.get('ticker', 'none')} edge={signal.get('edge_score', 'n/a')} price={signal.get('price', 'n/a')}\n"
        f"- Last action: {action.get('status', 'none')}\n"
        f"- Bovada bridge: {'configured' if bovada.get('enabled') else 'not configured'}; read-only odds only\n"
        f"- Research feeds: {len(research.get('sources') or [])} configured/read"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Kalshi paper/live-gated event bot.")
    parser.add_argument("--once", action="store_true", help="run one scan/action cycle")
    parser.add_argument("--status", action="store_true", help="print bot status")
    parser.add_argument("--kill", action="store_true", help="activate kill switch")
    parser.add_argument("--resume", action="store_true", help="clear kill switch")
    args = parser.parse_args(argv)
    if args.kill:
        KILL_PATH.parent.mkdir(parents=True, exist_ok=True)
        KILL_PATH.write_text(json.dumps({"activated_at": _utc_now()}))
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
    if args.once:
        print(json.dumps(run_once(), indent=2, sort_keys=True, default=str))
        return 0
    while True:
        run_once()
        time.sleep(max(60, int(os.environ.get("KALSHI_LOOP_INTERVAL_S", "600"))))


if __name__ == "__main__":
    raise SystemExit(main())
