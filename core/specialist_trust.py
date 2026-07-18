"""Fail-closed trust boundary for specialist-generated draft material."""

from __future__ import annotations

from typing import Any


_KINDS = frozenset({"action_readback", "sandbox_validation", "readonly_recompute"})


def manifest(kind: str) -> dict:
    if kind not in _KINDS:
        raise ValueError(f"unsupported verification kind: {kind}")
    return {
        "version": 1,
        "issuer": "specialist_dispatch",
        "source": "deterministic",
        "machine_verified": True,
        "kind": kind,
    }


def valid_manifest(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and value.get("version") == 1
        and value.get("issuer") == "specialist_dispatch"
        and value.get("source") == "deterministic"
        and value.get("machine_verified") is True
        and value.get("kind") in _KINDS
    )


def trusted_draft_material(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and value.get("verified") is True
        and valid_manifest(value.get("verification_manifest"))
        and bool(value.get("summary") or value.get("proof"))
    )


def fail_closed(result: Any) -> Any:
    """Downgrade any unproven `verified` claim before it reaches drafting or cache replay."""
    if not isinstance(result, dict):
        return result
    draft_material = result.get("draft_material")
    if not isinstance(draft_material, dict) or draft_material.get("verified") is not True:
        return result
    if trusted_draft_material(draft_material):
        return result
    safe = dict(result)
    safe_material = dict(draft_material)
    safe_material.pop("verification_manifest", None)
    safe_material.update({"verified": False, "mode": "propose", "band": "staged"})
    safe["draft_material"] = safe_material
    safe["needs_alex"] = True
    return safe
