#!/usr/bin/env python3
"""TenantProfile — the single identity object every per-tenant agent reads.

Replicating Operator's assistant for another person (Operator2 = tenant #2) without cloning his
employer/hustle-wired code: the reusable agents become tenant-aware and read a TenantProfile
instead of hardcoding Operator. One codebase, N isolated people — each with their own accounts,
voice, review queue, brain scope, send-guard. Air-gapped from Operator's work + hustle lanes.

Source of truth = config/users.toml (core.USERS) + the onboarding plan (accounts) +
core.profile_scope (work/life source tokens). Never invents identity.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class TenantProfile:
    user_id: str                      # users.toml key (e.g. "partner")
    tenant_id: str                    # onboarding tenant (e.g. "Operator2")
    display_name: str
    first_name: str
    voice_profile: str                # named voice spec (Phase D derives the real one)
    review_queue: Path                # her own draft review queue (draft-only fence)
    brain_scope: str                  # profile_scope key for source separation
    inbox_bot_enabled: bool
    accounts: list[dict] = field(default_factory=list)   # from onboarding plan_for(tenant)
    raw: dict = field(default_factory=dict)

    @property
    def is_owner(self) -> bool:
        return self.raw.get("role", "") == "owner"


def _expand(p: str) -> Path:
    return Path(str(p)).expanduser() if p else (ROOT / "data" / "runtime" / "review_queue.jsonl")


def _first_name(display: str) -> str:
    return (display or "").strip().split()[0] if display else ""


def load(user_id: str) -> TenantProfile | None:
    """Load a tenant profile by users.toml key. Returns None if unknown."""
    try:
        from core import USERS
    except Exception:
        USERS = {}
    prof = USERS.get(str(user_id or "").strip()) if isinstance(USERS, dict) else None
    if not prof:
        return None
    tenant_id = str(prof.get("onboarding_tenant") or user_id).strip().lower()
    display = str(prof.get("display_name") or user_id)
    # FIREWALL: a non-owner tenant's review queue is FORCED under its own sandbox
    # (data/tenants/<tenant>/), never a loose home-dir path — so it can never collide with
    # the owner's or another tenant's. The users.toml value is ignored for tenants.
    from core import tenant_firewall as fw
    is_owner = (prof.get("role") == "owner") or fw.is_owner_identity(tenant_id) or fw.is_owner_identity(user_id)
    if is_owner:
        review_queue = _expand(prof.get("review_queue") or "")
    else:
        review_queue = fw.scoped_path(tenant_id, "review_queue.jsonl")
    # accounts from the onboarding plan (best-effort; empty if wizard unavailable)
    accounts: list[dict] = []
    try:
        from tools import onboarding_wizard as wiz
        accounts = list(wiz.plan_for(tenant_id))
    except Exception:
        pass
    return TenantProfile(
        user_id=str(user_id),
        tenant_id=tenant_id,
        display_name=display,
        first_name=_first_name(display),
        voice_profile=str(prof.get("voice_profile") or "neutral_warm"),
        review_queue=review_queue,
        brain_scope=str(prof.get("brain_scope") or tenant_id),
        inbox_bot_enabled=bool(prof.get("inbox_bot_enabled")),
        accounts=accounts,
        raw=dict(prof),
    )


def load_by_tenant(tenant_id: str) -> TenantProfile | None:
    """Load by onboarding tenant id (e.g. 'Operator2') by scanning users.toml."""
    try:
        from core import USERS
    except Exception:
        return None
    tid = str(tenant_id or "").strip().lower()
    for uid, prof in (USERS or {}).items():
        if str(prof.get("onboarding_tenant") or uid).strip().lower() == tid:
            return load(uid)
    return None


def inbox_enabled_tenants() -> list[TenantProfile]:
    """Every tenant that has opted into the inbox agent (drives the per-tenant loop)."""
    out = []
    try:
        from core import USERS
    except Exception:
        return out
    for uid, prof in (USERS or {}).items():
        if prof.get("inbox_bot_enabled"):
            p = load(uid)
            if p:
                out.append(p)
    return out
