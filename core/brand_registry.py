"""Thin loader for data/brand_registry.json — the single source of truth for the
house of competing brands. Every brand surface (site, email, dedup, Stripe,
funnel, chat) DERIVES from here so they can never disagree.

Public API:
    all_brands(include_inactive=False)  -> list[dict]
    get_brand(domain)                   -> dict | None   (by serving/sending host, www/port-tolerant, honors aliases)
    get_brand_by_key(key)               -> dict | None
    brand_price(key, sku)               -> str            (env-overridable Stripe price id on the ONE live account)
    host_index()                        -> {host: key}

No identity is ever exposed here; records carry pseudonymous persona signers only.
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

_REGISTRY_PATH = Path(
    os.environ.get("BRAND_REGISTRY_PATH")
    or (Path(__file__).resolve().parent.parent / "data" / "brand_registry.json")
)


@lru_cache(maxsize=1)
def _load() -> dict:
    with open(_REGISTRY_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def reload() -> None:
    """Drop the cache (after editing the JSON in-process)."""
    _load.cache_clear()
    _by_host.cache_clear()
    _by_key.cache_clear()


def _norm_host(raw: str) -> str:
    h = (raw or "").split(",")[0].split(":")[0].strip().lower().rstrip(".")
    return h[4:] if h.startswith("www.") else h


@lru_cache(maxsize=1)
def _by_key() -> dict:
    return {b["key"]: b for b in _load().get("brands", [])}


@lru_cache(maxsize=1)
def _by_host() -> dict:
    idx: dict[str, str] = {}
    for b in _load().get("brands", []):
        hosts = [b.get("domain", "")] + list(b.get("aliases", []) or [])
        for h in hosts:
            nh = _norm_host(h)
            if nh:
                idx.setdefault(nh, b["key"])
    return idx


def all_brands(include_inactive: bool = False) -> list[dict]:
    brands = _load().get("brands", [])
    if include_inactive:
        return list(brands)
    return [b for b in brands if b.get("active", True)]


def get_brand(domain: str) -> dict | None:
    """Resolve a brand by serving/sending host (www/port/alias tolerant)."""
    return _by_key().get(_by_host().get(_norm_host(domain)))


def get_brand_by_key(key: str) -> dict | None:
    return _by_key().get((key or "").strip().lower())


def host_index() -> dict:
    return dict(_by_host())


def shared() -> dict:
    return dict(_load().get("shared", {}))


def brand_price(key: str, sku: str) -> str:
    """Stripe price id for `key:sku` on the ONE live account. Env override wins,
    else the registry default. Empty -> not yet created (checkout fails loudly)."""
    brand = get_brand_by_key(key)
    if not brand:
        return ""
    entry = (brand.get("stripe_tiers") or {}).get(sku)
    if not entry:
        return ""
    env_name = entry.get("env")
    if env_name:
        val = os.environ.get(env_name, "").strip()
        if val:
            return val
    return (entry.get("price_id") or "").strip()


if __name__ == "__main__":
    bs = all_brands()
    print(f"{len(bs)} active brands:")
    for b in bs:
        print(f"  {b['key']:13s} {b['domain']:18s} {b['brand_name']!r} signer={b['persona_name']!r}")
    assert get_brand("www.redacted.com:443")["key"] == "builtfast"
    assert get_brand("opsapp.app")["key"] == "opsapp"  # alias resolves
    assert brand_price("scannerapp", "fix_plan").startswith("price_")
    print("host index:", host_index())
    print("OK")
