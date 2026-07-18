"""Read-only Apple Mail local index access.

This lets background agents reference Apple Mail while Mail.app is closed.
The data comes from Apple's local Envelope Index plus .emlx files for full
message bodies. All queries are read-only and best-effort.
"""

from __future__ import annotations

import email
import html
import json
import re
import sqlite3
import time
from email import policy
from functools import lru_cache
from pathlib import Path
from typing import Iterable
from urllib.parse import unquote


MAIL_DIR = Path.home() / "Library" / "Mail"
ACCOUNTS_DB = Path.home() / "Library" / "Accounts" / "Accounts4.sqlite"
BASE_DIR = Path(__file__).resolve().parent.parent
CACHE_FILE = BASE_DIR / "data" / "apple_mail_index_cache.json"

NICOLE_TOKENS = ("Operator2", "nicoleredacted", "cnovate")


def envelope_index_path() -> Path | None:
    paths = sorted(MAIL_DIR.glob("V*/MailData/Envelope Index"), reverse=True)
    return paths[0] if paths else None


def mail_version_dir(index_path: Path | None = None) -> Path | None:
    idx = index_path or envelope_index_path()
    if not idx:
        return None
    # ~/Library/Mail/V10/MailData/Envelope Index -> ~/Library/Mail/V10
    return idx.parent.parent


def available(index_path: Path | None = None) -> bool:
    path = index_path or envelope_index_path()
    return bool(path and path.exists())


def _load_cache(max_age_s: int | None = None) -> dict:
    try:
        payload = json.loads(CACHE_FILE.read_text())
        if not isinstance(payload, dict):
            return {}
        if max_age_s is not None:
            ts = float(payload.get("created_at_ts") or 0)
            if ts <= 0 or time.time() - ts > max_age_s:
                return {}
        return payload
    except Exception:
        return {}


def cache_age_seconds() -> float | None:
    payload = _load_cache()
    ts = float(payload.get("created_at_ts") or 0) if payload else 0
    if ts <= 0:
        return None
    return max(0.0, time.time() - ts)


def _save_cache(payload: dict) -> None:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(payload, indent=2, default=str))


def update_cache_snapshot(*, unread_inbox: list[dict] | None = None, recent_inbox: list[dict] | None = None) -> dict:
    payload = _load_cache()
    if unread_inbox is not None:
        payload["unread_inbox"] = unread_inbox
    if recent_inbox is not None:
        payload["recent_inbox"] = recent_inbox
    payload["created_at_ts"] = time.time()
    payload["created_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    payload["source"] = payload.get("source") or "apple_mail_runtime_snapshot"
    messages = _dedupe_messages(
        list(payload.get("unread_inbox") or [])
        + list(payload.get("recent_inbox") or [])
        + list(payload.get("recent_sent") or [])
    )
    payload["messages"] = messages
    payload["recent_inbox_subjects"] = sorted({
        str(row.get("subject") or "")
        for row in list(payload.get("recent_inbox") or []) + list(payload.get("unread_inbox") or [])
        if str(row.get("subject") or "").strip()
    })
    payload["counts"] = {
        "unread_inbox": len(payload.get("unread_inbox") or []),
        "recent_sent": len(payload.get("recent_sent") or []),
        "recent_inbox": len(payload.get("recent_inbox") or []),
        "messages": len(messages),
    }
    _save_cache(payload)
    return payload


def _dedupe_messages(rows: list[dict]) -> list[dict]:
    out = []
    seen = set()
    for row in rows:
        key = (row.get("id") or row.get("rowid") or row.get("message_id") or "").strip()
        if not key:
            key = f"{row.get('mailbox','')}:{row.get('subject','')}:{row.get('received','')}"
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def _cache_unread(limit: int = 80) -> list[dict]:
    payload = _load_cache()
    return list(payload.get("unread_inbox") or [])[: int(limit)]


def _cache_sent(limit: int = 40) -> list[dict]:
    payload = _load_cache()
    return list(payload.get("recent_sent") or [])[: int(limit)]


def _cache_recent_inbox(limit: int = 10) -> list[dict]:
    payload = _load_cache()
    return list(payload.get("recent_inbox") or [])[: int(limit)]


def _cache_inbox_subjects() -> set[str]:
    payload = _load_cache()
    return {str(s) for s in payload.get("recent_inbox_subjects") or [] if str(s).strip()}


def _cache_search(query: str, limit: int = 10) -> list[dict]:
    query = str(query or "").lower().strip()
    if not query:
        return []
    payload = _load_cache()
    rows = payload.get("messages") or []
    out = []
    for row in rows:
        haystack = " ".join(
            str(row.get(k, "") or "")
            for k in ("subject", "sender_str", "from_email", "from_name", "preview", "mailbox", "recipients")
        ).lower()
        if query in haystack:
            out.append(row)
        if len(out) >= int(limit):
            break
    return out


def _connect(path: Path | None = None) -> sqlite3.Connection:
    idx = path or envelope_index_path()
    if not idx:
        raise FileNotFoundError("Apple Mail Envelope Index not found")
    con = sqlite3.connect(f"file:{idx}?mode=ro", uri=True, timeout=2)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    return con


def _account_uuid(mailbox_url: str) -> str:
    m = re.match(r"^[a-z]+://([^/]+)/", str(mailbox_url or ""), flags=re.I)
    return m.group(1).upper() if m else ""


def account_map(accounts_db: Path = ACCOUNTS_DB) -> dict[str, dict[str, str]]:
    if not accounts_db.exists():
        return {}
    try:
        con = sqlite3.connect(f"file:{accounts_db}?mode=ro", uri=True, timeout=2)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            """
            SELECT
                a.ZIDENTIFIER AS uuid,
                lower(coalesce(a.ZUSERNAME, '')) AS username,
                coalesce(a.ZACCOUNTDESCRIPTION, '') AS description,
                coalesce(t.ZIDENTIFIER, '') AS account_type
            FROM ZACCOUNT a
            LEFT JOIN ZACCOUNTTYPE t ON a.ZACCOUNTTYPE = t.Z_PK
            WHERE a.ZIDENTIFIER IS NOT NULL
            """
        ).fetchall()
        con.close()
    except Exception:
        return {}
    return {
        str(row["uuid"]).upper(): {
            "username": str(row["username"] or ""),
            "description": str(row["description"] or ""),
            "account_type": str(row["account_type"] or ""),
        }
        for row in rows
    }


