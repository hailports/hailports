"""Authenticated WebUI operator terminal sessions.

This module owns local PTY processes for the WebUI. It intentionally keeps
process environment and credentials server-side; the browser only receives
terminal bytes and sends keystrokes.
"""

from __future__ import annotations

import fcntl
import json
import os
import pty
import select
import shutil
import signal
import socket
import struct
import subprocess
import termios
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_CWD = _REPO_ROOT
_AUDIT_PATH = _REPO_ROOT / "data" / "operator_terminal" / "audit.jsonl"
_MAX_READ_BYTES = 256 * 1024
_MAX_TAIL_CHARS = 120_000
_SESSION_IDLE_TTL_S = 8 * 3600
_EXITED_TTL_S = 300


def _now() -> float:
    return time.time()


def _shell_path() -> str:
    shell = str(os.environ.get("SHELL") or "").strip()
    if shell and Path(shell).exists():
        return shell
    return "/bin/zsh"


def _path_prepend() -> str:
    home = Path.home()
    parts = [
        "/opt/homebrew/bin",
        "/usr/local/bin",
        str(home / ".local" / "bin"),
        str(home / ".npm-global" / "bin"),
        str(home / ".bun" / "bin"),
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
    ]
    existing = str(os.environ.get("PATH") or "")
    if existing:
        parts.append(existing)
    seen: set[str] = set()
    clean: list[str] = []
    for part in parts:
        if part and part not in seen:
            seen.add(part)
            clean.append(part)
    return ":".join(clean)


def _operator_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PATH"] = _path_prepend()
    env.setdefault("TERM", "xterm-256color")
    env.setdefault("COLORTERM", "truecolor")
    env.setdefault("LANG", "en_US.UTF-8")
    env["CLAUDE_STACK_OPERATOR_TERMINAL"] = "1"
    return env


def _candidate_binary(name: str) -> str | None:
    env = _operator_env()
    found = shutil.which(name, path=env.get("PATH"))
    if found:
        return found
    home = Path.home()
    for raw in (
        f"/opt/homebrew/bin/{name}",
        f"/usr/local/bin/{name}",
        str(home / ".local" / "bin" / name),
        str(home / ".npm-global" / "bin" / name),
        str(home / ".bun" / "bin" / name),
    ):
        path = Path(raw)
        if path.exists() and os.access(path, os.X_OK):
            return str(path)
    return None


def _resolve_cwd(cwd: str | None) -> Path:
    if cwd:
        try:
            path = Path(cwd).expanduser().resolve()
            if path.exists() and path.is_dir():
                return path
        except Exception:
            pass
    return _DEFAULT_CWD


def _safe_cols(value: int | None) -> int:
    try:
        cols = int(value or 100)
    except Exception:
        return 100
    return min(max(cols, 40), 240)


def _safe_rows(value: int | None) -> int:
    try:
        rows = int(value or 30)
    except Exception:
        return 30
    return min(max(rows, 10), 80)


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except Exception:
        pass


def _audit(event: dict) -> None:
    payload = dict(event)
    payload.setdefault("ts", _now())
    try:
        _AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _AUDIT_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, sort_keys=True, default=str) + "\n")
    except Exception:
        pass


def _command_for(kind: str) -> tuple[list[str], str]:
    shell = _shell_path()
    normalized = str(kind or "local").strip().lower()
    if normalized in {"local", "shell", "machine"}:
        return [shell, "-l"], "Local Shell"
    if normalized in {"codex", "codex_cli", "codex-cli"}:
        binary = _candidate_binary("codex")
        if binary:
            return [binary], "Codex"
        return [shell, "-l"], "Local Shell"
    if normalized in {"claude", "claude_code", "claude-code"}:
        binary = _candidate_binary("claude")
        if binary:
            return [binary], "Claude Code"
        return [shell, "-l"], "Local Shell"
    return [shell, "-l"], "Local Shell"


@dataclass
class OperatorSession:
    id: str
    kind: str
    title: str
    cwd: str
    rows: int
    cols: int
    process: subprocess.Popen
    master_fd: int
    created_at: float = field(default_factory=_now)
    updated_at: float = field(default_factory=_now)
    tail: str = ""
    lock: threading.Lock = field(default_factory=threading.Lock)

    def alive(self) -> bool:
        return self.process.poll() is None


