"""Coordinate heavyweight background local-model jobs on one machine.

This keeps long-running builders from competing with each other for the same
local Ollama slot. Interactive calls should stay fast; background jobs can
either wait briefly or fall back to deterministic output.
"""

from __future__ import annotations

import json
import os
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
RUNTIME_DIR = BASE_DIR / "data" / "runtime"
LEASE_PATH = RUNTIME_DIR / "background_llm_lease.json"


def _hostname() -> str:
    try:
        return socket.gethostname()
    except Exception:
        return "unknown-host"


def _now() -> float:
    return time.time()


def _lease_stale_after_s() -> int:
    raw = os.environ.get("BACKGROUND_LLM_STALE_S", "300").strip()
    try:
        return max(30, min(3600, int(raw)))
    except Exception:
        return 300


def _lease_poll_s() -> float:
    raw = os.environ.get("BACKGROUND_LLM_POLL_S", "2").strip()
    try:
        return max(0.25, min(30.0, float(raw)))
    except Exception:
        return 2.0


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _read_lease() -> dict:
    try:
        return json.loads(LEASE_PATH.read_text())
    except Exception:
        return {}


def _write_payload(payload: dict) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = LEASE_PATH.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2))
    tmp_path.replace(LEASE_PATH)


def _remove_if_owned(payload: dict) -> None:
    current = _read_lease()
    if not current:
        return
    if (
        str(current.get("owner_host") or "") == str(payload.get("owner_host") or "")
        and int(current.get("owner_pid") or 0) == int(payload.get("owner_pid") or 0)
        and str(current.get("job_name") or "") == str(payload.get("job_name") or "")
    ):
        try:
            LEASE_PATH.unlink()
        except FileNotFoundError:
            pass


def _lease_owner_summary(payload: dict) -> str:
    job = str(payload.get("job_name") or "unknown-job")
    host = str(payload.get("owner_host") or "unknown-host")
    pid = int(payload.get("owner_pid") or 0)
    return f"{job} (pid {pid} on {host})"


def _lease_is_active(payload: dict, now: float | None = None) -> bool:
    if not payload:
        return False
    now = _now() if now is None else now
    owner_host = str(payload.get("owner_host") or "")
    owner_pid = int(payload.get("owner_pid") or 0)
    heartbeat = float(payload.get("heartbeat_at") or payload.get("acquired_at") or 0.0)
    if heartbeat <= 0:
        return False
    if (now - heartbeat) > _lease_stale_after_s():
        return False
    if owner_host in {"", _hostname()} and not _pid_alive(owner_pid):
        return False
    return True


def current_background_llm_lease() -> dict:
    payload = _read_lease()
    active = _lease_is_active(payload)
    return {
        "active": active,
        "job_name": payload.get("job_name"),
        "owner_host": payload.get("owner_host"),
        "owner_pid": payload.get("owner_pid"),
        "purpose": payload.get("purpose"),
        "acquired_at": payload.get("acquired_at"),
        "heartbeat_at": payload.get("heartbeat_at"),
        "summary": _lease_owner_summary(payload) if payload else "",
    }


@dataclass
class BackgroundLLMLease:
    job_name: str
    purpose: str = ""
    wait_timeout_s: float = 0.0
    poll_interval_s: float = field(default_factory=_lease_poll_s)
    acquired: bool = False
    payload: dict = field(default_factory=dict)
    skip_reason: str = ""

    def acquire(self) -> bool:
        deadline = _now() + max(0.0, float(self.wait_timeout_s or 0.0))
        while True:
            existing = _read_lease()
            if existing and _lease_is_active(existing):
                if self._is_same_owner(existing):
                    self.acquired = True
                    self.payload = existing
                    return True
                if _now() >= deadline:
                    self.skip_reason = f"busy: {_lease_owner_summary(existing)}"
                    return False
                time.sleep(self.poll_interval_s)
                continue

            if existing and not _lease_is_active(existing):
                try:
                    LEASE_PATH.unlink()
                except FileNotFoundError:
                    pass

            payload = {
                "job_name": self.job_name,
                "purpose": self.purpose,
                "owner_host": _hostname(),
                "owner_pid": os.getpid(),
                "acquired_at": _now(),
                "heartbeat_at": _now(),
            }
            try:
                RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
                fd = os.open(str(LEASE_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                try:
                    os.write(fd, json.dumps(payload, indent=2).encode("utf-8"))
                finally:
                    os.close(fd)
                self.acquired = True
                self.payload = payload
                self.skip_reason = ""
                return True
            except FileExistsError:
                if _now() >= deadline:
                    current = _read_lease()
                    self.skip_reason = f"busy: {_lease_owner_summary(current)}" if current else "busy"
                    return False
                time.sleep(self.poll_interval_s)

    def _is_same_owner(self, payload: dict) -> bool:
        return (
            int(payload.get("owner_pid") or 0) == os.getpid()
            and str(payload.get("owner_host") or "") == _hostname()
            and str(payload.get("job_name") or "") == self.job_name
        )

    def refresh(self) -> bool:
        if not self.acquired:
            return False
        current = _read_lease()
        if current and not self._is_same_owner(current):
            self.acquired = False
            self.skip_reason = f"lost lease to {_lease_owner_summary(current)}"
            return False
        payload = dict(self.payload or {})
        payload["heartbeat_at"] = _now()
        payload["purpose"] = self.purpose
        _write_payload(payload)
        self.payload = payload
        return True

    def release(self) -> None:
        if not self.acquired:
            return
        _remove_if_owned(self.payload or {})
        self.acquired = False

    def __enter__(self) -> "BackgroundLLMLease":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        self.release()
