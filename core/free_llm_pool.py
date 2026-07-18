"""Free LLM API pool — smart routing via LiteLLM across free providers.

Sits between local Qwen3 and paid Anthropic API in the routing chain:
    Qwen3 local -> free_llm_pool -> Anthropic API

Uses LiteLLM for unified interface across all providers with smart
routing based on task type: coding/analysis -> larger models,
quick classification -> fastest models, general -> balanced.

Usage:
    from core.free_llm_pool import try_free_providers
    result = await try_free_providers(prompt, system=None, max_tokens=4096)
    # Returns (text, provider_name) or (None, None) if all fail
"""

import asyncio
import hashlib
import logging
import os
import time
import re

from core.runtime_pressure import get_runtime_pressure

log = logging.getLogger("free-llm-pool")
_TRUE_VALUES = {"1", "true", "yes", "on"}

# Model tiers by capability
# NOTE: all model IDs below were live-validated 2026-05-30 against each provider's
# current catalog. Cerebras only serves zai-glm-4.7 + gpt-oss-120b now (old
# llama3.1-8b / qwen-235b IDs 404). Ordered by speed + reliability; preferred
# open families (Qwen / DeepSeek / GLM) lead where capability is comparable.
#
# GitHub Models: FREE tier via GITHUB_TOKEN (Personal Access Token with 'models' scope).
#   - gpt-4o-mini: fast, strong reasoning, 128k context
#   - claude-3.5-sonnet: best-in-class reasoning, 200k context
#   - llama-3.1-405b: long-context (128k), free on GitHub
#   LiteLLM format: "github/<model>"
#
# Cloudflare Workers AI: FREE tier via CLOUDFLARE_API_KEY + CLOUDFLARE_ACCOUNT_ID.
#   - ~10k neurons/day free quota
#   - Llama 3.1 8B + 70B models, includes audio-to-text
#   - LiteLLM format: "cloudflare/@cf/<model-path>"
#   - Aliased from CF_API_TOKEN / CF_ACCOUNT_ID to match stack config
#
def _is_placeholder(v: str) -> bool:
    # A truthy-but-fake value (e.g. `.env` shipping `CLOUDFLARE_API_KEY=<your_api_key>`)
    # must NOT shadow the real CF_API_TOKEN alias, or Workers AI auths with junk and
    # every strong-tier call 401s. Treat obvious placeholders as unset.
    s = (v or "").strip().lower()
    return (not s) or s.startswith("<") or "your_" in s or "placeholder" in s or "changeme" in s

def _alias_env(target: str, source: str) -> None:
    if _is_placeholder(os.environ.get(target, "")) and not _is_placeholder(os.environ.get(source, "")):
        os.environ[target] = os.environ[source]

_alias_env("CLOUDFLARE_API_KEY", "CF_API_TOKEN")
_alias_env("CLOUDFLARE_ACCOUNT_ID", "CF_ACCOUNT_ID")

# GitHub Models: keys are hailports-account (lane="hustle") — NEVER work/PII.
# The litellm `github/` provider targets GitHub's RETIRED endpoint (Bad credentials);
# entries tagged {"github_direct": True} are dispatched by _call_litellm straight to
# the LIVE endpoint (models.github.ai/inference). Model IDs are the live namespaced
# form (openai/gpt-4o, openai/gpt-4o-mini). Free tier has a hard daily cap, so GitHub
# leads the high-value STRONG/BALANCED lanes (displaces paid Sonnet/Opus) but sits
# BEHIND groq-instant in FAST, to not burn the daily cap on trivial classify work.
FAST_MODELS = [
    # Quick classification, scoring, short answers — instant, NON-reasoning models
    # only (reasoning models like zai-glm eat tiny token budgets thinking and
    # return empty, so they're kept out of the fast lane).
    {"model": "groq/llama-3.1-8b-instant", "name": "groq-instant", "env": "GROQ_API_KEY"},
    {"model": "cloudflare/@cf/meta/llama-3.1-8b-instruct", "name": "cloudflare-8b", "env": "CLOUDFLARE_API_KEY"},
    {"model": "gemini/gemini-2.5-flash-lite", "name": "gemini-flash-lite", "env": "GEMINI_API_KEY"},
    {"model": "openai/gpt-4o-mini", "name": "github-4o-mini", "env": "GITHUB_TOKEN", "github_direct": True, "lane": "hustle"},
    {"model": "groq/llama-3.3-70b-versatile", "name": "groq", "env": "GROQ_API_KEY"},
    {"model": "sambanova/Meta-Llama-3.3-70B-Instruct", "name": "sambanova", "env": "SAMBANOVA_API_KEY"},
]