def _is_nicole_account(mailbox_url: str, recipients: str = "", accounts: dict[str, dict[str, str]] | None = None) -> bool:
    account = (accounts or {}).get(_account_uuid(mailbox_url), {})
    text = " ".join(
        [
            str(mailbox_url or ""),
            str(recipients or ""),
            account.get("username", ""),
            account.get("description", ""),
        ]
    ).lower()
    return any(token in text for token in NICOLE_TOKENS)


def _format_sender(address: str, comment: str) -> str:
    address = str(address or "").strip()
    comment = str(comment or "").strip()
    if comment and address:
        return f"{comment} <{address}>"
    return address or comment


def _row_message_id(row: sqlite3.Row) -> str:
    for key in ("message_id_header", "message_id", "rowid"):
        val = row[key] if key in row.keys() else ""
        if val not in (None, ""):
            return str(val).strip("<>") or str(row["rowid"])
    return str(row["rowid"])


def _row_to_message(row: sqlite3.Row, accounts: dict[str, dict[str, str]]) -> dict:
    mailbox_url = str(row["mailbox_url"] or "")
    uuid = _account_uuid(mailbox_url)
    account = accounts.get(uuid, {})
    sender_email = str(row["sender_email"] or "").strip()
    sender_name = str(row["sender_name"] or "").strip()
    recipients = str(row["recipients"] or "").strip(",")
    return {
        "rowid": str(row["rowid"]),
        "id": _row_message_id(row),
        "message_id": str(row["message_id_header"] or row["message_id"] or ""),
        "subject": str(row["subject"] or ""),
        "sender_str": _format_sender(sender_email, sender_name),
        "from_email": sender_email,
        "from_name": sender_name,
        "preview": str(row["summary"] or "")[:1000],
        "received": str(row["received"] or ""),
        "sent": str(row["sent"] or ""),
        "mailbox": mailbox_url,
        "account_uuid": uuid,
        "account_email": account.get("username", ""),
        "recipients": recipients,
        "read": bool(row["read"]) if "read" in row.keys() else None,
    }


_BASE_SELECT = """
SELECT
    m.ROWID AS rowid,
    m.message_id AS message_id,
    mgd.message_id_header AS message_id_header,
    m.read AS read,
    datetime(m.date_received, 'unixepoch', 'localtime') AS received,
    datetime(m.date_sent, 'unixepoch', 'localtime') AS sent,
    a.address AS sender_email,
    a.comment AS sender_name,
    s.subject AS subject,
    su.summary AS summary,
    mb.url AS mailbox_url,
    group_concat(ra.address, ',') AS recipients
FROM messages m
LEFT JOIN addresses a ON m.sender = a.ROWID
LEFT JOIN subjects s ON m.subject = s.ROWID
LEFT JOIN summaries su ON m.summary = su.ROWID
LEFT JOIN mailboxes mb ON m.mailbox = mb.ROWID
LEFT JOIN message_global_data mgd ON m.global_message_id = mgd.ROWID
LEFT JOIN recipients r ON r.message = m.ROWID
LEFT JOIN addresses ra ON r.address = ra.ROWID
"""


