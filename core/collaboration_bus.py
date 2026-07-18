"""Shared coordination bus for multiple coding agents.

The goal is simple: let separate agents coordinate through durable repo-local
artifacts instead of asking a human to relay task ownership and handoffs.
"""

from __future__ import annotations

import fcntl
import json
import re
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _slug(value: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(value or "").strip())
    return text.strip("-") or "unknown"


def _normalize_files(files: list[str] | tuple[str, ...] | None, *, base_dir: Path) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in files or []:
        text = str(raw or "").strip()
        if not text:
            continue
        path = Path(text)
        if not path.is_absolute():
            path = (base_dir / text).resolve()
        else:
            path = path.resolve()
        try:
            display = str(path.relative_to(base_dir))
        except ValueError:
            display = str(path)
        if display not in seen:
            seen.add(display)
            normalized.append(display)
    return normalized


class CollaborationBus:
    def __init__(self, base_dir: Path | str | None = None):
        self.base_dir = Path(base_dir or BASE_DIR).resolve()
        self.data_dir = self.base_dir / "data" / "collab_bus"
        self.runtime_dir = self.base_dir / "data" / "runtime"
        self.status_dir = self.data_dir / "status"
        self.lock_path = self.runtime_dir / "collaboration_bus.lock"
        self.queue_path = self.data_dir / "task_queue.json"
        self.claims_path = self.data_dir / "claims.json"
        self.handoffs_path = self.data_dir / "handoffs.json"
        self.blocked_path = self.data_dir / "blocked.json"
        self.messages_path = self.data_dir / "messages.json"

    def initialize(self) -> dict[str, Any]:
        with self._locked():
            self._ensure_layout()
            return self.snapshot(limit=20)

    @contextmanager
    def _locked(self):
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        fd = self.lock_path.open("a+")
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX)
            self._ensure_layout()
            yield
        finally:
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
            fd.close()

    def _ensure_layout(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.status_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_json_file(self.queue_path, {"version": 1, "updated_at": _iso_now(), "tasks": []})
        self._ensure_json_file(self.claims_path, {"version": 1, "updated_at": _iso_now(), "claims": []})
        self._ensure_json_file(self.handoffs_path, {"version": 1, "updated_at": _iso_now(), "handoffs": []})
        self._ensure_json_file(self.blocked_path, {"version": 1, "updated_at": _iso_now(), "blocked": []})
        self._ensure_json_file(self.messages_path, {"version": 1, "updated_at": _iso_now(), "messages": []})

    @staticmethod
    def _ensure_json_file(path: Path, default_payload: dict[str, Any]) -> None:
        if path.exists():
            return
        path.write_text(json.dumps(default_payload, indent=2))

    @staticmethod
    def _read_json(path: Path, default_payload: dict[str, Any]) -> dict[str, Any]:
        try:
            payload = json.loads(path.read_text())
            if isinstance(payload, dict):
                return payload
        except Exception:
            pass
        return dict(default_payload)

    @staticmethod
    def _write_json(path: Path, payload: dict[str, Any]) -> None:
        payload = dict(payload or {})
        payload["updated_at"] = _iso_now()
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(payload, indent=2))
        tmp_path.replace(path)

    def _load_queue(self) -> dict[str, Any]:
        return self._read_json(self.queue_path, {"version": 1, "updated_at": _iso_now(), "tasks": []})

    def _load_claims(self) -> dict[str, Any]:
        return self._read_json(self.claims_path, {"version": 1, "updated_at": _iso_now(), "claims": []})

    def _load_handoffs(self) -> dict[str, Any]:
        return self._read_json(self.handoffs_path, {"version": 1, "updated_at": _iso_now(), "handoffs": []})

    def _load_blocked(self) -> dict[str, Any]:
        return self._read_json(self.blocked_path, {"version": 1, "updated_at": _iso_now(), "blocked": []})

    def _load_messages(self) -> dict[str, Any]:
        return self._read_json(self.messages_path, {"version": 1, "updated_at": _iso_now(), "messages": []})

    def snapshot(self, limit: int = 50) -> dict[str, Any]:
        queue = self._load_queue()
        claims = self._load_claims()
        handoffs = self._load_handoffs()
        blocked = self._load_blocked()
        messages = self._load_messages()
        statuses = {}
        for status_path in sorted(self.status_dir.glob("*.json")):
            try:
                statuses[status_path.stem] = json.loads(status_path.read_text())
            except Exception:
                continue
        return {
            "paths": {
                "queue": str(self.queue_path),
                "claims": str(self.claims_path),
                "handoffs": str(self.handoffs_path),
                "blocked": str(self.blocked_path),
                "messages": str(self.messages_path),
                "status_dir": str(self.status_dir),
            },
            "queue": queue,
            "claims": claims,
            "handoffs": {"handoffs": list((handoffs.get("handoffs") or [])[-max(0, int(limit)):])},
            "blocked": {"blocked": list((blocked.get("blocked") or [])[-max(0, int(limit)):])},
            "messages": {"messages": list((messages.get("messages") or [])[-max(0, int(limit)):])},
            "status": statuses,
        }

    def list_queue(self, include_completed: bool = True) -> dict[str, Any]:
        with self._locked():
            queue = self._load_queue()
            tasks = list(queue.get("tasks") or [])
            if not include_completed:
                tasks = [task for task in tasks if str(task.get("status") or "") != "completed"]
            tasks.sort(
                key=lambda row: (
                    0 if str(row.get("status") or "pending") in {"pending", "claimed"} else 1,
                    -int(row.get("priority") or 0),
                    str(row.get("created_at") or ""),
                    str(row.get("id") or ""),
                )
            )
            return {"tasks": tasks}

    def add_task(
        self,
        *,
        task_id: str,
        title: str,
        summary: str = "",
        priority: int = 50,
        owner_hint: str = "",
        files: list[str] | None = None,
        depends_on: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        token = str(task_id or "").strip()
        if not token:
            raise ValueError("task_id is required")
        with self._locked():
            queue = self._load_queue()
            tasks = list(queue.get("tasks") or [])
            for existing in tasks:
                if str(existing.get("id") or "") == token:
                    raise ValueError(f"task already exists: {token}")
            task = {
                "id": token,
                "title": str(title or token).strip(),
                "summary": str(summary or "").strip(),
                "priority": int(priority or 0),
                "owner_hint": str(owner_hint or "").strip(),
                "files": _normalize_files(files, base_dir=self.base_dir),
                "depends_on": [str(item).strip() for item in (depends_on or []) if str(item).strip()],
                "metadata": dict(metadata or {}),
                "status": "pending",
                "created_at": _iso_now(),
            }
            tasks.append(task)
            queue["tasks"] = tasks
            self._write_json(self.queue_path, queue)
            return task

    def reopen_task(
        self,
        *,
        task_id: str,
        title: str | None = None,
        summary: str | None = None,
        priority: int | None = None,
        owner_hint: str | None = None,
        files: list[str] | None = None,
        depends_on: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        token = str(task_id or "").strip()
        if not token:
            raise ValueError("task_id is required")
        with self._locked():
            queue = self._load_queue()
            task = self._task_by_id(queue, token)
            if not task:
                raise ValueError(f"unknown task: {token}")

            status = str(task.get("status") or "pending")
            if status in {"pending", "claimed", "running"}:
                return task

            task["title"] = str(title or task.get("title") or token).strip()
            task["summary"] = str(summary or task.get("summary") or "").strip()
            if priority is not None:
                task["priority"] = int(priority or 0)
            if owner_hint is not None:
                task["owner_hint"] = str(owner_hint or "").strip()
            if files is not None:
                task["files"] = _normalize_files(files, base_dir=self.base_dir)
            if depends_on is not None:
                task["depends_on"] = [str(item).strip() for item in depends_on if str(item).strip()]
            if metadata is not None:
                task["metadata"] = dict(metadata)

            task["status"] = "pending"
            task["reopened_at"] = _iso_now()
            for key in ("released_by", "released_at", "release_notes", "claimed_by", "claimed_at", "blocked_at"):
                task.pop(key, None)
            self._write_json(self.queue_path, queue)
            return task

    def _task_by_id(self, queue: dict[str, Any], task_id: str) -> dict[str, Any] | None:
        for task in queue.get("tasks") or []:
            if str(task.get("id") or "") == str(task_id or ""):
                return task
        return None

    @staticmethod
    def _claim_conflicts(
        claims: list[dict[str, Any]],
        *,
        agent: str,
        task_id: str,
        files: list[str],
    ) -> list[dict[str, Any]]:
        conflicts = []
        file_set = set(files)
        for claim in claims:
            claim_agent = str(claim.get("agent") or "")
            if claim_agent == agent:
                continue
            claim_task = str(claim.get("task_id") or "")
            claim_files = set(claim.get("files") or [])
            if claim_task == task_id or (file_set and claim_files.intersection(file_set)):
                conflicts.append(claim)
        return conflicts

    def active_claims_by_file(self) -> dict[str, list[dict[str, Any]]]:
        with self._locked():
            claims = self._load_claims()
            by_file: dict[str, list[dict[str, Any]]] = {}
            for claim in claims.get("claims") or []:
                for path in claim.get("files") or []:
                    by_file.setdefault(str(path), []).append(claim)
            return by_file

    def claim_task(
        self,
        *,
        agent: str,
        task_id: str,
        files: list[str] | None = None,
        summary: str = "",
    ) -> dict[str, Any]:
        agent_name = str(agent or "").strip()
        token = str(task_id or "").strip()
        if not agent_name:
            raise ValueError("agent is required")
        if not token:
            raise ValueError("task_id is required")
        with self._locked():
            queue = self._load_queue()
            claims_payload = self._load_claims()
            task = self._task_by_id(queue, token)
            if not task:
                raise ValueError(f"unknown task: {token}")
            normalized_files = _normalize_files(files or task.get("files") or [], base_dir=self.base_dir)
            existing_claims = list(claims_payload.get("claims") or [])
            for claim in existing_claims:
                if str(claim.get("agent") or "") == agent_name and str(claim.get("task_id") or "") == token:
                    return claim
            conflicts = self._claim_conflicts(existing_claims, agent=agent_name, task_id=token, files=normalized_files)
            if conflicts:
                raise ValueError(
                    "claim conflict: "
                    + ", ".join(
                        f"{row.get('agent')}:{row.get('task_id')}"
                        for row in conflicts
                    )
                )
            claim = {
                "agent": agent_name,
                "task_id": token,
                "files": normalized_files,
                "summary": str(summary or task.get("summary") or "").strip(),
                "claimed_at": _iso_now(),
            }
            existing_claims.append(claim)
            claims_payload["claims"] = existing_claims
            task["status"] = "claimed"
            task["claimed_by"] = agent_name
            task["claimed_at"] = claim["claimed_at"]
            task["files"] = normalized_files or list(task.get("files") or [])
            self._write_json(self.claims_path, claims_payload)
            self._write_json(self.queue_path, queue)
            return claim

    def release_task(
        self,
        *,
        agent: str,
        task_id: str,
        outcome: str = "completed",
        notes: str = "",
    ) -> dict[str, Any]:
        agent_name = str(agent or "").strip()
        token = str(task_id or "").strip()
        with self._locked():
            queue = self._load_queue()
            claims_payload = self._load_claims()
            task = self._task_by_id(queue, token)
            if not task:
                raise ValueError(f"unknown task: {token}")
            new_claims = []
            matched = None
            for claim in claims_payload.get("claims") or []:
                if str(claim.get("agent") or "") == agent_name and str(claim.get("task_id") or "") == token:
                    matched = claim
                    continue
                new_claims.append(claim)
            if not matched and str(task.get("claimed_by") or "") not in {"", agent_name}:
                raise ValueError(f"task is claimed by {task.get('claimed_by')}")
            claims_payload["claims"] = new_claims
            task["status"] = str(outcome or "completed").strip()
            task["released_by"] = agent_name
            task["released_at"] = _iso_now()
            task["release_notes"] = str(notes or "").strip()
            task.pop("claimed_by", None)
            task.pop("claimed_at", None)
            self._write_json(self.claims_path, claims_payload)
            self._write_json(self.queue_path, queue)
            return task

    def write_status(
        self,
        *,
        agent: str,
        state: str,
        summary: str = "",
        files: list[str] | None = None,
        task_id: str = "",
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        agent_name = str(agent or "").strip()
        if not agent_name:
            raise ValueError("agent is required")
        with self._locked():
            payload = {
                "agent": agent_name,
                "state": str(state or "").strip() or "unknown",
                "summary": str(summary or "").strip(),
                "task_id": str(task_id or "").strip(),
                "files": _normalize_files(files, base_dir=self.base_dir),
                "details": dict(details or {}),
                "updated_at": _iso_now(),
            }
            status_path = self.status_dir / f"{_slug(agent_name)}.json"
            self._write_json(status_path, payload)
            return payload

    def add_handoff(
        self,
        *,
        from_agent: str,
        to_agent: str,
        task_id: str,
        summary: str,
        files: list[str] | None = None,
        notes: str = "",
    ) -> dict[str, Any]:
        token = str(task_id or "").strip()
        if not token:
            raise ValueError("task_id is required")
        with self._locked():
            handoffs = self._load_handoffs()
            record = {
                "id": f"handoff_{int(time.time() * 1000)}",
                "from_agent": str(from_agent or "").strip(),
                "to_agent": str(to_agent or "").strip(),
                "task_id": token,
                "summary": str(summary or "").strip(),
                "notes": str(notes or "").strip(),
                "files": _normalize_files(files, base_dir=self.base_dir),
                "created_at": _iso_now(),
            }
            rows = list(handoffs.get("handoffs") or [])
            rows.append(record)
            handoffs["handoffs"] = rows[-200:]
            self._write_json(self.handoffs_path, handoffs)
            return record

    def add_blocked(
        self,
        *,
        agent: str,
        task_id: str,
        reason: str,
        files: list[str] | None = None,
    ) -> dict[str, Any]:
        token = str(task_id or "").strip()
        if not token:
            raise ValueError("task_id is required")
        with self._locked():
            blocked = self._load_blocked()
            queue = self._load_queue()
            record = {
                "id": f"blocked_{int(time.time() * 1000)}",
                "agent": str(agent or "").strip(),
                "task_id": token,
                "reason": str(reason or "").strip(),
                "files": _normalize_files(files, base_dir=self.base_dir),
                "created_at": _iso_now(),
            }
            rows = list(blocked.get("blocked") or [])
            rows.append(record)
            blocked["blocked"] = rows[-200:]
            task = self._task_by_id(queue, token)
            if task:
                task["status"] = "blocked"
                task["blocked_at"] = record["created_at"]
                task["blocked_reason"] = record["reason"]
            self._write_json(self.blocked_path, blocked)
            self._write_json(self.queue_path, queue)
            return record

    def list_messages(self, limit: int = 20) -> dict[str, Any]:
        with self._locked():
            messages = self._load_messages()
            rows = list(messages.get("messages") or [])
            return {"messages": rows[-max(1, int(limit)):]}

    def post_message(
        self,
        *,
        from_agent: str,
        to_agent: str = "",
        task_id: str = "",
        message: str,
        files: list[str] | None = None,
    ) -> dict[str, Any]:
        from_name = str(from_agent or "").strip()
        body = str(message or "").strip()
        if not from_name:
            raise ValueError("from_agent is required")
        if not body:
            raise ValueError("message is required")
        with self._locked():
            messages = self._load_messages()
            record = {
                "id": f"msg_{int(time.time() * 1000)}",
                "from_agent": from_name,
                "to_agent": str(to_agent or "").strip(),
                "task_id": str(task_id or "").strip(),
                "message": body,
                "files": _normalize_files(files, base_dir=self.base_dir),
                "created_at": _iso_now(),
            }
            rows = list(messages.get("messages") or [])
            rows.append(record)
            messages["messages"] = rows[-500:]
            self._write_json(self.messages_path, messages)
            return record


def default_bus() -> CollaborationBus:
    return CollaborationBus(BASE_DIR)
