"""Guards for external LLM spend."""

from __future__ import annotations

import os

FALSE_VALUES = {"0", "false", "no", "off"}
TRUE_VALUES = {"1", "true", "yes", "on"}


def _falsy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in FALSE_VALUES


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in TRUE_VALUES


def paid_llm_api_enabled() -> bool:
    return not _truthy("CLAUDE_STACK_DISABLE_PAID_LLM_API")


def paid_agent_processes_enabled() -> bool:
    return not _truthy("CLAUDE_STACK_DISABLE_PAID_AGENT_HANDOFFS")


def external_free_llm_enabled() -> bool:
    return not _truthy("CLAUDE_STACK_DISABLE_EXTERNAL_FREE_LLM")


def paid_llm_block_message(action: str = "Paid external LLM API call") -> str:
    return f"{action} blocked: set CLAUDE_STACK_DISABLE_PAID_LLM_API=1 to disable."


def paid_agent_block_message(action: str = "Paid external agent process") -> str:
    return f"{action} blocked: set CLAUDE_STACK_DISABLE_PAID_AGENT_HANDOFFS=1 to disable."


def external_free_llm_block_message(action: str = "External free LLM pool") -> str:
    return f"{action} blocked: set CLAUDE_STACK_DISABLE_EXTERNAL_FREE_LLM=1 to disable."


def require_paid_llm_api(action: str = "Paid external LLM API call") -> None:
    if not paid_llm_api_enabled():
        raise RuntimeError(paid_llm_block_message(action))


def paid_llm_ok(action: str = "Paid external LLM API call", *, lane: str = "auto") -> bool:
    """Non-raising paid-LLM gate. Returns True only if paid spend is permitted.

    smart_router imports this; when it was missing the import raised and the
    caller fell open (``_floor_ok = True``), letting the paid floor spend anyway.
    """
    return paid_llm_api_enabled()


def require_paid_agent_process(action: str = "Paid external agent process") -> None:
    if not paid_agent_processes_enabled():
        raise RuntimeError(paid_agent_block_message(action))
