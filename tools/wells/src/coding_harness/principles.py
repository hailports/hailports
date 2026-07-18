"""Behavioral principles for every agent in the harness (the AGENT.md constitution).

These are the harness's own operating rules — distinct from per-project
``AGENTS.md`` memory. They govern **how** every agent reasons and acts,
regardless of which model the user has configured. The principles are always
active and injected into the system prompt of every agent node (planner,
architect, coder, tester, reviewer) and the executor loop.

Resolution order (highest precedence first):
  1. ``$WELLS_PRINCIPLES`` env var — point at an arbitrary file path.
  2. ``AGENT.md`` in the workspace root — lets a team customize the principles
     for a specific project (version-controlled with that project).
  3. The bundled ``AGENT.md`` shipped inside the package — the default
     constitution. Always present.

This guarantees the harness always has a behavioral baseline, even when a user
has configured no project memory at all.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

# The bundled default, shipped inside the package so it survives `pip install`.
BUNDLED_PATH = Path(__file__).parent / "AGENT.md"


def _bundled_text() -> str:
    """Read the bundled AGENT.md (the default constitution)."""
    try:
        return BUNDLED_PATH.read_text(encoding="utf-8")
    except Exception:
        return ""


def find_principles_file(workspace: str | None = None) -> Path | None:
    """Resolve which AGENT.md file is in effect, or None if only the bundled default applies.

    Precedence: $WELLS_PRINCIPLES > <workspace>/AGENT.md > bundled default.
    Returns the chosen path (or None when falling back to the bundled default).
    """
    env_path = os.environ.get("WELLS_PRINCIPLES", "").strip()
    if env_path and Path(env_path).is_file():
        return Path(env_path)
    if workspace:
        ws_file = Path(workspace) / "AGENT.md"
        if ws_file.is_file():
            return ws_file
    return None


@lru_cache(maxsize=8)
def _load_cached(path_key: str) -> str:
    """Cache loaded principles by resolved path key."""
    if path_key == "__bundled__":
        return _bundled_text()
    try:
        return Path(path_key).read_text(encoding="utf-8")
    except Exception:
        return _bundled_text()


def principles_text(workspace: str | None = None) -> str:
    """Return the active principles text (raw markdown)."""
    path = find_principles_file(workspace)
    key = str(path) if path else "__bundled__"
    return _load_cached(key)


def principles_block(workspace: str | None = None, *, max_chars: int = 4000) -> str:
    """Return the principles formatted as a labeled block for system prompts.

    Always returns a non-empty string (falls back to the bundled default), so
    every agent invocation is governed by the constitution. The block is
    labeled so the model knows these are mandatory operating rules, not context.
    """
    text = principles_text(workspace).strip()
    if not text:
        return ""
    if len(text) > max_chars:
        # Keep the head (the rules) and the guiding-principle footer.
        head = text[: max_chars - 400]
        tail = text[-300:]
        text = f"{head}\n... (principles trimmed) ...\n{tail}"
    return (
        "=== HARNESS OPERATING PRINCIPLES (AGENT.md) ===\n"
        "These rules govern how you operate. Follow them at all times.\n"
        f"{text}\n"
        "=== END PRINCIPLES ===\n"
    )


def source_label(workspace: str | None = None) -> str:
    """Human-readable label of where the active principles came from."""
    path = find_principles_file(workspace)
    if path is None:
        return "bundled (default)"
    return str(path)


def clear_cache() -> None:
    """Drop cached principles text (used when overrides change at runtime)."""
    _load_cached.cache_clear()


def inject_into_prompt(prompt: str, workspace: str | None = None) -> str:
    """Prepend the principles block to ``prompt`` (no-op if principles are empty).

    This is the single chokepoint every agent uses, so the constitution is
    guaranteed present in every system prompt with zero per-agent wiring.
    """
    block = principles_block(workspace)
    if not block:
        return prompt
    return f"{block}\n{prompt}"