def unread_inbox(limit: int = 80, since_hours: int = 168, *, index_path: Path | None = None) -> list[dict]:
    accounts = account_map()
    cutoff = int(max(1, since_hours)) * 3600
    try:
        con = _connect(index_path)
    except Exception:
        return _cache_unread(limit)
    try:
        rows = con.execute(
            _BASE_SELECT
            + """
            WHERE m.deleted = 0
              AND m.read = 0
              AND m.date_received >= strftime('%s','now') - ?
              AND lower(mb.url) LIKE '%/inbox%'
            GROUP BY m.ROWID
            ORDER BY m.date_received DESC
            LIMIT ?
            """,
            (cutoff, int(limit)),
        ).fetchall()
    finally:
        con.close()
    return [
        _row_to_message(row, accounts)
        for row in rows
        if not _is_nicole_account(row["mailbox_url"], row["recipients"], accounts)
    ]


def recent_inbox_subjects(days: int = 7, *, index_path: Path | None = None) -> set[str]:
    accounts = account_map()
    cutoff = int(max(1, days)) * 86400
    try:
        con = _connect(index_path)
    except Exception:
        return _cache_inbox_subjects()
    try:
        rows = con.execute(
            """
            SELECT s.subject AS subject, mb.url AS mailbox_url, group_concat(ra.address, ',') AS recipients
            FROM messages m
            LEFT JOIN subjects s ON m.subject = s.ROWID
            LEFT JOIN mailboxes mb ON m.mailbox = mb.ROWID
            LEFT JOIN recipients r ON r.message = m.ROWID
            LEFT JOIN addresses ra ON r.address = ra.ROWID
            WHERE m.deleted = 0
              AND m.date_received >= strftime('%s','now') - ?
              AND lower(mb.url) LIKE '%/inbox%'
            GROUP BY m.ROWID
            """,
            (cutoff,),
        ).fetchall()
    finally:
        con.close()
    out = set()
    for row in rows:
        if _is_nicole_account(row["mailbox_url"], row["recipients"], accounts):
            continue
        subject = str(row["subject"] or "").strip()
        if subject:
            out.add(subject)
    return out


def recent_sent(limit: int = 40, days: int = 5, *, index_path: Path | None = None) -> list[dict]:
    accounts = account_map()
    cutoff = int(max(1, days)) * 86400
    try:
        con = _connect(index_path)
    except Exception:
        return _cache_sent(limit)
    try:
        rows = con.execute(
            _BASE_SELECT
            + """
            WHERE m.deleted = 0
              AND m.date_sent >= strftime('%s','now') - ?
              AND (
                lower(mb.url) LIKE '%/sent%'
                OR lower(mb.url) LIKE '%sent%20messages%'
                OR lower(mb.url) LIKE '%sent items%'
              )
            GROUP BY m.ROWID
            ORDER BY m.date_sent DESC
            LIMIT ?
            """,
            (cutoff, int(limit)),
        ).fetchall()
    finally:
        con.close()
    return [
        _row_to_message(row, accounts)
        for row in rows
        if not _is_nicole_account(row["mailbox_url"], row["recipients"], accounts)
    ]


def search_messages(query: str, limit: int = 10, *, index_path: Path | None = None) -> list[dict]:
    query = str(query or "").strip()
    if not query:
        return []
    accounts = account_map()
    like = f"%{query}%"
    try:
        con = _connect(index_path)
    except Exception:
        return _cache_search(query, limit)
    try:
        rows = con.execute(
            _BASE_SELECT
            + """
            WHERE m.deleted = 0
              AND (
                s.subject LIKE ?
                OR a.address LIKE ?
                OR a.comment LIKE ?
                OR su.summary LIKE ?
              )
            GROUP BY m.ROWID
            ORDER BY coalesce(m.date_received, m.date_sent) DESC
            LIMIT ?
            """,
            (like, like, like, like, int(limit)),
        ).fetchall()
    finally:
        con.close()
    return [
        _row_to_message(row, accounts)
        for row in rows
        if not _is_nicole_account(row["mailbox_url"], row["recipients"], accounts)
    ]


