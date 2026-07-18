"""In-thread context builder for inbound iMessage (and any threaded frontend).

THE GAP THIS CLOSES
-------------------
`core.engine` threads conversation history into the primary Claude API path
(`get_history(user_id, thread_id=...)` at engine.py:1309) and the free-pool path
(`_with_history` at engine.py:846). BUT the compound / multi-source synthesis
path — `core.local_compound.handle_compound(user_id, text, tools, frontend)`
(engine.py:814,836) — takes NO thread_id and builds a single stateless
`messages=[{"role":"user", ...}]` turn. Its fast snapshots
(`_fast_day_snapshot` / `_fast_week_snapshot` / `_fast_work_items_snapshot`)
are stateless templates too.

So a follow-up that trips `is_compound_query` — "what about the second one",
"catch me up" said twice, a reworded repeat — lands in a handler with zero
memory of the prior turns and answers dumb.

This module assembles a compact, correct in-thread context block: the last N
turns for THIS thread + relevant recalled memory (redacted_memory.search on the
query) + a light follow-up-reference note, as a string the compound responder
prepends to its user message so the follow-up resolves instead of going dumb.

WIRE-IN (staged, do NOT restart the relay)
------------------------------------------
1. Give `handle_compound` a `thread_id` param and, right before it builds
   `user_msg`, prepend the block:

       from core.openclaw_thread_context import build_thread_context
       ctx = build_thread_context(user_id, thread_id, text)   # "" when no history
       user_msg = (ctx + "\n\n" + user_msg) if ctx else user_msg

   (Also thread it into the `_fast_*_snapshot` returns if you want those
   stateless templates to honor a follow-up; otherwise return None on a
   detected follow-up so it falls through to the synthesizing path.)

2. Pass thread_id at the two call sites in engine.py (814, 836):
       await handle_compound(user_id, clean_text, self.tools, frontend, thread_id=thread_id)

The iMessage bridge already flows the right thread_id
(`_thread_id(handle)` -> "imessage:<handle>") and user_id ("Operator"/"Operator2")
down through `engine.handle_message`, so no relay change is needed.

Deterministic-first, fail-open: any provider error yields an empty block so the
responder degrades to today's stateless behavior — never worse.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger(__name__)

# Keep the block compact — it rides in front of already-large tool context.
DEFAULT_MAX_TURNS = 6
DEFAULT_MAX_MEMORIES = 3
_TURN_CHARS = 240
_MEMORY_CHARS = 220

# Light follow-up-reference signal. Used only to add a hint line for the model
# and to let callers detect "this needs prior turns" cheaply. Intentionally
# conservative — a false negative just means no hint, never a wrong answer.
_FOLLOWUP_MARKERS = (
    "the second", "the first", "the third", "the last", "the other",
    "that one", "this one", "the one", "what about", "how about",
    "and the", "same as", "again", "instead", "it instead",
    "number two", "number one", "#2", "#1", "the 2nd", "the 1st",
)
_PRONOUN_ONLY = {"it", "that", "this", "those", "them", "they", "one"}


@dataclass
class ThreadContext:
    thread_id: str
    turns: list[tuple[str, str]] = field(default_factory=list)  # (speaker, text)
    memories: list[str] = field(default_factory=list)
    resolved_query: str = ""
    is_followup: bool = False

    @property
    def has_context(self) -> bool:
        return bool(self.turns or self.memories)

    def as_block(self) -> str:
        if not self.has_context:
            return ""
        lines: list[str] = [f"[in-thread context — {self.thread_id}]"]
        if self.turns:
            lines.append("recent turns (oldest first):")
            for speaker, body in self.turns:
                lines.append(f"- {speaker}: {body}")
        if self.memories:
            lines.append("relevant recalled memory:")
            for mem in self.memories:
                lines.append(f"- {mem}")
        if self.is_followup:
            lines.append(
                "note: the new message is a follow-up — resolve its reference "
                "(e.g. \"the second one\", \"that\", a reworded repeat) against "
                "the recent turns above before answering."
            )
        return "\n".join(lines)


def _flatten_content(content: Any) -> str:
    """Turn a stored message's content (str or list of blocks) into plain text.
    Mirrors core.engine._flatten_content so blocks render the same way."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(str(block.get("text") or ""))
                elif "text" in block:
                    parts.append(str(block.get("text") or ""))
            elif isinstance(block, str):
                parts.append(block)
        return " ".join(p for p in parts if p).strip()
    if content is None:
        return ""
    return str(content)


