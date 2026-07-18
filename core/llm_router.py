"""LLM routing — local-first with smart tier selection + cloud escalation.

JIT local tiers on the Mini:
    FAST_LOCAL_MODEL      small resident model for short/simple work
    MODEL_QUALITY         larger on-demand model for complex work

    FAST tier             prepends `/no_think` to skip reasoning tokens
    QUALITY tier          full thinking/model capacity for reasoning/synthesis

Pattern:
    1. Auto-pick tier (or caller specifies) based on prompt shape.
    2. Call local_client.generate with the tier-adjusted prompt.
    3. Validate the response.
    4. Only on failure or invalid output → escalate to API (haiku/sonnet/opus).
    5. Log displaced cost to savings.jsonl on local success.

Auto-selection heuristic:
    - Short prompts (<3000 chars) with no "heavy" keyword markers → FAST
    - Long prompts OR multi-step/reasoning/code markers → QUALITY
    - Caller can override with local_model="fast" / "quality" / LOCAL_MODEL.

Use:
    from core.llm_router import try_local_then_api

    text, source = await try_local_then_api(
        prompt, api_fn=..., validator=..., displaced_tier="sonnet",
        source="my_agent:my_step",
        local_model="fast",       # or "quality", or omit for auto
    )

`source` is "local" if the local model handled it, "api" if we escalated.
"""

import asyncio
import fcntl
import json
import logging
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from core import BASE_DIR, local_client, mountain_now
from core.constants import FAST_LOCAL_MODEL, LOCAL_MODEL
from core.free_llm_pool import force_local_routing, free_pool_allowed, hard_force_local_routing
from core.local_model_registry import get_active_local_model
from core.policy_levers import evaluate_external_request
from core.runtime_pressure import get_runtime_pressure

log = logging.getLogger(__name__)

SAVINGS_LOG = BASE_DIR / "data" / "logs" / "savings.jsonl"

# Fast-tier local attempts get a SHORT timeout: if the local box can't turn a
# short/simple prompt around quickly, the instant free lane (groq ~0.2s) is both
# free AND faster, so we should spill there rather than stall up to local_timeout.
# Quality/long tiers keep the full timeout. Override via env.
FAST_LOCAL_TIMEOUT = float(os.environ.get("CLAUDE_STACK_FAST_LOCAL_TIMEOUT", "25"))

MODEL_FAST    = os.environ.get("CLAUDE_STACK_JIT_FAST_MODEL", FAST_LOCAL_MODEL)
MODEL_QUALITY = os.environ.get(
    "CLAUDE_STACK_JIT_QUALITY_MODEL",
    os.environ.get("CLAUDE_STACK_QUALITY_LOCAL_MODEL", get_active_local_model(LOCAL_MODEL)),
)

# --- Optional on-demand MLX "strong" local server (load-on-demand, NOT resident) ---
# A big MoE (Qwen3-30B-A3B) served by scripts/mlx_strong_server.sh on port 8080.
# OFF by default: only used when CLAUDE_STACK_MLX_STRONG=1 AND the server is up.
# Probe-first — a down server costs one short connect attempt, then we skip and
# fall through to the normal Ollama local path. Keeps 16GB off RAM until asked for.
MLX_STRONG_URL = os.environ.get("CLAUDE_STACK_MLX_STRONG_URL", "http://127.0.0.1:8080")
_MLX_STRONG_MODEL = os.environ.get(
    "CLAUDE_STACK_MLX_STRONG_MODEL", "mlx-community/Qwen3-30B-A3B-Instruct-2507-4bit"
)


def _mlx_strong_enabled() -> bool:
    return os.environ.get("CLAUDE_STACK_MLX_STRONG", "0").strip().lower() in ("1", "true", "yes", "on")


def mlx_strong_up(timeout: float = 0.4) -> bool:
    if not _mlx_strong_enabled():
        return False
    import urllib.request
    try:
        with urllib.request.urlopen(f"{MLX_STRONG_URL}/v1/models", timeout=timeout) as r:
            return getattr(r, "status", 200) == 200
    except Exception:
        return False


def _mlx_strong_generate_blocking(prompt, system, max_tokens, timeout):
    import urllib.request
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt})
    body = json.dumps({
        "model": _MLX_STRONG_MODEL,
        "messages": msgs,
        "max_tokens": max_tokens,
        "temperature": 0.35,
    }).encode()
    req = urllib.request.Request(
        f"{MLX_STRONG_URL}/v1/chat/completions",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read())
    return data["choices"][0]["message"]["content"]


