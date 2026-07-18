"""Cross-channel adaptive learning for the shared assistant engine.

This module is deliberately deterministic. It logs every request lightly and
stores durable lessons only when an exchange contains evidence of a real
operational outcome or strategy directive.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core import BASE_DIR
from core import redacted_memory


LESSONS_PATH = BASE_DIR / "data" / "learning" / "cross_channel_lessons.jsonl"
SEEN_PATH = BASE_DIR / "data" / "learning" / "seen_exchange_hashes.json"

SALESFORCE_RE = re.compile(
    r"\b(EXECUTION (?:APPLIED|FINISHED|VERIFIED|BLOCKED)|Salesforce|SOQL|PermissionSet|PermissionSetAssignment|"
    r"FieldPermissions|ObjectPermissions|FLS|dashboard|report|metadata deploy|AsyncApexJob|rollback|Ticket Completion Blurb)\b",
    re.I,
)
REVENUE_RE = re.compile(r"\b(Strategy directive|revenue stack|revenue machine|next-money|new revenue|hustle|Gumroad|outreach)\b", re.I)
MAILBOX_RE = re.compile(r"\b(Searched Outlook|Apple Mail|mailbox|inbox|travel-related|receipts?|hotel|flight|rideshare)\b", re.I)
OUTCOME_RE = re.compile(
    r"\b(EXECUTION (?:APPLIED|FINISHED|VERIFIED|BLOCKED)|Ticket Completion Blurb|Resolution|Rollback|Strategy directive|"
    r"Searched Outlook|Found \d+ likely matching items|No Salesforce write was run|Salesforce was changed)\b",
    re.I,
)


def capture_exchange(
    *,
    user_id: str,
    prompt: str,
    response: str,
    frontend: str = "",
    thread_id: str | None = None,
    attachments: Any = None,
) -> None:
    """Record request telemetry and durable operational lessons when warranted."""
    prompt_text = str(prompt or "").strip()
    response_text = str(response or "").strip()
    source = _source(frontend, thread_id)
    category = _category(prompt_text, response_text)
    outcome = _outcome(response_text)
    try:
        redacted_memory.log_request(
            source=source,
            request_text=prompt_text[:4000],
            category=category,
            outcome=outcome,
            metadata={
                "user_id": str(user_id or ""),
                "thread_id": str(thread_id or ""),
                "has_attachments": bool(attachments),
                "response_chars": len(response_text),
            },
        )
    except Exception:
        pass

    if not _should_remember(prompt_text, response_text):
        return
    digest = _digest(prompt_text, response_text, source)
    if _seen(digest):
        return
    lesson = {
        "id": digest,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "category": category,
        "outcome": outcome,
        "prompt": prompt_text[:3000],
        "summary": _summary(prompt_text, response_text),
        "systems": _systems(prompt_text, response_text),
        "entities": _entities(prompt_text + "\n" + response_text),
    }
    _append_lesson(lesson)
    # Mailbox intelligence runs inline on chat/iMessage/Telegram. Keep that
    # path deterministic and cheap: the JSONL lesson is enough for the shared
    # brain to reuse later, while the heavier memory indexer can run offline.
    if category == "mailbox":
        _mark_seen(digest)
        return
    try:
        redacted_memory.remember(
            kind="ticket_lesson" if category == "salesforce" else "playbook",
            title=lesson["summary"][:280] or f"{category} operational lesson",
            body=_memory_body(lesson, response_text),
            source=source,
            systems=lesson["systems"],
            entities=lesson["entities"],
            confidence=0.86,
            pinned=False,
            metadata={"lesson_id": digest, "category": category, "outcome": outcome},
        )
    except Exception:
        pass
    _mark_seen(digest)


def _source(frontend: str, thread_id: str | None) -> str:
    token = str(frontend or "api").strip() or "api"
    if thread_id:
        token = f"{token}:{thread_id}"
    return token


def _category(prompt: str, response: str) -> str:
    text = f"{prompt}\n{response}"
    if MAILBOX_RE.search(text):
        return "mailbox"
    if SALESFORCE_RE.search(text):
        return "salesforce"
    if REVENUE_RE.search(text):
        return "revenue"
    return "general"


def _outcome(response: str) -> str:
    upper = response.upper()
    if "EXECUTION APPLIED" in upper or "EXECUTION FINISHED" in upper or "SALESFORCE WAS CHANGED" in upper:
        return "applied"
    if "EXECUTION VERIFIED" in upper:
        return "verified_no_write"
    if "EXECUTION BLOCKED" in upper or "NO SALESFORCE WRITE WAS RUN" in upper:
        return "blocked"
    if "STRATEGY DIRECTIVE:" in upper:
        return "strategy_queued"
    if "SEARCHED OUTLOOK" in upper or "APPLE MAIL" in upper:
        return "system_query_answered"
    return "answered"


def _should_remember(prompt: str, response: str) -> bool:
    text = f"{prompt}\n{response}"
    return bool(OUTCOME_RE.search(text) or (SALESFORCE_RE.search(text) and len(response) > 500) or REVENUE_RE.search(text))


def _digest(prompt: str, response: str, source: str) -> str:
    raw = f"{source}\n{prompt[:2000]}\n{response[:5000]}".encode("utf-8", errors="ignore")
    return hashlib.sha256(raw).hexdigest()[:20]


def _seen(digest: str) -> bool:
    try:
        data = json.loads(SEEN_PATH.read_text())
        return digest in set(data[-2000:] if isinstance(data, list) else [])
    except Exception:
        return False


def _mark_seen(digest: str) -> None:
    try:
        data = json.loads(SEEN_PATH.read_text())
        if not isinstance(data, list):
            data = []
    except Exception:
        data = []
    data.append(digest)
    SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    SEEN_PATH.write_text(json.dumps(data[-2000:], indent=2))


def _append_lesson(lesson: dict[str, Any]) -> None:
    LESSONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LESSONS_PATH, "a") as handle:
        handle.write(json.dumps(lesson, ensure_ascii=False, sort_keys=True) + "\n")


def _systems(prompt: str, response: str) -> list[str]:
    text = f"{prompt}\n{response}".lower()
    systems: list[str] = []
    for key, label in [
        ("salesforce", "salesforce"),
        ("soql", "salesforce"),
        ("outlook", "outlook"),
        ("apple mail", "apple_mail"),
        ("gmail", "gmail"),
        ("telegram", "telegram"),
        ("imessage", "imessage"),
        ("revenue", "revenue_stack"),
        ("gumroad", "gumroad"),
        ("monday", "monday"),
    ]:
        if key in text and label not in systems:
            systems.append(label)
    return systems or ["shared_engine"]


def _entities(text: str) -> list[str]:
    ids = re.findall(r"\b(?:00D|001|003|005|00O|01Z|0PS|0Pa|707|08e|a3k)[A-Za-z0-9]{12,18}\b", text)
    tickets = re.findall(r"\bSF-\d+\b", text, flags=re.I)
    names = re.findall(r"\b[A-Z][a-z]+ [A-Z][a-z]+\b", text)
    out: list[str] = []
    for item in ids + tickets + names[:10]:
        if item not in out:
            out.append(item)
    return out[:30]


def _summary(prompt: str, response: str) -> str:
    if "Ticket Completion Blurb:" in response:
        blurb = response.split("Ticket Completion Blurb:", 1)[1].strip().splitlines()[0]
        if blurb:
            return blurb[:300]
    first = str(prompt or "").strip().splitlines()[0] if prompt else ""
    if first:
        return first[:300]
    return str(response or "").strip().splitlines()[0][:300]


def _memory_body(lesson: dict[str, Any], response: str) -> str:
    tail = response.strip()
    if len(tail) > 4500:
        tail = tail[:4500] + "\n...[truncated]"
    return (
        f"Category: {lesson.get('category')}\n"
        f"Outcome: {lesson.get('outcome')}\n"
        f"Prompt: {lesson.get('prompt')}\n\n"
        f"Useful result / pattern:\n{tail}"
    )
