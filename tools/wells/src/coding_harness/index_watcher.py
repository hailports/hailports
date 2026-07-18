"""Background file-system watcher that keeps the wells-index current.

Watches WORKSPACE_ROOT for source-file changes, debounces rapid saves (e.g.
editor auto-save, formatter runs), and fires an incremental engine.index()
call once things settle.  Only files whose BLAKE3 hash changed are re-parsed
by the Rust engine, so the update is near-instant for typical dev workflows
(a handful of changed files at a time).

Usage:
    from coding_harness import index_watcher
    index_watcher.start(workspace)   # call once at Wells startup
    index_watcher.stop()             # call on shutdown (optional — daemon thread)
    index_watcher.is_active()        # True while watcher is running
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

_log = logging.getLogger(__name__)

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler, FileSystemEvent
    _WATCHDOG = True
except ImportError:  # graceful fallback: watcher is a no-op
    _WATCHDOG = False

# Source-file extensions the index engine understands.
_WATCHED_EXT = {
    ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx",
    ".go", ".rs", ".java", ".c", ".h",
    ".cpp", ".cc", ".cxx", ".hpp", ".hh",
}

# Directories that should never trigger re-indexing.
_SKIP_DIRS = {
    ".git", ".wells_index", "__pycache__", "node_modules",
    "target", ".venv", "venv", "dist", "build", ".tox", ".mypy_cache",
}

# Shared lock so the watcher thread and the main thread never index in parallel.
_index_lock = threading.Lock()

_observer: Optional[object] = None
_active = False


# ---------------------------------------------------------------------------
# Internal handler
# ---------------------------------------------------------------------------

class _Handler(FileSystemEventHandler):
    def __init__(self, workspace: str, debounce: float):
        self._workspace = workspace
        self._debounce = debounce
        self._timer: Optional[threading.Timer] = None
        self._tlock = threading.Lock()

    def _relevant(self, path: str) -> bool:
        from pathlib import Path
        p = Path(path)
        for part in p.parts:
            if part in _SKIP_DIRS or (part.startswith(".") and part != "."):
                return False
        return p.suffix.lower() in _WATCHED_EXT

    def on_any_event(self, event: "FileSystemEvent") -> None:
        if event.is_directory:
            return
        src = str(getattr(event, "src_path", ""))
        dest = str(getattr(event, "dest_path", ""))  # for moves
        if not (self._relevant(src) or (dest and self._relevant(dest))):
            return
        self._schedule()

    def _schedule(self) -> None:
        with self._tlock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self._debounce, self._reindex)
            self._timer.daemon = True
            self._timer.start()

    def _reindex(self) -> None:
        try:
            from coding_harness.index_tools import INDEXER_AVAILABLE
            if not INDEXER_AVAILABLE:
                return
            from wells_index import IndexEngine
            with _index_lock:
                engine = IndexEngine(self._workspace)
                stats = engine.index()
            changed = stats.get("indexed_files", stats.get("changed_files", "?"))
            total_sym = stats.get("total_symbols", "?")
            if changed and changed != "?" and int(str(changed)) > 0:
                print(f"[dim][index] {changed} file(s) re-indexed · {total_sym:,} symbols total[/dim]")
        except Exception as exc:
            _log.debug("index_watcher reindex error: %s", exc)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def start(workspace: str, debounce: float = 1.5) -> bool:
    """Start the background watcher for *workspace*.

    Returns True if the watcher is now running, False if watchdog is not
    installed (degraded: index is updated at task-start instead).
    Idempotent — safe to call multiple times.
    """
    global _observer, _active

    if not _WATCHDOG:
        return False

    if _observer is not None:
        return True  # already running

    handler = _Handler(workspace, debounce)
    obs = Observer()
    obs.schedule(handler, str(workspace), recursive=True)
    obs.daemon = True
    obs.start()
    _observer = obs
    _active = True
    return True


def stop() -> None:
    """Stop the watcher (called on clean shutdown; optional for daemon threads)."""
    global _observer, _active
    if _observer is not None:
        try:
            _observer.stop()  # type: ignore[union-attr]
            _observer.join(timeout=2.0)  # type: ignore[union-attr]
        except Exception:
            pass
        _observer = None
    _active = False


def is_active() -> bool:
    """True while the background watcher thread is running."""
    return _active


def lock() -> threading.Lock:
    """The shared lock for index operations.

    Acquire this before calling engine.index() from the main thread when the
    watcher might also be running, to avoid concurrent SQLite writes.
    """
    return _index_lock
