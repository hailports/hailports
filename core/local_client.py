"""Local model client via Ollama API — zero cost inference."""

from core.constants import LOCAL_MODEL
import asyncio
import json
import logging
import os
import time
from pathlib import Path
import httpx
from core.local_model_registry import get_active_local_model, options_for_model

log = logging.getLogger(__name__)

OLLAMA_URL = "http://localhost:11434"
FAST_MODEL = LOCAL_MODEL
MAIN_MODEL = LOCAL_MODEL
METRICS_LOG = Path(__file__).resolve().parent.parent / "data" / "runtime" / "local_llm_metrics.jsonl"

# Circuit breaker state
_cb_failures = 0
_CB_THRESHOLD = 3
_CB_COOLDOWN = 60  # seconds
_cb_open_since: float | None = None

import re as _re


def _parse_positive_int(name: str, default: int, minimum: int = 1, maximum: int = 64) -> int:
    raw = str(os.environ.get(name, default)).strip()
    try:
        return max(minimum, min(maximum, int(raw)))
    except Exception:
        return default


def _parse_positive_float(name: str, default: float, minimum: float = 0.1, maximum: float = 300.0) -> float:
    raw = str(os.environ.get(name, default)).strip()
    try:
        return max(minimum, min(maximum, float(raw)))
    except Exception:
        return default


_LOCAL_LLM_MAX_INFLIGHT = _parse_positive_int("LOCAL_LLM_MAX_INFLIGHT", 2, minimum=1, maximum=16)
_LOCAL_LLM_QUEUE_TIMEOUT_S = _parse_positive_float("LOCAL_LLM_QUEUE_TIMEOUT_S", 90.0)
_INFLIGHT_SEMAPHORE = asyncio.Semaphore(_LOCAL_LLM_MAX_INFLIGHT)
_last_queue_pressure_at: float | None = None


def _parse_keep_alive_seconds(name: str, default: int) -> int:
    raw = str(os.environ.get(name, "")).strip()
    if not raw:
        return default
    try:
        value = int(float(raw))
    except Exception:
        return default
    if value < -1:
        return default
    return min(value, 86400)


def ollama_keep_alive_value(mode: str | None = None) -> int:
    """Return a pressure-aware Ollama keep-alive window in seconds.

    Defaults:
    - normal: 300s
    - constrained: 60s
    - overloaded: 0s

    Set OLLAMA_KEEPALIVE_SECONDS to override all modes globally.
    """
    explicit = str(os.environ.get("OLLAMA_KEEPALIVE_SECONDS", "")).strip()
    if explicit and explicit != "86400":
        return _parse_keep_alive_seconds("OLLAMA_KEEPALIVE_SECONDS", 86400)

    resolved_mode = str(mode or "").strip().lower()
    if not resolved_mode:
        try:
            from core.runtime_pressure import get_runtime_pressure

            resolved_mode = str((get_runtime_pressure() or {}).get("mode") or "normal").strip().lower()
        except Exception:
            resolved_mode = "normal"

    env_key = {
        "normal": "OLLAMA_KEEPALIVE_NORMAL_SECONDS",
        "constrained": "OLLAMA_KEEPALIVE_CONSTRAINED_SECONDS",
        "overloaded": "OLLAMA_KEEPALIVE_OVERLOADED_SECONDS",
    }.get(resolved_mode, "OLLAMA_KEEPALIVE_NORMAL_SECONDS")
    default = {
        "normal": 300,
        "constrained": 60,
        "overloaded": 0,
    }.get(resolved_mode, 300)
    return _parse_keep_alive_seconds(env_key, default)

def _clean_short_think_only_text(text: str) -> str:
    candidate = str(text or "").strip()
    if not candidate:
        return ""
    if len(candidate) > 160 or candidate.count("\n") > 3:
        return ""
    lines = [line.strip(" -*>\t\r") for line in candidate.splitlines() if line.strip()]
    if not lines:
        return ""
    for line in reversed(lines):
        normalized = _re.sub(
            r"^(final answer|answer|response|reply|classification|category|priority|sentiment|language)\s*[:\-]\s*",
            "",
            line,
            flags=_re.IGNORECASE,
        ).strip(" \"'")
        if normalized:
            return normalized
    # Last resort: return the last non-empty line as-is (covers single-word answers)
    for line in reversed(lines):
        cleaned = line.strip(" \"'*->:\t\r")
        if cleaned:
            return cleaned
    return ""


