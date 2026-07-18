#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path

STATE = Path.home() / ".openclaw"
TASKS = STATE / "tasks"
DB = TASKS / "runs.sqlite"
BACKUPS = TASKS / "backups"
SUMMARY = TASKS / "task-keeper-summary.json"
LOCK = TASKS / ".task-keeper.lock"


def run_json(args: list[str], timeout: int = 25) -> dict:
    try:
        proc = subprocess.run(args, text=True, capture_output=True, timeout=timeout)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    text = (proc.stdout or "").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        return {"ok": False, "returncode": proc.returncode, "stderr": (proc.stderr or "")[-500:]}
    try:
        data = json.loads(text[start : end + 1])
    except Exception as exc:
        return {"ok": False, "error": str(exc), "stderr": (proc.stderr or "")[-500:]}
    data["_returncode"] = proc.returncode
    return data


def sqlite_backup() -> str:
    if not DB.exists():
        return ""
    BACKUPS.mkdir(parents=True, exist_ok=True)
    target = BACKUPS / f"runs-{time.strftime('%Y%m%d-%H%M%S')}.sqlite"
    src = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)
    try:
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    backups = sorted(BACKUPS.glob("runs-*.sqlite"), key=lambda p: p.stat().st_mtime)
    for old in backups[:-48]:
        old.unlink(missing_ok=True)
    return str(target)


def main() -> int:
    TASKS.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return 0
    try:
        os.write(fd, str(os.getpid()).encode())
        maintenance = run_json(["openclaw", "tasks", "maintenance", "--apply", "--json"])
        active = run_json(["openclaw", "tasks", "list", "--status", "running", "--json"], timeout=15)
        queued = run_json(["openclaw", "tasks", "list", "--status", "queued", "--json"], timeout=15)
        backup = ""
        backup_error = ""
        try:
            backup = sqlite_backup()
        except Exception as exc:
            backup_error = str(exc)
        payload = {
            "updatedAt": int(time.time() * 1000),
            "ok": not backup_error,
            "backup": backup,
            "backupError": backup_error,
            "maintenance": maintenance.get("maintenance", {}),
            "activeCount": active.get("count", 0) if isinstance(active, dict) else 0,
            "queuedCount": queued.get("count", 0) if isinstance(queued, dict) else 0,
        }
        SUMMARY.write_text(json.dumps(payload, indent=2) + "\n")
        return 0 if payload["ok"] else 1
    finally:
        os.close(fd)
        Path(LOCK).unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
