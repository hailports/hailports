#!/usr/bin/env python3
"""grunt — offload bulk/mechanical codegen to the local stack ($0).

Any Claude Code / terminal session can delegate the token-heavy grunt work here —
boilerplate, test scaffolds, mass renames, data transforms, regexes, docstrings,
config blocks — instead of spending flat-rate premium capacity generating it. Keeps
the premium model as planner/reviewer; the mini does the typing. Routes to the local
coder (qwen2.5-coder:14b) then the free cloud pool; never touches paid APIs.

    python3 tools/grunt.py "write a pytest fixture that spins up a temp sqlite db"
    python3 tools/grunt.py - < spec.txt          # read the task from stdin
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

MODEL = "qwen2.5-coder:14b"
SYSTEM = "You are a senior engineer. Output exactly what is asked — code or text, no preamble, no explanation unless requested."


def _local(prompt: str) -> str | None:
    try:
        from core.local_client import generate
        return asyncio.run(generate(prompt, model=MODEL, system=SYSTEM,
                                    max_tokens=2048, temperature=0.1))
    except Exception:
        return None


def _free(prompt: str) -> str | None:
    try:
        from core.free_llm_pool import try_free_providers
        txt, _ = asyncio.run(try_free_providers(prompt, system=SYSTEM, max_tokens=2048,
                                                explicit=True, tier="strong"))
        return txt
    except Exception:
        return None


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print('usage: grunt.py "<task>"  |  grunt.py - < spec.txt', file=sys.stderr)
        return 2
    prompt = (sys.stdin.read() if args == ["-"] else " ".join(args)).strip()
    if not prompt:
        return 2
    out = _local(prompt) or _free(prompt)   # both $0; free pool is the offline/local-down fallback
    if not out or not out.strip():
        print("grunt: local coder + free pool both unavailable", file=sys.stderr)
        return 1
    print(out.strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
