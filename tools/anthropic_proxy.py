"""Anthropic API proxy — local-first routing for Claude Code.

Chain per request (Anthropic-FREE — the 'anthropic' route points at OpenRouter, never api.anthropic.com):
  tool-use / complex  → OpenRouter          (dynamic model; ANTHROPIC_BASE=openrouter.ai/api/v1)
  simple / balanced   → free_llm_pool       (free tier APIs)
  fast / short        → Qwen3 via Ollama    ($0)

Usage:
  python -m tools.anthropic_proxy          # starts on port 8099
  ANTHROPIC_BASE_URL=http://127.0.0.1:8099 claude ...
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import uuid
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

log = logging.getLogger("anthropic-proxy")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)

app = FastAPI(title="Anthropic Local-First Proxy")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
# OpenRouter is the configured paid provider. Use it whenever present, even if
# ANTHROPIC_API_KEY is populated as a Claude CLI compatibility alias.
if OPENROUTER_API_KEY:
    ANTHROPIC_BASE = "https://openrouter.ai/api/v1"
    ANTHROPIC_API_KEY = OPENROUTER_API_KEY
else:
    ANTHROPIC_BASE = "https://openrouter.ai/api/v1"
    ANTHROPIC_API_KEY = ""
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
PROXY_PORT = int(os.environ.get("ANTHROPIC_PROXY_PORT", "8099"))
TRUE_VALUES = {"1", "true", "yes", "on"}


def _paid_llm_disabled() -> bool:
    if os.environ.get("CLAUDE_STACK_DISABLE_PAID_LLM_API", "").strip().lower() in TRUE_VALUES:
        return True
    # This proxy forwards to OpenRouter (paid) on its own key, so it must ALSO honor the stack's
    # circuit breaker + $0 daily caps — otherwise it's an off-guard bypass (it was). Blocks the
    # forward when paid is disallowed; clients fall back to local/free.
    try:
        from core.paid_llm_guard import paid_llm_ok
        if not paid_llm_ok("anthropic_proxy openrouter forward", lane="auto"):
            return True
    except Exception:
        pass
    return False

# Use the stack's model constant; override with env var
try:
    from core.constants import LOCAL_MODEL as _STACK_LOCAL_MODEL
except ImportError:
    _STACK_LOCAL_MODEL = "qwen2.5:7b"
LOCAL_MODEL = os.environ.get("CLAUDE_STACK_LOCAL_MODEL", os.environ.get("LOCAL_MODEL", _STACK_LOCAL_MODEL))

# Patterns in the last user message that need full Claude
_MUST_ANTHROPIC = re.compile(
    r"(write code|implement|debug|refactor|architecture|"
    r"create a function|write a function|write a class|write a script|"
    r"fix.*bug|traceback|stack trace|syntax error|"
    r"ImportError|AttributeError|TypeError|KeyError|"
    r"edit (the |this |my )?file|modify (the |this |my )?file|"
    r"update (the |this |my )?file|add (to|a) (the |this )?file)",
    re.I,
)

# Action/investigation intent — needs tool execution, keep on Anthropic
_ACTION_PATTERNS = re.compile(
    r"(check|look at|show me|find|search|list|read|run|execute|"
    r"what (is|are|does)|how (does|is)|tell me about|explain|"
    r"can you (get|fetch|pull|scan|inspect|analyze|review|audit))",
    re.I,
)

# Haiku is 3.75x cheaper than Sonnet and handles simple tool-bearing turns fine
_HAIKU_MODEL = "claude-haiku-4-5-20251001"
_DOWNGRADE_PATTERN = re.compile(r"claude-(sonnet|opus)-", re.I)


# ── routing ──────────────────────────────────────────────────────────────────

def _has_tool_blocks(body: dict) -> bool:
    """True if any message contains tool_use or tool_result content blocks."""
    for msg in body.get("messages", []):
        content = msg.get("content", "")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") in ("tool_use", "tool_result"):
                    return True
    return False


def _last_user_text(body: dict) -> str:
    """Extract text from the most recent user message only."""
    messages = body.get("messages", [])
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [b.get("text", "") for b in content
                     if isinstance(b, dict) and b.get("type") == "text"]
            return " ".join(parts)
    return ""


def classify_route(body: dict) -> str:
    """Return 'local' | 'free' | 'anthropic'."""
    # Mid-execution (tool_use/tool_result in message history) → must use Anthropic
    if _has_tool_blocks(body):
        return "anthropic"

    last_msg = _last_user_text(body)

    if not last_msg:
        return "anthropic"

    # Coding/engineering intent → Anthropic
    if _MUST_ANTHROPIC.search(last_msg):
        return "anthropic"

    # Action/investigation intent with tools defined → Anthropic (needs tool execution)
    if body.get("tools") and _ACTION_PATTERNS.search(last_msg):
        return "anthropic"

    # Classify by task type using existing stack logic
    try:
        from core.free_llm_pool import _classify_task
        tier = _classify_task(last_msg)
    except Exception:
        tier = "balanced"

    # Very short + fast-tier → local Qwen3
    if tier == "fast" and len(last_msg) < 500:
        return "local"

    # Conversational / simple → free pool
    if tier in ("fast", "balanced") and len(last_msg) < 3000:
        return "free"

    return "anthropic"


def maybe_downgrade_model(body: dict) -> dict:
    """Downgrade sonnet/opus → haiku for simple Anthropic-bound turns (3.75x cheaper)."""
    if not _DOWNGRADE_PATTERN.search(body.get("model", "")):
        return body
    last_msg = _last_user_text(body)
    # Don't downgrade mid-execution, long, or complex turns
    if _has_tool_blocks(body) or not last_msg or len(last_msg) > 1500:
        return body
    if _MUST_ANTHROPIC.search(last_msg):
        return body
    try:
        from core.free_llm_pool import _classify_task
        tier = _classify_task(last_msg)
    except Exception:
        return body
    if tier in ("fast", "balanced"):
        return {**body, "model": _HAIKU_MODEL}
    return body


# ── local Qwen3 via Ollama ────────────────────────────────────────────────────

def _build_ollama_messages(body: dict) -> list[dict]:
    messages: list[dict] = []
    system = body.get("system", "")
    if isinstance(system, list):
        for b in system:
            if isinstance(b, dict) and b.get("type") == "text":
                system = b.get("text", "")
                break
    if system:
        messages.append({"role": "system", "content": str(system)[:2000]})

    for msg in body.get("messages", []):
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if isinstance(content, list):
            parts = [b.get("text", "") for b in content
                     if isinstance(b, dict) and b.get("type") == "text"]
            content = " ".join(parts)
        if content and role in ("user", "assistant", "system"):
            messages.append({"role": role, "content": content})
    return messages


async def _qwen3_generate(body: dict) -> tuple[str | None, str | None]:
    messages = _build_ollama_messages(body)
    if not messages or messages[-1]["role"] != "user":
        return None, "no user message"

    payload = {
        "model": LOCAL_MODEL,
        "messages": messages,
        "stream": False,
        "options": {"num_predict": min(body.get("max_tokens", 2048), 4096)},
    }
    try:
        async with httpx.AsyncClient(timeout=90) as client:
            r = await client.post(f"{OLLAMA_URL}/api/chat", json=payload)
            r.raise_for_status()
            text = r.json().get("message", {}).get("content", "")
            text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
            if "</think>" in text:
                text = text.split("</think>")[-1].strip()
            if not text:
                return None, "empty response"
            log.info("local %s: %d chars", LOCAL_MODEL, len(text))
            return text, None
    except Exception as exc:
        log.warning("Qwen3 failed: %s", exc)
        return None, str(exc)


# ── free pool ─────────────────────────────────────────────────────────────────

async def _free_pool_generate(body: dict) -> tuple[str | None, str | None]:
    try:
        from core.free_llm_pool import try_free_providers
        last_msg = _last_user_text(body)
        system = body.get("system", "")
        if isinstance(system, list):
            for b in system:
                if isinstance(b, dict) and b.get("type") == "text":
                    system = b.get("text", "")
                    break
        result, provider = await try_free_providers(
            prompt=last_msg,
            system=str(system)[:1000] if system else None,
            max_tokens=min(body.get("max_tokens", 2048), 4096),
            explicit=True,
        )
        if result:
            log.info("free pool (%s): %d chars", provider, len(result))
            return result, None
        return None, "all free providers failed"
    except Exception as exc:
        log.warning("free pool error: %s", exc)
        return None, str(exc)


# ── Anthropic passthrough ─────────────────────────────────────────────────────

def _anthropic_to_openai(body: dict) -> dict:
    """Convert Anthropic messages API body to OpenAI chat completions format for OpenRouter."""
    messages = []
    system = body.get("system", "")
    if isinstance(system, list):
        system = " ".join(b.get("text","") for b in system if isinstance(b,dict) and b.get("type")=="text")
    if system:
        messages.append({"role": "system", "content": system})
    for msg in body.get("messages", []):
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(b.get("text","") for b in content if isinstance(b,dict) and b.get("type")=="text")
        messages.append({"role": role, "content": content})
    model = body.get("model", "deepseek/deepseek-chat")
    model = model.replace("openrouter/", "")
    # Map Anthropic model names to OpenRouter equivalents
    model_map = {
        "claude-haiku-4-5-20251001": "anthropic/claude-haiku-4-5",
        "claude-sonnet-4-6": "anthropic/claude-sonnet-4-6",
        "claude-opus-4-7": "anthropic/claude-opus-4-7",
    }
    model = model_map.get(model, model)
    return {"model": model, "messages": messages, "max_tokens": body.get("max_tokens", 2048), "stream": body.get("stream", False)}

def _openai_to_anthropic(data: dict) -> dict:
    """Convert OpenRouter/OpenAI response back to Anthropic format."""
    choices = data.get("choices", [{}])
    text = choices[0].get("message", {}).get("content", "") if choices else ""
    return {"id": data.get("id",""), "type":"message", "role":"assistant",
            "content":[{"type":"text","text":text}],
            "model": data.get("model",""), "stop_reason":"end_turn",
            "usage":{"input_tokens": data.get("usage",{}).get("prompt_tokens",0),
                     "output_tokens": data.get("usage",{}).get("completion_tokens",0)}}

async def _forward_to_anthropic(request: Request, body: dict, stream: bool):
    if _paid_llm_disabled():
        return JSONResponse(
            status_code=403,
            content={
                "error": {
                    "type": "paid_llm_disabled",
                    "message": "Anthropic passthrough blocked: CLAUDE_STACK_DISABLE_PAID_LLM_API=1.",
                }
            },
        )

    use_openrouter = "openrouter" in ANTHROPIC_BASE
    headers = {"content-type": "application/json"}
    if use_openrouter:
        headers["Authorization"] = f"Bearer {ANTHROPIC_API_KEY}"
        headers["HTTP-Referer"] = "https://Operator-stack.local"
        fwd_body = _anthropic_to_openai(body)
        endpoint = f"{ANTHROPIC_BASE}/chat/completions"
    else:
        headers["x-api-key"] = ANTHROPIC_API_KEY
        headers["anthropic-version"] = request.headers.get("anthropic-version", "2023-06-01")
        for k, v in request.headers.items():
            if k.lower().startswith("anthropic-") and k.lower() != "anthropic-version":
                headers[k] = v
        fwd_body = body
        endpoint = f"{ANTHROPIC_BASE}/v1/messages"

    async with httpx.AsyncClient(timeout=300) as client:
        r = await client.post(endpoint, headers=headers, json=fwd_body)
        if use_openrouter and r.status_code == 200:
            return JSONResponse(status_code=200, content=_openai_to_anthropic(r.json()))
        return JSONResponse(status_code=r.status_code, content=r.json())


# ── SSE / JSON response builders ──────────────────────────────────────────────

def _sse(name: str, data: dict) -> str:
    return f"event: {name}\ndata: {json.dumps(data)}\n\n"


def _make_sse_stream(text: str, model: str) -> str:
    msg_id = f"msg_{uuid.uuid4().hex[:24]}"
    parts = [
        _sse("message_start", {
            "type": "message_start",
            "message": {
                "id": msg_id, "type": "message", "role": "assistant",
                "content": [], "model": model,
                "stop_reason": None, "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 1},
            },
        }),
        _sse("content_block_start", {
            "type": "content_block_start", "index": 0,
            "content_block": {"type": "text", "text": ""},
        }),
        _sse("ping", {"type": "ping"}),
    ]
    for i in range(0, len(text), 40):
        parts.append(_sse("content_block_delta", {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": text[i:i + 40]},
        }))
    parts += [
        _sse("content_block_stop", {"type": "content_block_stop", "index": 0}),
        _sse("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": len(text.split())},
        }),
        _sse("message_stop", {"type": "message_stop"}),
    ]
    return "".join(parts)


def _make_json_response(text: str, model: str) -> dict:
    return {
        "id": f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "model": model,
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 0, "output_tokens": len(text.split())},
    }


# ── main endpoint ─────────────────────────────────────────────────────────────

@app.post("/v1/messages")
async def messages(request: Request):
    body = await request.json()
    stream = body.get("stream", False)
    route = classify_route(body)

    # Downgrade model for simple Anthropic-bound turns before forwarding
    if route == "anthropic":
        body = maybe_downgrade_model(body)

    log.info(
        "route=%-10s stream=%s model=%s last_msg=%d chars",
        route, stream, body.get("model", "?"), len(_last_user_text(body)),
    )

    text: str | None = None
    source = "anthropic"

    if route == "local":
        text, err = await _qwen3_generate(body)
        if text:
            source = LOCAL_MODEL
        else:
            log.info("local failed (%s) → free pool", err)
            route = "free"

    if route == "free" and text is None:
        text, err = await _free_pool_generate(body)
        if text:
            source = "free_pool"
        else:
            log.info("free pool failed (%s) → anthropic", err)
            route = "anthropic"

    if route == "anthropic" or text is None:
        return await _forward_to_anthropic(request, body, stream)

    log.info("served by %s (%d chars)", source, len(text))

    if stream:
        sse = _make_sse_stream(text, model=source)
        return StreamingResponse(
            iter([sse.encode()]), media_type="text/event-stream",
            headers={"cache-control": "no-cache"},
        )
    return JSONResponse(_make_json_response(text, model=source))


@app.get("/health")
async def health():
    ollama_ok = False
    try:
        async with httpx.AsyncClient(timeout=3) as c:
            r = await c.get(f"{OLLAMA_URL}/api/version")
            ollama_ok = r.status_code == 200
    except Exception:
        pass
    try:
        from core.free_llm_pool import get_pool_status
        pool = get_pool_status()
    except Exception:
        pool = {}
    return {"status": "ok", "ollama": ollama_ok, "local_model": LOCAL_MODEL, "free_pool": pool}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=PROXY_PORT, log_level="info")
