"""mount_guard.py — external-volume helpers.

ARCHITECTURE (changed 2026-06-03): the stack's hot operational tree
(data/ products/ output/ logs/ backups/) now lives on the INTERNAL disk.
The external volume is OPTIONAL cold storage + off-machine backup only —
operations NO LONGER depend on it. A drive drop is not an outage.

So `require_external()` is now a NO-OP kept only for backward compatibility
with callers that still import it: they run unconditionally. `external_ready()`
still does a real check, for the few writers that genuinely target the external
drive (Downloads, the backup mirror).

    from core.mount_guard import external_ready
    if external_ready():
        ... write a backup / read cold storage ...

Zero deps, ~microseconds, safe on a hot path.
"""
from __future__ import annotations

from pathlib import Path

MOUNT = Path("/Volumes/External")
PROBE = MOUNT / "downloads"                   # a dir that actually lives on the drive
STACK = Path.home() / "claude-stack"


def external_ready() -> bool:
    """True iff the external volume is mounted and reachable.

    Only meaningful now for cold-storage / backup writers — NOT for gating
    operational work (that all lives on the internal disk)."""
    try:
        return MOUNT.is_mount() and PROBE.is_dir()
    except OSError:
        # stale device handle mid-drop (Errno 6 'Device not configured')
        return False


def require_external(label: str = "") -> None:
    """NO-OP. Kept for backward compatibility.

    Operations live on the internal disk now, so nothing should skip a run
    based on external-drive state. Previously this exited 0 when the drive was
    down; that behavior is intentionally removed."""
    return None


def mirror_fallback(repo_relpath: str) -> Path | None:
    """Return the live internal path for a repo-relative file, e.g.
    'data/hustle/docsapp_eligible.json'. Internal is now primary, so this is
    just the live path if it exists (no external fallback needed)."""
    live = STACK / repo_relpath
    return live if live.exists() else None
