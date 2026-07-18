"""Workspace confinement + safety policy for the tool layer.

Every tool operation that touches the filesystem or runs a shell command goes
through :func:`resolve_path` (path confinement) and the safety policy gate
(:func:`gate`). This is the single chokepoint that keeps the agent inside the
workspace root and applies the auto/approve/dryrun policy selected by
``HARNESS_SAFETY``.

Confinement rules:
  * All filesystem paths are resolved relative to WORKSPACE_ROOT and must stay
    inside it (symlinks resolved; ``..`` escapes blocked).
  * Shell commands run with WORKSPACE_ROOT as cwd.
  * A configurable blocklist of command patterns is always refused.

Safety policy (``HARNESS_SAFETY``):
  * ``auto``    — execute immediately (default).
  * ``approve`` — call the approval callback; if none is wired, fall back to
                  dry-run so nothing destructive happens unattended.
  * ``dryrun``  — never execute; the tool returns a description of what it
                  *would* do (used by plan mode and the MCP ``review`` paths).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

# A callable that receives (action, detail) and returns True to approve.
# When None, ``approve`` policy degrades to dry-run for safety.
Approver = Callable[[str, str], bool] | None

_APPROVER: Approver = None


def set_approver(approver: Approver) -> None:
    """Register a process-wide approval callback (used by ``approve`` policy)."""
    global _APPROVER
    _APPROVER = approver


def get_approver() -> Approver:
    return _APPROVER


@dataclass
class SafetyDecision:
    """Outcome of a safety gate check."""

    allowed: bool
    mode: str  # auto | approve | dryrun
    simulated: bool  # True when we did NOT actually perform the action
    reason: str = ""

    @property
    def dry_run(self) -> bool:
        return self.simulated


class PathEscapeError(PermissionError):
    """Raised when a requested path resolves outside the workspace root."""


class BlockedCommandError(PermissionError):
    """Raised when a shell command matches the blocklist."""


# ---------------------------------------------------------------------------
# Path confinement
# ---------------------------------------------------------------------------


def workspace_root(workspace: str | None = None) -> Path:
    """Resolve the effective workspace root."""
    root = (workspace or os.environ.get("WORKSPACE_ROOT") or os.getcwd()).strip()
    return Path(root).resolve()


def resolve_path(path: str, workspace: str | None = None) -> Path:
    """Resolve ``path`` relative to the workspace root and enforce confinement.

    Relative paths and ``~`` are resolved under the workspace. Existing symlinks
    are resolved (non-strict, so new-file paths also work); escapes via ``..`` or
    links pointing outside the root raise :class:`PathEscapeError`. Confinement
    is checked on the *normalized* path regardless of existence, so a request
    for ``../../../etc/passwd`` is refused even when that path doesn't exist.
    """
    if not path or not path.strip():
        raise ValueError("path is required")

    root = workspace_root(workspace)
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = root / p

    # Non-strict resolve: normalizes `..`, resolves existing symlinks, never
    # raises for non-existent targets. This is what we want for confinement.
    resolved = p.resolve()

    # Confinement check: resolved must be the root itself or under it.
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise PathEscapeError(
            f"Path {path!r} resolves outside the workspace root ({resolved} not "
            f"under {root}). Tool operations are confined to the workspace."
        ) from exc
    return resolved


# ---------------------------------------------------------------------------
# Command screening
# ---------------------------------------------------------------------------


def _compiled_blocklist() -> list[re.Pattern]:
    raw = os.environ.get(
        "BLOCKED_COMMANDS",
        r"rm\s+-rf\s+/|mkfs|dd\s+if=|:\(\)\s*\{|shutdown|reboot",
    )
    out = []
    for piece in raw.split("|"):
        s = piece.strip()
        if not s:
            continue
        try:
            out.append(re.compile(s))
        except re.error:
            out.append(re.compile(re.escape(s)))
    return out


def screen_command(command: str) -> None:
    """Refuse ``command`` if it matches the blocklist."""
    for pat in _compiled_blocklist():
        if pat.search(command):
            raise BlockedCommandError(
                f"Command refused (matches blocked pattern {pat.pattern!r}): {command}"
            )


# ---------------------------------------------------------------------------
# Safety gate
# ---------------------------------------------------------------------------


def policy(workspace_or_safety: str | None = None) -> str:
    """Read the effective safety policy."""
    v = (
        (workspace_or_safety or os.environ.get("HARNESS_SAFETY") or "auto")
        .strip()
        .lower()
    )
    return v if v in ("auto", "approve", "dryrun") else "auto"


def gate(
    action: str,
    detail: str,
    *,
    safety: str | None = None,
    approver: Approver = None,
    simulated_default: bool = False,
) -> SafetyDecision:
    """Decide whether ``action`` (with ``detail``) may proceed.

    ``action`` is a short verb like ``"write_file"`` / ``"run_command"``;
    ``detail`` is a human description (path or command). The decision honours
    the configured policy and any registered/local approver.
    """
    mode = policy(safety)
    if mode == "auto":
        return SafetyDecision(allowed=True, mode=mode, simulated=False)
    if mode == "dryrun":
        return SafetyDecision(
            allowed=False,
            mode=mode,
            simulated=True,
            reason=f"[dry-run] would {action}: {detail}",
        )
    # approve
    ap = approver if approver is not None else _APPROVER
    if ap is None:
        # No approver wired: degrade to dry-run so nothing destructive happens.
        return SafetyDecision(
            allowed=False,
            mode=mode,
            simulated=True,
            reason=f"[no-approver; dry-run] would {action}: {detail}",
        )
    approved = bool(ap(action, detail))
    return SafetyDecision(
        allowed=approved,
        mode=mode,
        simulated=not approved,
        reason="" if approved else f"[denied by approver] {action}: {detail}",
    )
