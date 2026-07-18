"""single_instance(name) — flock-based singleton guard for launchd-scheduled tools.

A job whose run outlives its StartInterval (or that gets double-spawned) leaves two
live instances. runaway_guard reads that as a pile-up and reaps the older one
(SIGTERM -> bash exits 143 -> launchd_health flags a false 'crash', and chronic
reaping escalates to QUARANTINE, disabling the job). Guarding the entrypoint means
a second spawn exits 0 immediately instead of stacking. Same idiom proven in
core.pii_guard._acquire_singleton / core.hive.

Usage:
    from core.singleton import single_instance
    lock = single_instance("work_ledger")
    if lock is None:
        sys.exit(0)            # another instance owns the slot; nothing to do
    ...                        # lock auto-releases when the process exits
"""
from __future__ import annotations

import fcntl
import os
from pathlib import Path

_LOCK_DIR = Path.home() / ".stack-locks"


def single_instance(name: str):
    """Return an open file handle holding an exclusive lock, or None if another
    instance already holds it. Keep the returned handle alive for the process's
    lifetime — the OS releases the lock on exit even if we crash."""
    _LOCK_DIR.mkdir(parents=True, exist_ok=True)
    fh = open(_LOCK_DIR / f"{name}.lock", "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    try:
        fh.write(str(os.getpid()))
        fh.flush()
    except OSError:
        pass
    return fh
