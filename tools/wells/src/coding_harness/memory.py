"""Project memory: AGENTS.md knowledge that accumulates across runs.

The harness reads ``AGENTS.md`` (and optional ``.agents/`` notes) from the
workspace root at the start of every run and injects the distilled knowledge
into the planner/architect/coder context. After a successful run, durable
facts (key files, conventions, gotchas, commands) are appended back, so the
harness gets progressively better at operating in a given repo.

Design:
  * Memory lives *in the repo* (version-controlled), not in a side DB — it
    travels with the code and is human-readable/editable.
  * Reads never block a run: missing/corrupt memory is treated as empty.
  * Writes are append-only structured sections with timestamps, so humans can
    review and prune. A size cap keeps the file from growing without bound;
    when exceeded, the oldest entries are summarized into a compact "Lessons"
    block.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path

from coding_harness import safety

AGENTS_FILE = "AGENTS.md"
MEMORY_DIR = ".agents"
MAX_FILE_BYTES = 16_000  # ~4k tokens; keep memory a small slice of context


@dataclass
class Memory:
    """Loaded project memory."""

    text: str
    path: Path | None
    exists: bool

    def section_for_context(self, max_chars: int = 4000) -> str:
        """Compact slice of memory suitable for prompt injection."""
        if not self.text.strip():
            return ""
        body = self.text.strip()
        if len(body) <= max_chars:
            return body
        # Keep the head (purpose/conventions) and the most recent lessons tail.
        head = body[: max_chars // 2]
        tail = body[-max_chars // 2 :]
        return f"{head}\n\n... (memory trimmed; see AGENTS.md) ...\n\n{tail}"


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------


def load(workspace: str | None = None) -> Memory:
    """Read AGENTS.md from the workspace root (empty Memory if absent).

    Tolerant of encoding mismatches: tries UTF-8 first, then the locale default,
    then a lossy UTF-8-with-replacement decode — so a non-UTF-8 file (e.g. written
    by a Windows tool in cp1252) still loads rather than silently disappearing.
    """
    try:
        root = safety.workspace_root(workspace)
    except Exception:
        return Memory(text="", path=None, exists=False)
    path = root / AGENTS_FILE
    if not path.exists():
        return Memory(text="", path=path, exists=False)
    raw = b""
    try:
        raw = path.read_bytes()
    except Exception:
        return Memory(text="", path=path, exists=False)
    text = _decode_lenient(raw)
    return Memory(text=text, path=path, exists=True)


def _decode_lenient(raw: bytes) -> str:
    """Decode bytes as text, trying UTF-8, locale default, then lossy UTF-8."""
    if not raw:
        return ""
    for enc in ("utf-8", "utf-8-sig"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    try:
        import locale

        loc = locale.getpreferredencoding(False)
        if loc:
            return raw.decode(loc)
    except Exception:
        pass
    # Last resort: never raise — replace undecodable bytes.
    return raw.decode("utf-8", errors="replace")


def inject_into_prompt(
    prompt: str,
    workspace: str | None = None,
    *,
    label: str = "PROJECT MEMORY (AGENTS.md)",
) -> str:
    """Prepend a trimmed memory slice to ``prompt`` (no-op when memory is empty)."""
    mem = load(workspace).section_for_context()
    if not mem:
        return prompt
    return f"{label}:\n{mem}\n\n---\n\n{prompt}"


# ---------------------------------------------------------------------------
# Append (write-back)
# ---------------------------------------------------------------------------


_LESSON_HEADER_RE = re.compile(r"^##\s+Lessons Learned\s*$", re.M)


def append_lesson(
    workspace: str | None,
    *,
    goal: str,
    summary: str,
    key_files: list[str] | None = None,
    commands: list[str] | None = None,
    gotchas: list[str] | None = None,
) -> Path | None:
    """Append a structured, timestamped lesson to AGENTS.md.

    Honours the safety gate (writes go through the policy like any other write).
    Returns the path written, or None when skipped (dry-run / denied / empty).
    """
    block = _format_lesson(
        goal=goal,
        summary=summary,
        key_files=key_files or [],
        commands=commands or [],
        gotchas=gotchas or [],
    )
    if not block.strip():
        return None

    try:
        root = safety.workspace_root(workspace)
    except Exception:
        return None
    path = root / AGENTS_FILE

    detail = f"append memory lesson to {AGENTS_FILE}"
    decision = safety.gate("write_file", detail)
    if not decision.allowed:
        return None  # dry-run / denied — silently skip; memory is best-effort

    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    updated = _merge_lesson(existing, block)
    path.write_text(updated, encoding="utf-8")
    return path


def _format_lesson(
    *,
    goal: str,
    summary: str,
    key_files: list[str],
    commands: list[str],
    gotchas: list[str],
) -> str:
    ts = time.strftime("%Y-%m-%d %H:%M")
    lines = [f"### {ts} — {goal.strip()[:120]}"]
    if summary.strip():
        lines.append(f"Summary: {summary.strip()[:400]}")
    if key_files:
        lines.append("Key files: " + ", ".join(key_files[:15]))
    if commands:
        lines.append("Commands: " + " | ".join(commands[:8]))
    if gotchas:
        for g in gotchas[:6]:
            lines.append(f"- gotcha: {g.strip()[:200]}")
    return "\n".join(lines).strip()


def _merge_lesson(existing: str, block: str) -> str:
    """Insert ``block`` under a ``## Lessons Learned`` section.

    Creates the section if absent; preserves everything else in the file.
    Trims oldest entries when the file exceeds the size cap.
    """
    if not existing.strip():
        header = (
            "# AGENTS.md — project memory for the Wells harness\n\n"
            "This file accumulates durable facts the agent has learned about this repo.\n"
            "Edit or prune freely; it is version-controlled with the code.\n\n"
            "## Lessons Learned\n\n"
        )
        return header + block + "\n"

    if _LESSON_HEADER_RE.search(existing):
        # Insert right after the header line.
        def _push(m: re.Match) -> str:
            return m.group(0) + "\n" + block + "\n"

        updated = _LESSON_HEADER_RE.sub(_push, existing, count=1)
    else:
        # No section yet; append one.
        updated = existing.rstrip() + "\n\n## Lessons Learned\n\n" + block + "\n"

    if len(updated.encode("utf-8")) > MAX_FILE_BYTES:
        updated = _compact_oldest(updated)
    return updated


def _compact_oldest(text: str) -> str:
    """When memory is too large, fold older `### timestamp` blocks into one summary."""
    blocks = re.split(r"(?=^### \d{4}-\d{2}-\d{2})", text, flags=re.M)
    if len(blocks) <= 3:
        return text
    head, rest = blocks[0], blocks[1:]
    # Keep the most recent half; summarize the older half by counting them.
    keep_n = max(1, len(rest) // 2)
    old, recent = rest[:-keep_n], rest[-keep_n:]
    n_old = len([b for b in old if b.strip()])
    compacted = (
        head.rstrip()
        + f"\n\n_(compacted {n_old} older lessons to stay within memory budget)_\n\n"
        + "".join(recent)
    )
    return compacted