BALANCED_MODELS = [
    # General chat, drafting, summarization — GitHub 4o-mini leads (free, 128k, strong).
    {"model": "openai/gpt-4o-mini", "name": "github-4o-mini", "env": "GITHUB_TOKEN", "github_direct": True, "lane": "hustle"},
    {"model": "cloudflare/@cf/meta/llama-3.3-70b-instruct-fp8-fast", "name": "cloudflare-70b", "env": "CLOUDFLARE_API_KEY"},
    {"model": "groq/llama-3.3-70b-versatile", "name": "groq", "env": "GROQ_API_KEY"},
    {"model": "cerebras/zai-glm-4.7", "name": "cerebras-glm", "env": "CEREBRAS_API_KEY", "max_ctx": 8192},
    {"model": "gemini/gemini-2.0-flash", "name": "gemini", "env": "GEMINI_API_KEY"},
    # Gemini text expansion (distinct names for per-name cooldown; keep 2.0-flash above).
    {"model": "gemini/gemini-2.5-flash", "name": "gemini-2.5-flash", "env": "GEMINI_API_KEY"},
    {"model": "sambanova/Meta-Llama-3.3-70B-Instruct", "name": "sambanova", "env": "SAMBANOVA_API_KEY"},
    # NVIDIA NIM free tier (integrate.api.nvidia.com, OpenAI-compatible, no CC, 40 RPM).
    # Credits are 1/CALL — kept LAST as deep backup. HUSTLE-LANE ONLY (lane="hustle").
    {"model": "nvidia_nim/deepseek-ai/deepseek-v4-flash", "name": "nvidia-flash", "env": "NVIDIA_NIM_API_KEY", "lane": "hustle"},
]

STRONG_MODELS = [
    # Analysis, complex reasoning, code review. GitHub gpt-4o leads (free, strongest
    # in pool, displaces expensive paid Sonnet/Opus). Then Qwen/DeepSeek/GLM families.
    {"model": "openai/gpt-4o", "name": "github-4o", "env": "GITHUB_TOKEN", "github_direct": True, "lane": "hustle"},
    {"model": "cerebras/zai-glm-4.7", "name": "cerebras-glm", "env": "CEREBRAS_API_KEY", "max_ctx": 8192},
    {"model": "gemini/gemini-2.0-flash", "name": "gemini", "env": "GEMINI_API_KEY"},
    {"model": "cerebras/gpt-oss-120b", "name": "cerebras-oss", "env": "CEREBRAS_API_KEY", "max_ctx": 8192},
    {"model": "sambanova/Meta-Llama-3.3-70B-Instruct", "name": "sambanova", "env": "SAMBANOVA_API_KEY"},
    # NVIDIA NIM reasoning models (live-verified 2026-07-08). Kept last — credits precious.
    {"model": "nvidia_nim/z-ai/glm-5.2", "name": "nvidia-glm", "env": "NVIDIA_NIM_API_KEY", "lane": "hustle"},
    {"model": "nvidia_nim/qwen/qwen3.5-397b-a17b", "name": "nvidia-qwen", "env": "NVIDIA_NIM_API_KEY", "lane": "hustle"},
]

