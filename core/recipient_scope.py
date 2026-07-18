"""Recipient-scope gate — is a work-lane reply going to an INTERNAL colleague or an OUTSIDE party?

Shared by the inbox + zoom reply drafters. The failure this prevents: an internal-brain reply
(grounded in CompanyA's private work context — tickets, sandbox state, roadmap) getting auto-delivered
to an external or unidentifiable recipient. Internal-only facts must never auto-flow outward.

    scope(peer) -> {"scope": "internal" | "external" | "unknown", "why": str}

`peer` is a zoom display name ("Panowicz, Rich"), an email, or a raw string. Resolution is best-effort
and FAILS SAFE: anything not positively resolved as internal is external (a non-internal email domain)
or unknown (an unrecognizable display name) — and a caller treats BOTH external and unknown as
"hold for Operator, never auto-tee". Deterministic, $0, read-only.

Internal =
  - an email at an internal domain (@CompanyA.com / @tavant.com, extendable via INTERNAL_DOMAINS), OR
  - a display name whose tokens match a known-staff roster (the shared VIP allowlist + optional
    data/work/staff_roster.json).
"""
from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path

from core import BASE_DIR

INTERNAL_DOMAINS = {
    d.strip().lower().lstrip("@")
    for d in os.environ.get("WORK_INTERNAL_DOMAINS", "CompanyA.com,tavant.com").split(",")
    if d.strip()
}

_VIP_FILE = BASE_DIR / "data" / "work" / "vip_reply_allowlist.json"
_ROSTER_FILE = BASE_DIR / "data" / "work" / "staff_roster.json"
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")


def _tokens(name: str) -> set[str]:
    return {t for t in re.findall(r"[a-z]+", str(name or "").lower()) if len(t) >= 2}


@lru_cache(maxsize=1)
def _roster() -> list[frozenset[str]]:
    """Known-internal-staff token sets, from the VIP allowlist + an optional staff roster file.
    Cached; both files are small. Fail-soft to []."""
    people: list[frozenset[str]] = []
    for f in (_VIP_FILE, _ROSTER_FILE):
        try:
            cfg = json.loads(f.read_text())
        except Exception:
            continue
        rows = cfg.get("senders") or cfg.get("staff") or cfg.get("people") or []
        for s in rows if isinstance(rows, list) else []:
            name = (s.get("name") if isinstance(s, dict) else str(s)) or ""
            local = ""
            if isinstance(s, dict):
                local = str(s.get("email") or "").split("@")[0].replace(".", " ")
            toks = _tokens(name) | _tokens(local)
            if len(toks) >= 2:
                people.append(frozenset(toks))
    return people


def scope(peer) -> dict:
    raw = str(peer or "").strip()
    if not raw:
        return {"scope": "unknown", "why": "empty peer"}

    m = _EMAIL.search(raw)
    if m:
        domain = m.group(1).lower()
        if domain in INTERNAL_DOMAINS or any(domain.endswith("." + d) for d in INTERNAL_DOMAINS):
            return {"scope": "internal", "why": f"internal domain @{domain}"}
        return {"scope": "external", "why": f"external domain @{domain}"}

    # display name -> roster match (need the surname, i.e. the longest token, plus >=2 overlap)
    stoks = _tokens(raw)
    if stoks:
        for person in _roster():
            if not person:
                continue
            surname = max(person, key=len)
            if surname in stoks and len(person & stoks) >= min(2, len(person)):
                return {"scope": "internal", "why": "known-staff roster match"}
    return {"scope": "unknown", "why": "unresolved display name (fails safe -> hold)"}


def _selftest() -> int:
    assert scope("user@example.com")["scope"] == "internal"
    assert scope("user@example.com")["scope"] == "internal"
    assert scope("user@example.com")["scope"] == "external"
    assert scope("")["scope"] == "unknown"
    # roster display-name match (Rich Panowicz is a VIP)
    assert scope("Panowicz, Rich")["scope"] == "internal", scope("Panowicz, Rich")
    assert scope("Random Outsider")["scope"] == "unknown"
    print("recipient_scope selftest ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
