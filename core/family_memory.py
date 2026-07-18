"""Durable family memory store with append-only event history."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from core import BASE_DIR

MEMORY_DIR = BASE_DIR / "data" / "memory"
FAMILY_EVENTS_FILE = MEMORY_DIR / "family_events.jsonl"
FAMILY_ENTITIES_FILE = MEMORY_DIR / "family_entities.json"

DEFAULT_TTL_DAYS: dict[str, int] = {
    "event": 30,
    "warning": 14,
    "open_loop": 30,
    "obligation": 90,
}

CONFIRMATION_KINDS = {
    "preference",
    "relationship",
    "routine",
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat_utc(value: datetime | None = None) -> str:
    dt = value or utc_now()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def parse_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _memory_dir(events_path: Path | None = None, entities_path: Path | None = None) -> Path:
    if events_path is not None:
        return Path(events_path).parent
    if entities_path is not None:
        return Path(entities_path).parent
    return MEMORY_DIR


def _events_path(events_path: Path | None = None, entities_path: Path | None = None) -> Path:
    if events_path is not None:
        return Path(events_path)
    return _memory_dir(events_path=events_path, entities_path=entities_path) / FAMILY_EVENTS_FILE.name


def _entities_path(entities_path: Path | None = None, events_path: Path | None = None) -> Path:
    if entities_path is not None:
        return Path(entities_path)
    return _memory_dir(events_path=events_path, entities_path=entities_path) / FAMILY_ENTITIES_FILE.name


def subject_key(value: str) -> str:
    text = " ".join(str(value or "").strip().split())
    if not text:
        return "unknown"
    key = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return key or "unknown"


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def _normalize_kind(value: Any) -> str:
    text = _clean_text(value).lower().replace(" ", "_").replace("-", "_")
    if not text:
        raise ValueError("kind is required")
    return text


def _normalize_confidence(value: Any) -> float:
    try:
        confidence = float(value)
    except Exception:
        confidence = 0.5
    return max(0.0, min(1.0, round(confidence, 3)))


def _default_requires_confirmation(kind: str, confidence: float) -> bool:
    return kind in CONFIRMATION_KINDS and confidence < 0.95


def _default_expiry(kind: str, *, now: datetime, explicit: str = "", expires_in_days: Any = None) -> str | None:
    if explicit:
        parsed = parse_timestamp(explicit)
        return isoformat_utc(parsed) if parsed else None
    if expires_in_days not in (None, ""):
        try:
            days = float(expires_in_days)
            if days <= 0:
                return isoformat_utc(now)
            return isoformat_utc(now + timedelta(days=days))
        except Exception:
            return None
    ttl_days = DEFAULT_TTL_DAYS.get(kind)
    if ttl_days is None:
        return None
    return isoformat_utc(now + timedelta(days=ttl_days))


def _normalize_tags(value: Any) -> list[str]:
    tags = []
    if isinstance(value, str):
        value = [part.strip() for part in value.split(",")]
    if isinstance(value, (list, tuple, set)):
        for raw in value:
            text = _clean_text(raw)
            if text:
                tags.append(text)
    return sorted(set(tags))


def _normalize_relations(value: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not isinstance(value, list):
        return rows
    for raw in value:
        if not isinstance(raw, dict):
            continue
        target = _clean_text(raw.get("target"))
        relation_type = _clean_text(raw.get("type") or raw.get("relationship")).lower().replace(" ", "_")
        if not target or not relation_type:
            continue
        relation: dict[str, Any] = {
            "target": target,
            "target_key": subject_key(target),
            "type": relation_type,
        }
        if raw.get("notes"):
            relation["notes"] = _clean_text(raw.get("notes"))
        if raw.get("confidence") not in (None, ""):
            relation["confidence"] = _normalize_confidence(raw.get("confidence"))
        rows.append(relation)
    deduped: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (row["target_key"], row["type"])
        existing = deduped.get(key)
        if existing is None:
            deduped[key] = row
            continue
        if row.get("confidence", 0.0) > existing.get("confidence", 0.0):
            deduped[key] = row
    return sorted(deduped.values(), key=lambda item: (item["target_key"], item["type"]))


def _fingerprint(subject: str, kind: str, content: str, source: str, source_ref: str) -> str:
    payload = {
        "subject": subject_key(subject),
        "kind": kind,
        "content": _clean_text(content).lower() if not source_ref else "",
        "source": _clean_text(source).lower(),
        "source_ref": _clean_text(source_ref).lower(),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:24]


def explain_memory(memory: dict[str, Any]) -> str:
    source = _clean_text(memory.get("source") or "unknown source")
    source_ref = _clean_text(memory.get("source_ref"))
    subject = _clean_text(memory.get("subject") or "unknown subject")
    kind = _clean_text(memory.get("kind") or "memory")
    explanation = f"Remembered as {kind} for {subject} from {source}"
    if source_ref:
        explanation += f" ({source_ref})"
    if memory.get("requires_confirmation") and not memory.get("confirmed_by_user"):
        explanation += "; awaiting user confirmation"
    return explanation


def _default_state() -> dict[str, Any]:
    return {"updated_at": "", "subjects": {}, "memories": []}


def _read_state(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text())
    except Exception:
        return _default_state()
    if not isinstance(payload, dict):
        return _default_state()
    memories = payload.get("memories")
    if not isinstance(memories, list):
        payload["memories"] = []
    subjects = payload.get("subjects")
    if not isinstance(subjects, dict):
        payload["subjects"] = {}
    payload.setdefault("updated_at", "")
    return payload


def _build_subject_index(memories: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    subjects: dict[str, dict[str, Any]] = {}
    for memory in memories:
        if str(memory.get("status") or "active") != "active":
            continue
        key = subject_key(memory.get("subject") or "")
        bucket = subjects.setdefault(
            key,
            {
                "key": key,
                "label": _clean_text(memory.get("subject") or "Unknown"),
                "memory_ids": [],
                "kinds": set(),
                "pending_confirmation": 0,
                "open_loops": 0,
                "last_seen_at": "",
            },
        )
        bucket["memory_ids"].append(memory["id"])
        bucket["kinds"].add(memory.get("kind") or "")
        if memory.get("requires_confirmation") and not memory.get("confirmed_by_user"):
            bucket["pending_confirmation"] += 1
        if memory.get("kind") in {"open_loop", "obligation", "warning"}:
            bucket["open_loops"] += 1
        last_seen = _clean_text(memory.get("last_seen_at"))
        if last_seen and (not bucket["last_seen_at"] or last_seen > bucket["last_seen_at"]):
            bucket["last_seen_at"] = last_seen
    for bucket in subjects.values():
        bucket["memory_ids"] = sorted(bucket["memory_ids"])
        bucket["kinds"] = sorted(kind for kind in bucket["kinds"] if kind)
        bucket["memory_count"] = len(bucket["memory_ids"])
    return dict(sorted(subjects.items()))


def _write_state(state: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": state.get("updated_at") or "",
        "subjects": _build_subject_index(list(state.get("memories") or [])),
        "memories": sorted(
            list(state.get("memories") or []),
            key=lambda item: (item.get("last_seen_at") or "", item.get("created_at") or "", item.get("id") or ""),
            reverse=True,
        ),
    }
    path.write_text(json.dumps(payload, indent=2))


def _append_event(action: str, memory: dict[str, Any], *, events_path: Path, ts: str, actor: str = "", note: str = "") -> None:
    events_path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": ts,
        "action": action,
        "memory_id": memory.get("id"),
        "fingerprint": memory.get("fingerprint"),
        "subject": memory.get("subject"),
        "kind": memory.get("kind"),
        "status": memory.get("status"),
        "source": memory.get("source"),
        "source_ref": memory.get("source_ref"),
        "confidence": memory.get("confidence"),
        "requires_confirmation": bool(memory.get("requires_confirmation")),
        "confirmed_by_user": bool(memory.get("confirmed_by_user")),
        "reason": note or memory.get("reason") or explain_memory(memory),
    }
    if actor:
        row["actor"] = actor
    with events_path.open("a") as handle:
        handle.write(json.dumps(row) + "\n")


def load_memory_state(
    *,
    entities_path: Path | None = None,
    events_path: Path | None = None,
    now: datetime | None = None,
    prune_expired: bool = False,
) -> dict[str, Any]:
    state_path = _entities_path(entities_path=entities_path, events_path=events_path)
    if prune_expired:
        expire_due_memories(entities_path=state_path, events_path=events_path, now=now)
    return _read_state(state_path)


def list_memories(
    *,
    subject: str = "",
    kind: str = "",
    include_deleted: bool = False,
    include_expired: bool = False,
    requires_confirmation: bool | None = None,
    entities_path: Path | None = None,
    events_path: Path | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    state = load_memory_state(entities_path=entities_path, events_path=events_path, now=now, prune_expired=True)
    rows = list(state.get("memories") or [])
    subject_filter = subject_key(subject) if subject else ""
    kind_filter = _normalize_kind(kind) if kind else ""
    filtered: list[dict[str, Any]] = []
    for row in rows:
        status = str(row.get("status") or "active")
        if status == "deleted" and not include_deleted:
            continue
        if status == "expired" and not include_expired:
            continue
        if subject_filter and subject_key(row.get("subject") or "") != subject_filter:
            continue
        if kind_filter and str(row.get("kind") or "") != kind_filter:
            continue
        if requires_confirmation is not None and bool(row.get("requires_confirmation")) != requires_confirmation:
            continue
        filtered.append(dict(row))
    return filtered


def get_memory(
    memory_id: str,
    *,
    entities_path: Path | None = None,
    events_path: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    for row in list_memories(
        include_deleted=True,
        include_expired=True,
        entities_path=entities_path,
        events_path=events_path,
        now=now,
    ):
        if row.get("id") == memory_id:
            return row
    return None


def remember_memory(
    payload: dict[str, Any],
    *,
    entities_path: Path | None = None,
    events_path: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    now_dt = now or utc_now()
    ts = isoformat_utc(now_dt)
    state_path = _entities_path(entities_path=entities_path, events_path=events_path)
    log_path = _events_path(events_path=events_path, entities_path=entities_path)

    expire_due_memories(entities_path=state_path, events_path=log_path, now=now_dt)
    state = _read_state(state_path)

    subject = _clean_text(payload.get("subject"))
    content = _clean_text(payload.get("content"))
    source = _clean_text(payload.get("source"))
    if not subject:
        raise ValueError("subject is required")
    if not content:
        raise ValueError("content is required")
    if not source:
        raise ValueError("source is required")

    kind = _normalize_kind(payload.get("kind"))
    source_ref = _clean_text(payload.get("source_ref"))
    confidence = _normalize_confidence(payload.get("confidence"))
    explicit_confirmation = payload.get("requires_confirmation")
    requires_confirmation = (
        bool(explicit_confirmation)
        if explicit_confirmation is not None
        else _default_requires_confirmation(kind, confidence)
    )
    expires_at = _default_expiry(
        kind,
        now=now_dt,
        explicit=_clean_text(payload.get("expires_at")),
        expires_in_days=payload.get("expires_in_days"),
    )
    tags = _normalize_tags(payload.get("tags"))
    relations = _normalize_relations(payload.get("relations"))
    reason = _clean_text(payload.get("reason"))
    visibility = _clean_text(payload.get("visibility") or "private").lower()
    fingerprint = _fingerprint(subject, kind, content, source, source_ref)

    existing = None
    for row in state.get("memories") or []:
        if row.get("fingerprint") == fingerprint and str(row.get("status") or "active") == "active":
            existing = row
            break

    if existing is None:
        memory = {
            "id": f"mem_{uuid4().hex[:12]}",
            "fingerprint": fingerprint,
            "subject": subject,
            "subject_key": subject_key(subject),
            "kind": kind,
            "content": content,
            "source": source,
            "source_ref": source_ref,
            "confidence": confidence,
            "visibility": visibility,
            "requires_confirmation": requires_confirmation,
            "confirmed_by_user": False,
            "confirmed_by": "",
            "confirmed_at": "",
            "created_at": ts,
            "updated_at": ts,
            "last_seen_at": ts,
            "expires_at": expires_at or "",
            "status": "active",
            "tags": tags,
            "relations": relations,
            "reason": reason,
        }
        state.setdefault("memories", []).append(memory)
        action = "remembered"
    else:
        memory = existing
        memory["subject"] = subject
        memory["subject_key"] = subject_key(subject)
        memory["kind"] = kind
        memory["content"] = content
        memory["source"] = source
        if source_ref:
            memory["source_ref"] = source_ref
        memory["confidence"] = max(_normalize_confidence(memory.get("confidence")), confidence)
        memory["visibility"] = visibility or memory.get("visibility") or "private"
        memory["requires_confirmation"] = bool(memory.get("requires_confirmation")) or requires_confirmation
        memory["updated_at"] = ts
        memory["last_seen_at"] = ts
        if expires_at:
            memory["expires_at"] = expires_at
        memory["status"] = "active"
        memory["tags"] = sorted(set(_normalize_tags(memory.get("tags")) + tags))
        memory["relations"] = _normalize_relations(list(memory.get("relations") or []) + relations)
        if reason:
            memory["reason"] = reason
        action = "refreshed"

    memory["explanation"] = explain_memory(memory)
    state["updated_at"] = ts
    _write_state(state, state_path)
    _append_event(action, memory, events_path=log_path, ts=ts)
    return dict(memory)


def confirm_memory(
    memory_id: str,
    *,
    confirmed_by: str,
    note: str = "",
    entities_path: Path | None = None,
    events_path: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    now_dt = now or utc_now()
    ts = isoformat_utc(now_dt)
    state_path = _entities_path(entities_path=entities_path, events_path=events_path)
    log_path = _events_path(events_path=events_path, entities_path=entities_path)
    state = _read_state(state_path)
    for row in state.get("memories") or []:
        if row.get("id") != memory_id:
            continue
        row["confirmed_by_user"] = True
        row["confirmed_by"] = _clean_text(confirmed_by)
        row["confirmed_at"] = ts
        row["requires_confirmation"] = False
        row["updated_at"] = ts
        row["explanation"] = explain_memory(row)
        state["updated_at"] = ts
        _write_state(state, state_path)
        _append_event("confirmed", row, events_path=log_path, ts=ts, actor=_clean_text(confirmed_by), note=_clean_text(note))
        return dict(row)
    raise KeyError(memory_id)


def delete_memory(
    memory_id: str,
    *,
    deleted_by: str,
    note: str = "",
    entities_path: Path | None = None,
    events_path: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    now_dt = now or utc_now()
    ts = isoformat_utc(now_dt)
    state_path = _entities_path(entities_path=entities_path, events_path=events_path)
    log_path = _events_path(events_path=events_path, entities_path=entities_path)
    state = _read_state(state_path)
    for row in state.get("memories") or []:
        if row.get("id") != memory_id:
            continue
        row["status"] = "deleted"
        row["deleted_at"] = ts
        row["deleted_by"] = _clean_text(deleted_by)
        row["updated_at"] = ts
        row["explanation"] = explain_memory(row)
        state["updated_at"] = ts
        _write_state(state, state_path)
        _append_event("deleted", row, events_path=log_path, ts=ts, actor=_clean_text(deleted_by), note=_clean_text(note))
        return dict(row)
    raise KeyError(memory_id)


def expire_due_memories(
    *,
    entities_path: Path | None = None,
    events_path: Path | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    now_dt = now or utc_now()
    ts = isoformat_utc(now_dt)
    state_path = _entities_path(entities_path=entities_path, events_path=events_path)
    log_path = _events_path(events_path=events_path, entities_path=entities_path)
    state = _read_state(state_path)
    expired: list[dict[str, Any]] = []
    changed = False
    for row in state.get("memories") or []:
        if str(row.get("status") or "active") != "active":
            continue
        expires_at = parse_timestamp(row.get("expires_at"))
        if expires_at is None or expires_at > now_dt.astimezone(timezone.utc):
            continue
        row["status"] = "expired"
        row["expired_at"] = ts
        row["updated_at"] = ts
        row["explanation"] = explain_memory(row)
        expired.append(dict(row))
        changed = True
    if changed:
        state["updated_at"] = ts
        _write_state(state, state_path)
        for row in expired:
            _append_event("expired", row, events_path=log_path, ts=ts)
    return expired

