"""Shared savings math for local LLM calls."""

from __future__ import annotations

from typing import Any


# Conservative fallback rates for audit-only estimates. These are Haiku-class
# rates per 1K tokens, not Opus-class rates. Audit estimates must not be shown
# as saved spend unless a caller explicitly writes a qualified savings row.
CLAUDE_BASELINE_INPUT_PER_1K = 0.00025
CLAUDE_BASELINE_OUTPUT_PER_1K = 0.00125
CHARS_PER_TOKEN = 4


def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def estimate_claude_baseline_savings(
    *,
    input_tokens: float = 0,
    output_tokens: float = 0,
    input_chars: float = 0,
    output_chars: float = 0,
) -> float:
    """Estimate local-model savings against the Claude paid baseline.

    Baseline:
      - $0.00025 / 1K input tokens
      - $0.00125 / 1K output tokens
    """
    in_tokens = _num(input_tokens)
    out_tokens = _num(output_tokens)
    if in_tokens <= 0 and input_chars:
        in_tokens = _num(input_chars) / CHARS_PER_TOKEN
    if out_tokens <= 0 and output_chars:
        out_tokens = _num(output_chars) / CHARS_PER_TOKEN
    saved = (in_tokens / 1000.0) * CLAUDE_BASELINE_INPUT_PER_1K
    saved += (out_tokens / 1000.0) * CLAUDE_BASELINE_OUTPUT_PER_1K
    return round(saved, 6)


def estimate_entry_savings(entry: dict[str, Any]) -> float:
    """Estimate savings from any local LLM ledger shape used in the stack."""
    input_tokens = _num(entry.get("input_tokens") or entry.get("input"))
    output_tokens = _num(entry.get("output_tokens") or entry.get("output"))
    input_chars = _num(entry.get("input_chars") or entry.get("in_chars"))
    output_chars = _num(entry.get("output_chars") or entry.get("out_chars"))
    return estimate_claude_baseline_savings(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        input_chars=input_chars,
        output_chars=output_chars,
    )