async def try_mlx_strong(prompt, system=None, max_tokens=4096, timeout=180.0):
    """On-demand MLX strong-server generation, or None if disabled/down/errors.
    Best-effort optional path — never raises."""
    if not mlx_strong_up():
        return None
    try:
        return await asyncio.to_thread(
            _mlx_strong_generate_blocking, prompt, system, max_tokens, timeout
        )
    except Exception:
        return None

# Per-1M-token rates used to estimate displaced API cost on local success.
from core.constants import COST_RATES as _RATES
_CHARS_PER_TOKEN = 4

# Keywords that force QUALITY tier regardless of length.
# Bias toward false positives (use quality) over false negatives — the cost of
# fast-tier producing a bad fix is a failed attempt that escalates to Sonnet anyway.
_HEAVY_MARKERS = (
    "fix this error", "debug", "refactor", "review this code",
    "analyze the", "synthesize", "summarize the following",
    "write a report", "write a plan", "outline the",
    "strategy", "root cause",
    '"old":', '"new":',            # healer fix JSON patches
    "root-cause", "step-by-step",
    "compare", "contrast", "pros and cons",
)


def _pick_tier(prompt: str, override=None) -> str:
    """Return 'fast' or 'quality' for this call."""
    if override == "fast":
        return "fast"
    if override == "quality":
        return "quality"
    if override:
        # Explicit model string — treat as quality (no /no_think injection)
        return "quality"
    if len(prompt) > 3000:
        return "quality"
    low = prompt.lower()
    if any(m in low for m in _HEAVY_MARKERS):
        return "quality"
    return "fast"


def pick_local_model(prompt: str, override=None) -> str:
    """Decide which local model to use for JIT local routing.

    Default policy:
    - simple/short prompts -> configured fast model (normally 7B)
    - complex/long prompts -> configured quality model (30B/14B/on-demand when set)
    - explicit model strings pass through unchanged
    """
    if override and override not in ("fast", "quality"):
        return override  # explicit model name passthrough
    tier = _pick_tier(prompt, override)
    return MODEL_FAST if tier == "fast" else MODEL_QUALITY


def _apply_tier(prompt: str, tier: str) -> str:
    """DISABLED 2026-04-17: previously prepended "/no_think" for FAST tier.
    That broke structured-output tasks (exec_assistant:score needed JSON; with
    /no_think qwen3 still thought, thinking-tail fallback surfaced mid-thought
    garbage, validator rejected it, every call escalated to Haiku — burned
    $0.15 silently before detection).

    Leaving the tier-selection logic in place for future use (analytics
    tagging, potentially different sampling params per tier), but NO
    prompt injection. Qwen3:30b-a3b is fast enough on /api/chat that
    the speed gain wasn't worth the quality hit."""
    return prompt


def _displaced_cost(prompt: str, response: str, displaced_tier: str) -> float:
    rates = _RATES.get(displaced_tier, _RATES["sonnet"])
    in_tok = len(prompt) / _CHARS_PER_TOKEN
    out_tok = len(response) / _CHARS_PER_TOKEN
    return (in_tok * rates["input"] + out_tok * rates["output"]) / 1_000_000


def log_savings(prompt: str, response: str, displaced_tier: str, source: str,
                local_model: str = "", tier: str = ""):
    try:
        SAVINGS_LOG.parent.mkdir(parents=True, exist_ok=True)
        now = mountain_now()
        entry = {
            "ts": time.time(),
            "date": now.strftime("%Y-%m-%d"),
            "source": source,
            "displaced_tier": displaced_tier,
            "local_model": local_model,
            "tier": tier,
            "in_chars": len(prompt),
            "out_chars": len(response),
            "saved": round(_displaced_cost(prompt, response, displaced_tier), 6),
        }
        with open(SAVINGS_LOG, "a") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


# Per-unit paid-API prices the free engines displace (conservative — the cheapest
# paid tier we'd otherwise pay). image=fal/schnell-class /img; voice=ElevenLabs
# creator /char; video=paid gen /clip. Override per-call via displaced_usd.
_MEDIA_UNIT_USD = {"image": 0.003, "voice": 0.00018, "video": 0.10}


