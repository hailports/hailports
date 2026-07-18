"""model_perf — warm models + fast hotswap + fastest-capable routing (no lag, full power).

The stack already knows *which* model to use per prompt (llm_router._pick_tier,
free_llm_pool tier lists) and *how* to pin the fast local model resident
(interactive_preemption.ensure_warm_model). What it lacks is a single seam that a caller
hits BEFORE a request to (a) guarantee the fast interactive model is hot so the first
reply never eats a cold-load, and (b) get a deterministic best-model decision — fastest
warm local for interactive/simple, a stronger tier for heavy reasoning, batched where it
can be. This module is that seam. It pilfers the existing routing + warming, never
reinvents it.

Two public entrypoints:

  ensure_warm(models=None, keep_alive="30m") -> dict
    Keep the FAST interactive local model (llm_router.MODEL_FAST / FAST_LOCAL_MODEL) hot in
    Ollama. Delegates to interactive_preemption.ensure_warm_model (cheap /api/ps check;
    no-op if already loaded). Accepts an explicit model or list of models to warm (hotswap
    prep: warm several so a task switch has no reload stall). Fail-soft.

  route(task) -> dict
    Deterministic tiering. `task` is a prompt str or a dict {prompt, interactive, batch,
    heavy, override}. Returns the chosen model, tier, execution mode
    (interactive|heavy|batch), whether it's a warm local path, whether it may run async,
    and which free_llm_pool tier to spill to. Pure + network-free — safe to call on the
    hot path.

Coordination with interactive_preemption: when the operator is actively pinging the box,
warm the fast model proactively (warm_for_operator) so their first reply is instant.

Smoke test (safe, no heavy pulls): proves ensure_warm keeps the fast model loaded / no-ops
when already warm, and route() picks fastest-warm local for an interactive task vs a
stronger tier for a heavy one.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.llm_router import MODEL_FAST, MODEL_QUALITY, _pick_tier  # noqa: E402
from core import interactive_preemption as _preempt  # noqa: E402

# free_llm_pool tier names we spill to when the local box can't turn a tier around fast
# enough (or when async escalation is wanted). We reference the tier by NAME so the actual
# provider list stays single-sourced in free_llm_pool — no duplication here.
_FREE_TIER_FAST = "FAST_MODELS"
_FREE_TIER_STRONG = "STRONG_MODELS"


# --- warm / hotswap -------------------------------------------------------
def ensure_warm(models=None, keep_alive: str = "30m") -> dict:
    """Keep the fast interactive local model(s) resident so the first reply has no cold-load.

    models: None -> warm MODEL_FAST (the interactive default). A str or iterable of model
    names warms exactly those — pre-warming a heavier model here means a later hotswap to it
    is a resident-model switch (no download/load stall). Each warm delegates to
    interactive_preemption.ensure_warm_model, which is a cheap /api/ps check + no-op when the
    model is already loaded. Fail-soft: a single model's failure is captured, never raised.
    """
    if models is None:
        targets = [MODEL_FAST]
    elif isinstance(models, str):
        targets = [models]
    else:
        targets = [m for m in models if m]
        if not targets:
            targets = [MODEL_FAST]

    results = []
    for m in targets:
        try:
            results.append(_preempt.ensure_warm_model(m, keep_alive=keep_alive))
        except Exception as e:  # never let warming raise on the hot path
            results.append({"model": m, "warm": False, "action": "error", "error": str(e)})

    all_warm = all(r.get("warm") for r in results)
    return {"warm": all_warm, "count": len(results), "models": results}


def is_warm(model: str | None = None) -> bool:
    """True if the given model (default MODEL_FAST) is currently resident in Ollama.
    Cheap /api/ps read; matches on the model's base name (tags like :latest ignored)."""
    model = model or MODEL_FAST
    loaded = _preempt._ollama_loaded_models()
    base = model.split(":")[0]
    return any(m == model or m.startswith(base) for m in loaded)


def warm_for_operator(window_s: int = _preempt.ACTIVE_WINDOW_S) -> dict:
    """Coordinate with interactive_preemption: if the operator is actively pinging the box,
    warm the fast model NOW so their first interactive reply is instant. No-op when idle
    (don't hold RAM the operator isn't about to use). Returns the activity + warm result."""
    active, age, source = _preempt.is_operator_active(window_s)
    out = {"active": active, "seconds_since": age, "source": source}
    if active:
        out["warm"] = ensure_warm()
    else:
        out["warm"] = {"warm": is_warm(), "action": "skipped-idle"}
    return out


# --- routing --------------------------------------------------------------
def _coerce(task) -> dict:
    """Normalize a task into {prompt, interactive, batch, heavy, override}."""
    if isinstance(task, dict):
        return {
            "prompt": str(task.get("prompt") or task.get("text") or ""),
            "interactive": bool(task.get("interactive", False)),
            "batch": bool(task.get("batch", False)),
            "heavy": bool(task.get("heavy", False)),
            "override": task.get("override"),
        }
    return {"prompt": str(task or ""), "interactive": False,
            "batch": False, "heavy": False, "override": None}


