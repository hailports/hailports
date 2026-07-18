"""smart_money_tracker — $0 Solana on-chain opportunity + flow tracker.

Free-only (DexScreener, no key). Surfaces where fast money is moving by the
observable on-chain footprint: fresh liquidity + volume surge + buy-pressure on
young pairs. True per-wallet PnL leaderboards need a paid key (Birdeye/GMGN/Nansen)
— that slot is stubbed in rank_top_wallets() and lights up when a key is present.

Matches core/market_signals idioms (urllib, file cache, module funcs) so it plugs
straight into that ingest pipeline via provide_smart_money(). Research/paper only —
emits signals, never places a trade.
"""
from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CACHE_PATH = ROOT / "data" / "cache" / "smart_money.json"
CACHE_TTL_S = 300.0

_DEX = "https://api.dexscreener.com"
_UA = "Mozilla/5.0 (compatible; smt/1.0)"
_MIN_LIQ = 20_000.0  # illiquid floor — below this the fill is a mirage


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _f(x: Any, default: float = 0.0) -> float:
    try:
        if x is None:
            return default
        return float(x)
    except (TypeError, ValueError):
        return default


def _http_json(url: str, timeout: float = 15) -> Any:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


def _load_cache() -> dict[str, Any]:
    try:
        return json.loads(CACHE_PATH.read_text())
    except Exception:
        return {}


def _save_cache(cache: dict[str, Any]) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(json.dumps(cache))
    except Exception:
        pass


def _cache_get(cache: dict[str, Any], key: str, ttl_s: float = CACHE_TTL_S) -> Any:
    ent = cache.get(key)
    if isinstance(ent, dict) and (time.time() - ent.get("t", 0)) < ttl_s:
        return ent.get("v")
    return None


def _cache_put(cache: dict[str, Any], key: str, value: Any) -> None:
    cache[key] = {"t": time.time(), "v": value}


# ── DexScreener free endpoints ──────────────────────────────────────────────

def fetch_trending(limit: int = 30) -> list[str]:
    """Boosted/trending tokens = attention proxy. Return solana token addresses."""
    data = _http_json(f"{_DEX}/token-boosts/top/v1") or []
    out: list[str] = []
    for it in data if isinstance(data, list) else []:
        if isinstance(it, dict) and it.get("chainId") == "solana":
            addr = it.get("tokenAddress")
            if addr:
                out.append(addr)
    seen, uniq = set(), []
    for a in out:
        if a not in seen:
            seen.add(a)
            uniq.append(a)
    return uniq[:limit]


def fetch_token_pairs(token_address: str) -> list[dict[str, Any]]:
    data = _http_json(f"{_DEX}/latest/dex/tokens/{urllib.parse.quote(token_address)}")
    if isinstance(data, dict):
        return data.get("pairs") or []
    return []


def search_pairs(query: str) -> list[dict[str, Any]]:
    data = _http_json(f"{_DEX}/latest/dex/search?q={urllib.parse.quote(query)}")
    if isinstance(data, dict):
        return data.get("pairs") or []
    return []


def _best_solana_pair(pairs: list[dict[str, Any]]) -> dict[str, Any] | None:
    sol = [p for p in pairs if isinstance(p, dict) and p.get("chainId") == "solana"]
    if not sol:
        return None
    return max(sol, key=lambda p: _f((p.get("liquidity") or {}).get("usd")))


# ── scoring ─────────────────────────────────────────────────────────────────

def score_pair(pair: dict[str, Any]) -> dict[str, Any]:
    liq = pair.get("liquidity") or {}
    vol = pair.get("volume") or {}
    txn = pair.get("txns") or {}
    chg = pair.get("priceChange") or {}
    base = pair.get("baseToken") or {}
    h1 = txn.get("h1") or {}

    liquidity_usd = _f(liq.get("usd"))
    vol_h24 = _f(vol.get("h24"))
    vol_h1 = _f(vol.get("h1"))
    buys_h1 = _f(h1.get("buys"))
    sells_h1 = _f(h1.get("sells"))
    mom_h1 = _f(chg.get("h1"))
    mom_h6 = _f(chg.get("h6"))
    mom_h24 = _f(chg.get("h24"))

    vol_surge = (vol_h1 * 24.0) / max(vol_h24, 1.0)
    buy_ratio_h1 = buys_h1 / max(buys_h1 + sells_h1, 1.0)
    created = _f(pair.get("pairCreatedAt"))
    age_hours = (time.time() * 1000.0 - created) / 3.6e6 if created > 0 else 1e9

    s = 0.0
    s += 0.35 * buy_ratio_h1                              # buy pressure
    s += 0.25 * min(vol_surge / 3.0, 1.0)                 # volume acceleration
    s += 0.15 if age_hours < 72 else 0.0                  # fresh-pair bonus
    s += 0.15 * max(min(mom_h1 / 20.0, 1.0), 0.0)         # short momentum
    s += 0.10 * max(min(mom_h6 / 40.0, 1.0), 0.0)         # medium momentum
    if liquidity_usd < _MIN_LIQ:
        s -= 0.50                                         # illiquid = untradeable
    if buy_ratio_h1 < 0.40:
        s -= 0.35                                         # net dumping
    flow_score = max(0.0, min(1.0, s))

    return {
        "symbol": base.get("symbol") or "?",
        "name": base.get("name") or "",
        "address": base.get("address") or "",
        "url": pair.get("url") or "",
        "price": _f(pair.get("priceUsd")),
        "liquidity_usd": round(liquidity_usd, 2),
        "vol_h24": round(vol_h24, 2),
        "vol_h1": round(vol_h1, 2),
        "vol_surge": round(vol_surge, 2),
        "buys_h1": int(buys_h1),
        "sells_h1": int(sells_h1),
        "buy_ratio_h1": round(buy_ratio_h1, 3),
        "mom_h1": mom_h1,
        "mom_h6": mom_h6,
        "mom_h24": mom_h24,
        "age_hours": round(age_hours, 1) if age_hours < 1e8 else None,
        "flow_score": round(flow_score, 3),
    }