def recent_inbox(limit: int = 10, *, index_path: Path | None = None) -> list[dict]:
    accounts = account_map()
    try:
        con = _connect(index_path)
    except Exception:
        return _cache_recent_inbox(limit)
    try:
        rows = con.execute(
            _BASE_SELECT
            + """
            WHERE m.deleted = 0
              AND lower(mb.url) LIKE '%/inbox%'
            GROUP BY m.ROWID
            ORDER BY m.date_received DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
    finally:
        con.close()
    return [
        _row_to_message(row, accounts)
        for row in rows
        if not _is_nicole_account(row["mailbox_url"], row["recipients"], accounts)
    ]


def refresh_cache(
    *,
    unread_limit: int = 120,
    sent_limit: int = 80,
    inbox_limit: int = 120,
    since_hours: int = 168,
    sent_days: int = 5,
) -> dict:
    """Refresh the agent-readable cache from the direct Apple Mail index."""
    unread = unread_inbox(limit=unread_limit, since_hours=since_hours)
    sent = recent_sent(limit=sent_limit, days=sent_days)
    inbox = recent_inbox(limit=inbox_limit)
    subjects = sorted(recent_inbox_subjects(days=max(sent_days + 2, 7)))
    messages = _dedupe_messages(unread + inbox + sent)
    payload = {
        "created_at_ts": time.time(),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "source": "apple_mail_envelope_index",
        "unread_inbox": unread,
        "recent_sent": sent,
        "recent_inbox": inbox,
        "recent_inbox_subjects": subjects,
        "messages": messages,
        "counts": {
            "unread_inbox": len(unread),
            "recent_sent": len(sent),
            "recent_inbox": len(inbox),
            "messages": len(messages),
        },
    }
    _save_cache(payload)
    return payload


def _candidate_emlx_paths(rowid: str, mailbox_url: str, index_path: Path | None = None) -> Iterable[Path]:
    base = mail_version_dir(index_path)
    if not base:
        return []
    uuid = _account_uuid(mailbox_url)
    roots = [base / uuid] if uuid else [p for p in base.iterdir() if p.is_dir() and p.name != "MailData"]
    for root in roots:
        if not root.exists():
            continue
        if uuid:
            for messages_dir in _cached_message_dirs(str(root), _mailbox_name(mailbox_url)):
                for filename in (f"{rowid}.emlx", f"{rowid}.partial.emlx"):
                    path = messages_dir / filename
                    if path.exists():
                        yield path
            return
        yield from root.rglob(f"{rowid}.emlx")


def _mailbox_name(mailbox_url: str) -> str:
    value = unquote(str(mailbox_url or ""))
    match = re.match(r"^[a-z]+://[^/]+/(.+)$", value, flags=re.I)
    if not match:
        return ""
    return match.group(1).strip("/")


@lru_cache(maxsize=256)
def _cached_message_dirs(root_str: str, mailbox_name: str) -> tuple[Path, ...]:
    root = Path(root_str)
    mailbox_norm = str(mailbox_name or "").lower().strip("/")
    mailbox_dirs = []
    for candidate in root.glob("*.mbox"):
        stem = candidate.stem.lower()
        if not mailbox_norm or stem == mailbox_norm or stem.replace(" ", "") == mailbox_norm.replace(" ", ""):
            mailbox_dirs.append(candidate)
    if mailbox_norm and not mailbox_dirs:
        direct = root / f"{mailbox_name}.mbox"
        if direct.exists():
            mailbox_dirs.append(direct)

    dirs = []
    for mailbox_dir in mailbox_dirs:
        for messages_dir in mailbox_dir.glob("*/Data/**/Messages"):
            if messages_dir.is_dir():
                dirs.append(messages_dir)
    return tuple(sorted(dirs))


def _extract_message_text(raw: bytes) -> str:
    msg = email.message_from_bytes(raw, policy=policy.default)
    parts = []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition", "")).lower()
            if "attachment" in disp:
                continue
            if ctype == "text/plain":
                parts.append(part.get_content())
        if not parts:
            for part in msg.walk():
                if part.get_content_type() == "text/html":
                    parts.append(_html_to_text(str(part.get_content())))
                    break
    else:
        content = str(msg.get_content())
        parts.append(_html_to_text(content) if msg.get_content_type() == "text/html" else content)
    return "\n".join(str(p or "").strip() for p in parts if str(p or "").strip())


def _html_to_text(value: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", str(value or ""))
    text = re.sub(r"(?s)<br\s*/?>", "\n", text)
    text = re.sub(r"(?s)</p\s*>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"[ \t]+", " ", text).strip()


def read_body(rowid: str, mailbox_url: str = "", *, index_path: Path | None = None, max_chars: int = 5000) -> str:
    for path in _candidate_emlx_paths(str(rowid), mailbox_url, index_path):
        try:
            with path.open("rb") as f:
                first = f.readline().strip()
                byte_count = int(first)
                raw = f.read(byte_count)
            text = _extract_message_text(raw)
            if text:
                return text[:max_chars]
        except Exception:
            continue
    return ""