def route(task) -> dict:
    """Pick the fastest-CAPABLE model for `task`. Deterministic + network-free.

    Decision:
      tier  = llm_router._pick_tier(prompt, override)  (single-sourced tiering; heavy
              markers / >3k chars -> 'quality', else 'fast'). An explicit heavy=True flag
              also forces 'quality'.
      mode  = 'batch'       if caller flagged batch (many items / no latency budget)
              'interactive' if fast tier OR caller flagged interactive (latency-sensitive)
              'heavy'       otherwise (quality tier reasoning)
      model = MODEL_FAST for interactive fast path (warm local, no cold-load),
              MODEL_QUALITY for heavy/batch (stronger local reasoning model).
      async_ok  = heavy/batch may run off the hot path (async worker / MLX strong server).
      free_tier = which free_llm_pool tier to spill to if local is slow/unavailable.
    """
    t = _coerce(task)
    prompt, override = t["prompt"], t["override"]

    tier = _pick_tier(prompt, override)
    if t["heavy"]:
        tier = "quality"

    if t["batch"]:
        mode = "batch"
    elif tier == "fast" or (t["interactive"] and not t["heavy"]):
        mode = "interactive"
    else:
        mode = "heavy"

    if mode == "interactive":
        model, local, free_tier = MODEL_FAST, True, _FREE_TIER_FAST
        # interactive path can still be quality content on a short prompt; keep tier honest
        tier = "fast" if tier == "fast" else "quality"
    else:  # heavy or batch
        model, local, free_tier = MODEL_QUALITY, True, _FREE_TIER_STRONG
        tier = "quality"

    async_ok = mode in ("heavy", "batch")
    return {
        "model": model,
        "tier": tier,
        "mode": mode,
        "local": local,
        "async_ok": async_ok,
        "warm_recommended": mode == "interactive",
        "free_tier": free_tier,
        "reason": _reason(mode, tier, t),
    }


def _reason(mode: str, tier: str, t: dict) -> str:
    if mode == "batch":
        return "batchable -> stronger tier, run async/batched"
    if mode == "interactive":
        return "interactive/simple -> fastest warm local (no cold-load)"
    return f"heavy reasoning ({tier}) -> stronger tier, async-ok"


def route_and_warm(task) -> dict:
    """route() + proactively warm the chosen model when it's the interactive warm path.
    Convenience for callers that want the decision AND the model hot in one call."""
    decision = route(task)
    if decision.get("warm_recommended"):
        decision["warm"] = ensure_warm(decision["model"])
    return decision


if __name__ == "__main__":
    print("=== model_perf smoke test (no heavy pulls) ===")
    print(f"MODEL_FAST={MODEL_FAST!r}  MODEL_QUALITY={MODEL_QUALITY!r}")

    # 1) warmth: prove ensure_warm keeps the fast model loaded, or no-ops if already warm.
    before = is_warm()
    print(f"\n[warm] fast model resident before: {before}")
    res = ensure_warm()
    print(f"[warm] ensure_warm() -> warm={res['warm']} "
          f"action={[r.get('action') for r in res['models']]}")
    after = is_warm()
    print(f"[warm] fast model resident after:  {after}")
    if res["models"] and res["models"][0].get("action") == "already-loaded":
        print("[warm] OK: already-loaded -> no-op (no forced reload)")
    elif res["warm"]:
        print("[warm] OK: model loaded + pinned")
    else:
        # Ollama may be down in the test env; the path must still be fail-soft, not raise.
        print("[warm] Ollama unreachable -> fail-soft (no crash), acceptable in test env")

    # 2) routing: interactive -> fastest warm local; heavy -> stronger tier.
    i = route("what's on my calendar today?")
    h = route("debug this stack trace and root-cause the memory leak, step-by-step")
    b = route({"prompt": "summarize the following 400 rows", "batch": True})
    print(f"\n[route] interactive -> model={i['model']!r} tier={i['tier']} "
          f"mode={i['mode']} async_ok={i['async_ok']}")
    print(f"[route] heavy       -> model={h['model']!r} tier={h['tier']} "
          f"mode={h['mode']} async_ok={h['async_ok']}")
    print(f"[route] batch       -> model={b['model']!r} tier={b['tier']} "
          f"mode={b['mode']} async_ok={b['async_ok']}")

    assert i["mode"] == "interactive" and i["model"] == MODEL_FAST, "interactive must pick fast warm local"
    assert i["tier"] == "fast" and not i["async_ok"], "interactive must be fast + on hot path"
    assert h["mode"] == "heavy" and h["tier"] == "quality", "heavy must pick quality tier"
    assert h["model"] == MODEL_QUALITY and h["async_ok"], "heavy must use stronger model + async-ok"
    assert b["mode"] == "batch" and b["async_ok"], "batch must be async-ok"
    assert i["free_tier"] == _FREE_TIER_FAST and h["free_tier"] == _FREE_TIER_STRONG, \
        "spill tiers must match mode"

    # 3) operator coordination (read-only; warms only if operator is active right now).
    op = warm_for_operator()
    print(f"\n[coord] operator active={op['active']} "
          f"(warm action={op['warm'].get('action', op['warm'].get('warm'))})")

    print("\nsmoke ok")
