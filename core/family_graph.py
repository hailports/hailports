"""Family graph derivation from the durable family memory store."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from core.family_memory import (
    isoformat_utc,
    list_memories,
    load_memory_state,
    subject_key,
)


def _add_node(nodes: dict[str, dict[str, Any]], label: str) -> dict[str, Any]:
    key = subject_key(label)
    node = nodes.setdefault(
        key,
        {
            "id": key,
            "label": str(label or "Unknown").strip() or "Unknown",
            "memory_ids": [],
            "kinds": set(),
            "pending_confirmation": 0,
            "last_seen_at": "",
        },
    )
    return node


def build_family_graph(memories: list[dict[str, Any]], *, now: datetime | None = None) -> dict[str, Any]:
    nodes: dict[str, dict[str, Any]] = {}
    edges: dict[tuple[str, str, str], dict[str, Any]] = {}
    pending_confirmation: list[dict[str, Any]] = []
    open_loops: list[dict[str, Any]] = []

    for memory in memories:
        if str(memory.get("status") or "active") != "active":
            continue
        subject = str(memory.get("subject") or "Unknown").strip() or "Unknown"
        node = _add_node(nodes, subject)
        if memory.get("id"):
            node["memory_ids"].append(memory["id"])
        if memory.get("kind"):
            node["kinds"].add(memory["kind"])
        if memory.get("requires_confirmation") and not memory.get("confirmed_by_user"):
            node["pending_confirmation"] += 1
            pending_confirmation.append(
                {
                    "memory_id": memory.get("id"),
                    "subject": subject,
                    "kind": memory.get("kind"),
                    "content": memory.get("content"),
                }
            )
        last_seen = str(memory.get("last_seen_at") or "")
        if last_seen and (not node["last_seen_at"] or last_seen > node["last_seen_at"]):
            node["last_seen_at"] = last_seen

        if memory.get("kind") in {"open_loop", "obligation", "warning"}:
            open_loops.append(
                {
                    "memory_id": memory.get("id"),
                    "subject": subject,
                    "kind": memory.get("kind"),
                    "content": memory.get("content"),
                    "expires_at": memory.get("expires_at") or "",
                }
            )

        for relation in list(memory.get("relations") or []):
            target_label = str(relation.get("target") or "").strip()
            relation_type = str(relation.get("type") or "").strip()
            if not target_label or not relation_type:
                continue
            target_node = _add_node(nodes, target_label)
            edge_key = (node["id"], target_node["id"], relation_type)
            edge = edges.setdefault(
                edge_key,
                {
                    "source": node["id"],
                    "target": target_node["id"],
                    "type": relation_type,
                    "memory_ids": [],
                    "confidence": 0.0,
                },
            )
            if memory.get("id"):
                edge["memory_ids"].append(memory["id"])
            edge["confidence"] = max(float(edge.get("confidence") or 0.0), float(relation.get("confidence") or memory.get("confidence") or 0.0))
            if relation.get("notes"):
                edge["notes"] = relation["notes"]

    node_rows = []
    for node in nodes.values():
        node_rows.append(
            {
                "id": node["id"],
                "label": node["label"],
                "memory_ids": sorted(set(node["memory_ids"])),
                "memory_count": len(set(node["memory_ids"])),
                "kinds": sorted(kind for kind in node["kinds"] if kind),
                "pending_confirmation": int(node["pending_confirmation"]),
                "last_seen_at": node["last_seen_at"],
            }
        )
    edge_rows = []
    for edge in edges.values():
        edge_rows.append(
            {
                "source": edge["source"],
                "target": edge["target"],
                "type": edge["type"],
                "memory_ids": sorted(set(edge["memory_ids"])),
                "confidence": round(float(edge.get("confidence") or 0.0), 3),
                **({"notes": edge["notes"]} if edge.get("notes") else {}),
            }
        )

    return {
        "generated_at": isoformat_utc(now),
        "node_count": len(node_rows),
        "edge_count": len(edge_rows),
        "nodes": sorted(node_rows, key=lambda item: (item["label"].lower(), item["id"])),
        "edges": sorted(edge_rows, key=lambda item: (item["source"], item["target"], item["type"])),
        "pending_confirmation": sorted(pending_confirmation, key=lambda item: ((item.get("subject") or "").lower(), item.get("memory_id") or "")),
        "open_loops": sorted(open_loops, key=lambda item: ((item.get("subject") or "").lower(), item.get("memory_id") or "")),
    }


def load_family_graph(
    *,
    entities_path: Path | None = None,
    events_path: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    state = load_memory_state(entities_path=entities_path, events_path=events_path, now=now, prune_expired=True)
    return build_family_graph(list(state.get("memories") or []), now=now)


def family_subjects(
    *,
    entities_path: Path | None = None,
    events_path: Path | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    state = load_memory_state(entities_path=entities_path, events_path=events_path, now=now, prune_expired=True)
    subjects = state.get("subjects") or {}
    return [dict(value) for _, value in sorted(subjects.items())]


def family_subject_memories(
    subject: str,
    *,
    entities_path: Path | None = None,
    events_path: Path | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    return list_memories(subject=subject, entities_path=entities_path, events_path=events_path, now=now)