def _strip_think(text):
    """Strip <think>...</think> tags without blanking short think-only answers.

    Qwen occasionally puts the entire short answer inside a think block. If we
    blindly remove the block and return an empty string, callers interpret that
    as model failure. Only salvage very short think-only payloads; long
    reasoning-only blocks should still fall through as empty.
    """
    if not text:
        return text

    original = str(text)
    stripped = _re.sub(r"<think>.*?</think>", "", original, flags=_re.DOTALL).strip()
    if "</think>" in stripped:
        stripped = stripped.split("</think>")[-1].strip()
    if stripped:
        return stripped

    blocks = _re.findall(r"<think>(.*?)</think>", original, flags=_re.DOTALL)
    if blocks:
        salvaged = _clean_short_think_only_text(blocks[-1])
        if salvaged:
            return salvaged

    fallback = _re.sub(r"</?think>", "", original, flags=_re.DOTALL).strip()
    salvaged = _clean_short_think_only_text(fallback)
    return salvaged or ""


def _format_ollama_error(exc: Exception) -> str:
    name = type(exc).__name__
    msg = str(exc).strip()
    if isinstance(exc, httpx.TimeoutException) or name in {"TimeoutError", "ReadTimeout"}:
        return f"{name}: timed out waiting for Ollama response"
    if msg:
        return f"{name}: {msg}"
    return name


def _record_inference_metric(endpoint: str, model: str, data: dict, *, wall_s: float, prompt_chars: int = 0, response_chars: int = 0) -> None:
    try:
        eval_count = int(data.get("eval_count") or 0)
        eval_duration_s = float(data.get("eval_duration") or 0) / 1e9
        total_duration_s = float(data.get("total_duration") or 0) / 1e9
        prompt_eval_count = int(data.get("prompt_eval_count") or 0)
        METRICS_LOG.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "ts": time.time(),
            "endpoint": endpoint,
            "model": model,
            "prompt_chars": int(prompt_chars or 0),
            "response_chars": int(response_chars or 0),
            "prompt_eval_count": prompt_eval_count,
            "eval_count": eval_count,
            "eval_duration_s": round(eval_duration_s, 4),
            "total_duration_s": round(total_duration_s, 4) if total_duration_s else 0,
            "wall_s": round(float(wall_s or 0), 4),
            "tokens_per_s": round(eval_count / eval_duration_s, 2) if eval_count and eval_duration_s else 0,
        }
        with open(METRICS_LOG, "a") as fh:
            fh.write(json.dumps(row, default=str) + "\n")
    except Exception:
        pass



def _circuit_open() -> bool:
    """Return True if the circuit breaker is open (Ollama calls should be skipped)."""
    global _cb_open_since
    if _cb_open_since is None:
        return False
    elapsed = time.monotonic() - _cb_open_since
    if elapsed >= _CB_COOLDOWN:
        log.info("Circuit breaker: cooldown elapsed, moving to half-open")
        _cb_open_since = None
        return False
    return True


def _cb_record_success():
    global _cb_failures, _cb_open_since
    if _cb_failures > 0:
        log.info(f"Circuit breaker: success, resetting failure counter (was {_cb_failures})")
    _cb_failures = 0
    _cb_open_since = None


def _cb_record_failure():
    global _cb_failures, _cb_open_since
    _cb_failures += 1
    log.warning(f"Circuit breaker: failure #{_cb_failures}/{_CB_THRESHOLD}")
    if _cb_failures >= _CB_THRESHOLD and _cb_open_since is None:
        _cb_open_since = time.monotonic()
        log.error(f"Circuit breaker: OPEN — skipping Ollama for {_CB_COOLDOWN}s")


