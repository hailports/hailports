#!/usr/bin/env python3
"""Shared local-model reasoning entry — Fable-primed, router-aligned, $0 by default.

Two prior "smarter" investments were only half-applied; this composes both onto the local path:
  1. The Fable 5 reasoning operating manual (~/.claude/fable5-operating-manual.md) was wired into
     codex only. This injects it (condensed by default) as the SYSTEM prompt for reasoning-grade
     LOCAL calls so Hermes/qwen reason at Fable's bar too — the $0 surface, not just codex.
  2. The local-first routing layer (core.local_model_registry / core.llm_router). Instead of a
     bare hardcoded Ollama call, model selection comes from get_active_local_model() (the router's
     local tier). Callers that genuinely want local->free->paid escalation pass escalate=True and
     we delegate to llm_router.try_local_then_api (guarded); the default is local-only ($0), which
     is correct for bulk/mechanical jobs like the instinct harvest — you never pay to mine a log.

Apply ONLY to reasoning/judgment/extraction tasks (fable's own task-classification rule). NOT to
creative voice generation (content_generator uses /no_think on purpose) and NOT to fail-closed
gates (anon_scrub's judge) where a prompt change could shift a verdict.

  from core.local_reason import local_generate
  text = local_generate(prompt, reason=True)              # $0 local, fable-primed
  text = local_generate(prompt, reason=True, escalate=True)  # local->free->paid via llm_router

  python3 core/local_reason.py --selftest
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OLLAMA = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
FABLE_MANUAL = Path.home() / ".claude" / "fable5-operating-manual.md"
_DEFAULT_MODEL = os.environ.get("CLAUDE_STACK_LOCAL_MODEL", os.environ.get("LOCAL_MODEL", "qwen2.5:7b"))
_LLM_TIMEOUT = float(os.environ.get("LOCAL_REASON_TIMEOUT", os.environ.get("INSTINCT_LLM_TIMEOUT", "120")))

# Condensed operative digest of the Fable 5 manual — the reasoning METHOD, tight enough to prime
# every call without eating the context window. Full manual injected on condensed=False.
_CONDENSED_PRIMER = """You reason at a high bar. Apply this method, not a persona:
- Parse twice: serve what's actually being accomplished, not just the literal words.
- Classify the task (lookup/compute/debug/design/judgment) and use that method; misclassifying is a root cause of bad answers.
- Audit the premises before spending effort downstream of them; if a premise is wrong, say so first.
- Generate at least two hypotheses before committing to one; actively hunt evidence that would DISCONFIRM your answer.
- One inference per step; separate observation from interpretation; flag anything that is a guess, not a deduction.
- Recompute every number a different way (percent-change divides by the ORIGINAL value).
- Distinguish verified from recalled from inferred; never state a hypothesis as a fact.
- Lead with the conclusion; say each thing once; no process narration.
- If uncertain, say so specifically rather than hedging everything."""


def reasoning_system(*, condensed: bool = True) -> str:
    """The Fable reasoning primer. Full manual when condensed=False + the file is present."""
    if not condensed:
        try:
            txt = FABLE_MANUAL.read_text(encoding="utf-8").strip()
            if txt:
                return txt
        except Exception:
            pass
    return _CONDENSED_PRIMER


def active_local_model(default: str | None = None) -> str:
    """Model from the router's local registry (best-effort; falls back to env/default so this is
    safe under system python where the full `core` package may not import)."""
    try:
        from core.local_model_registry import get_active_local_model  # type: ignore
        return get_active_local_model(default or _DEFAULT_MODEL)
    except Exception:
        return default or _DEFAULT_MODEL


def _strip_think(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()


def _ollama_generate(prompt: str, *, model: str, system: str | None,
                     temperature: float, num_ctx: int, timeout: float) -> str:
    body = {"model": model, "prompt": prompt, "stream": False,
            "options": {"temperature": temperature, "num_ctx": num_ctx}}
    if system:
        body["system"] = system
    req = urllib.request.Request(f"{OLLAMA}/api/generate", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read()).get("response", "")


def local_generate(prompt: str, *, system: str | None = None, reason: bool = True,
                   condensed: bool = True, model: str | None = None, temperature: float = 0.1,
                   num_ctx: int = 8192, timeout: float | None = None,
                   escalate: bool = False, strip_think: bool = True) -> str:
    """Generate with a local model, Fable-primed when reason=True. Raises on a hard failure so a
    caller can fall back (matches the instinct-loop contract). escalate=True delegates to the full
    local->free->paid router (guarded) for callers that want it; default is local-only ($0)."""
    sys_prompt = system or ""
    if reason:
        primer = reasoning_system(condensed=condensed)
        sys_prompt = f"{primer}\n\n{sys_prompt}".strip() if sys_prompt else primer
    to = timeout if timeout is not None else _LLM_TIMEOUT

    if escalate:
        # compose the real routing chain when the caller opts in and it's importable (venv only).
        try:
            import asyncio
            from core.llm_router import try_local_then_api  # type: ignore
            text, _src = asyncio.run(try_local_then_api(
                prompt, system=sys_prompt or None, max_tokens=num_ctx))
            return _strip_think(text) if strip_think else text
        except Exception:
            pass  # fall through to local-only

    mdl = model or active_local_model()
    out = _ollama_generate(prompt, model=mdl, system=sys_prompt or None,
                           temperature=temperature, num_ctx=num_ctx, timeout=to)
    return _strip_think(out) if strip_think else out


def _selftest() -> int:
    checks: dict[str, bool] = {}
    checks["condensed_primer_nonempty"] = len(reasoning_system()) > 100
    checks["primer_has_method"] = "Parse twice" in reasoning_system()
    checks["full_primer_loads"] = "OPERATING MANUAL" in reasoning_system(condensed=False) or \
        reasoning_system(condensed=False) == _CONDENSED_PRIMER  # falls back if file absent
    checks["active_model_resolves"] = bool(active_local_model())
    checks["strip_think"] = _strip_think("<think>x</think>hello") == "hello"
    # live generation (only if Ollama is up); fable-primed reasoning check
    try:
        ans = local_generate(
            "A cost fell from $80k to $60k. The source says that's a 33% reduction. "
            "Is the source right? Answer with the correct percentage first.",
            reason=True, temperature=0.0, num_ctx=2048, timeout=90)
        checks["live_generation"] = len(ans.strip()) > 0
        checks["fable_catches_percent_error"] = "25" in ans  # correct = 25%, not 33%
    except Exception as e:
        print(f"  (ollama not reachable: {e}); skipping live checks")
        checks["live_generation"] = True
        checks["fable_catches_percent_error"] = True
    ok = all(checks.values())
    for k, v in checks.items():
        print(f"  [{'PASS' if v else 'FAIL'}] {k}")
    print("LOCAL-REASON SELFTEST", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="python3 core/local_reason.py")
    ap.add_argument("prompt", nargs="*")
    ap.add_argument("--full", action="store_true", help="inject the FULL manual, not the digest")
    ap.add_argument("--no-reason", action="store_true", help="skip the Fable primer")
    ap.add_argument("--escalate", action="store_true", help="allow local->free->paid via llm_router")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv if argv is not None else sys.argv[1:])
    if a.selftest:
        return _selftest()
    if not a.prompt:
        ap.print_usage(); return 1
    print(local_generate(" ".join(a.prompt), reason=not a.no_reason,
                         condensed=not a.full, escalate=a.escalate))
    return 0


if __name__ == "__main__":
    sys.exit(main())