def _shorten(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def detect_followup(query: str) -> bool:
    """Cheap heuristic: does this message lean on prior turns to be answerable?"""
    low = " ".join(str(query or "").lower().split())
    if not low:
        return False
    if low.rstrip("?.! ") in _PRONOUN_ONLY:
        return True
    if any(marker in low for marker in _FOLLOWUP_MARKERS):
        return True
    # Very short, pronoun-led messages ("what about it?", "and those?")
    words = low.rstrip("?.! ").split()
    if len(words) <= 4 and any(w in _PRONOUN_ONLY for w in words):
        return True
    return False


def _resolve_query(query: str, history_turns: list[tuple[str, str]]) -> str:
    """Reuse core.intent_resolver if a sibling build provides it; else identity.

    Defensive: the resolver is an optional sibling. Any import/shape mismatch
    falls back to the raw query so this never becomes a failure point."""
    try:
        from core import intent_resolver  # type: ignore
    except Exception:
        return query
    prior = [{"role": ("user" if sp != "You" else "assistant"), "content": body}
             for sp, body in history_turns]
    for attr in ("resolve", "resolve_query", "rewrite", "expand"):
        fn = getattr(intent_resolver, attr, None)
        if not callable(fn):
            continue
        for args in ((query, prior), (query,)):
            try:
                out = fn(*args)
            except TypeError:
                continue
            except Exception as exc:
                log.debug("intent_resolver.%s failed: %s", attr, exc)
                break
            if isinstance(out, dict):
                out = out.get("resolved") or out.get("query") or out.get("text")
            if isinstance(out, str) and out.strip():
                return out.strip()
    return query


def _default_history_provider(user_id: str, thread_id: str, limit: int) -> list[dict]:
    from core import conversation
    try:
        rows = conversation.get_thread_history(user_id, thread_id, limit=limit)
        if rows:
            return rows
    except Exception as exc:
        log.debug("get_thread_history failed (%s); trying get_history", exc)
    try:
        return conversation.get_history(user_id, thread_id=thread_id)
    except Exception as exc:
        log.debug("get_history failed: %s", exc)
        return []


def _default_memory_search(query: str, limit: int) -> list[dict]:
    from core import redacted_memory
    try:
        return redacted_memory.search(query, limit=limit)
    except Exception as exc:
        log.debug("redacted_memory.search failed: %s", exc)
        return []


def build_thread_context_obj(
    user_id: str,
    thread_id: str,
    query: str,
    *,
    max_turns: int = DEFAULT_MAX_TURNS,
    max_memories: int = DEFAULT_MAX_MEMORIES,
    history_provider: Callable[[str, str, int], list[dict]] | None = None,
    memory_search: Callable[[str, int], list[dict]] | None = None,
) -> ThreadContext:
    """Assemble the in-thread context object. Providers are injectable so this
    can be exercised with zero live-service / live-DB access."""
    ctx = ThreadContext(thread_id=str(thread_id or "unknown"))
    if not str(query or "").strip():
        return ctx

    hp = history_provider or _default_history_provider
    ms = memory_search or _default_memory_search

    # Pull a few extra so we can drop an echo of the current query if it was
    # already persisted, then trim to max_turns.
    try:
        raw = hp(user_id, thread_id, max_turns * 2 + 2) or []
    except Exception as exc:
        log.debug("history provider raised: %s", exc)
        raw = []

    cur_norm = " ".join(str(query).lower().split())
    turns: list[tuple[str, str]] = []
    for msg in raw:
        role = (msg or {}).get("role")
        body = _flatten_content((msg or {}).get("content"))
        if not body.strip():
            continue
        # Skip summary scaffolding injected by conversation.get_history.
        if body.startswith("[Previous conversation summary:") or body.startswith(
            "Understood, I have the context"
        ):
            continue
        speaker = "Operator" if role == "user" else "You"
        turns.append((speaker, _shorten(body, _TURN_CHARS)))

    # Drop a trailing echo of the current inbound message if it's already saved.
    if turns:
        last_sp, last_body = turns[-1]
        if last_sp == "Operator" and " ".join(last_body.lower().split()).rstrip(
            "…"
        ).startswith(cur_norm[: _TURN_CHARS - 1].rstrip("…")[:40]):
            turns = turns[:-1]

    ctx.turns = turns[-max_turns:]
    ctx.is_followup = detect_followup(query)
    ctx.resolved_query = _resolve_query(query, ctx.turns)

    # Memory recall keyed off the resolved query (falls back to raw).
    mem_query = ctx.resolved_query or query
    try:
        mems = ms(mem_query, max_memories) or []
    except Exception as exc:
        log.debug("memory search raised: %s", exc)
        mems = []
    out_mems: list[str] = []
    for m in mems[:max_memories]:
        title = str((m or {}).get("title") or "").strip()
        body = str((m or {}).get("body") or "").strip()
        piece = f"{title}: {body}" if title and body else (title or body)
        piece = _shorten(piece, _MEMORY_CHARS)
        if piece:
            out_mems.append(piece)
    ctx.memories = out_mems
    return ctx


def build_thread_context(
    user_id: str,
    thread_id: str,
    query: str,
    *,
    max_turns: int = DEFAULT_MAX_TURNS,
    max_memories: int = DEFAULT_MAX_MEMORIES,
    history_provider: Callable[[str, str, int], list[dict]] | None = None,
    memory_search: Callable[[str, int], list[dict]] | None = None,
) -> str:
    """Convenience: return the ready-to-prepend context block string ("" when
    there's nothing useful, so callers degrade to stateless behavior)."""
    return build_thread_context_obj(
        user_id,
        thread_id,
        query,
        max_turns=max_turns,
        max_memories=max_memories,
        history_provider=history_provider,
        memory_search=memory_search,
    ).as_block()


if __name__ == "__main__":
    # Isolated smoke test — no live relay, no live DB. Synthesizes a 3-turn
    # thread + a context-dependent follow-up via injected fakes, and proves the
    # builder surfaces the prior turns so the follow-up is answerable.
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

    THREAD = "imessage:XPHONEX"

    fake_thread = [
        {"role": "user", "content": "what are my top 3 open tickets"},
        {"role": "assistant", "content": (
            "your top 3 open tickets:\n"
            "1) VAL-1042 rebate connector auth failing\n"
            "2) VAL-1067 SP portal coverage gap\n"
            "3) VAL-1090 monday sprint sync drift"
        )},
    ]
    follow_up = "what about the second one"

    def fake_history(user_id, thread_id, limit):
        assert thread_id == THREAD, thread_id
        return list(fake_thread)[-limit:]

    def fake_memory(query, limit):
        # Pretend memory recall keys off the query and returns a related fact.
        return [{
            "title": "VAL-1067 SP portal coverage gap",
            "body": "prod portal scope drops to 53% when UserOpportunityController "
                    "is 'with sharing'; fix = 'without sharing' + explicit WHERE scope.",
        }][:limit]

    obj = build_thread_context_obj(
        "Operator", THREAD, follow_up,
        history_provider=fake_history, memory_search=fake_memory,
    )
    block = obj.as_block()

    print("=== assembled context block ===")
    print(block)
    print("=== checks ===")

    failures = []

    def check(name, cond):
        print(f"[{'PASS' if cond else 'FAIL'}] {name}")
        if not cond:
            failures.append(name)

    check("block is non-empty", bool(block))
    check("follow-up detected", obj.is_followup)
    check("prior user turn present (top 3 open tickets)",
          "top 3 open tickets" in block)
    check("second-ticket detail present (VAL-1067)", "VAL-1067" in block)
    check("assistant enumeration preserved (numbered 2)",
          "2) VAL-1067" in block)
    check("recalled memory injected (fix detail)", "without sharing" in block)
    check("follow-up resolution hint present",
          "resolve its reference" in block)
    check("current query NOT echoed as a stored turn",
          block.count(follow_up) == 0)

    # Negative case: a fresh thread with no history yields an empty block so the
    # responder degrades to today's stateless behavior (never worse).
    empty = build_thread_context(
        "Operator", "imessage:new", "hey",
        history_provider=lambda u, t, l: [], memory_search=lambda q, l: [],
    )
    check("no-history thread -> empty block (fail-open)", empty == "")

    # Reworded-repeat case: same intent, different words still gets prior turns.
    reworded = build_thread_context_obj(
        "Operator", THREAD, "remind me what my open tickets were again",
        history_provider=fake_history, memory_search=fake_memory,
    )
    check("reworded repeat still surfaces prior turns",
          "top 3 open tickets" in reworded.as_block())

    print("=== result ===")
    if failures:
        print(f"SMOKE TEST FAILED: {failures}")
        sys.exit(1)
    print("SMOKE TEST PASSED")
