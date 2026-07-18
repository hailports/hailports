"""Firewall-guarded WebUI data access for non-owner tenants (e.g. Operator2).

Historically the WebUI read Operator2's state from loose files at data/nicole_*.json
with a generic "partner" user_id and NO firewall check. That let her data live
outside her sandbox and share a lane with the owner's.

This module is the single WebUI entry point for a tenant's own state. Every path
it returns is produced by ``core.tenant_firewall.guard_path(tenant, ...)``, so a
read/write can only ever resolve inside ``data/tenants/<tenant>/`` (0700) and is
fail-closed against owner/cross-tenant paths.

No secrets live here. The data-relocation from the legacy loose files is an
explicit, operator-run migration (see ``migrate_from_legacy``) — it is dry-run by
default and never runs implicitly at import.
"""
from __future__ import annotations

import json
from pathlib import Path

from core import tenant_firewall as fw

ROOT = Path(__file__).resolve().parent.parent

# Canonical tenant id for Operator's wife. NEVER the generic "partner"/"spouse".
Operator2 = "Operator2"

# Logical name -> (sandbox filename, legacy loose path). The sandbox filename is
# resolved through the firewall on every access; the legacy path is read-only and
# only consulted until the operator runs the migration.
_STATE_FILES = {
    "inbox_state": ("inbox_state.json", ROOT / "data" / "nicole_inbox_state.json"),
    "insights": ("insights.json", ROOT / "data" / "nicole_insights.json"),
    "exec_state": ("exec_state.json", ROOT / "data" / "nicole_exec_state.json"),
}


def tenant_state_path(tenant_id: str, name: str) -> Path:
    """Guarded absolute path for one piece of a tenant's WebUI state.

    Raises TenantFirewallError for owner ids or any path that would escape the
    tenant sandbox. This is the choke point acceptance test (a) asserts on.
    """
    fname = _STATE_FILES.get(name, (name, None))[0]
    # scoped_path builds under data/tenants/<tenant>/ and runs guard_path on it.
    return fw.scoped_path(tenant_id, fname)


def legacy_path(name: str) -> Path | None:
    return _STATE_FILES.get(name, (name, None))[1]


def nicole_state_path(name: str) -> Path:
    return tenant_state_path(Operator2, name)


def read_json(tenant_id: str, name: str, default):
    """Read a tenant state file from the sandbox, falling back read-only to the
    legacy loose file until migration runs. All resolution is firewall-guarded."""
    p = tenant_state_path(tenant_id, name)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            return default
    legacy = legacy_path(name)
    if legacy and legacy.exists():
        try:
            return json.loads(legacy.read_text())
        except Exception:
            return default
    return default


def write_json(tenant_id: str, name: str, obj) -> Path:
    """Write a tenant state file INSIDE the sandbox (guarded). Never writes to the
    legacy loose location."""
    p = tenant_state_path(tenant_id, name)
    p.write_text(json.dumps(obj, indent=2))
    return p


def migrate_from_legacy(tenant_id: str = Operator2, *, apply: bool = False) -> dict:
    """Move loose data/nicole_*.json into the tenant sandbox.

    OPERATOR-ONLY / dry-run by default. Pass apply=True to actually move files.
    Returns a report {name: {legacy, dest, action}}.
    """
    report: dict[str, dict] = {}
    for name, (_fname, legacy) in _STATE_FILES.items():
        dest = tenant_state_path(tenant_id, name)  # guarded
        if not legacy or not legacy.exists():
            report[name] = {"legacy": str(legacy), "dest": str(dest), "action": "skip:no-legacy"}
            continue
        if dest.exists():
            report[name] = {"legacy": str(legacy), "dest": str(dest), "action": "skip:dest-exists"}
            continue
        if apply:
            dest.write_text(legacy.read_text())
            legacy.unlink()
            action = "moved"
        else:
            action = "would-move"
        report[name] = {"legacy": str(legacy), "dest": str(dest), "action": action}
    return report
