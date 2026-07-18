#!/usr/bin/env python3
"""Per-tenant secret namespacing — the isolation primitive for onboarding.

Every onboarded tenant (Operator2, a future client, etc.) gets its own namespace so
one tenant's credentials can never collide with or leak into another's. Secrets
live in the login Keychain (encrypted at rest, see core/secret_store) under a
service name of the form:

    tenant__{tenant_id}__{account}__{field}

plus a per-tenant on-disk directory (data/tenants/<tenant_id>/, chmod 700) that
holds captured browser sessions (auth.json) and a small index of which
(account, field) secrets exist — the index is what lets offboard() enumerate and
delete an entire namespace deterministically. Values are NEVER written to the
index or logged; only their keychain service names.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from core.secret_store import _KC_ACCOUNT, get_secret, set_secret

ROOT = Path(__file__).resolve().parent.parent
TENANTS_DIR = ROOT / "data" / "tenants"

_SAFE = re.compile(r"[^a-z0-9@._-]+")


def _norm(part: str) -> str:
    return _SAFE.sub("_", str(part or "").strip().lower()).strip("_") or "_"


def tenant_id_ok(tenant_id: str) -> bool:
    return bool(str(tenant_id or "").strip())


def ns_key(tenant_id: str, account: str, field: str) -> str:
    """Keychain service name for one tenant/account/field secret."""
    return f"tenant__{_norm(tenant_id)}__{_norm(account)}__{_norm(field)}"


# ---------------------------------------------------------------------------
# Per-tenant paths
# ---------------------------------------------------------------------------
def tenant_dir(tenant_id: str, *, create: bool = True) -> Path:
    p = TENANTS_DIR / _norm(tenant_id)
    if create:
        p.mkdir(parents=True, exist_ok=True)
        try:
            p.chmod(0o700)
        except Exception:
            pass
    return p


def sessions_dir(tenant_id: str, *, create: bool = True) -> Path:
    p = tenant_dir(tenant_id, create=create) / "sessions"
    if create:
        p.mkdir(parents=True, exist_ok=True)
        try:
            p.chmod(0o700)
        except Exception:
            pass
    return p


def session_path(tenant_id: str, account: str) -> Path:
    """auth.json path for a captured session (mirrors the login-capture pattern)."""
    return sessions_dir(tenant_id) / _norm(account) / "auth.json"


def _index_path(tenant_id: str) -> Path:
    return tenant_dir(tenant_id) / "secret_index.json"


def _load_index(tenant_id: str) -> list[list[str]]:
    p = _index_path(tenant_id)
    try:
        if p.exists():
            data = json.loads(p.read_text())
            if isinstance(data, list):
                return [list(x) for x in data if isinstance(x, (list, tuple)) and len(x) == 2]
    except Exception:
        pass
    return []


def _save_index(tenant_id: str, entries: list[list[str]]) -> None:
    p = _index_path(tenant_id)
    p.write_text(json.dumps(entries, indent=2, sort_keys=True))
    try:
        p.chmod(0o600)
    except Exception:
        pass


def _index_add(tenant_id: str, account: str, field: str) -> None:
    entries = _load_index(tenant_id)
    pair = [_norm(account), _norm(field)]
    if pair not in entries:
        entries.append(pair)
        _save_index(tenant_id, entries)


def _index_remove(tenant_id: str, account: str, field: str) -> None:
    entries = _load_index(tenant_id)
    pair = [_norm(account), _norm(field)]
    remaining = [e for e in entries if e != pair]
    if remaining != entries:
        _save_index(tenant_id, remaining)


# ---------------------------------------------------------------------------
# Secret store / get / delete (keychain-backed, per-tenant namespaced)
# ---------------------------------------------------------------------------
def set_tenant_secret(tenant_id: str, account: str, field: str, value: str) -> bool:
    """Encrypt-at-rest a tenant secret in the login keychain. Never logs the value."""
    if not (tenant_id_ok(tenant_id) and account and field):
        return False
    ok = set_secret(ns_key(tenant_id, account, field), str(value or ""))
    if ok:
        _index_add(tenant_id, account, field)
    return ok


def get_tenant_secret(tenant_id: str, account: str, field: str) -> str:
    """Read a tenant secret. file_fallback is OFF — tenant secrets live only in keychain."""
    return get_secret(ns_key(tenant_id, account, field), file_fallback=False)


def _keychain_delete(service: str) -> bool:
    try:
        r = subprocess.run(
            ["security", "delete-generic-password", "-a", _KC_ACCOUNT, "-s", service],
            capture_output=True, text=True, timeout=10)
        return r.returncode == 0
    except Exception:
        return False


def delete_tenant_secret(tenant_id: str, account: str, field: str) -> bool:
    ok = _keychain_delete(ns_key(tenant_id, account, field))
    _index_remove(tenant_id, account, field)
    return ok


def list_tenant_secrets(tenant_id: str) -> list[tuple[str, str]]:
    """(account, field) pairs known for this tenant. Values are never returned here."""
    return [(a, f) for a, f in _load_index(tenant_id)]


def delete_tenant_namespace(tenant_id: str) -> int:
    """Delete every keychain secret in this tenant's namespace. Returns count removed."""
    removed = 0
    for account, field in list_tenant_secrets(tenant_id):
        if _keychain_delete(ns_key(tenant_id, account, field)):
            removed += 1
    _save_index(tenant_id, [])
    return removed


def purge_tenant_dir(tenant_id: str) -> bool:
    """Remove the tenant's on-disk directory (sessions/auth.json + index)."""
    import shutil
    p = tenant_dir(tenant_id, create=False)
    try:
        if p.exists():
            shutil.rmtree(p)
        return True
    except Exception:
        return False


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3 and sys.argv[1] == "list":
        for a, f in list_tenant_secrets(sys.argv[2]):
            print(f"{a}\t{f}")