def log_media_savings(kind: str, units: float = 1, *, source: str = "",
                      engine: str = "", displaced_usd: float | None = None):
    """Log $ saved by generating media locally/free instead of a paid API.
    Same ledger as LLM savings so the totals are one number. Never raises."""
    try:
        SAVINGS_LOG.parent.mkdir(parents=True, exist_ok=True)
        now = mountain_now()
        saved = displaced_usd if displaced_usd is not None else _MEDIA_UNIT_USD.get(kind, 0) * units
        entry = {
            "ts": time.time(),
            "date": now.strftime("%Y-%m-%d"),
            "source": source or f"media:{kind}",
            "media_kind": kind,
            "engine": engine,
            "units": units,
            "saved": round(float(saved), 6),
        }
        with open(SAVINGS_LOG, "a") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def _default_validator(text):
    return bool(text and text.strip())


async def _try_local_once(prompt, model, max_tokens, system, timeout):
    """Single local attempt. Returns text or None."""
    try:
        return await asyncio.wait_for(
            local_client.generate(
                prompt,
                model=model,
                max_tokens=max_tokens,
                system=system,
                queue_timeout_s=6.0,
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        return None
    except Exception:
        return None


def _extract_structured(text):
    """Extract JSON from Qwen output that may have untagged reasoning before it.
    Qwen3 often outputs pages of reasoning then the JSON at the end."""
    if not text:
        return text
    import re as _re
    # Strip <think> blocks
    text = _re.sub(r"<think>.*?</think>", "", text, flags=_re.DOTALL).strip()
    if "</think>" in text:
        text = text.split("</think>")[-1].strip()
    # Try to find JSON object — use brace-counting to handle nested braces
    # (e.g. {"reason": "this {thing} broke"} was missed by the old regex)
    for i, ch in enumerate(text):
        if ch == '{':
            depth = 0
            for j in range(i, len(text)):
                if text[j] == '{':
                    depth += 1
                elif text[j] == '}':
                    depth -= 1
                    if depth == 0:
                        candidate = text[i:j+1]
                        try:
                            import json as _json
                            _json.loads(candidate)
                            return candidate
                        except Exception:
                            break
            break
    # Fallback: try simple non-nested match
    m = _re.search(r"(\{[^{}]*\})", text, _re.DOTALL)
    if m:
        return m.group(1)
    # Try to find JSON array
    m = _re.search(r"(\[\s*\{.*?\}\s*\])", text, _re.DOTALL)
    if m:
        return m.group(1)
    return text


async def try_local_then_api(
    prompt: str,
    *,
    api_fn,
    validator=None,
    displaced_tier: str = "sonnet",
    source: str = "unknown",
    local_timeout: float = 180.0,
    system: str = None,
    max_tokens: int = 4096,
    local_model=None,
    allow_api_fallback: bool = True,
):
    """Try local first, escalate to api_fn on failure — with a CHEAP retry guard.

    If this `source` has been escalating to API aggressively in the last 60s
    (per core.cost_guard.should_retry_local_first), we force ONE more local
    attempt with a larger token budget before paying API rates. This catches
    the class of bug where a brittle validator (exec_assistant:score's JSON
    check) trips on a minor formatting glitch and silently bleeds Haiku for
    hours. The extra retry costs 30-120s of qwen3 time (free) and saves us
    from a sustained API burn.

    Returns (text, "local"|"api").
    """
    from core.corp_tenant import is_corp_tenant, run_corp_codex
    if is_corp_tenant():
        # Work/corp/Salesforce/inbox agents are pinned to the corporate Codex
        # lane (Enterprise ~/.codex); never spill to Operator's local/personal/free/paid.
        return await run_corp_codex(
            prompt, system=system, max_tokens=max_tokens, source=source
        ), "corp:codex"
    validator = validator or _default_validator
    tier = _pick_tier(prompt, local_model)
    chosen = pick_local_model(prompt, local_model)
    effective_prompt = _apply_tier(prompt, tier)
    # Bound fast-tier local latency so trivial tasks fail over to the instant free
    # lane quickly instead of stalling on a busy local model.
    eff_local_timeout = min(local_timeout, FAST_LOCAL_TIMEOUT) if tier == "fast" else local_timeout
    runtime_pressure = get_runtime_pressure()
    avoid_local = runtime_pressure.get("avoid_local", False)
    if avoid_local and force_local_routing():
        if hard_force_local_routing():
            log.info("Hard force-local routing active for %s — ignoring runtime pressure", source)
            avoid_local = False
        else:
            log.info("Soft force-local routing suppressed for %s under runtime pressure", source)
    policy = evaluate_external_request("Anthropic API", source)
    external_fallback_allowed = bool(policy.get("allowed", True))

    result = None
    retry_result = None

    if not avoid_local:
        # --- Optional on-demand MLX strong server (quality tier only) ---
        if tier == "quality":
            mlx_text = await try_mlx_strong(
                prompt, system=system, max_tokens=max_tokens, timeout=local_timeout
            )
            if mlx_text:
                mlx_text = _extract_structured(mlx_text)
                if mlx_text and validator(mlx_text):
                    log_savings(prompt, mlx_text, displaced_tier, source,
                                local_model="mlx:qwen3-30b-a3b", tier=tier)
                    return mlx_text, "local"
        # --- First local attempt ---
        result = await _try_local_once(effective_prompt, chosen, max_tokens, system, eff_local_timeout)
        if result:
            result = _extract_structured(result)
        if result and validator(result):
            log_savings(prompt, result, displaced_tier, source, local_model=chosen, tier=tier)
            return result, "local"

        queue_backpressure = bool(getattr(local_client, "recent_queue_pressure", lambda *_a, **_k: False)())
        if queue_backpressure:
            log.warning(
                "Local queue pressure detected for %s — skipping second local retry and spilling to free providers",
                source,
            )
        else:
            # --- ALWAYS retry locally before escalating — local is free, API is not ---
            retry_prompt = prompt + "\n\n(IMPORTANT: respond with ONLY the answer, no preamble, no explanation, no thinking text — just the final structured output.)"
            retry_result = await _try_local_once(
                retry_prompt, chosen, max_tokens * 2, system, eff_local_timeout,
            )
            if retry_result:
                retry_result = _extract_structured(retry_result)
            if retry_result and validator(retry_result):
                log_savings(prompt, retry_result, displaced_tier, source,
                            local_model=chosen, tier=tier + "_retry")
                return retry_result, "local"
    else:
        import logging as _log
        _log.getLogger(__name__).warning(
            "Runtime pressure overloaded for %s — bypassing local inference (%s)",
            source,
            ", ".join(runtime_pressure.get("reasons", [])) or "no reason recorded",
        )

    # --- Try free LLM providers (always, before any paid API or giving up) ---
    free_result = None
    if external_fallback_allowed and free_pool_allowed():
        try:
            from core.free_llm_pool import try_free_providers
            free_result, free_provider = await try_free_providers(prompt, system=system, max_tokens=max_tokens)
            if free_result:
                free_result = _extract_structured(free_result)
            if free_result and validator(free_result):
                log_savings(prompt, free_result, displaced_tier, source,
                            local_model=free_provider, tier="free_pool")
                return free_result, "free:" + free_provider
        except Exception as e:
            import logging as _log
            _log.getLogger(__name__).warning(f"Free LLM pool failed: {e}")
    else:
        log.info(
            "External fallback disabled for %s (%s)",
            source or "unknown",
            policy.get("effective_mode", "allow_paid"),
        )

    # --- Local-only mode: block paid API (free pool already tried above) ---
    if not allow_api_fallback or not external_fallback_allowed:
        import logging as _log
        _log.getLogger(__name__).info(f"Local-only mode: no paid API for {source}, returning best free/local result")
        return free_result or retry_result or result or "", "local"

    # --- Quiet hours: block paid API (free pool already tried above) ---
    from core.api_client import _is_quiet_hours
    if _is_quiet_hours():
        import logging as _log
        _log.getLogger(__name__).info(f"Quiet hours: no paid API for {source}, returning best free/local result")
        return free_result or retry_result or result or "", "local"

    # --- Paid-LLM guard: honor the circuit breaker + $0 daily caps + master switch. This is the
    # single shared escalation lane for the whole stack, so gating it HERE binds every caller
    # (infra_intelligence, exec_assistant, diagnostician, revenue_coordinator, proposal_submitter,
    # llm_api, ...) to the paid policy with no per-caller guard. When paid is disallowed, keep the
    # best free/local result instead of spending. (Was the structural bypass behind the slow bleed.) ---
    try:
        from core.paid_llm_guard import paid_llm_ok
        if not paid_llm_ok(f"llm_router escalation ({source})", lane="auto"):
            import logging as _log
            _log.getLogger(__name__).info(f"paid-LLM gate closed: no paid API for {source}, returning free/local")
            return free_result or retry_result or result or "", "local"
    except Exception:
        pass

    # --- Last resort: escalate to paid API ---
    try:
        from core import cost_guard
        cost_guard.note_escalation(source)
    except Exception:
        pass

    try:
        api_result = await api_fn()
        return api_result or "", "api"
    except Exception as e:
        # BULLETPROOF: never let a paid-provider outage propagate to the caller
        # (chat front-end would 500). Degrade to best-effort partial result.
        log.warning("Paid API fallback failed for %s: %s", source, e)
        return free_result or retry_result or result or "", "api_failed"
