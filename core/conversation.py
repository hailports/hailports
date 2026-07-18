"""Per-user conversation state with SQLite persistence.
Includes smart context window management — summarizes old messages when history gets long."""

from core.constants import LOCAL_MODEL
import json
import sqlite3
import time
import logging
from pathlib import Path
from core import BASE_DIR, SETTINGS

log = logging.getLogger(__name__)

DB_PATH = BASE_DIR / "data" / "conversations.db"
MAX_HISTORY = SETTINGS["routing"]["max_conversation_history"]
SUMMARIZE_THRESHOLD = 30  # After this many messages, summarize older ones
KEEP_RECENT = 10  # Always keep this many recent messages unsummarized


DEFAULT_THREAD = "general"


def _get_db():
    db = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=5000")
    db.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at REAL NOT NULL
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            summary TEXT NOT NULL,
            messages_covered INTEGER NOT NULL,
            created_at REAL NOT NULL
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS instructions (
            user_id TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            updated_at REAL NOT NULL
        )
    """)
    # Thread support
    db.execute("""
        CREATE TABLE IF NOT EXISTS threads (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT 'General',
            created_at REAL NOT NULL,
            last_message_at REAL,
            context_summary TEXT DEFAULT ''
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_threads_user ON threads(user_id, last_message_at)")
    # Add thread_id to messages if not exists
    try:
        db.execute("ALTER TABLE messages ADD COLUMN thread_id TEXT DEFAULT 'general'")
        log.info("Added thread_id column to messages table")
    except sqlite3.OperationalError:
        pass  # Column already exists
    # Add file_refs to messages if not exists (JSON list of file IDs)
    try:
        db.execute("ALTER TABLE messages ADD COLUMN file_refs TEXT DEFAULT '[]'")
        log.info("Added file_refs column to messages table")
    except sqlite3.OperationalError:
        pass
    db.execute("CREATE INDEX IF NOT EXISTS idx_messages_user ON messages(user_id, created_at)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_id, created_at)")
    db.commit()
    return db


_db = None


def get_db():
    global _db
    if _db is None:
        _db = _get_db()
    return _db


def get_history(user_id, thread_id=None):
    """Return last N messages for user as Anthropic API message format.
    If thread_id provided, returns only that thread's messages.
    If there's a summary, prepend it as context."""
    db = get_db()

    # Get any summary
    summary_row = db.execute(
        "SELECT summary FROM summaries WHERE user_id = ? ORDER BY created_at DESC LIMIT 1",
        (user_id,),
    ).fetchone()

    if thread_id:
        rows = db.execute(
            "SELECT role, content FROM messages WHERE user_id = ? AND thread_id = ? ORDER BY created_at DESC LIMIT ?",
            (user_id, thread_id, MAX_HISTORY),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT role, content FROM messages WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
            (user_id, MAX_HISTORY),
        ).fetchall()
    rows.reverse()

    messages = []

    # Prepend summary as a system-like context message if exists
    if summary_row:
        messages.append({
            "role": "user",
            "content": f"[Previous conversation summary: {summary_row[0]}]"
        })
        messages.append({
            "role": "assistant",
            "content": "Understood, I have the context from our previous conversation."
        })

    for role, content in rows:
        messages.append({"role": role, "content": json.loads(content)})

    return messages


def save_message(user_id, role, content, thread_id=None, summarize=True):
    """Save a message. Content can be a string or list of content blocks."""
    db = get_db()
    if isinstance(content, str):
        content_json = json.dumps(content)
    else:
        content_json = json.dumps(content, default=str)
    tid = _ensure_thread(db, user_id, thread_id or DEFAULT_THREAD)
    now = time.time()
    db.execute(
        "INSERT INTO messages (user_id, role, content, created_at, thread_id) VALUES (?, ?, ?, ?, ?)",
        (user_id, role, content_json, now, tid),
    )
    db.execute("UPDATE threads SET last_message_at = ? WHERE id = ? AND user_id = ?", (now, tid, user_id))
    db.commit()

    # Check if we need to summarize
    count = db.execute(
        "SELECT COUNT(*) FROM messages WHERE user_id = ?", (user_id,)
    ).fetchone()[0]

    if summarize and count > SUMMARIZE_THRESHOLD:
        _auto_summarize(user_id, db)


def _auto_summarize(user_id, db):
    """Summarize older messages and delete them to keep context window manageable.
    Uses local LOCAL_MODEL (free) to generate summary."""
    try:
        # Get messages to summarize (all except most recent KEEP_RECENT)
        rows = db.execute(
            "SELECT id, role, content FROM messages WHERE user_id = ? ORDER BY created_at ASC",
            (user_id,),
        ).fetchall()

        if len(rows) <= KEEP_RECENT:
            return

        to_summarize = rows[:-KEEP_RECENT]
        to_keep_ids = [r[0] for r in rows[-KEEP_RECENT:]]

        # Build text for summarization
        text_parts = []
        for _, role, content in to_summarize:
            try:
                parsed = json.loads(content)
                if isinstance(parsed, str):
                    text_parts.append(f"{role}: {parsed[:200]}")
                elif isinstance(parsed, list):
                    for block in parsed:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text_parts.append(f"{role}: {block['text'][:200]}")
            except Exception:
                pass

        if not text_parts:
            return

        conversation_text = "\n".join(text_parts[-20:])  # Last 20 of the old messages

        # Use local model for free summarization
        import asyncio
        from core import local_client

        async def _summarize():
            return await local_client.generate(
                f"Summarize this conversation in 2-3 sentences, capturing the key topics and any decisions made:\n\n{conversation_text}",
                model=LOCAL_MODEL,
                max_tokens=2048,
            )

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # We're already in an async context
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    summary = executor.submit(asyncio.run, _summarize()).result(timeout=30)
            else:
                summary = asyncio.run(_summarize())
        except Exception as e:
            log.warning(f"Auto-summarize failed: {e}")
            # Fallback: just truncate without summary
            summary = f"[{len(to_summarize)} older messages truncated]"

        if summary:
            # Save summary
            db.execute(
                "INSERT INTO summaries (user_id, summary, messages_covered, created_at) VALUES (?, ?, ?, ?)",
                (user_id, summary, len(to_summarize), time.time()),
            )

            db.commit()

            log.info(f"Auto-summarized {len(to_summarize)} messages for {user_id}")

    except Exception as e:
        log.warning(f"Auto-summarize error: {e}")


def clear_history(user_id, thread_id=None):
    """Clear conversation history. If thread_id given, only that thread."""
    db = get_db()
    if thread_id:
        cursor = db.execute("DELETE FROM messages WHERE user_id = ? AND thread_id = ?", (user_id, thread_id))
    else:
        cursor = db.execute("DELETE FROM messages WHERE user_id = ?", (user_id,))
        db.execute("DELETE FROM summaries WHERE user_id = ?", (user_id,))
    db.commit()
    return cursor.rowcount


# === Per-user persistent instructions (Projects equivalent) ===

def get_instructions(user_id):
    """Get persistent instructions for a user."""
    db = get_db()
    row = db.execute(
        "SELECT content FROM instructions WHERE user_id = ?", (user_id,)
    ).fetchone()
    return row[0] if row else ""


def set_instructions(user_id, content):
    """Set persistent instructions for a user (like CLAUDE.md / Projects)."""
    db = get_db()
    db.execute(
        "INSERT OR REPLACE INTO instructions (user_id, content, updated_at) VALUES (?, ?, ?)",
        (user_id, content, time.time()),
    )
    db.commit()


def get_message_count(user_id):
    """Get total message count for a user."""
    db = get_db()
    return db.execute(
        "SELECT COUNT(*) FROM messages WHERE user_id = ?", (user_id,)
    ).fetchone()[0]


# === Thread Management ===

def _gen_thread_id():
    import hashlib
    return hashlib.sha256(f"{time.time()}".encode()).hexdigest()[:12]


def _default_thread_title(thread_id):
    prefix = str(thread_id or DEFAULT_THREAD).split(":", 1)[0].strip().lower()
    titles = {
        "telegram": "Telegram",
        "imessage": "iMessage",
        "whatsapp": "WhatsApp",
        "discord": "Discord",
        DEFAULT_THREAD: "General",
    }
    return titles.get(prefix, prefix.title() if prefix else "General")


def _ensure_thread(db, user_id, thread_id, title=None):
    tid = thread_id or DEFAULT_THREAD
    now = time.time()
    db.execute(
        "INSERT OR IGNORE INTO threads (id, user_id, title, created_at, last_message_at) VALUES (?, ?, ?, ?, ?)",
        (tid, user_id, title or _default_thread_title(tid), now, now),
    )
    return tid


def create_thread(user_id, title="New Thread"):
    """Create a new conversation thread."""
    db = get_db()
    thread_id = _gen_thread_id()
    now = time.time()
    db.execute(
        "INSERT INTO threads (id, user_id, title, created_at, last_message_at) VALUES (?, ?, ?, ?, ?)",
        (thread_id, user_id, title, now, now),
    )
    db.commit()
    return {"id": thread_id, "title": title, "created_at": now}


def list_threads(user_id):
    """List all threads for a user, most recent first."""
    db = get_db()
    rows = db.execute(
        "SELECT id, title, created_at, last_message_at, context_summary FROM threads "
        "WHERE user_id = ? ORDER BY last_message_at DESC",
        (user_id,),
    ).fetchall()
    threads = []
    for tid, title, created, last_msg, summary in rows:
        # Get message count and last message preview
        msg_count = db.execute(
            "SELECT COUNT(*) FROM messages WHERE user_id = ? AND thread_id = ?", (user_id, tid)
        ).fetchone()[0]
        last_preview = ""
        last_row = db.execute(
            "SELECT content FROM messages WHERE user_id = ? AND thread_id = ? ORDER BY created_at DESC LIMIT 1",
            (user_id, tid),
        ).fetchone()
        if last_row:
            try:
                parsed = json.loads(last_row[0])
                if isinstance(parsed, str):
                    last_preview = parsed[:80]
                elif isinstance(parsed, list):
                    for block in parsed:
                        if isinstance(block, dict) and block.get("type") == "text":
                            last_preview = block["text"][:80]
                            break
            except Exception:
                pass
        threads.append({
            "id": tid,
            "title": title,
            "created_at": created,
            "last_message_at": last_msg,
            "message_count": msg_count,
            "last_preview": last_preview,
            "context_summary": summary or "",
        })
    return threads


def get_thread_history(user_id, thread_id, limit=None):
    """Get message history for a specific thread."""
    db = get_db()
    lim = limit or MAX_HISTORY
    rows = db.execute(
        "SELECT role, content, file_refs FROM messages WHERE user_id = ? AND thread_id = ? "
        "ORDER BY created_at DESC LIMIT ?",
        (user_id, thread_id, lim),
    ).fetchall()
    rows.reverse()
    messages = []
    for role, content, file_refs in rows:
        msg = {"role": role, "content": json.loads(content)}
        if file_refs and file_refs != "[]":
            msg["file_refs"] = json.loads(file_refs)
        messages.append(msg)
    return messages


def save_thread_message(user_id, thread_id, role, content, file_refs=None):
    """Save a message to a specific thread."""
    db = get_db()
    _ensure_thread(db, user_id, thread_id)
    content_json = json.dumps(content) if isinstance(content, str) else json.dumps(content, default=str)
    refs_json = json.dumps(file_refs or [])
    now = time.time()
    db.execute(
        "INSERT INTO messages (user_id, role, content, created_at, thread_id, file_refs) VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, role, content_json, now, thread_id, refs_json),
    )
    # Update thread last_message_at
    db.execute(
        "UPDATE threads SET last_message_at = ? WHERE id = ?",
        (now, thread_id),
    )
    db.commit()

    # Auto-title thread if it's the first message and title is default
    msg_count = db.execute(
        "SELECT COUNT(*) FROM messages WHERE user_id = ? AND thread_id = ?", (user_id, thread_id)
    ).fetchone()[0]
    if msg_count == 1 and role == "user":
        text = content if isinstance(content, str) else str(content)
        auto_title = text[:50].strip()
        if auto_title:
            db.execute("UPDATE threads SET title = ? WHERE id = ? AND title = 'New Thread'",
                       (auto_title, thread_id))
            db.commit()


def rename_thread(thread_id, title):
    """Rename a thread."""
    db = get_db()
    db.execute("UPDATE threads SET title = ? WHERE id = ?", (title, thread_id))
    db.commit()


def delete_thread(thread_id, user_id=None):
    """Delete a thread and its messages."""
    db = get_db()
    if user_id is None:
        db.execute("DELETE FROM messages WHERE thread_id = ?", (thread_id,))
        db.execute("DELETE FROM threads WHERE id = ?", (thread_id,))
    else:
        db.execute("DELETE FROM messages WHERE user_id = ? AND thread_id = ?", (user_id, thread_id))
        db.execute("DELETE FROM threads WHERE user_id = ? AND id = ?", (user_id, thread_id))
    db.commit()


def get_thread_message_count(user_id, thread_id):
    db = get_db()
    return db.execute(
        "SELECT COUNT(*) FROM messages WHERE user_id = ? AND thread_id = ?",
        (user_id, thread_id),
    ).fetchone()[0]


def search_thread_history(user_id, thread_id, query, limit=20):
    db = get_db()
    rows = db.execute(
        "SELECT role, content, created_at FROM messages "
        "WHERE user_id = ? AND thread_id = ? AND content LIKE ? "
        "ORDER BY created_at ASC LIMIT ?",
        (user_id, thread_id, f"%{query}%", int(limit or 20)),
    ).fetchall()
    results = []
    for role, content, created_at in rows:
        try:
            parsed = json.loads(content)
            text = parsed if isinstance(parsed, str) else str(parsed)
        except Exception:
            text = str(content)
        results.append({"role": role, "text": text, "created_at": created_at})
    return results


def search_threads(user_id, query):
    """Search across all threads for a query. Used by Telegram/iMessage for cross-thread context."""
    db = get_db()
    rows = db.execute(
        "SELECT m.thread_id, t.title, m.role, m.content, m.created_at "
        "FROM messages m LEFT JOIN threads t ON m.thread_id = t.id "
        "WHERE m.user_id = ? AND m.content LIKE ? "
        "ORDER BY m.created_at DESC LIMIT 20",
        (user_id, f"%{query}%"),
    ).fetchall()
    results = []
    for thread_id, thread_title, role, content, created_at in rows:
        try:
            parsed = json.loads(content)
            text = parsed if isinstance(parsed, str) else str(parsed)[:200]
        except Exception:
            text = content[:200]
        results.append({
            "thread_id": thread_id,
            "thread_title": thread_title or "General",
            "role": role,
            "text": text[:200],
            "created_at": created_at,
        })
    return results
