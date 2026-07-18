"""Anthropic prompt-caching helper.

Adds `cache_control: {"type": "ephemeral"}` breakpoints to the LARGE STABLE
prefix of a Messages-API request (system prompt + any big fixed context block +
prior conversation turns) so repeated-context calls hit the cache (~0.1x read
cost vs full price). The breakpoint always lands on the stable prefix, never on
the volatile final user turn.

Anthropic rules respected:
  - Caching is a PREFIX match: a breakpoint caches everything rendered up to it
    (tools -> system -> messages). Stable content must physically precede the
    volatile turn, so breakpoints go on the LAST stable block only.
  - Max 4 breakpoints per request.
  - Minimum cacheable prefix is model-dependent (Opus 4.8: 4096 tok;
    Fable 5 / Sonnet 4.6: 2048; Sonnet 4.5: 1024). A shorter prefix silently
    won't cache and still bills the ~1.25x write premium -> we SKIP it (no
    breakpoint, no error) rather than pay for a cache nobody reads.

Pure/inert: does no I/O, imports nothing heavy, mutates nothing (deep-copies the
inputs). Safe to call on the hot path; the live api_client is not edited here.

WIRE-IN (staged, one line) in core/api_client.py create_message(), right after
the `kwargs = {...}` / `if tools: kwargs["tools"]=...` block (~line 694):

    from core.prompt_cache import apply_cache_control
    kwargs["system"], kwargs["messages"] = apply_cache_control(
        kwargs["system"], kwargs["messages"], model=model)

`_build_system` already caches the single system block; apply_cache_control
leaves that intact and ADDS the prior-turn / fixed-context breakpoints that the
hot path currently misses.
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_EPHEMERAL = {"type": "ephemeral"}

# Per-model minimum cacheable prefix, in tokens. A prefix below the floor
# silently won't cache, so we don't place a breakpoint there.
_MIN_TOKENS_BY_PREFIX = {
    "claude-opus-4-8": 4096,
    "claude-opus-4-7": 4096,
    "claude-opus-4-6": 4096,
    "claude-opus-4-5": 4096,
    "claude-haiku-4-5": 4096,
    "claude-fable-5": 2048,
    "claude-mythos-5": 2048,
    "claude-sonnet-4-6": 2048,
    "claude-sonnet-4-5": 1024,
    "claude-sonnet-5": 1024,
    "claude-sonnet-4": 1024,
    "claude-3-7-sonnet": 1024,
}
_DEFAULT_MIN_TOKENS = 1024  # absolute API floor when the model is unknown


def min_cache_tokens_for(model: str | None) -> int:
    """Minimum cacheable-prefix size (tokens) for a model id. Conservative:
    unknown models get the absolute API floor (1024)."""
    m = str(model or "").strip().lower()
    for prefix, floor in _MIN_TOKENS_BY_PREFIX.items():
        if m.startswith(prefix):
            return floor
    return _DEFAULT_MIN_TOKENS


def _estimate_tokens(char_count: int) -> int:
    """~4 chars/token rough estimate. Deliberately conservative (we round DOWN
    the effective size by using the ceiling divisor) so we err toward skipping a
    marginal prefix rather than paying a write premium that never reads back."""
    return char_count // 4


def _block_text_len(block) -> int:
    """Characters of cacheable text in a content block or raw string."""
    if isinstance(block, str):
        return len(block)
    if isinstance(block, dict):
        t = block.get("text")
        if isinstance(t, str):
            return len(t)
        # documents / tool_result carry text nested under content
        c = block.get("content")
        if isinstance(c, str):
            return len(c)
        if isinstance(c, list):
            return sum(_block_text_len(b) for b in c)
    return 0


def _content_len(content) -> int:
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(_block_text_len(b) for b in content)
    return 0


def _has_cache_control(block) -> bool:
    return isinstance(block, dict) and "cache_control" in block


def _mark(block: dict) -> dict:
    block["cache_control"] = _EPHEMERAL
    return block


def apply_cache_control(system, messages, *, model=None, min_tokens=None,
                        max_breakpoints: int = 4):
    """Return (system, messages) copies with cache_control breakpoints on the
    large stable prefix. Never touches the volatile final user turn.

    Breakpoint placement, in priority order (capped at `max_breakpoints`, and at
    Anthropic's hard limit of 4):
      1. End of the system prompt (last system block), if the system text meets
         the model's minimum cacheable size.
      2. Last content block of the last STABLE message (the turn(s) preceding the
         final user turn) — enables multi-turn prefix reuse.
      3. A big FIXED context block inside the final user turn that precedes the
         volatile question (the shared-prefix / varying-suffix pattern) — only
         when that message has multiple blocks and the leading block is large.

    A prefix smaller than the model's minimum is skipped silently (no marker, no
    error) — placing one there would bill the ~1.25x write premium for a cache
    that never reads back.
    """
    max_bp = max(0, min(int(max_breakpoints), 4))
    floor = int(min_tokens) if min_tokens is not None else min_cache_tokens_for(model)
    system = copy.deepcopy(system)
    messages = copy.deepcopy(messages)

    used = 0

    # (1) System prefix.
    system = _apply_system(system, floor)
    if _system_has_breakpoint(system):
        used += 1

    if not isinstance(messages, list) or not messages:
        return system, messages

    # Identify the volatile turn: the final user message we're answering.
    last = messages[-1]
    volatile_is_user = isinstance(last, dict) and last.get("role") == "user"
    stable_msgs = messages[:-1] if volatile_is_user else messages[:]

    # (2) Last stable message (prior conversation turns).
    if used < max_bp and stable_msgs:
        cumulative = sum(_content_len(m.get("content")) for m in stable_msgs
                         if isinstance(m, dict))
        if _estimate_tokens(cumulative) >= floor:
            if _mark_last_block(stable_msgs[-1]):
                used += 1

    # (3) Big fixed context block inside the final user turn, before the
    #     volatile question. Only when that turn has >=2 blocks and the leading
    #     block(s) are large enough on their own.
    if used < max_bp and volatile_is_user:
        _mark_leading_context(last, floor)  # counts internally; safe if it no-ops

    return system, messages


def _apply_system(system, floor: int):
    """Attach cache_control to the last system block if the system prompt meets
    the size floor. Leaves an already-marked system untouched."""
    if system is None:
        return system
    if isinstance(system, str):
        if _estimate_tokens(len(system)) >= floor:
            return [_mark({"type": "text", "text": system})]
        return system  # too small — leave as plain string, uncached
    if isinstance(system, list) and system:
        if any(_has_cache_control(b) for b in system):
            return system  # caller (e.g. _build_system) already marked it
        total = sum(_block_text_len(b) for b in system)
        if _estimate_tokens(total) >= floor:
            last = system[-1]
            if isinstance(last, dict):
                _mark(last)
    return system


def _system_has_breakpoint(system) -> bool:
    return isinstance(system, list) and any(_has_cache_control(b) for b in system)


def _mark_last_block(message) -> bool:
    """Put a breakpoint on the last content block of a message. Converts a raw
    string content into a single text block so the marker has somewhere to live.
    Returns True if a breakpoint was placed."""
    if not isinstance(message, dict):
        return False
    content = message.get("content")
    if isinstance(content, str):
        message["content"] = [_mark({"type": "text", "text": content})]
        return True
    if isinstance(content, list) and content:
        last = content[-1]
        if isinstance(last, dict):
            if _has_cache_control(last):
                return False
            _mark(last)
            return True
    return False


def _mark_leading_context(message, floor: int) -> bool:
    """If the final user turn is [big fixed context..., varying question], cache
    the last LARGE leading block (never the trailing question block)."""
    if not isinstance(message, dict):
        return False
    content = message.get("content")
    if not isinstance(content, list) or len(content) < 2:
        return False
    # Everything except the final (volatile) block is candidate prefix.
    prefix_blocks = content[:-1]
    prefix_chars = sum(_block_text_len(b) for b in prefix_blocks)
    if _estimate_tokens(prefix_chars) < floor:
        return False
    target = prefix_blocks[-1]
    if isinstance(target, dict) and not _has_cache_control(target):
        _mark(target)
        return True
    return False


def _count_breakpoints(system, messages) -> int:
    n = 0
    if isinstance(system, list):
        n += sum(1 for b in system if _has_cache_control(b))
    for m in (messages or []):
        if isinstance(m, dict) and isinstance(m.get("content"), list):
            n += sum(1 for b in m["content"] if _has_cache_control(b))
    return n


def _smoke() -> int:
    big_system = "You are a helpful assistant. " * 400  # ~11k chars -> ~2.8k tok
    short_user = "hi, what's 2+2?"
    messages = [{"role": "user", "content": short_user}]

    print("=== BEFORE ===")
    print(f"system: str len={len(big_system)} (~{_estimate_tokens(len(big_system))} tok)")
    print(f"messages: {messages}")

    system_out, messages_out = apply_cache_control(
        big_system, messages, model="claude-opus-4-8")

    print("\n=== AFTER (model=claude-opus-4-8, floor=4096 tok) ===")
    print(f"system blocks: {[{k: (v if k!='text' else f'<{len(v)} chars>') for k,v in b.items()} for b in system_out] if isinstance(system_out, list) else system_out}")
    print(f"messages: {messages_out}")

    ok = True

    # Opus floor is 4096 tok; ~2.8k-tok system is BELOW it -> must skip, stay str.
    if isinstance(system_out, list):
        print("!! system cached under Opus floor (should have skipped)")
        ok = False

    # Same request on Sonnet (floor 1024) -> system SHOULD cache.
    sys2, msg2 = apply_cache_control(big_system, messages, model="claude-sonnet-4-5")
    if not (_system_has_breakpoint(sys2)):
        print("!! system not cached on sonnet floor (should have)")
        ok = False
    else:
        print("\nsonnet: system prefix cached ✓")

    # The volatile user turn must NEVER carry a breakpoint.
    def _user_marked(msgs):
        m = msgs[-1]
        c = m.get("content")
        if isinstance(c, list):
            return any(_has_cache_control(b) for b in c)
        return False
    if _user_marked(msg2):
        print("!! breakpoint landed on the volatile user turn")
        ok = False
    else:
        print("volatile user turn left uncached ✓")

    # Multi-turn: a large prior turn should get a breakpoint; final user turn not.
    big_prior = "context blob. " * 500  # ~7k chars -> ~1.7k tok
    convo = [
        {"role": "user", "content": big_prior},
        {"role": "assistant", "content": "acknowledged."},
        {"role": "user", "content": "now summarize."},
    ]
    _, convo_out = apply_cache_control(None, convo, model="claude-sonnet-4-5")
    stable_marked = _count_breakpoints(None, convo_out[:-1]) >= 1
    final_clean = not _user_marked(convo_out)
    print(f"\nmulti-turn: stable prefix cached={stable_marked} final-turn clean={final_clean}")
    if not (stable_marked and final_clean):
        print("!! multi-turn placement wrong")
        ok = False

    # Shared-prefix pattern: [big doc, short question] -> doc cached, question not.
    doc = "REFERENCE DOCUMENT. " * 400  # ~8k chars
    sp = [{"role": "user", "content": [
        {"type": "text", "text": doc},
        {"type": "text", "text": "what does it say?"},
    ]}]
    _, sp_out = apply_cache_control(None, sp, model="claude-sonnet-4-5")
    blocks = sp_out[-1]["content"]
    doc_cached = _has_cache_control(blocks[0])
    q_clean = not _has_cache_control(blocks[-1])
    print(f"shared-prefix: doc-block cached={doc_cached} question-block clean={q_clean}")
    if not (doc_cached and q_clean):
        print("!! shared-prefix placement wrong")
        ok = False

    # Breakpoint cap never exceeds 4.
    total = _count_breakpoints(sys2, msg2)
    print(f"\ntotal breakpoints (single-turn): {total} (<=4 ✓)" if total <= 4 else f"!! {total} breakpoints > 4")
    ok = ok and total <= 4

    print("\nSMOKE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_smoke())
