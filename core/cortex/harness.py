#!/usr/bin/env python3
"""cortex.harness — supervised agent runner (verify + retry + fan-out).

Today every autonomous call in the stack is a SINGLE-SHOT `scripts/agent_run.sh`
("claude -p ..."): bad output is never caught. This module wraps that seam in a
self-critiquing loop — run, verify, and on rejection re-run with the verifier's
reason folded back into the prompt — and exposes a small concurrent fan-out so the
stack has a stack-runtime analogue of a dynamic-workflow.

LEAF MODULE (hard constraint): imports NOTHING from the rest of core.cortex — the
spine imports harness, never the reverse. Pure stdlib + a subprocess shell-out to
scripts/agent_run.sh (which already owns provider routing + AGENT_RUN_DAILY_CAP;
we never re-implement either).

Everything is fail-soft: any import/exception yields a safe default; run_supervised
NEVER raises. Master kill: data/hustle/CORTEX_OFF present => do nothing, spawn nothing.

    python3 -m core.cortex.harness --selftest   # deterministic verify/retry proof, no spawn
    python3 -m core.cortex.harness run "<prompt>" [max_turns] [cwd] [system]  # VERIFY_MODE env
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(os.environ.get("CLAUDE_STACK_DIR", Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(ROOT))

AGENT_RUN = ROOT / "scripts" / "agent_run.sh"
HUSTLE = ROOT / "data" / "hustle"
CORTEX_OFF = HUSTLE / "CORTEX_OFF"           # master kill switch (mirrors cortex.gates)
PROVIDER_LOG = HUSTLE / "logs" / "agent_run.log"

# Bounds (respect the shared Max-20x pool + never hang / never fan-out unbounded).
_RETRY_CEILING = 5                            # hard cap on retries regardless of caller
FANOUT_CONCURRENCY = 3                         # small thread-pool cap
FANOUT_CEILING = max(1, int(os.environ.get("HARNESS_FANOUT_CEILING", "12") or "12"))
_HARNESS_TIMEOUT = int(os.environ.get("HARNESS_TIMEOUT", "6000") or "6000")  # subprocess ceiling


def _cortex_off() -> bool:
    try:
        return CORTEX_OFF.exists()
    except Exception:
        return False


def _provider_log_tail(n: int = 8) -> str:
    try:
        lines = PROVIDER_LOG.read_text(errors="ignore").splitlines()
        return "\n".join(lines[-n:])
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Verifiers. A verifier takes the output string and returns
# {ok: bool, score: float, reason: str}. DEFAULT_VERIFIERS is the named registry
# the shell wrapper (VERIFY_MODE) and callers select from.
# ---------------------------------------------------------------------------
def _verify_nonempty(output: str) -> dict:
    ok = bool((output or "").strip())
    return {"ok": ok, "score": 1.0 if ok else 0.0,
            "reason": "" if ok else "output was empty"}


_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def _extract_json(text: str):
    """Best-effort: parse the first JSON object/array in text (bare or fenced)."""
    if not text:
        return None
    for candidate in (text, *(m.group(1) for m in _JSON_FENCE.finditer(text))):
        candidate = candidate.strip()
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except Exception:
            pass
        m = re.search(r"(\{.*\}|\[.*\])", candidate, re.S)
        if m:
            try:
                return json.loads(m.group(1))
            except Exception:
                continue
    return None


def _verify_json(output: str) -> dict:
    obj = _extract_json(output)
    if obj is None:
        return {"ok": False, "score": 0.0,
                "reason": "output was not parseable as JSON"}
    return {"ok": True, "score": 1.0, "reason": ""}


def _schema_verifier(schema_hint):
    """Build a verifier that checks the output parses as JSON AND its top-level shape
    matches schema_hint (same container type; if a dict, all schema_hint keys present)."""
    def _v(output: str) -> dict:
        obj = _extract_json(output)
        if obj is None:
            return {"ok": False, "score": 0.0, "reason": "output was not parseable as JSON"}
        if isinstance(schema_hint, dict):
            if not isinstance(obj, dict):
                return {"ok": False, "score": 0.2,
                        "reason": f"expected a JSON object, got {type(obj).__name__}"}
            missing = [k for k in schema_hint if k not in obj]
            if missing:
                return {"ok": False, "score": 0.5,
                        "reason": f"JSON missing required top-level keys: {missing}"}
        elif isinstance(schema_hint, list):
            if not isinstance(obj, list):
                return {"ok": False, "score": 0.2,
                        "reason": f"expected a JSON array, got {type(obj).__name__}"}
        return {"ok": True, "score": 1.0, "reason": ""}
    return _v


DEFAULT_VERIFIERS = {
    "nonempty": _verify_nonempty,
    "json": _verify_json,
}


def _norm_verdict(v) -> dict:
    """Coerce whatever a verifier returned into {ok, score, reason}. Fail-soft."""
    if isinstance(v, dict):
        ok = bool(v.get("ok"))
        try:
            score = float(v.get("score", 1.0 if ok else 0.0))
        except (TypeError, ValueError):
            score = 1.0 if ok else 0.0
        return {"ok": ok, "score": score, "reason": str(v.get("reason", ""))}
    ok = bool(v)
    return {"ok": ok, "score": 1.0 if ok else 0.0, "reason": ""}


# ---------------------------------------------------------------------------
# The runner seam. The default shells out to scripts/agent_run.sh; the selftest
# injects a fake runner via `_runner=` to prove the loop WITHOUT spawning claude.
# A runner takes (prompt, *, system, max_turns, cwd) and returns
# {returncode:int, output:str, log_tail:str}.
# ---------------------------------------------------------------------------
def _default_runner(prompt: str, *, system, max_turns: int, cwd) -> dict:
    if not AGENT_RUN.exists():
        return {"returncode": 127, "output": "", "log_tail": "agent_run.sh missing"}
    args = ["/bin/bash", str(AGENT_RUN), prompt, str(max_turns), str(cwd or ROOT)]
    if system:
        args.append(system)
    try:
        proc = subprocess.run(args, capture_output=True, text=True,
                              timeout=_HARNESS_TIMEOUT, env=os.environ.copy())
        return {"returncode": proc.returncode, "output": proc.stdout or "",
                "stderr": proc.stderr or "", "log_tail": _provider_log_tail()}
    except subprocess.TimeoutExpired:
        return {"returncode": 124, "output": "", "log_tail": _provider_log_tail()}
    except Exception as e:  # never let a spawn failure raise into a caller
        return {"returncode": 1, "output": "", "log_tail": f"spawn error: {e}"}


# ---------------------------------------------------------------------------
def run_supervised(prompt, *, system=None, max_turns=30, cwd=None, verify=None,
                   max_retries=2, min_quality=0.0, _runner=None) -> dict:
    """Run agent_run.sh under supervision. Verify+retry with the verifier's reason
    folded back into the prompt. NEVER raises. Returns:
        {ok, output, attempts, scores:[...], provider_log_tail, note?, reason?}

    `verify` (optional): callable(output)->{ok,score,reason}. On ok=False (or a score
    below `min_quality`) and retries remaining, the prompt is re-issued with
    "previous attempt rejected because: <reason>" appended. `_runner` is a private
    injection seam for testing (defaults to the real subprocess call)."""
    base = {"ok": False, "output": "", "attempts": 0, "scores": [], "provider_log_tail": ""}
    try:
        if _cortex_off():
            base["note"] = "CORTEX_OFF"
            return base
        runner = _runner or _default_runner
        try:
            retries = max(0, min(int(max_retries), _RETRY_CEILING))
        except (TypeError, ValueError):
            retries = 0
        try:
            floor = float(min_quality)
        except (TypeError, ValueError):
            floor = 0.0

        scores, attempts = [], 0
        output, log_tail, returncode = "", "", None
        last_reason = ""
        attempt_prompt = str(prompt)

        for i in range(retries + 1):
            attempts += 1
            res = runner(attempt_prompt, system=system, max_turns=max_turns, cwd=cwd)
            if not isinstance(res, dict):  # tolerate a bare-string runner
                res = {"returncode": 0, "output": str(res), "log_tail": ""}
            output = res.get("output", "") or ""
            log_tail = res.get("log_tail", "") or log_tail
            returncode = res.get("returncode", 0)

            if verify is None:
                ok = returncode == 0 and bool(output.strip())
                return {"ok": ok, "output": output, "attempts": attempts,
                        "scores": scores, "provider_log_tail": log_tail,
                        "returncode": returncode}

            try:
                verdict = _norm_verdict(verify(output))
            except Exception as e:
                verdict = {"ok": False, "score": 0.0, "reason": f"verifier raised: {e}"}
            scores.append(verdict["score"])
            passed = verdict["ok"] and verdict["score"] >= floor
            if passed:
                return {"ok": True, "output": output, "attempts": attempts,
                        "scores": scores, "provider_log_tail": log_tail,
                        "returncode": returncode}
            last_reason = verdict["reason"] or (
                f"quality {verdict['score']} below required {floor}")
            if i < retries:
                attempt_prompt = (
                    f"{prompt}\n\nprevious attempt rejected because: {last_reason}\n"
                    "produce a corrected result that resolves that specific problem.")

        return {"ok": False, "output": output, "attempts": attempts, "scores": scores,
                "provider_log_tail": log_tail, "returncode": returncode,
                "reason": last_reason}
    except Exception as e:  # absolute backstop — the contract is NEVER raises
        base["note"] = f"harness error: {e}"
        return base


def run_json(prompt, *, schema_hint, **kw):
    """run_supervised with a built-in verify that the output parses as JSON whose
    top-level shape matches schema_hint. Returns (obj|None, meta) where meta is the
    run_supervised result dict."""
    kw.pop("verify", None)
    meta = run_supervised(prompt, verify=_schema_verifier(schema_hint), **kw)
    obj = _extract_json(meta.get("output", "")) if meta.get("ok") else None
    return obj, meta


def fanout(subtasks, *, system=None, max_retries=1, _runner=None) -> list:
    """Run several agent_run.sh calls concurrently (thread-pool, cap FANOUT_CONCURRENCY)
    and return each result dict, in input order. The stack-runtime analogue of a
    dynamic-workflow fan-out. Concurrency is capped and the number of subtasks per
    invocation is bounded by FANOUT_CEILING; overflow tasks return a skipped result."""
    try:
        tasks = [str(t) for t in (subtasks or [])]
    except Exception:
        return []
    if not tasks:
        return []
    if _cortex_off():
        return [{"ok": False, "output": "", "attempts": 0, "scores": [],
                 "provider_log_tail": "", "note": "CORTEX_OFF"} for _ in tasks]

    accepted, overflow = tasks[:FANOUT_CEILING], tasks[FANOUT_CEILING:]
    results: list = [None] * len(accepted)

    def _one(idx_task):
        idx, task = idx_task
        return idx, run_supervised(task, system=system, max_retries=max_retries,
                                   verify=DEFAULT_VERIFIERS["nonempty"], _runner=_runner)

    try:
        workers = max(1, min(FANOUT_CONCURRENCY, len(accepted)))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for idx, res in ex.map(_one, list(enumerate(accepted))):
                results[idx] = res
    except Exception as e:  # fail-soft: partial results beat an exception
        for i, r in enumerate(results):
            if r is None:
                results[i] = {"ok": False, "output": "", "attempts": 0, "scores": [],
                              "provider_log_tail": "", "note": f"fanout error: {e}"}

    for task in overflow:
        results.append({"ok": False, "output": "", "attempts": 0, "scores": [],
                        "provider_log_tail": "", "note": "fanout_ceiling",
                        "subtask": task[:80]})
    return results


# ---------------------------------------------------------------------------
# CLI: `run` (for scripts/agent_run_supervised.sh) + `--selftest`.
# ---------------------------------------------------------------------------
def _cli_run(argv) -> int:
    prompt = argv[0] if argv else ""
    if prompt in ("", "-"):
        prompt = sys.stdin.read()
    if not prompt.strip():
        print("agent_run_supervised: prompt required", file=sys.stderr)
        return 2
    max_turns = int(argv[1]) if len(argv) > 1 and str(argv[1]).isdigit() else 30
    cwd = argv[2] if len(argv) > 2 and argv[2] else None
    system = argv[3] if len(argv) > 3 and argv[3] else None
    mode = os.environ.get("VERIFY_MODE", "nonempty").strip().lower()
    verify = DEFAULT_VERIFIERS.get(mode) if mode not in ("", "none", "off") else None
    try:
        retries = int(os.environ.get("HARNESS_MAX_RETRIES", "2"))
    except ValueError:
        retries = 2
    try:
        floor = float(os.environ.get("HARNESS_MIN_QUALITY", "0.0"))
    except ValueError:
        floor = 0.0
    res = run_supervised(prompt, system=system, max_turns=max_turns, cwd=cwd,
                         verify=verify, max_retries=retries, min_quality=floor)
    sys.stdout.write(res.get("output", ""))
    if res.get("output") and not res["output"].endswith("\n"):
        sys.stdout.write("\n")
    if res.get("note") == "CORTEX_OFF":
        print("[harness] CORTEX_OFF — nothing spawned", file=sys.stderr)
    print(f"[harness] ok={res.get('ok')} attempts={res.get('attempts')} "
          f"scores={res.get('scores')} verify={mode}", file=sys.stderr)
    return 0 if res.get("ok") else 1


def _selftest() -> int:
    fails = []

    # 1. verify+retry: first attempt returns junk, the folded-back reason flips it to
    #    valid JSON on the SECOND attempt. Deterministic, no spawn.
    calls = {"n": 0}

    def json_runner(prompt, *, system, max_turns, cwd):
        calls["n"] += 1
        good = "previous attempt rejected because" in prompt
        return {"returncode": 0, "log_tail": "stub",
                "output": '{"ok": true, "value": 42}' if good else "totally not json"}

    r = run_supervised("give me json", verify=DEFAULT_VERIFIERS["json"],
                       max_retries=2, _runner=json_runner)
    if not (r["ok"] and r["attempts"] == 2 and calls["n"] == 2):
        fails.append(f"verify+retry loop wrong: {r} calls={calls['n']}")
    if r["scores"] != [0.0, 1.0]:
        fails.append(f"scores not accumulated per attempt: {r['scores']}")

    # 2. retry exhaustion: nonempty verifier on an always-empty runner => ok False,
    #    attempts == retries+1, never raises.
    def empty_runner(prompt, *, system, max_turns, cwd):
        return {"returncode": 0, "output": "", "log_tail": ""}

    r2 = run_supervised("x", verify=DEFAULT_VERIFIERS["nonempty"], max_retries=1,
                        _runner=empty_runner)
    if r2["ok"] or r2["attempts"] != 2:
        fails.append(f"exhaustion path wrong: {r2}")

    # 3. no-verify path: ok tracks returncode + nonempty output; runs exactly once.
    once = {"n": 0}

    def plain_runner(prompt, *, system, max_turns, cwd):
        once["n"] += 1
        return {"returncode": 0, "output": "hello", "log_tail": ""}

    r3 = run_supervised("hi", _runner=plain_runner)
    if not (r3["ok"] and r3["attempts"] == 1 and once["n"] == 1):
        fails.append(f"no-verify path wrong: {r3}")

    # 4. min_quality floor: a verifier that passes ok=True but low score is rejected
    #    until the reason-fold lifts the score.
    def scored_runner(prompt, *, system, max_turns, cwd):
        hi = "previous attempt rejected because" in prompt
        return {"returncode": 0, "output": "HI" if hi else "lo", "log_tail": ""}

    def scoring_verify(out):
        return {"ok": True, "score": 0.9 if out == "HI" else 0.3, "reason": "raise quality"}

    r4 = run_supervised("q", verify=scoring_verify, max_retries=2, min_quality=0.8,
                        _runner=scored_runner)
    if not (r4["ok"] and r4["attempts"] == 2 and r4["scores"] == [0.3, 0.9]):
        fails.append(f"min_quality floor wrong: {r4}")

    # 5. run_json returns the parsed object.
    obj, meta = run_json("give json", schema_hint={"ok": True, "value": 0},
                         max_retries=2, _runner=json_runner)
    if obj != {"ok": True, "value": 42} or not meta["ok"]:
        fails.append(f"run_json parse wrong: {obj} / {meta}")

    # 6. run_json schema mismatch (wrong container) => obj None, ok False.
    def arr_runner(prompt, *, system, max_turns, cwd):
        return {"returncode": 0, "output": "[1,2,3]", "log_tail": ""}

    obj2, meta2 = run_json("give obj", schema_hint={"need": 1}, max_retries=0,
                           _runner=arr_runner)
    if obj2 is not None or meta2["ok"]:
        fails.append(f"run_json schema mismatch not caught: {obj2} / {meta2}")

    # 7. fanout: concurrent, in-order, cap respected, all succeed on a nonempty runner.
    def echo_runner(prompt, *, system, max_turns, cwd):
        return {"returncode": 0, "output": f"answer::{prompt[:3]}", "log_tail": ""}

    fr = fanout(["aaa", "bbb", "ccc", "ddd"], _runner=echo_runner)
    if len(fr) != 4 or not all(x["ok"] for x in fr):
        fails.append(f"fanout results wrong: {fr}")
    if fr[0]["output"] != "answer::aaa" or fr[3]["output"] != "answer::ddd":
        fails.append(f"fanout order not preserved: {[x['output'] for x in fr]}")

    # 8. fanout ceiling: overflow tasks return a skipped marker, never spawn.
    global FANOUT_CEILING
    saved_ceiling = FANOUT_CEILING
    FANOUT_CEILING = 2
    try:
        fr2 = fanout(["a", "b", "c"], _runner=echo_runner)
    finally:
        FANOUT_CEILING = saved_ceiling
    if len(fr2) != 3 or fr2[2].get("note") != "fanout_ceiling" or not fr2[0]["ok"]:
        fails.append(f"fanout ceiling not enforced: {fr2}")

    # 9. kill switch: with CORTEX_OFF present, spawn NOTHING and report the note.
    global CORTEX_OFF
    saved_off = CORTEX_OFF
    import tempfile
    tf = tempfile.NamedTemporaryFile(prefix="CORTEX_OFF_", delete=False)
    tf.close()
    CORTEX_OFF = Path(tf.name)
    spawned = {"n": 0}

    def tripwire_runner(prompt, *, system, max_turns, cwd):
        spawned["n"] += 1
        return {"returncode": 0, "output": "should-not-run", "log_tail": ""}

    try:
        rk = run_supervised("blocked", verify=DEFAULT_VERIFIERS["nonempty"],
                            _runner=tripwire_runner)
        fk = fanout(["a", "b"], _runner=tripwire_runner)
        if rk.get("note") != "CORTEX_OFF" or rk["ok"] or rk["attempts"] != 0:
            fails.append(f"kill switch didn't short-circuit run_supervised: {rk}")
        if spawned["n"] != 0:
            fails.append(f"kill switch spawned a runner {spawned['n']}x")
        if not (len(fk) == 2 and all(x.get("note") == "CORTEX_OFF" for x in fk)):
            fails.append(f"kill switch didn't short-circuit fanout: {fk}")
    finally:
        CORTEX_OFF = saved_off
        try:
            os.unlink(tf.name)
        except Exception:
            pass

    # 10. a raising verifier is caught (never propagates), retries, then fails soft.
    def boom_verify(out):
        raise ValueError("kaboom")

    r10 = run_supervised("q", verify=boom_verify, max_retries=1, _runner=plain_runner)
    if r10["ok"] or "verifier raised" not in (r10.get("reason") or ""):
        fails.append(f"raising verifier not contained: {r10}")

    if fails:
        print("HARNESS SELFTEST FAILED:")
        for f in fails:
            print("  -", f)
        return 1
    print("HARNESS SELFTEST OK — verify+retry, min_quality floor, run_json, fanout "
          "cap/ceiling/order, CORTEX_OFF kill, raising-verifier containment all pass")
    return 0


def main(argv) -> int:
    if not argv or argv[0] in ("--selftest", "-t", "selftest"):
        return _selftest()
    if argv[0] == "run":
        return _cli_run(argv[1:])
    print(f"usage: python3 -m core.cortex.harness [--selftest | run <prompt> "
          f"[max_turns] [cwd] [system]]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
