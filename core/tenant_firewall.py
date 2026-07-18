#!/usr/bin/env python3
"""Tenant firewall — HARD, fail-closed isolation between tenants (and from the owner).

Requirement (Operator 2026-07-16): a tenant's work brain / inbox / data must be COMPLETELY
firewalled from the owner's and from every other tenant — zero chance of drift. This module
is the single enforcement point. It is fail-closed: any access that can't be PROVEN in-bounds
raises TenantFirewallError. Every per-tenant agent routes its file IO + identity checks here.

Two-way isolation:
  * A tenant agent may only ever touch paths under data/tenants/<its-own-tenant>/ (0700).
  * It may NEVER be run as the owner, and NEVER touch owner/other-tenant paths (the owner's
    work_context.db, his local digests, his review queue, or another tenant's dir).
  * The owner's own pipeline lives outside data/tenants/ and never looks inside it, so the
    reverse direction is satisfied structurally + asserted here.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

OWNER_TENANTS = {"Operator", "owner", "Operator"}  # pii-allow: owner-identity anchor / root of trust for the boundary
OWNER_USERS = {"Operator", "owner"}

# Paths a tenant agent must NEVER read or write — the owner's brain + work lane + anything
# outside the tenant sandbox. Checked by resolved-prefix, so no traversal can slip past.
_OWNER_FORBIDDEN = [
    (ROOT / "data" / "work_context.db"),
    (ROOT / "data" / "hustle"),
    (Path.home() / ".openclaw" / "workspace" / "CompanyA-local"),  # pii-allow: real owner data path
    (Path.home() / "OneDrive-redactedIndustries, Inc"),  # pii-allow: real owner data path
]


class TenantFirewallError(RuntimeError):
    """Raised on any cross-tenant / owner-boundary violation. Never caught-and-ignored."""


def _norm(x) -> str:
    return str(x or "").strip().lower()


def is_owner_identity(x) -> bool:
    return _norm(x) in OWNER_TENANTS or _norm(x) in OWNER_USERS


def assert_tenant(tenant_id: str) -> str:
    """A per-tenant agent may ONLY run as a real, non-owner tenant. Fail-closed."""
    t = _norm(tenant_id)
    if not t:
        raise TenantFirewallError("empty tenant id — refusing to run a tenant agent with no identity")
    if is_owner_identity(t):
        raise TenantFirewallError(f"'{tenant_id}' is the OWNER — tenant agents must never run as the owner")
    return t


def tenant_root(tenant_id: str, *, create: bool = True) -> Path:
    """The ONLY root a tenant agent may touch: data/tenants/<tenant>/ (aligned with
    tenant_secrets, 0700). Owner ids are refused."""
    t = assert_tenant(tenant_id)
    try:
        from tools import tenant_secrets
        root = tenant_secrets.tenant_dir(t, create=create)
    except Exception:
        root = ROOT / "data" / "tenants" / t
        if create:
            root.mkdir(parents=True, exist_ok=True)
            try:
                root.chmod(0o700)
            except Exception:
                pass
    return Path(root).resolve()


def guard_path(tenant_id: str, path) -> Path:
    """Return the resolved path IFF it is inside this tenant's sandbox AND not inside any
    owner-forbidden root. Otherwise raise. Use for EVERY tenant file read/write."""
    t = assert_tenant(tenant_id)
    p = Path(path).resolve()
    root = tenant_root(t)
    # must be within this tenant's own sandbox
    try:
        p.relative_to(root)
    except ValueError:
        raise TenantFirewallError(f"path {p} is OUTSIDE tenant '{t}' sandbox {root} — blocked")
    # belt: never inside an owner-forbidden root (defends against a mis-set tenant_root)
    for forbidden in _OWNER_FORBIDDEN:
        try:
            p.relative_to(forbidden.resolve())
            raise TenantFirewallError(f"path {p} is inside owner-forbidden root {forbidden} — blocked")
        except ValueError:
            continue
    return p


def assert_isolated(a: str, b: str) -> None:
    """Two identities that must never be the same lane (e.g. a tenant vs the owner)."""
    if _norm(a) == _norm(b):
        raise TenantFirewallError(f"identity collision: '{a}' == '{b}' — firewall breach")
    if is_owner_identity(a) and is_owner_identity(b):
        raise TenantFirewallError("both identities are owner — not a tenant boundary")


def scoped_path(tenant_id: str, *parts: str) -> Path:
    """Build + guard a path under the tenant sandbox in one call (dirs created)."""
    p = tenant_root(tenant_id).joinpath(*parts)
    p.parent.mkdir(parents=True, exist_ok=True)
    return guard_path(tenant_id, p)
