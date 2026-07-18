"""Guard unattended UI automation jobs when a higher-priority UI actor is active.

Background jobs that drive desktop apps should pause while the Mac is actively
being controlled through Screen Sharing / Apple Remote Desktop, or while
another UI automation actor is visibly driving the pointer. That prevents
background scans from stealing focus, clicking the wrong window, or marking
notifications read out from under the active controller.

This deliberately ignores always-on helper processes like ARDAgent and
SSMenuAgent; they exist even when no one is remotely controlling the screen.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import subprocess
import time

_CACHE_TTL_S = 5
_REMOTE_PORTS = ("5900", "3283")
_SCREEN_PROCESS_MARKERS = ("ScreenSharingAgent",)
_CLAUDE_PROCESS_MARKERS = (
    "/applications/claude.app/contents/macos/claude",
    "com.anthropic.claudefordesktop",
    "/applications/claude.app/contents/frameworks/claude helper",
)

_cache_ts = 0.0
_cache_value: dict | None = None
_last_cursor_position: tuple[int, int] | None = None
_last_synthetic_pointer_ts = 0.0
_app_services = None


class _CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


def _run(args: list[str]) -> str:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=3)
    except Exception:
        return ""
    if result.returncode != 0:
        return ""
    return (result.stdout or "").strip()


def _env_true(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    raw = os.environ.get(name, "").strip()
    try:
        return max(minimum, min(maximum, float(raw)))
    except Exception:
        return default


def _screen_sharing_process_active() -> bool:
    out = _run(["/bin/ps", "aux"])
    if not out:
        return False
    lowered = out.lower()
    return any(marker.lower() in lowered for marker in _SCREEN_PROCESS_MARKERS)


def _claude_desktop_running() -> bool:
    out = _run(["/bin/ps", "ax", "-o", "command="])
    if not out:
        return False
    lowered = out.lower()
    return any(marker in lowered for marker in _CLAUDE_PROCESS_MARKERS)


def _remote_control_connections() -> list[str]:
    out = _run(["/usr/sbin/netstat", "-anv", "-p", "tcp"])
    if not out:
        return []

    hits: list[str] = []
    for line in out.splitlines():
        if "ESTABLISHED" not in line:
            continue
        parts = line.split()
        if len(parts) < 5:
            continue
        local_addr = parts[3]
        remote_addr = parts[4]
        if not any(local_addr.endswith(f".{port}") for port in _REMOTE_PORTS):
            continue
        remote_host = remote_addr.rsplit(".", 1)[0]
        if remote_host.startswith("127.") or remote_host == "::1" or remote_host.startswith("::ffff:127."):
            continue
        hits.append(f"{remote_addr}->{local_addr}")
    return hits


def _hid_idle_seconds() -> float | None:
    out = _run(["/usr/sbin/ioreg", "-c", "IOHIDSystem"])
    if not out:
        return None
    marker = '"HIDIdleTime" = '
    try:
        raw = out.split(marker, 1)[1].splitlines()[0].strip()
        return int(raw) / 1_000_000_000.0
    except Exception:
        return None


def _cursor_position() -> tuple[int, int] | None:
    global _app_services

    if _app_services is None:
        try:
            lib_path = ctypes.util.find_library("ApplicationServices")
            if not lib_path:
                _app_services = False
            else:
                lib = ctypes.cdll.LoadLibrary(lib_path)
                lib.CGEventCreate.restype = ctypes.c_void_p
                lib.CGEventGetLocation.argtypes = [ctypes.c_void_p]
                lib.CGEventGetLocation.restype = _CGPoint
                lib.CFRelease.argtypes = [ctypes.c_void_p]
                _app_services = lib
        except Exception:
            _app_services = False

    if not _app_services:
        return None

    event = None
    try:
        event = _app_services.CGEventCreate(None)
        if not event:
            return None
        point = _app_services.CGEventGetLocation(event)
        return int(round(point.x)), int(round(point.y))
    except Exception:
        return None
    finally:
        if event:
            try:
                _app_services.CFRelease(event)
            except Exception:
                pass


def _synthetic_pointer_active(now: float, *, claude_running: bool, hid_idle_seconds: float | None) -> dict:
    global _last_cursor_position, _last_synthetic_pointer_ts

    cursor_position = _cursor_position()
    cursor_moved = bool(
        cursor_position is not None
        and _last_cursor_position is not None
        and cursor_position != _last_cursor_position
    )

    synthetic_idle_floor_s = _env_float(
        "WORKOS_CLAUDE_UI_IDLE_FLOOR_S",
        8.0,
        minimum=1.0,
        maximum=300.0,
    )
    linger_s = _env_float(
        "WORKOS_CLAUDE_UI_LINGER_S",
        12.0,
        minimum=1.0,
        maximum=300.0,
    )

    if cursor_moved and claude_running and hid_idle_seconds is not None and hid_idle_seconds >= synthetic_idle_floor_s:
        _last_synthetic_pointer_ts = now

    if cursor_position is not None:
        _last_cursor_position = cursor_position

    active = bool(
        claude_running
        and _last_synthetic_pointer_ts > 0
        and (now - _last_synthetic_pointer_ts) <= linger_s
    )
    return {
        "active": active,
        "cursor_position": cursor_position,
        "cursor_moved": cursor_moved,
        "hid_idle_seconds": hid_idle_seconds,
        "last_motion_at": _last_synthetic_pointer_ts or None,
    }


def get_ui_automation_guard(force: bool = False) -> dict:
    global _cache_ts, _cache_value

    now = time.time()
    if not force and _cache_value is not None and (now - _cache_ts) < _CACHE_TTL_S:
        return dict(_cache_value)

    ignore_guard = _env_true("WORKOS_IGNORE_REMOTE_UI_GUARD")
    forced_pause = _env_true("WORKOS_FORCE_UI_JOBS_PAUSE")

    if ignore_guard:
        screen_process = False
        connections: list[str] = []
        claude_running = False
        pointer_state = {
            "active": False,
            "cursor_position": None,
            "cursor_moved": False,
            "hid_idle_seconds": None,
            "last_motion_at": None,
        }
    else:
        screen_process = _screen_sharing_process_active()
        connections = _remote_control_connections()
        claude_running = _claude_desktop_running()
        pointer_state = _synthetic_pointer_active(
            now,
            claude_running=claude_running,
            hid_idle_seconds=_hid_idle_seconds(),
        )

    # Only pause on actual active connections, not just the background
    # ScreenSharingAgent process (which runs even when no one is connected).
    remote_control_active = bool(connections)
    claude_ui_active = bool(pointer_state.get("active"))
    pause_ui_jobs = False if ignore_guard else (forced_pause or remote_control_active or claude_ui_active)

    reasons = []
    if forced_pause:
        reasons.append("forced by WORKOS_FORCE_UI_JOBS_PAUSE")
    if connections:
        reasons.append("active remote-control TCP session on port 5900/3283")
    if claude_ui_active:
        reasons.append("Claude app synthetic pointer activity detected")
    if screen_process:
        reasons.append("ScreenSharingAgent process active")
    if ignore_guard:
        reasons.append("guard disabled by WORKOS_IGNORE_REMOTE_UI_GUARD")

    _cache_value = {
        "pause_ui_jobs": pause_ui_jobs,
        "remote_control_active": remote_control_active,
        "claude_ui_active": claude_ui_active,
        "reasons": reasons,
        "indicators": {
            "screen_process": screen_process,
            "connections": connections,
            "claude_desktop_running": claude_running,
            "cursor_position": pointer_state.get("cursor_position"),
            "cursor_moved": pointer_state.get("cursor_moved"),
            "hid_idle_seconds": pointer_state.get("hid_idle_seconds"),
            "last_synthetic_pointer_at": pointer_state.get("last_motion_at"),
        },
        "as_of": now,
    }
    _cache_ts = now
    return dict(_cache_value)


def background_ui_jobs_paused(force: bool = False) -> bool:
    return bool(get_ui_automation_guard(force).get("pause_ui_jobs"))


def remote_viewer_priority_active(force: bool = False) -> bool:
    """Return True when a live UI actor should take priority over headless automation."""
    return bool(get_ui_automation_guard(force).get("pause_ui_jobs"))


def headless_automation_allowed(force: bool = False) -> bool:
    """Return True when aggressive unattended automation is allowed to run."""
    return not remote_viewer_priority_active(force)


def ui_automation_pause_reason(force: bool = False) -> str:
    status = get_ui_automation_guard(force)
    reasons = status.get("reasons") or []
    return "; ".join(reasons) if reasons else "ui automation guard inactive"
