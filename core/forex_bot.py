"""Forex paper bot with live trading double-locked.

Default mode is paper. Live Forex is intentionally blocked unless all gates are
set because retail FX is leveraged, dealer-counterparty trading and can lose
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import time
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
STATE_PATH = RUNTIME / "forex_bot_state.json"
STATUS_PATH = RUNTIME / "forex_bot_status.json"
TRADES_PATH = RUNTIME / "forex_bot_trades.jsonl"
KILL_PATH = RUNTIME / "forex_bot.kill"
LIVE_ARM_PATH = RUNTIME / "forex_live_armed.json"
LOG_PATH = LOGS / "forex_bot.jsonl"
TEST_SOURCES_PATH = BASE_DIR / "data" / "hustle" / "test_sources.json"

DEFAULT_PAIRS = ("EUR_USD", "GBP_USD", "USD_JPY", "AUD_USD", "USD_CAD", "USD_CHF", "NZD_USD")
OANDA_PRACTICE_BASE = "https://api-fxpractice.oanda.com/v3"
OANDA_LIVE_BASE = "https://api-fxtrade.oanda.com/v3"


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


def _get_json(url: str, headers: dict[str, str] | None = None, timeout: float = 12) -> dict[str, Any]:
    req = urllib.request.Request(url, headers=headers or {"Accept": "application/json"}, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as response:
        raw = response.read().decode("utf-8", errors="replace")
        return json.loads(raw) if raw else {}


def _post_json(url: str, payload: dict[str, Any], headers: dict[str, str], timeout: float = 12) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    merged = {"Accept": "application/json", "Content-Type": "application/json"}
    merged.update(headers)
    req = urllib.request.Request(url, data=body, headers=merged, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as response:
        raw = response.read().decode("utf-8", errors="replace")
        return json.loads(raw) if raw else {}


def _source_urls() -> tuple[str, ...]:
    try:
        data = _read_json(TEST_SOURCES_PATH, {})
        raw = data.get("forex_research_urls") or data.get("market_research_urls") or []
        urls: list[str] = []
        if isinstance(raw, list):
            for item in raw:
                url = str(item.get("url") if isinstance(item, dict) else item).strip()
                enabled = item.get("enabled", True) if isinstance(item, dict) else True
                if enabled and url.startswith(("http://", "https://")):
                    urls.append(url)
        return tuple(urls[:12])
    except Exception:
        return tuple()


@dataclass(frozen=True)
class ForexConfig:
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
    rates_provider_url: str
    research_urls: tuple[str, ...]
    live_allowed: bool
    broker: str
    oanda_env: str
    oanda_account_id: str
    oanda_token: str

    @classmethod
    def from_env(cls) -> "ForexConfig":
        raw_pairs = os.environ.get("FOREX_PAIRS", ",".join(DEFAULT_PAIRS))
        pairs = tuple(p.strip().upper().replace("/", "_") for p in raw_pairs.split(",") if p.strip())
        mode = str(os.environ.get("FOREX_MODE", "paper")).strip().lower()
        if mode not in {"paper", "live"}:
            mode = "paper"
        env_sources = tuple(
            item.strip()
            for item in str(os.environ.get("FOREX_RESEARCH_URLS") or "").split(",")
            if item.strip()
        )
        return cls(
            mode=mode,
            pairs=pairs or DEFAULT_PAIRS,
            starting_cash=float(os.environ.get("FOREX_PAPER_STARTING_CASH", 100.0)),
            max_notional_usd=float(os.environ.get("FOREX_MAX_NOTIONAL_USD", 10.0)),
            max_open_positions=int(os.environ.get("FOREX_MAX_OPEN_POSITIONS", 4)),
            max_daily_loss=float(os.environ.get("FOREX_MAX_DAILY_LOSS", 3.0)),
            max_total_loss=float(os.environ.get("FOREX_MAX_TOTAL_LOSS", 20.0)),
            max_daily_trades=int(os.environ.get("FOREX_MAX_DAILY_TRADES", 8)),
            stop_loss_pct=float(os.environ.get("FOREX_STOP_LOSS_PCT", 0.0025)),
            take_profit_pct=float(os.environ.get("FOREX_TAKE_PROFIT_PCT", 0.0035)),
            max_hold_cycles=int(os.environ.get("FOREX_MAX_HOLD_CYCLES", 72)),
            min_edge=float(os.environ.get("FOREX_MIN_EDGE", 0.0014)),
            loop_interval_s=int(os.environ.get("FOREX_LOOP_INTERVAL_S", 600)),
            rates_provider_url=str(os.environ.get("FOREX_RATES_API_URL", "")).strip(),
            research_urls=env_sources or _source_urls(),
            live_allowed=_truthy(os.environ.get("FOREX_ALLOW_LIVE")),
            broker=str(os.environ.get("FOREX_BROKER", "oanda")).strip().lower(),
            oanda_env=str(os.environ.get("OANDA_ENV", "practice")).strip().lower(),
            oanda_account_id=str(os.environ.get("OANDA_ACCOUNT_ID", "")).strip(),
            oanda_token=str(os.environ.get("OANDA_API_TOKEN", "")).strip(),
        )


def initial_state(config: ForexConfig) -> dict[str, Any]:
    return {
        "created_at": _now(),
        "updated_at": _now(),
        "mode": config.mode,
        "cash": round(config.starting_cash, 4),
        "starting_cash": round(config.starting_cash, 4),
        "positions": {},
        "prices": {},
        "price_history": {},
        "realized_pnl": 0.0,
        "daily": {"date": _today(), "starting_equity": config.starting_cash, "trades": 0, "realized_pnl": 0.0},
        "notes": ["paper Forex only until FOREX_ALLOW_LIVE and forex_live_armed.json are both present"],
    }


def load_state(config: ForexConfig) -> dict[str, Any]:
    state = _read_json(STATE_PATH)
    if not state:
        state = initial_state(config)
    if (state.get("daily") or {}).get("date") != _today():
        state["daily"] = {"date": _today(), "starting_equity": equity(state), "trades": 0, "realized_pnl": 0.0}
    state.setdefault("positions", {})
    state.setdefault("prices", {})
    state.setdefault("price_history", {})
    return state


def save_state(state: dict[str, Any]) -> None:
    state["updated_at"] = _now()
    _write_json(STATE_PATH, state)


def _seed(pair: str) -> float:
    return {
        "EUR_USD": 1.08,
        "GBP_USD": 1.27,
        "USD_JPY": 155.0,
        "AUD_USD": 0.66,
        "USD_CAD": 1.36,
        "USD_CHF": 0.91,
        "NZD_USD": 0.60,
    }.get(pair, 1.0)


def _rng(pair: str, bucket: int) -> random.Random:
    digest = hashlib.sha256(f"fx:{pair}:{bucket}".encode()).hexdigest()
    return random.Random(int(digest[:16], 16))


def _fetch_provider_prices(config: ForexConfig) -> dict[str, float]:
    if not config.rates_provider_url:
        return {}
    try:
        data = _get_json(config.rates_provider_url)
    except Exception:
        return {}
    out: dict[str, float] = {}
    rates = data.get("rates") if isinstance(data, dict) else None
    if isinstance(rates, dict):
        for pair in config.pairs:
            left, right = pair.split("_", 1)
            if left == "USD" and right in rates:
                out[pair] = float(rates[right])
            elif left in rates and right == "USD":
                out[pair] = 1.0 / float(rates[left])
    direct = data.get("prices") or data.get("pairs") if isinstance(data, dict) else None
    if isinstance(direct, dict):
        for pair in config.pairs:
            value = direct.get(pair) or direct.get(pair.replace("_", "/"))
            try:
                if value:
                    out[pair] = float(value)
            except Exception:
                pass
    return out


def update_prices(state: dict[str, Any], config: ForexConfig, *, bucket_override: int | None = None) -> dict[str, float]:
    provider = _fetch_provider_prices(config)
    bucket = int(bucket_override) if bucket_override is not None else int(time.time() // max(60, config.loop_interval_s))
    prices: dict[str, float] = {}
    for pair in config.pairs:
        last = float((state.get("prices") or {}).get(pair, {}).get("price") or _seed(pair))
        if pair in provider:
            price = provider[pair]
            source = "provider"
        else:
            rng = _rng(pair, bucket)
            micro = (rng.random() - 0.5) * 0.0018
            wave = math.sin(bucket / 31.0 + len(pair)) * 0.00035
            price = max(0.0001, last * (1 + micro + wave))
            source = "synthetic"
        precision = 3 if pair.endswith("JPY") else 5
        prices[pair] = round(price, precision)
        state.setdefault("prices", {})[pair] = {"price": prices[pair], "source": source, "bucket": bucket, "updated_at": _now()}
        hist = state.setdefault("price_history", {}).setdefault(pair, [])
        if not hist or hist[-1].get("bucket") != bucket:
            hist.append({"bucket": bucket, "price": prices[pair], "source": source, "ts": _now()})
            del hist[:-240]
    return prices


def _hist(state: dict[str, Any], pair: str, n: int = 40) -> list[float]:
    rows = (state.get("price_history") or {}).get(pair) or []
    out = []
    for row in rows[-n:]:
        try:
            out.append(float(row.get("price")))
        except Exception:
            pass
    return out


def equity(state: dict[str, Any]) -> float:
    total = float(state.get("cash") or 0.0)
    prices = state.get("prices") or {}
    for pair, pos in (state.get("positions") or {}).items():
        current = float((prices.get(pair) or {}).get("price") or pos.get("entry") or 0.0)
        total += _position_value(pos, current)
    return round(total, 4)


def _position_value(pos: dict[str, Any], current: float) -> float:
    notional = float(pos.get("notional_usd") or 0.0)
    entry = float(pos.get("entry") or current)
    side = 1.0 if pos.get("side") == "long" else -1.0
    pnl = notional * side * ((current / entry) - 1.0)
    return notional + pnl


def _risk_blocks(state: dict[str, Any], config: ForexConfig) -> list[str]:
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
    if len(state.get("positions") or {}) >= config.max_open_positions:
        blocks.append(f"open position cap hit: {len(state.get('positions') or {})} >= {config.max_open_positions}")
    if config.mode == "live":
        if not config.live_allowed:
            blocks.append("live blocked: FOREX_ALLOW_LIVE is not set")
        if not LIVE_ARM_PATH.exists():
            blocks.append(f"live blocked: missing {LIVE_ARM_PATH}")
        if not config.oanda_account_id or not config.oanda_token:
            blocks.append("live blocked: OANDA_ACCOUNT_ID/OANDA_API_TOKEN not configured")
    return blocks


def _score_pair(state: dict[str, Any], pair: str) -> dict[str, Any] | None:
    hist = _hist(state, pair, 36)
    if len(hist) < 24:
        return None
    price = hist[-1]
    sma6 = sum(hist[-6:]) / 6
    sma24 = sum(hist[-24:]) / 24
    momentum = sma6 / sma24 - 1.0
    ret3 = hist[-1] / hist[-4] - 1.0 if hist[-4] else 0.0
    side = "long" if momentum + ret3 > 0 else "short"
    edge = abs(momentum) + abs(ret3) * 0.5
    return {
        "pair": pair,
        "price": price,
        "side": side,
        "edge": round(edge, 6),
        "reason": "short-term momentum" if side == "long" else "short-term downside momentum",
    }


def _paper_open(state: dict[str, Any], config: ForexConfig, signal: dict[str, Any]) -> dict[str, Any]:
    notional = min(config.max_notional_usd, float(state.get("cash") or 0.0))
    if notional < 1:
        return {"status": "blocked", "reason": "cash below $1"}
    pair = signal["pair"]
    state["cash"] = round(float(state.get("cash") or 0.0) - notional, 4)
    trade = {
        "id": f"fxpaper-{uuid.uuid4().hex[:10]}",
        "ts": _now(),
        "pair": pair,
        "side": signal["side"],
        "entry": signal["price"],
        "notional_usd": round(notional, 4),
        "edge": signal["edge"],
        "reason": signal["reason"],
        "mode": config.mode,
        "opened_cycle": len(_hist(state, pair, 9999)),
    }
    state.setdefault("positions", {})[pair] = trade
    state.setdefault("daily", {})["trades"] = int(state.get("daily", {}).get("trades") or 0) + 1
    _append_jsonl(TRADES_PATH, {"event": "open", **trade})
    return {"status": "paper_opened", "trade": trade}


def _paper_close(state: dict[str, Any], config: ForexConfig, pair: str, price: float, reason: str) -> dict[str, Any]:
    pos = (state.get("positions") or {}).pop(pair, None)
    if not pos:
        return {"status": "hold", "reason": "no position"}
    value = _position_value(pos, price)
    pnl = value - float(pos.get("notional_usd") or 0.0)
    state["cash"] = round(float(state.get("cash") or 0.0) + value, 4)
    state["realized_pnl"] = round(float(state.get("realized_pnl") or 0.0) + pnl, 4)
    state.setdefault("daily", {})["realized_pnl"] = round(float(state.get("daily", {}).get("realized_pnl") or 0.0) + pnl, 4)
    state.setdefault("daily", {})["trades"] = int(state.get("daily", {}).get("trades") or 0) + 1
    trade = {
        "id": pos.get("id"),
        "ts": _now(),
        "pair": pair,
        "side": pos.get("side"),
        "entry": pos.get("entry"),
        "exit": price,
        "notional_usd": pos.get("notional_usd"),
        "pnl": round(pnl, 4),
        "return_pct": round((pnl / max(1e-9, float(pos.get("notional_usd") or 0.0))) * 100, 4),
        "reason": reason,
        "mode": config.mode,
    }
    _append_jsonl(TRADES_PATH, {"event": "close", **trade})
    return {"status": "paper_closed", "trade": trade}


def _live_oanda_order(config: ForexConfig, signal: dict[str, Any]) -> dict[str, Any]:
    blocks = _risk_blocks({"daily": {}, "positions": {}}, config)
    live_blocks = [b for b in blocks if b.startswith("live")]
    if live_blocks:
        return {"status": "blocked", "reasons": live_blocks}
    base = OANDA_LIVE_BASE if config.oanda_env == "live" else OANDA_PRACTICE_BASE
    units = int(max(1, config.max_notional_usd) * 10)
    if signal.get("side") == "short":
        units *= -1
    payload = {
        "order": {
            "type": "MARKET",
            "instrument": signal["pair"],
            "units": str(units),
            "timeInForce": "FOK",
            "positionFill": "DEFAULT",
        }
    }
    headers = {"Authorization": f"Bearer {config.oanda_token}"}
    return _post_json(f"{base}/accounts/{config.oanda_account_id}/orders", payload, headers)


def run_once() -> dict[str, Any]:
    config = ForexConfig.from_env()
    state = load_state(config)
    prices = update_prices(state, config)
    action: dict[str, Any] = {"status": "hold", "reason": "no qualifying signal"}

    # Exits first.
    for pair, pos in list((state.get("positions") or {}).items()):
        price = prices.get(pair)
        if not price:
            continue
        entry = float(pos.get("entry") or price)
        side = 1.0 if pos.get("side") == "long" else -1.0
        ret = side * ((price / entry) - 1.0)
        age = len(_hist(state, pair, 9999)) - int(pos.get("opened_cycle") or 0)
        if ret >= config.take_profit_pct:
            action = _paper_close(state, config, pair, price, f"take profit {ret * 100:.3f}%")
            break
        if ret <= -config.stop_loss_pct:
            action = _paper_close(state, config, pair, price, f"stop loss {ret * 100:.3f}%")
            break
        if age >= config.max_hold_cycles:
            action = _paper_close(state, config, pair, price, f"max hold {age} cycles")
            break

    if action.get("status") == "hold":
        blocks = _risk_blocks(state, config)
        if blocks:
            action = {"status": "blocked", "reasons": blocks}
        else:
            signals = [s for s in (_score_pair(state, p) for p in config.pairs) if s and s["pair"] not in state.get("positions", {})]
            signals.sort(key=lambda row: row["edge"], reverse=True)
            best = signals[0] if signals else None
            if best and best["edge"] >= config.min_edge:
                action = _live_oanda_order(config, best) if config.mode == "live" else _paper_open(state, config, best)
            state["last_signal"] = best or {}

    state["last_action"] = action
    save_state(state)
    status = build_status(state, config, action=action)
    _append_jsonl(LOG_PATH, {"ts": _now(), "event": "cycle", "status": status})
    try:
        from core.test_hustle import record_forex_snapshot

        record_forex_snapshot(status)
    except Exception:
        pass
    return status


def build_status(state: dict[str, Any], config: ForexConfig, *, action: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = {
        "ok": True,
        "generated_at": _now(),
        "mode": config.mode,
        "broker": config.broker,
        "cash": round(float(state.get("cash") or 0.0), 4),
        "equity": equity(state),
        "starting_cash": round(float(state.get("starting_cash") or config.starting_cash), 4),
        "realized_pnl": round(float(state.get("realized_pnl") or 0.0), 4),
        "positions": state.get("positions") or {},
        "daily": state.get("daily") or {},
        "prices": state.get("prices") or {},
        "last_signal": state.get("last_signal") or {},
        "last_action": action or state.get("last_action") or {},
        "risk": asdict(config) | {"oanda_token": bool(config.oanda_token), "oanda_account_id": bool(config.oanda_account_id)},
        "live_trading_possible": bool(config.mode == "live" and config.live_allowed and LIVE_ARM_PATH.exists()),
        "kill_switch": KILL_PATH.exists(),
        "important": "Forex remains paper-only until explicit live gates are armed.",
    }
    _write_json(STATUS_PATH, payload)
    return payload


def status_text() -> str:
    config = ForexConfig.from_env()
    state = load_state(config)
    status = build_status(state, config)
    signal = status.get("last_signal") or {}
    action = status.get("last_action") or {}
    return (
        "Forex Bot Status\n"
        f"- Mode: {status['mode']} / broker={status['broker']} ({'LIVE ARMED' if status['live_trading_possible'] else 'live disabled'})\n"
        f"- Equity: ${status['equity']:.2f} from ${status['starting_cash']:.2f}; cash ${status['cash']:.2f}\n"
        f"- Realized P/L: ${status['realized_pnl']:.2f}; positions: {len(status.get('positions') or {})}\n"
        f"- Daily trades: {status.get('daily', {}).get('trades', 0)}\n"
        f"- Last signal: {signal.get('pair', 'none')} {signal.get('side', '')} edge={signal.get('edge', 'n/a')}\n"
        f"- Last action: {action.get('status', 'none')}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Forex paper/live-gated bot.")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--kill", action="store_true")
    parser.add_argument("--resume", action="store_true")
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
    if args.once:
        print(json.dumps(run_once(), indent=2, sort_keys=True, default=str))
        return 0
    while True:
        run_once()
        time.sleep(max(60, ForexConfig.from_env().loop_interval_s))


if __name__ == "__main__":
    raise SystemExit(main())