# Long-context capable free models (tried first when the prompt is large, so we
# stay free even on big inputs; router escalates to paid long-context only if
# these fail). gemini-2.0-flash = 1M ctx, qwen3-next = 256k ctx,
# claude-3.5-sonnet = 200k ctx.
LONG_CONTEXT_MODELS = [
    {"model": "openai/gpt-4o", "name": "github-4o", "env": "GITHUB_TOKEN", "github_direct": True, "lane": "hustle"},
    {"model": "gemini/gemini-2.0-flash", "name": "gemini", "env": "GEMINI_API_KEY"},
    {"model": "nvidia_nim/z-ai/glm-5.2", "name": "nvidia-glm", "env": "NVIDIA_NIM_API_KEY", "lane": "hustle"},
]

# ---- OpenRouter :free lane (lane="hustle") --------------------------------
# HARD GUARD: only model IDs ending ':free' may EVER enter the pool through
# OpenRouter. OPENROUTER_API_KEY is also used as a PAID forwarder in
# tools/anthropic_proxy.py — that path is separate and must never be reachable
# from here, so every entry-build and every dispatch re-asserts the ':free' tail.
# Entries are tagged lane="hustle" (hailports-account key) so the work_lane
# air-gap excludes them from corp/PII traffic.
def _openrouter_free_entry(model_id: str, *, max_ctx: int | None = None) -> dict:
    if not model_id.endswith(":free"):
        raise ValueError(f"non-free OpenRouter model blocked: {model_id}")
    name = "or-" + model_id.split("/")[-1].replace(":free", "")
    entry = {"model": f"openrouter/{model_id}", "name": name,
             "env": "OPENROUTER_API_KEY", "lane": "hustle"}
    if max_ctx:
        entry["max_ctx"] = max_ctx
    return entry


# Preferred free IDs per tier (curated, all ':free'). Intersected with the LIVE
# catalog when reachable; used as-is (guard still holds) if the API is down.
_OR_STRONG_IDS = [
    "deepseek/deepseek-r1:free", "qwen/qwen3-coder:free",
    "openai/gpt-oss-120b:free", "nvidia/nemotron-3-super-120b-a12b:free",
]
_OR_BALANCED_IDS = [
    "meta-llama/llama-4-maverick:free", "meta-llama/llama-3.3-70b-instruct:free",
    "openai/gpt-oss-20b:free",
]
_OR_LONG_IDS = [  # current 1M-ctx free models
    "nvidia/nemotron-3-ultra-550b-a55b:free", "qwen/qwen3-coder:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
]


