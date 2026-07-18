from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path


CHAT_DB_PATH = Path.home() / "Library" / "Messages" / "chat.db"
SYNC_CHAT_DB_PATH = Path("/tmp/imessage_chat.db")
SYNC_DB_MAX_AGE_S = 30
DIRECT_QUERY_RETRIES = 3
DIRECT_QUERY_RETRY_SLEEP_S = 0.2
OSASCRIPT_PREFERENCE_TTL_S = 300
_prefer_osascript_until = 0.0


def _sqlite_uri(path: Path) -> str:
    return f"file:{path}?mode=ro"


def _effective_db_path(source: Path) -> Path:
    if source != CHAT_DB_PATH:
        return source
    try:
        if SYNC_CHAT_DB_PATH.exists():
            age_s = time.time() - SYNC_CHAT_DB_PATH.stat().st_mtime
            if age_s <= SYNC_DB_MAX_AGE_S:
                return SYNC_CHAT_DB_PATH
    except Exception:
        pass
    return source


def _run_query(db_path: Path, sql: str, *, timeout: float = 5.0):
    conn = sqlite3.connect(_sqlite_uri(db_path), uri=True, timeout=timeout)
    try:
        conn.execute("PRAGMA query_only = ON")
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def _snapshot_db(source: Path, target_dir: Path) -> Path:
    snapshot = target_dir / "chat.db"
    shutil.copy2(source, snapshot)
    for suffix in ("-wal", "-shm"):
        aux = Path(f"{source}{suffix}")
        if aux.exists():
            shutil.copy2(aux, target_dir / f"chat.db{suffix}")
    return snapshot


def _query_rows_via_osascript(sql: str, *, db_path: Path) -> list[tuple]:
    script = (
        f"set dbPath to {json.dumps(str(db_path))}\n"
        f"set sqlQuery to {json.dumps(sql)}\n"
        'do shell script "/usr/bin/sqlite3 -json " & quoted form of dbPath & " " & quoted form of sqlQuery'
    )
    proc = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if proc.returncode != 0:
        detail = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
        raise RuntimeError(detail or "osascript sqlite3 query failed")
    payload = (proc.stdout or "").strip()
    if not payload:
        return []
    rows = json.loads(payload)
    if not isinstance(rows, list):
        raise RuntimeError("osascript sqlite3 returned non-list payload")
    return [tuple(row.values()) if isinstance(row, dict) else tuple(row) for row in rows]


def query_rows(sql: str, *, db_path: Path | None = None):
    global _prefer_osascript_until
    source = _effective_db_path(Path(db_path) if db_path else CHAT_DB_PATH)
    errors: list[str] = []
    now = time.time()

    if now < _prefer_osascript_until:
        try:
            return _query_rows_via_osascript(sql, db_path=source)
        except Exception as exc:
            errors.append(f"preferred osascript fallback: {exc}")

    for attempt in range(1, DIRECT_QUERY_RETRIES + 1):
        try:
            return _run_query(source, sql)
        except Exception as exc:
            errors.append(f"direct attempt {attempt}: {exc}")
            if attempt < DIRECT_QUERY_RETRIES:
                time.sleep(DIRECT_QUERY_RETRY_SLEEP_S)

    try:
        rows = _query_rows_via_osascript(sql, db_path=source)
        _prefer_osascript_until = time.time() + OSASCRIPT_PREFERENCE_TTL_S
        return rows
    except Exception as exc:
        errors.append(f"osascript fallback: {exc}")

    try:
        with tempfile.TemporaryDirectory(prefix="imessage_chatdb_") as tmp:
            snapshot = _snapshot_db(source, Path(tmp))
            return _run_query(snapshot, sql, timeout=1.0)
    except Exception as exc:
        errors.append(f"snapshot fallback: {exc}")

    raise RuntimeError(f"chat.db query failed: {'; '.join(errors)}")


def latest_message_rowid(*, db_path: Path | None = None) -> int:
    rows = query_rows("SELECT MAX(ROWID) FROM message", db_path=db_path)
    if not rows or not rows[0] or rows[0][0] is None:
        return 0
    return int(rows[0][0])
