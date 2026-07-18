#!/usr/bin/env python3
"""Bounded OneDrive/File Provider healer driven by the truthful work-sync marker."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DIGESTS = Path.home() / ".openclaw/workspace/CompanyA-local/digests"
MARKER = DIGESTS / ".work_sync.json"
STATE = ROOT / "data/runtime/onedrive_health_guard.json"

FAILURES_BEFORE_RESTART = max(2, int(os.environ.get("ONEDRIVE_GUARD_FAILURES", "2")))
RESTART_COOLDOWN_SECONDS = max(
    300.0, float(os.environ.get("ONEDRIVE_GUARD_RESTART_COOLDOWN", "1800"))
)
LAUNCH_COOLDOWN_SECONDS = max(
    60.0, float(os.environ.get("ONEDRIVE_GUARD_LAUNCH_COOLDOWN", "300"))
)
MARKER_MAX_AGE_SECONDS = max(
    300.0, float(os.environ.get("ONEDRIVE_GUARD_MARKER_MAX_AGE", "900"))
)
ISSUE_KEY = "onedrive-file-provider-health"

_IO_ERROR = re.compile(
    r"(?:\b(?:EAGAIN|EBUSY|ETIMEDOUT|EIO|ENXIO|ESTALE|ENOTCONN)\b"
    r"|\[Errno\s+(?:5|6|16|35|57|60|70)\]"
    r"|resource temporarily unavailable"
    r"|device not configured"
    r"|input/output error"
    r"|operation timed out"
    r"|(?:unknown )?system error -11"
    r"|file provider[^\n]*(?:unavailable|not responding|failed|busy|timed out))",
    re.IGNORECASE,
)
_POLICY_OR_CONTENT = re.compile(
    r"(?:surface (?:preflight|validation) failed|leak|refusing (?:an )?unmanaged|"
    r"destination is not inside|managed target escapes|work_index_missing|"
    r"notes marker missing|sync_already_running|\bfatal:|"
    r"local bundle (?:verification|branch coverage)|git bundle (?:create failed|timed out))",
    re.IGNORECASE,
)


def _reason_is_io(reason: Any) -> bool:
    text = str(reason or "").strip()
    if not text or _POLICY_OR_CONTENT.search(text):
        return False
    return text == "work_reference_missing" or bool(_IO_ERROR.search(text))


def collect_io_failures(
    *,
    project: dict[str, Any],
    generated: dict[str, Any],
    status_error: str | None,
    notes_output: str,
) -> list[dict[str, str]]:
    """Return only failures that a File Provider restart could plausibly heal."""
    candidates = [
        ("projects", (project.get("onedrive") or {}).get("reason")),
        ("generated", generated.get("reason")),
        ("brain_status", status_error),
        ("notes", notes_output),
    ]
    failures: list[dict[str, str]] = []
    for source, reason in candidates:
        if _reason_is_io(reason):
            failures.append({"source": source, "reason": str(reason).strip()[-500:]})
    return failures


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temp, path)


def record_supervisor_failure(
    reason: str,
    *,
    marker_path: Path = MARKER,
    source: str = "outer_supervisor",
    now: float | None = None,
) -> dict[str, Any]:
    """Atomically turn an outer timeout/lock refusal into a fresh I/O marker.

    The outer supervisor is the only process guaranteed to return when a File
    Provider syscall wedges. Preserve the last proven sync time, but never leave
    the prior successful marker wearing the costume of the failed attempt.
    """
    reason = str(reason or "").strip()
    if not _reason_is_io(reason):
        raise ValueError("supervisor failure must be a recognized OneDrive I/O failure")
    previous = _load_json(marker_path)
    moment = datetime.fromtimestamp(
        time.time() if now is None else float(now), timezone.utc
    )
    attempted = moment.isoformat(timespec="microseconds").replace("+00:00", "Z")
    if attempted == previous.get("attempted"):
        attempted = (moment + timedelta(microseconds=1)).isoformat(
            timespec="microseconds"
        ).replace("+00:00", "Z")
    marker = {
        **previous,
        "attempted": attempted,
        "synced": previous.get("synced"),
        "complete": False,
        "supervisor_error": reason[-500:],
        "io_failures": [{"source": source, "reason": reason[-500:]}],
    }
    _write_json(marker_path, marker)
    return marker


def record_if_io_failure(
    reason: str,
    *,
    marker_path: Path = MARKER,
    source: str = "outer_supervisor",
    now: float | None = None,
) -> dict[str, Any] | None:
    """Record only errors that a OneDrive/File Provider restart can heal."""
    if not _reason_is_io(reason):
        return None
    return record_supervisor_failure(
        reason,
        marker_path=marker_path,
        source=source,
        now=now,
    )


@contextmanager
def _locked_state(path: Path):
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = _load_json(path)
        state.setdefault("consecutive_io_failures", 0)
        yield state
        _write_json(path, state)
        fcntl.flock(lock, fcntl.LOCK_UN)


def _is_running() -> bool:
    result = subprocess.run(
        ["/usr/bin/pgrep", "-x", "OneDrive"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=5,
        check=False,
    )
    return result.returncode == 0


def _launch() -> tuple[bool, str]:
    result = subprocess.run(
        ["/usr/bin/open", "-ga", "OneDrive"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    detail = (result.stderr or result.stdout or "").strip()[-300:]
    return result.returncode == 0, detail


def _restart() -> tuple[bool, str]:
    stopped = subprocess.run(
        ["/usr/bin/pkill", "-TERM", "-x", "OneDrive"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if stopped.returncode not in (0, 1):
        return False, (stopped.stderr or stopped.stdout or "terminate failed").strip()[-300:]
    deadline = time.monotonic() + 8.0
    while _is_running() and time.monotonic() < deadline:
        time.sleep(0.25)
    if _is_running():
        return False, "OneDrive did not exit after TERM; refusing a force-kill"
    return _launch()


def _notify(subject: str, body: str, *, level: str = "warn", healed: bool = False) -> None:
    try:
        from core import alert_gateway

        alert_gateway.notify(
            "onedrive-health",
            subject,
            body,
            issue_key=ISSUE_KEY,
            level=level,
            healed=healed,
        )
    except Exception:
        pass


def _timestamp(value: Any) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def run_guard(
    *,
    marker_path: Path = MARKER,
    state_path: Path = STATE,
    now: float | None = None,
    ensure_running_only: bool = False,
    is_running: Callable[[], bool] | None = None,
    launch: Callable[[], tuple[bool, str]] | None = None,
    restart: Callable[[], tuple[bool, str]] | None = None,
    notify: Callable[..., None] | None = None,
    failures_before_restart: int = FAILURES_BEFORE_RESTART,
    restart_cooldown: float = RESTART_COOLDOWN_SECONDS,
    launch_cooldown: float = LAUNCH_COOLDOWN_SECONDS,
    marker_max_age: float = MARKER_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    now = time.time() if now is None else float(now)
    is_running = is_running or _is_running
    launch = launch or _launch
    restart = restart or _restart
    notify = notify or _notify

    with _locked_state(state_path) as state:
        state["last_checked_at"] = now
        if not is_running():
            last_launch = float(state.get("last_launch_attempt_at") or 0)
            if now - last_launch < launch_cooldown:
                result = {"ok": False, "action": "launch_cooldown", "running": False}
            else:
                state["last_launch_attempt_at"] = now
                ok, detail = launch()
                state["last_launch_ok"] = bool(ok)
                result = {"ok": bool(ok), "action": "launched" if ok else "launch_failed", "running": False}
                if detail:
                    result["detail"] = detail
                notify(
                    "OneDrive relaunched" if ok else "OneDrive launch failed",
                    "OneDrive was not running; background launch requested."
                    if ok else f"OneDrive was absent and could not be launched: {detail or 'unknown error'}",
                    level="info" if ok else "critical",
                    healed=bool(ok),
                )
            state["last_action"] = result["action"]
            return result

        if ensure_running_only:
            state["last_action"] = "already_running"
            return {"ok": True, "action": "already_running", "running": True}

        marker = _load_json(marker_path)
        attempted = str(marker.get("attempted") or "")
        attempted_ts = _timestamp(attempted)
        if not attempted or attempted == state.get("last_marker_attempted"):
            state["last_action"] = "no_new_marker"
            return {"ok": True, "action": "no_new_marker", "running": True}

        state["last_marker_attempted"] = attempted
        if attempted_ts is None or now - attempted_ts > marker_max_age:
            state["last_action"] = "stale_or_invalid_marker"
            return {"ok": True, "action": "stale_or_invalid_marker", "running": True}

        previous_streak = int(state.get("consecutive_io_failures") or 0)
        if marker.get("complete") is True:
            state["consecutive_io_failures"] = 0
            awaiting_recovery = bool(state.pop("awaiting_recovery", False))
            state["last_action"] = "healthy"
            if previous_streak or awaiting_recovery:
                notify(
                    "OneDrive work sync recovered",
                    "A complete work-sync marker cleared the File Provider failure streak.",
                    level="info",
                    healed=True,
                )
            return {"ok": True, "action": "healthy", "running": True, "streak": 0}

        failures = marker.get("io_failures")
        failures = failures if isinstance(failures, list) else []
        failures = [failure for failure in failures if failure]
        if not failures:
            state["consecutive_io_failures"] = 0
            state["last_action"] = "non_io_failure"
            return {"ok": True, "action": "non_io_failure", "running": True, "streak": 0}

        streak = previous_streak + 1
        state["consecutive_io_failures"] = streak
        state["last_io_failures"] = failures[-4:]
        if streak < max(2, failures_before_restart):
            state["last_action"] = "io_failure_recorded"
            return {
                "ok": True,
                "action": "io_failure_recorded",
                "running": True,
                "streak": streak,
            }

        last_restart = float(state.get("last_restart_attempt_at") or 0)
        if now - last_restart < restart_cooldown:
            state["last_action"] = "restart_cooldown"
            return {
                "ok": True,
                "action": "restart_cooldown",
                "running": True,
                "streak": streak,
                "cooldown_remaining": round(restart_cooldown - (now - last_restart), 1),
            }

        state["last_restart_attempt_at"] = now
        ok, detail = restart()
        state["last_restart_ok"] = bool(ok)
        state["last_action"] = "restarted" if ok else "restart_failed"
        if ok:
            state["consecutive_io_failures"] = 0
            state["awaiting_recovery"] = True
        notify(
            "OneDrive File Provider restarted" if ok else "OneDrive File Provider restart failed",
            f"{streak} consecutive work-sync I/O failures. "
            + ("OneDrive was restarted in the background." if ok else f"restart failed: {detail or 'unknown error'}"),
            level="warn" if ok else "critical",
            healed=False,
        )
        result = {
            "ok": bool(ok),
            "action": "restarted" if ok else "restart_failed",
            "running": True,
            "streak": streak,
        }
        if detail:
            result["detail"] = detail
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--ensure-running", action="store_true")
    mode.add_argument(
        "--record-supervisor-failure",
        metavar="REASON",
        help="atomically record an ETIMEDOUT/EBUSY outer failure, then assess it",
    )
    mode.add_argument(
        "--record-if-io",
        metavar="OUTPUT",
        help="record and assess output only when it identifies a healable I/O failure",
    )
    parser.add_argument("--failure-source", default="outer_supervisor")
    args = parser.parse_args()
    recorded = None
    if args.record_supervisor_failure:
        try:
            recorded = record_supervisor_failure(
                args.record_supervisor_failure,
                source=args.failure_source,
            )
        except ValueError as exc:
            parser.error(str(exc))
    elif args.record_if_io:
        recorded = record_if_io_failure(
            args.record_if_io,
            source=args.failure_source,
        )
        if recorded is None:
            print(json.dumps({
                "ok": True,
                "action": "ignored_non_io_failure",
                "recorded": False,
            }, sort_keys=True))
            return 0
    result = run_guard(ensure_running_only=args.ensure_running)
    if recorded is not None:
        result = {
            **result,
            "recorded_attempted": recorded["attempted"],
            "recorded_failure": recorded["io_failures"][0],
        }
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
