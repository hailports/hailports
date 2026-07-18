#!/usr/bin/env python3
"""Run one command under a non-waiting hard deadline and inherited singleton lock."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def _write_marker(path: Path, **details) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        **details,
    }
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, indent=2) + "\n")
    temp.replace(path)


def supervise(
    command: list[str],
    *,
    timeout: float,
    lock_path: Path,
    marker_path: Path,
    term_grace: float = 2.0,
) -> tuple[int, str, dict]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = lock_path.open("a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        details = {"status": "singleton_busy", "lock": str(lock_path)}
        _write_marker(marker_path, **details)
        lock.close()
        return 75, "bounded worker already running; refusing overlap\n", details

    proc = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
        pass_fds=(lock.fileno(),),
    )
    started = time.monotonic()
    try:
        output, _ = proc.communicate(timeout=timeout)
        details = {
            "status": "complete",
            "pid": proc.pid,
            "exit_code": int(proc.returncode or 0),
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
        _write_marker(marker_path, **details)
        return int(proc.returncode or 0), output or "", details
    except subprocess.TimeoutExpired as exc:
        partial = exc.output or ""
        if isinstance(partial, bytes):
            partial = partial.decode(errors="replace")
        term_sent = kill_sent = False
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            term_sent = True
        except ProcessLookupError:
            pass
        stop_by = time.monotonic() + max(0.0, term_grace)
        while proc.poll() is None and time.monotonic() < stop_by:
            time.sleep(0.05)
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
                kill_sent = True
            except ProcessLookupError:
                pass
        # Deliberately do not communicate()/wait() again. A File Provider syscall can
        # remain uninterruptible; returning is more important than reaping it here.
        orphan_alive = proc.poll() is None
        if proc.stdout:
            proc.stdout.close()
        details = {
            "status": "timeout",
            "pid": proc.pid,
            "timeout_seconds": timeout,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "term_sent": term_sent,
            "kill_sent": kill_sent,
            "orphan_alive": orphan_alive,
            "lock_inherited": True,
        }
        _write_marker(marker_path, **details)
        suffix = (
            f"bounded worker pid={proc.pid} exceeded {timeout:g}s; "
            f"TERM={int(term_sent)} KILL={int(kill_sent)} orphan={int(orphan_alive)}\n"
        )
        return 124, partial + suffix, details
    finally:
        lock.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=float, required=True)
    parser.add_argument("--term-grace", type=float, default=2.0)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--marker", type=Path, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a command is required after --")
    rc, output, _details = supervise(
        command,
        timeout=args.timeout,
        term_grace=args.term_grace,
        lock_path=args.lock,
        marker_path=args.marker,
    )
    print(output, end="" if output.endswith("\n") else "\n")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
