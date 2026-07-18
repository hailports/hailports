#!/usr/bin/env python3
"""Browser Pool - serialize Playwright browser work across agents.

Multiple revenue agents launching Chromium at once can exhaust RAM and make the
Mini unstable. This file lock keeps browser sessions one-at-a-time, and cleans
up stale owners when a process is alive but no real browser is still running.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import signal
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

log = logging.getLogger("browser-pool")

_LOCK_DIR = Path.home() / "claude-stack" / "data" / "runtime"
LOCK_FILE = _LOCK_DIR / "browser_pool.lock"  # legacy compat
MAX_BROWSER_LANES = int(os.environ.get("BROWSER_POOL_LANES", "3"))
LOCK_FILES = [_LOCK_DIR / f"browser_pool_lane{i}.lock" for i in range(MAX_BROWSER_LANES)]
LOCK_TIMEOUT = int(os.environ.get("BROWSER_POOL_LOCK_TIMEOUT", "1800"))
MAX_WAIT = int(os.environ.get("BROWSER_POOL_MAX_WAIT", "1800"))
HARD_HOLDER_LIMIT = int(os.environ.get("BROWSER_POOL_HARD_HOLDER_LIMIT", "7200"))

REAL_BROWSER_PATTERNS = (
    "chrome-headless-shell",
    "Chromium.app/Contents",
    "Google Chrome for Testing",
    "Contents/MacOS/Chromium",
)
PLAYWRIGHT_SUPPORT_PATTERNS = (
    "ms-playwright",
    "playwright/driver",
)
BROWSER_PATTERNS = REAL_BROWSER_PATTERNS + PLAYWRIGHT_SUPPORT_PATTERNS


def _candidate_lock_files() -> list[Path]:
    """Return active lock lanes, preserving legacy LOCK_FILE test/agent overrides."""
    if LOCK_FILE.parent != _LOCK_DIR or LOCK_FILE.name != "browser_pool.lock":
        return [LOCK_FILE]
    return LOCK_FILES or [LOCK_FILE]


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def _parse_etime(value: str) -> int:
    """Parse macOS ps etime strings into seconds."""
    try:
        days = 0
        rest = value.strip()
        if "-" in rest:
            day_s, rest = rest.split("-", 1)
            days = int(day_s)
        parts = [int(p) for p in rest.split(":")]
        if len(parts) == 3:
            hours, minutes, seconds = parts
        elif len(parts) == 2:
            hours = 0
            minutes, seconds = parts
        else:
            hours = 0
            minutes = 0
            seconds = parts[0] if parts else 0
        return days * 86400 + hours * 3600 + minutes * 60 + seconds
    except Exception:
        return 0


def _process_table() -> dict[int, dict[str, Any]]:
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,etime=,command="],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return {}
    rows: dict[int, dict[str, Any]] = {}
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 3)
        if len(parts) < 4:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except ValueError:
            continue
        rows[pid] = {
            "pid": pid,
            "ppid": ppid,
            "age_s": _parse_etime(parts[2]),
            "command": parts[3],
        }
    return rows


def _descends_from(pid: int, ancestor_pid: int, rows: dict[int, dict[str, Any]]) -> bool:
    seen: set[int] = set()
    cur = pid
    while cur and cur not in seen:
        if cur == ancestor_pid:
            return True
        seen.add(cur)
        row = rows.get(cur)
        if not row:
            return False
        cur = int(row.get("ppid") or 0)
    return False


def _active_lock_pid() -> int | None:
    try:
        raw = LOCK_FILE.read_text().strip()
        if not raw:
            return None
        if raw.isdigit():
            pid = int(raw)
        else:
            pid = int((json.loads(raw) or {}).get("pid") or 0)
        return pid if pid > 0 and _pid_alive(pid) else None
    except Exception:
        return None


def _lock_holders() -> list[int]:
    try:
        result = subprocess.run(
            ["lsof", "-t", str(LOCK_FILE)],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return []
    holders: list[int] = []
    for line in result.stdout.splitlines():
        try:
            pid = int(line.strip())
        except ValueError:
            continue
        if pid > 0:
            holders.append(pid)
    return holders


def _has_descendant_matching(
    pid: int,
    rows: dict[int, dict[str, Any]],
    patterns: tuple[str, ...],
) -> bool:
    for child_pid, row in rows.items():
        command = str(row.get("command") or "")
        if not any(pattern in command for pattern in patterns):
            continue
        if _descends_from(child_pid, pid, rows):
            return True
    return False


def _kill_process_tree(pid: int, rows: dict[int, dict[str, Any]]) -> list[int]:
    descendants = [
        child_pid for child_pid in rows
        if child_pid != pid and _descends_from(child_pid, pid, rows)
    ]
    killed: list[int] = []
    for target in sorted(descendants, key=lambda child: int(rows[child].get("age_s") or 0)):
        try:
            os.kill(target, signal.SIGTERM)
            killed.append(target)
        except Exception:
            pass
    try:
        os.kill(pid, signal.SIGTERM)
        killed.append(pid)
    except Exception:
        pass
    time.sleep(2)
    for target in killed:
        if _pid_alive(target):
            try:
                os.kill(target, signal.SIGKILL)
            except Exception:
                pass
    return killed


def _break_stale_lock_holder(
    max_age_s: int = LOCK_TIMEOUT,
    hard_kill_after_s: int = HARD_HOLDER_LIMIT,
) -> list[int]:
    """Terminate stale lock holders.

    A Playwright driver process can stay alive after Chromium is gone. The old
    implementation treated that driver as proof of active browser work, which
    let a dead session block SalesIntel for hours. We only treat a real Chromium
    descendant as active, and even that gets a hard cap.
    """
    rows = _process_table()
    killed: list[int] = []
    for pid in _lock_holders():
        if pid == os.getpid():
            continue
        row = rows.get(pid)
        if not row:
            continue
        age_s = int(row.get("age_s") or 0)
        if age_s < max_age_s:
            continue
        has_real_browser = _has_descendant_matching(pid, rows, REAL_BROWSER_PATTERNS)
        if has_real_browser and age_s < hard_kill_after_s:
            continue
        reason = "hard limit" if has_real_browser else "driver-only/no-browser"
        log.warning(
            "Terminating stale browser lock holder pid=%d age_s=%d reason=%s",
            pid,
            age_s,
            reason,
        )
        killed.extend(_kill_process_tree(pid, rows))
    return killed


@contextmanager
def browser_lock(
    owner: str = "",
    *,
    max_wait_s: int | None = None,
    stale_after_s: int | None = None,
    hard_kill_after_s: int | None = None,
):
    """Acquire exclusive browser access for Playwright work."""
    wait_limit = MAX_WAIT if max_wait_s is None else max(1, int(max_wait_s))
    stale_limit = LOCK_TIMEOUT if stale_after_s is None else max(1, int(stale_after_s))
    hard_limit = HARD_HOLDER_LIMIT if hard_kill_after_s is None else max(stale_limit, int(hard_kill_after_s))

    for lane_file in _candidate_lock_files():
        lane_file.parent.mkdir(parents=True, exist_ok=True)
    cleanup_zombies(max_age_s=600)

    start = time.monotonic()
    acquired = False
    fd = None
    acquired_lock_file = None
    last_stale_check = 0.0

    while time.monotonic() - start < wait_limit:
        # Try each lane — first free one wins
        candidate_files = _candidate_lock_files()
        for lane_file in candidate_files:
            try:
                fd = open(lane_file, "a+")
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fd.seek(0)
                fd.truncate()
                fd.write(json.dumps({
                    "pid": os.getpid(),
                    "owner": owner or os.environ.get("LAUNCHD_JOB_LABEL") or "unknown",
                    "started_at": time.time(),
                    "lane": lane_file.name,
                }))
                fd.flush()
                os.utime(lane_file, None)
                acquired = True
                acquired_lock_file = lane_file
                log.debug("Browser lock acquired owner=%s pid=%d lane=%s", owner, os.getpid(), lane_file.name)
                break
            except (IOError, OSError):
                if fd:
                    fd.close()
                    fd = None
                continue

        if acquired:
            break

        now = time.monotonic()
        if now - last_stale_check >= 60:
            last_stale_check = now
            _break_stale_lock_holder(stale_limit, hard_limit)
        time.sleep(5 + (time.monotonic() % 10))

    if not acquired:
        log.error("Failed to acquire browser lock after %ds owner=%s (all %d lanes busy)", wait_limit, owner, len(_candidate_lock_files()))
        raise TimeoutError("Browser pool lock timeout — all lanes busy")

    try:
        yield
    finally:
        if fd:
            try:
                fd.seek(0)
                fd.truncate()
                fcntl.flock(fd, fcntl.LOCK_UN)
                fd.close()
            except Exception:
                pass
        cleanup_zombies(max_age_s=60)
        log.debug("Browser lock released owner=%s pid=%d", owner, os.getpid())


def cleanup_zombies(max_age_s: int = 600, force: bool = False) -> dict[str, Any]:
    """Kill orphaned Playwright/Chromium processes without touching the active owner tree."""
    rows = _process_table()
    active_owner = None if force else _active_lock_pid()
    killed: list[int] = []
    skipped_active = 0
    candidates: list[int] = []

    for pid, row in rows.items():
        command = str(row.get("command") or "")
        if not any(pattern in command for pattern in BROWSER_PATTERNS):
            continue
        if active_owner and _descends_from(pid, active_owner, rows):
            skipped_active += 1
            continue
        if not force and int(row.get("age_s") or 0) < max_age_s:
            continue
        candidates.append(pid)

    for pid in sorted(candidates, key=lambda p: int(rows[p].get("age_s") or 0), reverse=True):
        try:
            os.kill(pid, signal.SIGTERM)
            killed.append(pid)
        except Exception:
            pass
    if killed:
        time.sleep(1)
        for pid in killed:
            if _pid_alive(pid):
                try:
                    os.kill(pid, signal.SIGKILL)
                except Exception:
                    pass
        log.info("Cleaned up browser zombies: %s", killed)

    return {"killed": killed, "skipped_active": skipped_active, "active_owner": active_owner}