async def is_available():
    """Check if Ollama is running. Returns False if circuit breaker is open."""
    if _circuit_open():
        return False
    try:
        async with httpx.AsyncClient() as c:
            r = await c.get(f"{OLLAMA_URL}/api/version", timeout=3)
            return r.status_code == 200
    except Exception:
        return False


async def _acquire_inflight_slot(kind: str, model: str, queue_timeout_s: float | None = None) -> bool:
    global _last_queue_pressure_at
    started = time.monotonic()
    timeout_s = max(0.1, float(queue_timeout_s or _LOCAL_LLM_QUEUE_TIMEOUT_S))
    try:
        await asyncio.wait_for(_INFLIGHT_SEMAPHORE.acquire(), timeout=timeout_s)
        waited = time.monotonic() - started
        if waited >= 0.5:
            log.info(
                "Local [%s]: waited %.1fs for %s slot (%s inflight max)",
                model,
                waited,
                kind,
                _LOCAL_LLM_MAX_INFLIGHT,
            )
        return True
    except asyncio.TimeoutError:
        _last_queue_pressure_at = time.monotonic()
        log.warning(
            "Local [%s]: queue wait exceeded %.1fs for %s slot (%s inflight max)",
            model,
            timeout_s,
            kind,
            _LOCAL_LLM_MAX_INFLIGHT,
        )
        return False


def _release_inflight_slot() -> None:
    try:
        _INFLIGHT_SEMAPHORE.release()
    except ValueError:
        pass


def recent_queue_pressure(window_s: float = 20.0) -> bool:
    if _last_queue_pressure_at is None:
        return False
    return (time.monotonic() - _last_queue_pressure_at) <= max(1.0, float(window_s))


def _resolve_model(model=None) -> str:
    return str(model or get_active_local_model(MAIN_MODEL))


def _ollama_options(model: str, *, max_tokens: int, num_ctx=None, temperature=None) -> dict:
    options = dict(options_for_model(model))
    options["num_predict"] = max_tokens
    if temperature is not None:
        options["temperature"] = temperature
    else:
        options.setdefault("temperature", 0.7)
    if num_ctx:
        options["num_ctx"] = num_ctx
    return options


def _disable_thinking_for_interactive_chat(model: str, *, tools=None, max_tokens: int = 4096) -> bool:
    """Use Ollama's non-streaming final-answer path for Qwen3 interactive chat.

    The resident Qwen3 thinking model can spend seconds filling the `thinking`
    field before producing tiny answers. For ordinary no-tool chat, `think:
    false` returns the same final answer much faster and `_strip_think` removes
    any inline think block that older Ollama builds still include.
    """
    if tools:
        return False
    if int(max_tokens or 0) > 4096:
        return False
    return "qwen3" in str(model or "").lower()


async def generate(prompt, model=None, system=None, max_tokens=4096, num_ctx=None, temperature=None, queue_timeout_s=None):
    """Generate a response from a local model. Returns text string."""
    model = _resolve_model(model)

    # Auto-prepend /no_think for short-answer prompts to prevent Qwen3 from
    # wrapping the answer in <think> tags that get stripped to empty
    effective_prompt = prompt
    if max_tokens <= 256 and "/no_think" not in prompt:
        effective_prompt = "/no_think\n" + prompt

    body = {
        "model": model,
        "prompt": effective_prompt,
        "stream": False,
        "keep_alive": ollama_keep_alive_value(),
        "options": _ollama_options(model, max_tokens=max_tokens, num_ctx=num_ctx, temperature=temperature),
    }
    if system:
        body["system"] = system

    if _circuit_open():
        log.warning("Circuit breaker open — skipping Ollama generate")
        return None

    slot_acquired = await _acquire_inflight_slot("generate", model, queue_timeout_s=queue_timeout_s)
    if not slot_acquired:
        return None

    started = time.monotonic()
    try:
        async with httpx.AsyncClient(
            limits=httpx.Limits(
                max_connections=max(1, _LOCAL_LLM_MAX_INFLIGHT),
                max_keepalive_connections=max(1, _LOCAL_LLM_MAX_INFLIGHT),
            )
        ) as c:
            r = await c.post(f"{OLLAMA_URL}/api/generate", json=body, timeout=httpx.Timeout(300.0, connect=10.0))
            r.raise_for_status()
            data = r.json()
            response = data.get("response", "")
            eval_duration = data.get("eval_duration", 0) / 1e9  # nanoseconds to seconds
            tokens = data.get("eval_count", 0)
            _record_inference_metric(
                "generate",
                model,
                data,
                wall_s=time.monotonic() - started,
                prompt_chars=len(str(effective_prompt or "")),
                response_chars=len(str(response or "")),
            )
            if tokens and eval_duration:
                log.info(f"Local [{model}]: {tokens} tokens in {eval_duration:.1f}s ({tokens/eval_duration:.0f} tok/s)")
            _cb_record_success()
            return _strip_think(response.strip())
    except Exception as e:
        log.error(f"Ollama error: {_format_ollama_error(e)}")
        _cb_record_failure()
        return None
    finally:
        _release_inflight_slot()


