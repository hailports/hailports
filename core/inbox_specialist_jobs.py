"""Detached, deduplicated specialist work for the live Outlook intake loop."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from core import BASE_DIR, specialist_trust


JOB_DIR = BASE_DIR / "data" / "runtime" / "inbox_specialist_jobs"
LOG_FILE = BASE_DIR / "data" / "logs" / "inbox_specialist_workers.log"
POOL_LOCK = JOB_DIR / ".pool.lock"
SCHEMA_VERSION = 2


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest(value) -> str:
    raw = json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _job_key(email: dict, context: dict | None = None) -> str:
    stable = {
        "version": SCHEMA_VERSION,
        "id": str(email.get("id") or email.get("source_key") or ""),
        "thread_id": str(email.get("thread_id") or ""),
        "subject": str(email.get("subject") or ""),
        "sender": str(email.get("sender_email") or email.get("from_email") or ""),
        "body": str(email.get("body") or email.get("preview") or ""),
        "attachments": email.get("attachment_names") or [],
        "context_digest": _digest(context or {}),
    }
    raw = json.dumps(stable, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _path(key: str) -> Path:
    return JOB_DIR / f"{key}.json"


@contextmanager
def _locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        yield


def _load(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(value, indent=2, default=str))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _mutate(path: Path, update) -> dict:
    with _locked(path):
        value = _load(path)
        changed = update(dict(value))
        if changed is not None:
            value = changed
            _write(path, value)
        return value


def _pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def _priority(job: dict) -> tuple[int, str]:
    try:
        numeric_id = int(job.get("message_id") or 0)
    except (TypeError, ValueError):
        numeric_id = 0
    return numeric_id, str(job.get("created_at") or "")


def _pool_size() -> int:
    try:
        return max(1, min(6, int(os.environ.get("INBOX_SPECIALIST_WORKERS", "3"))))
    except ValueError:
        return 3


def _start_queued_workers() -> None:
    """Fill the bounded process pool, newest queued mail first."""
    JOB_DIR.mkdir(parents=True, exist_ok=True)
    with _locked(POOL_LOCK):
        jobs: list[tuple[Path, dict]] = []
        active = 0
        for path in JOB_DIR.glob("*.json"):
            job = _load(path)
            state = job.get("state")
            if state in {"starting", "running"}:
                if _pid_alive(job.get("pid")):
                    active += 1
                    continue
                job["state"] = "queued"
                job["pid"] = None
                job["updated_at"] = _now()
                _write(path, job)
            if job.get("state") == "queued":
                jobs.append((path, job))

        slots = max(0, _pool_size() - active)
        for path, job in sorted(jobs, key=lambda pair: _priority(pair[1]), reverse=True)[:slots]:
            def claim(current: dict) -> dict:
                if current.get("state") != "queued":
                    return current
                current.update({
                    "state": "starting",
                    "attempts": int(current.get("attempts") or 0) + 1,
                    "updated_at": _now(),
                })
                return current

            claimed = _mutate(path, claim)
            if claimed.get("state") != "starting":
                continue
            LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            with LOG_FILE.open("ab") as log:
                proc = subprocess.Popen(
                    [sys.executable, "-m", "core.inbox_specialist_jobs", "--run-job", path.stem],
                    cwd=str(BASE_DIR),
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    start_new_session=True,
                    close_fds=True,
                )

            def record_pid(current: dict) -> dict:
                if current.get("state") == "starting":
                    current.update({"state": "running", "pid": proc.pid, "started_at": _now(),
                                    "updated_at": _now()})
                return current

            _mutate(path, record_pid)


def request(email: dict, context: dict, *, timeout: float = 540.0) -> dict:
    """Return a completed dispatch or enqueue it without blocking mailbox intake."""
    context_digest = _digest(context or {})
    key = _job_key(email, context)
    path = _path(key)
    now = _now()

    def upsert(current: dict) -> dict:
        if not current:
            return {
                "schema": SCHEMA_VERSION,
                "key": key,
                "state": "queued",
                "attempts": 0,
                "message_id": str(email.get("id") or email.get("source_key") or ""),
                "subject": str(email.get("subject") or ""),
                "email": email,
                "context": context,
                "context_digest": context_digest,
                "timeout": float(timeout),
                "created_at": now,
                "updated_at": now,
            }
        current["last_requested_at"] = now
        if current.get("state") == "failed" and int(current.get("attempts") or 0) < 2:
            current.update({"state": "queued", "pid": None, "updated_at": now})
        return current

    job = _mutate(path, upsert)
    if job.get("state") == "done" and isinstance(job.get("result"), dict):
        return {"state": "done", "key": key, "result": specialist_trust.fail_closed(job["result"])}

    _start_queued_workers()
    job = _load(path)
    state = str(job.get("state") or "queued")
    return {
        "state": state,
        "key": key,
        "attempts": int(job.get("attempts") or 0),
        "error": job.get("error"),
    }


def _run_job(key: str) -> int:
    path = _path(key)
    job = _load(path)
    if not job:
        return 2

    def running(current: dict) -> dict:
        current.update({"state": "running", "pid": os.getpid(), "started_at": current.get("started_at") or _now(),
                        "updated_at": _now()})
        return current

    job = _mutate(path, running)
    try:
        from core import specialist_dispatch
        result = specialist_dispatch.dispatch_guarded(
            job.get("email") or {},
            job.get("context") or {},
            timeout=float(job.get("timeout") or 540),
        )
        if not isinstance(result, dict):
            raise RuntimeError("specialist returned no structured result")
        result = specialist_trust.fail_closed(result)

        def done(current: dict) -> dict:
            current.update({"state": "done", "pid": None, "result": result, "error": None,
                            "finished_at": _now(), "updated_at": _now()})
            return current

        _mutate(path, done)
        return 0
    except BaseException as exc:
        def failed(current: dict) -> dict:
            current.update({"state": "failed", "pid": None,
                            "error": f"{type(exc).__name__}: {str(exc)[:500]}",
                            "finished_at": _now(), "updated_at": _now()})
            return current

        _mutate(path, failed)
        return 1
    finally:
        _start_queued_workers()


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--run-job":
        raise SystemExit(_run_job(sys.argv[2]))
    raise SystemExit("usage: inbox_specialist_jobs.py --run-job JOB_KEY")