class OperatorTerminalManager:
    def __init__(self) -> None:
        self._sessions: dict[str, OperatorSession] = {}
        self._lock = threading.RLock()

    def status(self) -> dict:
        self._reap()
        with self._lock:
            sessions = [
                {
                    "id": sess.id,
                    "kind": sess.kind,
                    "title": sess.title,
                    "cwd": sess.cwd,
                    "pid": sess.process.pid,
                    "alive": sess.alive(),
                    "created_at": sess.created_at,
                    "updated_at": sess.updated_at,
                }
                for sess in self._sessions.values()
            ]
        return {
            "available": {
                "local": True,
                "codex": bool(_candidate_binary("codex")),
                "claude": bool(_candidate_binary("claude")),
            },
            "binaries": {
                "shell": _shell_path(),
                "codex": _candidate_binary("codex") or "",
                "claude": _candidate_binary("claude") or "",
            },
            "default_kind": "local",
            "host": socket.gethostname(),
            "repo_root": str(_REPO_ROOT),
            "sessions": sessions,
        }

    def start(self, *, kind: str = "local", cwd: str | None = None, rows: int | None = None, cols: int | None = None, user: str = "") -> dict:
        self._reap()
        rows_i = _safe_rows(rows)
        cols_i = _safe_cols(cols)
        command, title = _command_for(kind)
        normalized = str(kind or "local").strip().lower() or "local"
        if normalized in {"shell", "machine"}:
            normalized = "local"
        cwd_path = _resolve_cwd(cwd)
        master_fd, slave_fd = pty.openpty()
        _set_winsize(master_fd, rows_i, cols_i)
        env = _operator_env()
        try:
            process = subprocess.Popen(
                command,
                cwd=str(cwd_path),
                env=env,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                close_fds=True,
                start_new_session=True,
            )
        finally:
            try:
                os.close(slave_fd)
            except OSError:
                pass
        flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
        fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        sid = uuid.uuid4().hex[:16]
        session = OperatorSession(
            id=sid,
            kind=normalized,
            title=title,
            cwd=str(cwd_path),
            rows=rows_i,
            cols=cols_i,
            process=process,
            master_fd=master_fd,
        )
        with self._lock:
            self._sessions[sid] = session
        _audit({"event": "start", "id": sid, "kind": normalized, "title": title, "pid": process.pid, "cwd": str(cwd_path), "user": user})
        fallback = normalized in {"codex", "claude", "claude_code", "claude-code"} and title == "Local Shell"
        return {
            "id": sid,
            "kind": normalized,
            "title": title,
            "cwd": str(cwd_path),
            "host": socket.gethostname(),
            "pid": process.pid,
            "alive": True,
            "fallback": fallback,
        }

    def _get(self, sid: str) -> OperatorSession:
        with self._lock:
            session = self._sessions.get(str(sid or ""))
        if not session:
            raise KeyError("unknown session")
        return session

    def read(self, sid: str, *, max_bytes: int = _MAX_READ_BYTES) -> dict:
        session = self._get(sid)
        chunks: list[bytes] = []
        total = 0
        with session.lock:
            while total < max_bytes:
                try:
                    readable, _, _ = select.select([session.master_fd], [], [], 0)
                except Exception:
                    break
                if not readable:
                    break
                try:
                    data = os.read(session.master_fd, min(8192, max_bytes - total))
                except BlockingIOError:
                    break
                except OSError:
                    break
                if not data:
                    break
                chunks.append(data)
                total += len(data)
            session.updated_at = _now()
            alive = session.alive()
            code = session.process.poll()
        output = b"".join(chunks).decode("utf-8", errors="replace")
        if output:
            with session.lock:
                session.tail = (session.tail + output)[-_MAX_TAIL_CHARS:]
        return {"id": session.id, "output": output, "alive": alive, "exit_code": code}

    def snapshot(self, sid: str) -> dict:
        session = self._get(sid)
        with session.lock:
            session.updated_at = _now()
            return {
                "id": session.id,
                "kind": session.kind,
                "title": session.title,
                "cwd": session.cwd,
                "host": socket.gethostname(),
                "pid": session.process.pid,
                "alive": session.alive(),
                "exit_code": session.process.poll(),
                "tail": session.tail,
            }

    def write(self, sid: str, data: str, *, user: str = "") -> dict:
        session = self._get(sid)
        raw = str(data or "").encode("utf-8", errors="replace")
        with session.lock:
            if raw:
                os.write(session.master_fd, raw)
            session.updated_at = _now()
            alive = session.alive()
        _audit({"event": "write", "id": session.id, "kind": session.kind, "bytes": len(raw), "user": user})
        return {"id": session.id, "alive": alive, "bytes": len(raw)}

    def resize(self, sid: str, *, rows: int | None = None, cols: int | None = None, user: str = "") -> dict:
        session = self._get(sid)
        rows_i = _safe_rows(rows)
        cols_i = _safe_cols(cols)
        with session.lock:
            _set_winsize(session.master_fd, rows_i, cols_i)
            session.rows = rows_i
            session.cols = cols_i
            session.updated_at = _now()
            try:
                if session.alive():
                    os.killpg(session.process.pid, signal.SIGWINCH)
            except Exception:
                pass
        _audit({"event": "resize", "id": session.id, "kind": session.kind, "rows": rows_i, "cols": cols_i, "user": user})
        return {"id": session.id, "rows": rows_i, "cols": cols_i, "alive": session.alive()}

    def stop(self, sid: str, *, user: str = "") -> dict:
        session = self._get(sid)
        exit_code = session.process.poll()
        if exit_code is None:
            try:
                os.killpg(session.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except Exception:
                try:
                    session.process.terminate()
                except Exception:
                    pass
            try:
                session.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(session.process.pid, signal.SIGKILL)
                except Exception:
                    try:
                        session.process.kill()
                    except Exception:
                        pass
                try:
                    session.process.wait(timeout=2)
                except Exception:
                    pass
        with self._lock:
            self._sessions.pop(session.id, None)
        try:
            os.close(session.master_fd)
        except OSError:
            pass
        _audit({"event": "stop", "id": session.id, "kind": session.kind, "user": user})
        return {"id": session.id, "stopped": True, "exit_code": session.process.poll()}

    def _reap(self) -> None:
        now = _now()
        stale: list[str] = []
        with self._lock:
            for sid, session in self._sessions.items():
                idle = now - float(session.updated_at or session.created_at)
                if session.alive():
                    if idle > _SESSION_IDLE_TTL_S:
                        stale.append(sid)
                elif idle > _EXITED_TTL_S:
                    stale.append(sid)
        for sid in stale:
            try:
                self.stop(sid, user="reaper")
            except Exception:
                pass


_MANAGER = OperatorTerminalManager()


def get_operator_terminal_manager() -> OperatorTerminalManager:
    return _MANAGER