def top_opportunities(limit: int = 15, *, cache: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    cache = cache if cache is not None else _load_cache()
    cached = _cache_get(cache, "top_opps")
    if cached is not None:
        return cached[:limit]

    addrs = fetch_trending(30)
    scored: dict[str, dict[str, Any]] = {}
    for a in addrs:
        best = _best_solana_pair(fetch_token_pairs(a))
        if best:
            row = score_pair(best)
            if row["address"]:
                scored[row["address"]] = row
    for p in search_pairs("SOL/USDC"):
        if isinstance(p, dict) and p.get("chainId") == "solana":
            row = score_pair(p)
            if row["address"] and row["address"] not in scored:
                scored[row["address"]] = row

    ranked = sorted(scored.values(), key=lambda r: r["flow_score"], reverse=True)
    _cache_put(cache, "top_opps", ranked)
    _save_cache(cache)
    return ranked[:limit]


# ── paid-gated wallet PnL slot (Birdeye / GMGN / Nansen) ────────────────────

def rank_top_wallets(limit: int = 25) -> dict[str, Any]:
    """Top wallets by realized PnL. Requires a paid data key — $0 sources do not
    expose wallet-level PnL leaderboards. Returns empty+flagged until
    BIRDEYE_API_KEY / GMGN_API_KEY is set, then wire the provider here."""
    key = os.getenv("BIRDEYE_API_KEY") or os.getenv("GMGN_API_KEY")
    if not key:
        return {
            "wallets": [],
            "enabled": False,
            "note": "paid-gated: set BIRDEYE_API_KEY or GMGN_API_KEY to enable wallet-PnL ranking",
        }
    # TODO(paid): fetch Birdeye/GMGN top-trader leaderboard, rank by realized PnL,
    # return [{address, pnl_usd, win_rate, trades, tokens[...]}, ...]
    return {"wallets": [], "enabled": True, "note": "provider key present — implement fetch"}


# ── pipeline surface ────────────────────────────────────────────────────────

def ingest() -> dict[str, Any]:
    cache = _load_cache()
    opps = top_opportunities(15, cache=cache)
    return {
        "generated_at": _now_iso(),
        "source": "dexscreener_free",
        "chain": "solana",
        "opportunities": opps,
        "wallet_intel": rank_top_wallets(),
    }


def provide_smart_money(state: dict[str, Any], cache: dict[str, Any]) -> dict[str, Any]:
    """market_signals PROVIDERS-compatible: fn(state, cache) -> dict."""
    opps = top_opportunities(15, cache=cache)
    top = opps[0] if opps else None
    return {
        "opportunities": opps,
        "top_flow_score": top["flow_score"] if top else 0.0,
        "top_symbol": top["symbol"] if top else None,
        "count": len(opps),
        "wallet_intel_enabled": rank_top_wallets()["enabled"],
        "source": "dexscreener_free",
    }


HISTORY_PATH = ROOT / "data" / "smart_money_history.jsonl"


def monitor_once(min_flow: float = 0.55, limit: int = 15) -> dict[str, Any]:
    """Snapshot top opportunities, append a compact row per opp to the history
    JSONL (backtest fuel + live feed). Returns the flagged (>=min_flow) opps."""
    ts = _now_iso()
    opps = top_opportunities(limit)
    flagged = [o for o in opps if o["flow_score"] >= min_flow]
    try:
        HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        with HISTORY_PATH.open("a") as fh:
            for o in opps:
                fh.write(json.dumps({
                    "ts": ts, "symbol": o["symbol"], "address": o["address"],
                    "price": o["price"], "flow_score": o["flow_score"],
                    "liquidity_usd": o["liquidity_usd"], "vol_surge": o["vol_surge"],
                    "buy_ratio_h1": o["buy_ratio_h1"], "age_hours": o["age_hours"],
                }, default=str) + "\n")
    except Exception:
        pass
    return {"ts": ts, "flagged": flagged, "total": len(opps)}


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Solana smart-money / flow tracker ($0, paper-research only)")
    ap.add_argument("--table", action="store_true", help="compact ranked table instead of JSON")
    ap.add_argument("--monitor", action="store_true", help="snapshot->append history JSONL (for launchd loop)")
    ap.add_argument("--min-flow", type=float, default=0.55)
    ap.add_argument("--limit", type=int, default=15)
    args = ap.parse_args(argv)

    if args.monitor:
        res = monitor_once(min_flow=args.min_flow, limit=args.limit)
        print(json.dumps({"ts": res["ts"], "total": res["total"],
                          "flagged": [f["symbol"] for f in res["flagged"]]}, default=str))
        return 0

    if args.table:
        rows = top_opportunities(args.limit)
        print(f"{'SYM':<12}{'FLOW':>6}{'LIQ$':>12}{'VOLx':>7}{'BUY%':>7}{'AGEh':>8}  MOM h1/h6/h24")
        for r in rows:
            age = f"{r['age_hours']:.0f}" if r.get("age_hours") is not None else "-"
            print(f"{r['symbol'][:11]:<12}{r['flow_score']:>6.2f}{r['liquidity_usd']:>12,.0f}"
                  f"{r['vol_surge']:>7.1f}{r['buy_ratio_h1']*100:>6.0f}%{age:>8}"
                  f"  {r['mom_h1']:+.1f}/{r['mom_h6']:+.1f}/{r['mom_h24']:+.1f}")
        return 0

    print(json.dumps(ingest(), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