async def chat(messages, model=None, system=None, max_tokens=4096, tools=None, num_ctx=None, temperature=None, queue_timeout_s=None):
    """Chat completion with conversation history. Returns text string.
    If tools provided, uses Ollama native tool calling and returns (text, tool_calls)."""
    model = _resolve_model(model)

    body = {
        "model": model,
        "messages": messages,
        "stream": False,
        "keep_alive": ollama_keep_alive_value(),
        "options": _ollama_options(model, max_tokens=max_tokens, num_ctx=num_ctx, temperature=temperature),
    }

    if _disable_thinking_for_interactive_chat(model, tools=tools, max_tokens=max_tokens):
        body["think"] = False

    if system:
        body["messages"] = [{"role": "system", "content": system}] + body["messages"]

    if tools:
        body["tools"] = tools

    if _circuit_open():
        log.warning("Circuit breaker open — skipping Ollama chat")
        return None

    slot_acquired = await _acquire_inflight_slot("chat", model, queue_timeout_s=queue_timeout_s)
    if not slot_acquired:
        return None

    prompt_chars = sum(
        len(str(message.get("content") or "")) if isinstance(message, dict) else len(str(message or ""))
        for message in (body.get("messages") or [])
    )
    started = time.monotonic()
    try:
        log.info(f"LOCAL_CLIENT: posting to ollama, model={model}, num_predict={body['options'].get('num_predict')}, tools={'yes' if tools else 'no'}")
        async with httpx.AsyncClient(
            limits=httpx.Limits(
                max_connections=max(1, _LOCAL_LLM_MAX_INFLIGHT),
                max_keepalive_connections=max(1, _LOCAL_LLM_MAX_INFLIGHT),
            )
        ) as c:
            r = await c.post(f"{OLLAMA_URL}/api/chat", json=body, timeout=httpx.Timeout(600.0, connect=10.0))
            r.raise_for_status()
            data = r.json()
            msg = data.get("message", {})
            response = msg.get("content", "")
            tool_calls = msg.get("tool_calls", [])
            _record_inference_metric(
                "chat",
                model,
                data,
                wall_s=time.monotonic() - started,
                prompt_chars=prompt_chars,
                response_chars=len(str(response or "")),
            )
            _cb_record_success()
            cleaned = _strip_think(response.strip())
            if tools and tool_calls:
                return cleaned, tool_calls
            return cleaned
    except Exception as e:
        log.error(f"Ollama chat error: {_format_ollama_error(e)}")
        _cb_record_failure()
        return None
    finally:
        _release_inflight_slot()


async def classify_complexity(text):
    """Use the fast 3B model to classify if a message needs a big model.
    Returns 'simple' or 'complex'."""
    prompt = f"""Classify this user message as SIMPLE or COMPLEX. Reply with only one word.

SIMPLE: greetings, yes/no questions, status checks, factual lookups, basic math, time/date questions
COMPLEX: multi-step tasks, analysis, writing, debugging, planning, anything needing tools or reasoning

Message: {text}

Classification:"""

    result = await generate(prompt, model=FAST_MODEL, max_tokens=1024)
    if result and "complex" in result.lower():
        return "complex"
    return "simple"
