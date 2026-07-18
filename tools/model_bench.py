#!/usr/bin/env python3
"""model_bench — measure the real tradeoffs of each installed local model on THIS box.

For every Ollama model (or a chosen subset) it measures, one at a time (loading, timing,
then unloading via keep_alive=0 so the 24GB box never holds two at once):
  - cold-load seconds   (how long the first token takes = the lag you feel when it wakes)
  - resident RAM (GB)    (what it costs to keep warm — the RAM-pressure driver)
  - tokens/sec           (throughput once warm)
  - a tiny quality proxy (did it produce a valid, on-task answer?)

Use it to decide what's worth keeping warm vs load-on-demand vs not worth its RAM.

    python3 tools/model_bench.py                 # all installed models
    python3 tools/model_bench.py qwen3:14b qwen2.5-coder:14b   # a subset
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request

OLLAMA = "http://127.0.0.1:11434"
PROMPT = "Write a Python function that returns the nth Fibonacci number iteratively. Code only."
KEYWORD = "def"  # crude on-task check


def _get(path: str) -> dict:
    with urllib.request.urlopen(f"{OLLAMA}{path}", timeout=15) as r:
        return json.loads(r.read())


def _installed() -> list[str]:
    return [m["name"] for m in _get("/api/tags").get("models", [])]


def _resident_gb(model: str) -> float:
    try:
        for m in _get("/api/ps").get("models", []):
            if m.get("name") == model:
                return round(m.get("size", 0) / 1e9, 1)
    except Exception:
        pass
    return 0.0


def _unload(model: str) -> None:
    """Ask Ollama to drop the model now (keep_alive=0) so RAM is freed before the next."""
    try:
        body = json.dumps({"model": model, "keep_alive": 0, "prompt": "", "stream": False}).encode()
        urllib.request.urlopen(urllib.request.Request(f"{OLLAMA}/api/generate", data=body,
                               headers={"Content-Type": "application/json"}), timeout=30).read()
    except Exception:
        pass


def bench(model: str) -> dict:
    _unload(model)  # ensure a true COLD load
    time.sleep(2)
    body = json.dumps({
        "model": model, "prompt": PROMPT, "stream": False,
        "keep_alive": "30s",  # stay up just long enough to read resident size
        "options": {"num_predict": 220, "temperature": 0.1},
    }).encode()
    t0 = time.time()
    try:
        with urllib.request.urlopen(urllib.request.Request(f"{OLLAMA}/api/generate", data=body,
                                    headers={"Content-Type": "application/json"}), timeout=600) as r:
            d = json.loads(r.read())
    except Exception as e:
        return {"model": model, "error": str(e)[:80]}
    wall = time.time() - t0
    load_ns = d.get("load_duration", 0)          # cold-load nanoseconds
    eval_ct = d.get("eval_count", 0)              # tokens generated
    eval_ns = d.get("eval_duration", 1) or 1      # generation nanoseconds
    resp = d.get("response", "") or ""
    result = {
        "model": model,
        "cold_load_s": round(load_ns / 1e9, 1),
        "resident_gb": _resident_gb(model),
        "tokens_per_s": round(eval_ct / (eval_ns / 1e9), 1) if eval_ct else 0.0,
        "wall_s": round(wall, 1),
        "on_task": KEYWORD in resp.lower(),
        "out_tokens": eval_ct,
    }
    _unload(model)
    time.sleep(1)
    return result


def main() -> int:
    models = sys.argv[1:] or _installed()
    print(f"benchmarking {len(models)} models (one at a time, unloading between)…\n")
    rows = []
    for m in models:
        print(f"  … {m}", flush=True)
        rows.append(bench(m))
    ok = [r for r in rows if "error" not in r]
    ok.sort(key=lambda r: -r["tokens_per_s"])
    print("\nmodel                                    load_s  RAM_GB  tok/s   wall_s  on-task")
    print("-" * 82)
    for r in ok:
        print(f"{r['model']:<40} {r['cold_load_s']:>6} {r['resident_gb']:>7} {r['tokens_per_s']:>7} "
              f"{r['wall_s']:>7}  {'yes' if r['on_task'] else 'NO'}")
    for r in rows:
        if "error" in r:
            print(f"{r['model']:<40}  ERROR: {r['error']}")
    try:
        from pathlib import Path
        out = Path(__file__).resolve().parent.parent / "data" / "runtime" / "model_bench.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"ts": time.time(), "results": rows}, indent=2))
        print(f"\nsaved -> {out}")
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
