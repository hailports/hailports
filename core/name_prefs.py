"""Preferred first-name / nickname resolver shared by every reply drafter (zoom + inbox).

Operator's rule (2026-07-15): ALWAYS address people the way they expect from prior context — e.g. Fernanda
goes by "Fern". A drafter resolves a first name, then passes it through `preferred()` so the greeting
matches how the person is actually addressed in-thread.

Extensible without a code change: drop a JSON object {"<lowercased first name>": "<greeting>"} at the
path in NAME_PREFS_FILE (defaults to the work digests dir). File entries override the in-code seed.
Keyed by the LOWERCASED first-name token; value is the exact display form to greet with.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

# in-code seed — the known ones. Add here for permanence, or via the JSON override for quick edits.
_SEED: dict[str, str] = {
    "fernanda": "Fern",
    "vijaykrishna": "Vijay",   # AD concatenates "VijayKrishna"; Operator addresses him "Vijay"
}

NAME_PREFS_FILE = Path(os.environ.get(
    "NAME_PREFS_FILE",
    str(Path.home() / ".openclaw" / "workspace" / "CompanyA-local" / "digests" / "name_prefs.json"),
))

_cache: dict[str, str] | None = None


def _load() -> dict[str, str]:
    global _cache
    if _cache is not None:
        return _cache
    m = dict(_SEED)
    try:
        if NAME_PREFS_FILE.exists():
            data = json.loads(NAME_PREFS_FILE.read_text())
            if isinstance(data, dict):
                for k, v in data.items():
                    if isinstance(k, str) and isinstance(v, str) and v.strip():
                        m[k.strip().lower()] = v.strip()
    except Exception:
        pass  # fail-soft: a bad override file never breaks a draft
    _cache = m
    return m


def lookup(first_name: str) -> str | None:
    """Return the explicit preferred greeting for a first name, or None if there's no mapping.
    Use when the caller has its own default casing (e.g. the zoom drafter's lowercase voice) and only
    wants to override for known nicknames."""
    return _load().get(str(first_name or "").strip().lower())


def preferred(first_name: str) -> str:
    """Map a resolved first name to how the person expects to be greeted.
    Unknown names are returned Title-cased (fernanda->Fern via the map; harsha->Harsha via title-case)."""
    raw = str(first_name or "").strip()
    if not raw:
        return raw
    hit = _load().get(raw.lower())
    if hit:
        return hit
    return raw[:1].upper() + raw[1:] if raw.islower() else raw