def _fetch_openrouter_free_ids() -> set | None:
    """GET the live catalog and return the set of free (pricing.prompt=='0',
    id endswith ':free') model IDs. Returns None on any failure so the caller
    falls back to the static preferred lists. Never raises."""
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        return None
    import json, urllib.request
    try:
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/models",
            headers={"Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(req, timeout=15) as r:
            rows = json.loads(r.read()).get("data", [])
    except Exception as e:
        log.warning("OpenRouter free-catalog fetch failed: %s", str(e)[:120])
        return None
    ids = {m["id"] for m in rows
           if str(m.get("pricing", {}).get("prompt")) == "0"
           and str(m.get("id", "")).endswith(":free")}
    return ids or None


def _install_openrouter_free() -> None:
    """Build OpenRouter :free entries from the live catalog (static seeds on
    failure) and append to the STRONG/BALANCED/LONG tiers. Fail-safe: on any
    error the tiers are left untouched and the rest of the pool is unaffected."""
    try:
        live = _fetch_openrouter_free_ids()

        def keep(ids):
            # API down (live is None) -> trust the curated list; the ':free'
            # guard still holds and any dead ID just fails over at call time.
            return ids if live is None else [i for i in ids if i in live]

        for mid in keep(_OR_STRONG_IDS):
            STRONG_MODELS.append(_openrouter_free_entry(mid))
        for mid in keep(_OR_BALANCED_IDS):
            BALANCED_MODELS.append(_openrouter_free_entry(mid))
        for mid in keep(_OR_LONG_IDS):
            LONG_CONTEXT_MODELS.append(_openrouter_free_entry(mid))
        log.info("OpenRouter :free lane installed (live catalog=%s)", live is not None)
    except Exception as e:
        log.warning("OpenRouter :free lane install skipped: %s", str(e)[:120])


_install_openrouter_free()

# ---- Vision lane ----------------------------------------------------------
# Free multimodal models via litellm image_url content parts. Degrades to next
# on any key/endpoint failure, never raises.
VISION_MODELS = [
    {"model": "gemini/gemini-2.5-flash", "name": "gemini-vision", "env": "GEMINI_API_KEY"},
    {"model": "groq/meta-llama/llama-4-scout-17b-16e-instruct", "name": "groq-scout-vision", "env": "GROQ_API_KEY"},
    {"model": "cloudflare/@cf/llava-hf/llava-1.5-7b-hf", "name": "cloudflare-llava", "env": "CLOUDFLARE_API_KEY"},
]

# ---- Embeddings lane ------------------------------------------------------
# Local Ollama nomic-embed FIRST (keeps the $0 offline path), then Gemini cloud.
EMBED_MODELS = [
    {"model": "ollama/nomic-embed-text", "name": "ollama-nomic", "local": True},
    {"model": "gemini/gemini-embedding-001", "name": "gemini-embed", "env": "GEMINI_API_KEY"},
]

# Tier-aware timeouts (seconds): a stuck fast provider must fail over fast, not
# stall a trivial task; long/strong tasks get more room.
_TIER_TIMEOUT = {"fast": 12, "balanced": 25, "strong": 60, "long": 75}

# Above this approx token count we route to long-context models first.
_LONG_CTX_TOKENS = 6000  # ~24k chars

# Task classification patterns
_FAST_PATTERNS = re.compile(
    r"(classify|score|urgency|yes or no|true or false|pick one|choose|"
    r"respond with only|just the number|one word|JSON only)",
    re.IGNORECASE,
)
_STRONG_PATTERNS = re.compile(
    r"(analyze|compare|review|debug|explain why|write a plan|strategy|"
    r"implementation|architecture|refactor|synthesize|assessment|root cause|"
    r"summarize the following.{50,})",
    re.IGNORECASE,
)

# Rate limiting and failure tracking
_last_call: dict[str, float] = {}
_fail_count: dict[str, int] = {}
_BACKOFF_AFTER = 3
_COOLDOWN_S = 300


def _est_tokens(text: str) -> int:
    """Cheap token estimate (~4 chars/token) for context-budget guards."""
    return len(text or "") // 4


# ---- Exact-match response cache (DETERMINISTIC lanes only) -----------------
# Only the fast lane (classify/score/route JSON) is cached — creative
# balanced/strong output must never be served stale. Uses the installed Redis
# if reachable, else an in-process dict with TTL.
_CACHE_TTL_S = 3600
_mem_cache: dict[str, tuple[float, str]] = {}


def _redis_client():
    try:
        import redis  # optional dep
        c = redis.Redis(socket_connect_timeout=0.3, socket_timeout=0.3)
        c.ping()
        return c
    except Exception:
        return None


_REDIS = _redis_client()  # None -> in-process dict fallback


def _cache_key(task_type: str, system: str, prompt: str, max_tokens: int) -> str:
    h = hashlib.sha256()
    h.update(f"{task_type}|{system or ''}|{prompt}|{max_tokens}".encode())
    return "flp:" + h.hexdigest()


def _cache_get(key: str):
    if _REDIS is not None:
        try:
            v = _REDIS.get(key)
            if v is not None:
                return v.decode() if isinstance(v, bytes) else str(v)
        except Exception:
            pass
    hit = _mem_cache.get(key)
    if hit and time.time() - hit[0] < _CACHE_TTL_S:
        return hit[1]
    if hit:
        _mem_cache.pop(key, None)
    return None


def _cache_set(key: str, val: str) -> None:
    if _REDIS is not None:
        try:
            _REDIS.setex(key, _CACHE_TTL_S, val)
            return
        except Exception:
            pass
    _mem_cache[key] = (time.time(), val)


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUE_VALUES


def hard_force_local_routing() -> bool:
    """Whether local routing should override runtime-pressure safeguards."""
    return _env_truthy("CLAUDE_STACK_FORCE_LOCAL_ROUTING_HARD")


def force_local_routing() -> bool:
    """Whether the stack should prefer local routing when the box is healthy."""
    return (
        hard_force_local_routing()
        or _env_truthy("CLAUDE_STACK_FORCE_LOCAL_ROUTING")
        or _env_truthy("FORCE_LOCAL_LLM")
    )


def _soft_force_local_suppressed_by_pressure() -> bool:
    if hard_force_local_routing() or not force_local_routing():
        return False
    try:
        return bool(get_runtime_pressure().get("avoid_local", False))
    except Exception:
        return False


def free_pool_allowed(*, explicit: bool = False) -> bool:
    """Whether automatic free-pool fallback is allowed for this request."""
    if _env_truthy("CLAUDE_STACK_DISABLE_FREE_POOL"):
        return False
    if force_local_routing() and not explicit and not _soft_force_local_suppressed_by_pressure():
        return False
    if _env_truthy("CLAUDE_STACK_FREE_POOL_EXPLICIT_ONLY") and not explicit:
        return False
    return True


def _classify_task(prompt: str) -> str:
    """Classify the task to pick the right model tier.

    Short/simple prompts go to the fast free lane so trivial work never burns a
    heavier model: any prompt under 280 chars, or under 500 chars with a
    fast-pattern keyword. Big or explicitly-analytical prompts go strong; the
    'long' bucket is decided in try_free_providers by token estimate."""
    if len(prompt) < 280 or (len(prompt) < 500 and _FAST_PATTERNS.search(prompt)):
        return "fast"
    if _STRONG_PATTERNS.search(prompt) or len(prompt) > 3000:
        return "strong"
    return "balanced"


def _is_available(entry: dict) -> bool:
    """Check if provider has key and isn't in cooldown."""
    name = entry["name"]
    if not os.environ.get(entry["env"], ""):
        return False
    if _fail_count.get(name, 0) >= _BACKOFF_AFTER:
        if time.time() - _last_call.get(name, 0) < _COOLDOWN_S:
            return False
        _fail_count[name] = 0
    return True


def _github_direct_sync(model_id: str, prompt: str, system: str, max_tokens: int, timeout: int) -> str:
    """Direct call to GitHub Models (models.github.ai/inference) — $0. litellm's
    github/ provider targets GitHub's retired endpoint (Bad credentials); this hits
    the live one. gpt-4o may be account-gated → fall back to gpt-4o-mini before failing."""
    import json, urllib.request
    tok = os.environ.get("GITHUB_TOKEN", "")
    if not tok:
        return ""
    msgs = ([{"role": "system", "content": system}] if system else []) + \
           [{"role": "user", "content": prompt}]
    body = json.dumps({"model": model_id, "messages": msgs, "max_tokens": max_tokens}).encode()
    req = urllib.request.Request(
        "https://models.github.ai/inference/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read())
        return (d.get("choices", [{}])[0].get("message", {}).get("content") or "").strip()
    except urllib.error.HTTPError as e:
        # 404 = model gated for this account -> mini is worth a shot. 403/429 = rate
        # limit (same cap hits mini too) -> re-raise so the pool moves to next provider.
        if e.code == 404 and model_id != "openai/gpt-4o-mini":
            return _github_direct_sync("openai/gpt-4o-mini", prompt, system, max_tokens, timeout)
        raise


async def _call_litellm(entry: dict, prompt: str, system: str = None, max_tokens: int = 2048, timeout: int = 60) -> str:
    """Call a model via LiteLLM (or the GitHub-direct path for github_direct entries)."""
    # HARD GUARD: OpenRouter is a free-only lane here; the paid forwarder path
    # (tools/anthropic_proxy.py) must never be reachable through this pool.
    if entry["model"].startswith("openrouter/") and not entry["model"].endswith(":free"):
        raise RuntimeError(f"blocked non-free OpenRouter model: {entry['model']}")
    # Fail-fast caps so a hung GitHub or a slow NVIDIA reasoning model can't stall an
    # escalation for the full tier budget (GitHub answers <1s healthy; a rate-limit is
    # instant). Bounds the urllib socket timeout AND the litellm call timeout.
    if entry.get("github_direct"):
        timeout = min(timeout, 15)
    elif entry["model"].startswith("nvidia_nim/"):
        timeout = min(timeout, 35)

    if entry.get("github_direct"):
        return await asyncio.to_thread(
            _github_direct_sync, entry["model"], prompt, system or "", max_tokens, timeout)
    import litellm
    litellm.suppress_debug_info = True
    litellm.set_verbose = False

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    kwargs = dict(
        model=entry["model"],
        messages=messages,
        max_tokens=max_tokens,
        timeout=timeout,
    )
    # Cloudflare Workers AI and GitHub Models may reject the `temperature` param
    # (UnsupportedParamsError) — omit it there so they actually contribute free capacity.
    if not entry["model"].startswith("cloudflare/") and not entry["model"].startswith("github/"):
        kwargs["temperature"] = 0.7
    response = await litellm.acompletion(**kwargs)
    return response.choices[0].message.content


async def try_free_providers(
    prompt: str,
    system: str = None,
    max_tokens: int = 2048,
    *,
    explicit: bool = False,
    tier: str | None = None,
    work_lane: bool = False,
) -> tuple:
    """Smart-route through free providers based on task type.
    Returns (text, provider_name) or (None, None) if all fail.

    work_lane=True (or env CLAUDE_STACK_WORK_LANE) HARD-excludes providers whose
    keys are hailports-account (lane="hustle": GitHub Models, NVIDIA NIM) so corp
    work / PII can never traverse a hustle-persona credential. Air-gap is fail-safe."""
    from core.corp_tenant import is_corp_tenant, run_corp_codex
    if is_corp_tenant():
        # Corp-tenant work never rides the free provider pool; pin to corp Codex.
        return await run_corp_codex(
            prompt, system=system, max_tokens=max_tokens, source="free_pool"
        ), "corp:codex"
    work_lane = work_lane or _env_truthy("CLAUDE_STACK_WORK_LANE")
    if len(prompt) > 1024 and not free_pool_allowed(explicit=True) :
        log.info("Free pool bypassed by local-first routing policy")
        return None, None

    task_type = str(tier or "").strip().lower() or _classify_task(prompt)
    # Big inputs: route to long-context-capable free models first, and only fall
    # through to the strong tier. Router escalates to paid long-context if all fail.
    if task_type == "long" or (len(prompt) // 4) > _LONG_CTX_TOKENS:
        task_type = "long"
        candidates = LONG_CONTEXT_MODELS + STRONG_MODELS
    elif task_type == "fast":
        candidates = FAST_MODELS + BALANCED_MODELS
    elif task_type == "strong":
        candidates = STRONG_MODELS + BALANCED_MODELS
    else:
        candidates = BALANCED_MODELS + FAST_MODELS
    call_timeout = _TIER_TIMEOUT.get(task_type, 60)

    # Exact-match cache on the deterministic fast lane ONLY (never creative tiers).
    cache_key = _cache_key(task_type, system, prompt, max_tokens) if task_type == "fast" else None
    if cache_key:
        cached = _cache_get(cache_key)
        if cached is not None:
            log.info("Free pool: cache hit task=%s", task_type)
            return cached, "cache"

    # Cerebras free models cap at 8192 ctx — clamp-skip any candidate whose
    # prompt+output would overrun its max_ctx to stop silent truncation.
    budget = _est_tokens(prompt) + max_tokens

    # Dedupe by name (keep first occurrence = preferred)
    seen = set()
    unique = []
    for entry in candidates:
        if work_lane and entry.get("lane") == "hustle":
            continue  # air-gap: hailports-account free keys never touch work/PII
        max_ctx = entry.get("max_ctx")
        if max_ctx and budget > max_ctx - 192:
            continue  # would overrun context -> skip to avoid silent truncation
        if entry["name"] not in seen and _is_available(entry):
            seen.add(entry["name"])
            unique.append(entry)

    for entry in unique:
        name = entry["name"]
        _last_call[name] = time.time()
        try:
            result = await _call_litellm(entry, prompt, system, max_tokens, timeout=call_timeout)
            if result and result.strip():
                _fail_count[name] = 0
                log.info("Free pool: %s (%s) succeeded, %d chars, task=%s",
                         name, entry["model"], len(result), task_type)
                if cache_key:
                    _cache_set(cache_key, result.strip())
                return result.strip(), name
        except Exception as e:
            _fail_count[name] = _fail_count.get(name, 0) + 1
            log.warning("Free pool: %s failed: %s", name, str(e)[:100])
            continue

    return None, None


async def try_free_vision(
    prompt: str,
    image_url_or_b64: str,
    system: str = None,
    max_tokens: int = 1024,
    *,
    work_lane: bool = False,
) -> tuple:
    """Multimodal free lane. `image_url_or_b64` may be an http(s) URL, a data:
    URI, or raw base64 (wrapped as a PNG data URI). Returns (text, provider) or
    (None, None). Degrades to next provider on any failure, never raises."""
    work_lane = work_lane or _env_truthy("CLAUDE_STACK_WORK_LANE")
    img = image_url_or_b64
    if not (img.startswith("http") or img.startswith("data:")):
        img = "data:image/png;base64," + img
    content = [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": img}},
    ]
    messages = ([{"role": "system", "content": system}] if system else []) + \
               [{"role": "user", "content": content}]

    seen = set()
    for entry in VISION_MODELS:
        name = entry["name"]
        if work_lane and entry.get("lane") == "hustle":
            continue
        if name in seen or not _is_available(entry):
            continue
        seen.add(name)
        _last_call[name] = time.time()
        try:
            import litellm
            litellm.suppress_debug_info = True
            litellm.set_verbose = False
            resp = await litellm.acompletion(
                model=entry["model"], messages=messages,
                max_tokens=max_tokens, timeout=45)
            text = (resp.choices[0].message.content or "").strip()
            if text:
                _fail_count[name] = 0
                log.info("Free vision: %s (%s) succeeded, %d chars",
                         name, entry["model"], len(text))
                return text, name
        except Exception as e:
            _fail_count[name] = _fail_count.get(name, 0) + 1
            log.warning("Free vision: %s failed: %s", name, str(e)[:120])
            continue
    return None, None


# Circuit breaker: the local embed server (llama-server behind :11434/api/embed) flaps while
# ollama_guard/supervisor cycles it. Each hang used to block a caller for the full 20s timeout;
# stacked across every lane/incident in one ops_health run, that overran the run interval and let
# the STATE file go stale -> the "net alive" self-heal fired and a kickstart just restarted a run
# that stalled the same way. Once the server refuses/times out twice, trip the breaker so every
# subsequent embed fails in sub-ms for a cooldown window (callers already degrade gracefully to
# Gemini/None), then probe again. Bounded timeout so a single slow call can't hang either.
_EMBED_TIMEOUT = 8.0        # was 20 — bound a single hang
_EMBED_TRIP_AFTER = 2       # consecutive failures before the breaker opens
_EMBED_COOLDOWN = 60.0      # seconds the breaker stays open before re-probing
_embed_breaker = {"open_until": 0.0, "fails": 0}


def _ollama_embed(texts: list) -> list | None:
    """Local Ollama nomic-embed ($0, offline). Returns list of vectors or None.
    Fails fast (sub-ms) while the circuit breaker is open so a flapping embed server can never
    stall a caller for 20s+ per call."""
    import json, urllib.request
    now = time.monotonic()
    if now < _embed_breaker["open_until"]:
        raise RuntimeError("ollama embed circuit open (server flapping; cooling down)")
    host = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
    body = json.dumps({"model": "nomic-embed-text", "input": texts}).encode()
    req = urllib.request.Request(
        host + "/api/embed", data=body,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=_EMBED_TIMEOUT) as r:
            d = json.loads(r.read())
    except Exception:
        _embed_breaker["fails"] += 1
        if _embed_breaker["fails"] >= _EMBED_TRIP_AFTER:
            _embed_breaker["open_until"] = now + _EMBED_COOLDOWN
        raise
    _embed_breaker["fails"] = 0
    _embed_breaker["open_until"] = 0.0
    return d.get("embeddings") or None


async def try_free_embeddings(texts, *, work_lane: bool = False) -> tuple:
    """Free embeddings lane. Local Ollama nomic-embed FIRST (keeps the $0 offline
    path), then Gemini cloud fallback. Returns (list_of_vectors, provider) or
    (None, None). Degrades gracefully, never raises."""
    if isinstance(texts, str):
        texts = [texts]
    # 1) local Ollama ($0)
    try:
        vecs = await asyncio.to_thread(_ollama_embed, texts)
        if vecs:
            log.info("Free embed: ollama-nomic succeeded, %d vecs", len(vecs))
            return vecs, "ollama-nomic"
    except Exception as e:
        log.warning("Free embed: ollama failed: %s", str(e)[:120])
    # 2) Gemini cloud fallback
    if os.environ.get("GEMINI_API_KEY"):
        try:
            import litellm
            litellm.suppress_debug_info = True
            resp = await litellm.aembedding(
                model="gemini/gemini-embedding-001", input=texts)
            data = resp["data"] if isinstance(resp, dict) else resp.data
            vecs = [d["embedding"] if isinstance(d, dict) else d.embedding for d in data]
            if vecs:
                log.info("Free embed: gemini-embed succeeded, %d vecs", len(vecs))
                return vecs, "gemini-embed"
        except Exception as e:
            log.warning("Free embed: gemini failed: %s", str(e)[:120])
    return None, None


def get_available_providers() -> list[str]:
    """Return list of provider names that have API keys configured."""
    all_entries = FAST_MODELS + BALANCED_MODELS + STRONG_MODELS
    seen = set()
    result = []
    for e in all_entries:
        if e["name"] not in seen and os.environ.get(e["env"], ""):
            seen.add(e["name"])
            result.append(e["name"])
    return result


def get_pool_status() -> dict:
    """Return status of all providers for dashboard display."""
    all_entries = FAST_MODELS + BALANCED_MODELS + STRONG_MODELS
    seen = set()
    status = {
        "_policy": {
            "force_local_routing": force_local_routing(),
            "free_pool_enabled": free_pool_allowed(),
            "explicit_free_pool_enabled": free_pool_allowed(explicit=True),
        }
    }
    for e in all_entries:
        if e["name"] in seen:
            continue
        seen.add(e["name"])
        has_key = bool(os.environ.get(e["env"], ""))
        fails = _fail_count.get(e["name"], 0)
        in_cooldown = fails >= _BACKOFF_AFTER and (time.time() - _last_call.get(e["name"], 0)) < _COOLDOWN_S
        status[e["name"]] = {
            "configured": has_key,
            "model": e["model"],
            "failures": fails,
            "cooldown": in_cooldown,
        }
    return status

async def handle_request(prompt, system=None, max_tokens=4096):
    if len(prompt) > 1024:
        # Always attempt free providers for larger inputs
        free_result, free_provider = await try_free_providers(prompt, system=system, max_tokens=max_tokens)
        if free_result is not None:
            return free_result

    # Fallback to paid providers if necessary
    result = await try_paid_providers(prompt, system=system, max_tokens=max_tokens)
    return result
