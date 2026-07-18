"""Corp-tenant routing gate.

Work / corp-brand / Salesforce / inbox automations must run on the CORPORATE Codex
lane (Enterprise ``~/.codex`` auth, flat-rate) and must NEVER spill onto Operator's
personal ChatGPT sub, personal API keys, OpenRouter credit, or local machine.

An agent is recognized as corp-tenant in one of two ways:
  1. Explicit env override ``AGENT_TENANT=corp`` (or ``=personal``/``=hustle`` to
     force it OFF for a process that would otherwise match the allowlist).
  2. Its entry module / script matches the corp allowlist below.

The gate is consulted at the shared LLM chokepoints
(``llm_router.try_local_then_api`` and ``free_llm_pool.try_free_providers``).
When active, the prompt is routed to corp Codex and, on failure, raises loudly
rather than silently falling back to Operator's personal/local resources.
"""
from __future__ import annotations

import logging
import os
import sys

log = logging.getLogger(__name__)

# ``python -m <module>`` entrypoints that are corporate work.
CORP_AGENT_MODULES = {
    "agents.inbox_triage",
    "agents.sf_ticket_agent",
    "core.inbox_agent_pipeline",
    "core.work_rag",
    "apps.sf_copilot_service",
    "apps.chatgpt_work_action",
    "apps.chatgpt_sf_admin_action",
    "tools.redacted_sentinel",
    "tools.redacted_action_watchdog",
    "tools.work_action_learner",
}

# Bare script basenames (``python3 path/to/x.py``) and shell-wrapped python
# entries that are corporate work.
CORP_AGENT_SCRIPTS = {
    "work_followup_engine.py",
    "work_sf_audit.py",
    "work_brain_parity.py",
    "redacted_board_digest.py",
    "discern.py",         # outlook-triage/run_triage.sh
    "make_sweep_ids.py",  # outlook-triage/run_triage.sh
}


def _main_module_name() -> str:
    main = sys.modules.get("__main__")
    spec = getattr(main, "__spec__", None)
    return getattr(spec, "name", "") or ""


def _main_script_basename() -> str:
    argv0 = sys.argv[0] if sys.argv else ""
    return os.path.basename(argv0) if argv0 else ""


def is_corp_tenant() -> bool:
    """True when the running process must be pinned to the corp Codex lane.

    Signals, in priority order:
      1. ``AGENT_TENANT=personal|hustle`` -> hard OFF (explicit escape hatch).
      2. ``AGENT_TENANT=corp`` -> ON.
      3. ``AGENT_BRAND`` set to the corp brand -> ON. This is the load-bearing
         signal: it is set process-wide on every corp automation, so ALL corp work
         is pinned
         to corp Codex regardless of which module/script was the entrypoint (the
         allowlists below can never be exhaustive; the brand env can).
      4. Entry module / script matches the corp allowlist.
    """
    env = os.environ.get("AGENT_TENANT", "").strip().lower()
    if env in ("personal", "hustle"):
        return False
    if env == "corp":
        return True
    if os.environ.get("AGENT_BRAND", "").strip().lower() == "CompanyA":  # pii-allow
        return True
    if _main_module_name() in CORP_AGENT_MODULES:
        return True
    if _main_script_basename() in CORP_AGENT_SCRIPTS:
        return True
    return False


async def run_corp_codex(
    prompt: str,
    *,
    system: str | None = None,
    max_tokens: int = 4096,
    source: str = "",
    cwd: str | None = None,
) -> str:
    """Run ``prompt`` on the corporate Codex lane (Enterprise ``~/.codex``).

    Raises ``RuntimeError`` on failure -- corp tasks must never silently fall
    back to Operator's personal/local resources.
    """
    import uuid

    from core.codex_cli_bridge import _run_codex_exec  # lazy: avoid import cycle

    full = f"{system}\n\n{prompt}" if system else prompt
    job_id = "corp-" + uuid.uuid4().hex[:12]
    try:
        timeout_s = int(os.environ.get("AGENT_RUN_TIMEOUT", "900"))
    except ValueError:
        timeout_s = 900
    model = os.environ.get("AGENT_RUN_CODEX_MODEL") or "gpt-5.6-sol"
    reasoning = os.environ.get("AGENT_RUN_CODEX_REASONING") or "medium"

    ok, text, _path = await _run_codex_exec(
        full,
        job_id=job_id,
        timeout_s=timeout_s,
        model=model,
        reasoning=reasoning,
        cwd=cwd,
        corp=True,
    )
    if not ok:
        raise RuntimeError(
            f"corp Codex lane failed (source={source!r}); refusing to fall back "
            f"to personal/local resources: {text[:400]}"
        )
    log.info("corp Codex lane handled source=%s (%d chars)", source, len(text))
    return text
