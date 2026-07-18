#!/usr/bin/env python3
"""jit_healer.py — EVENT-DRIVEN self-heal for external launchd jobs. Fired by launchd
WatchPaths the moment any claude-stack job writes to its error log. It does a cheap
DETERMINISTIC check for an actually-failed job; only THEN does it spend a premium LLM
call to fix it. No real failure → no call. True JIT: failure triggers healing, nothing
is scheduled. Premium spend is guarded by the same budget breaker the hive uses.

Loaded by com.claude-stack.jit-healer (WatchPaths on ~/Library/Logs/claude-stack).

NOTE: this is a real module lifted from a running stack, published as a reference
for the *pattern* (deterministic-gate → local-first → verify → bounded escalation).
It depends on four shared-stack interfaces documented in README.md — forage() (the
local-first LLM router), verify_heal() (post-heal liveness re-check), record_heal()
(the heal ledger), and alert_gateway.route() (dedup/digest notifications). Swap in
your own equivalents; the escalation/cooldown/verify control flow is the point.
"""
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.environ.get("STACK_DIR", os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from core.bee import forage, log, _budget_tripped  # noqa: E402
from core.heal_verify import verify_heal  # noqa: E402
from core.health_ledger import record_heal  # noqa: E402

# Heal is LOCAL-FIRST: on-box Ollama ($0, works even when the OpenRouter budget breaker
# is tripped) is ALWAYS tried before any paid call. Paid OpenRouter is a LAST resort,
# reached only if local is unreachable — and it's still gated by the paid-LLM kill switch
# + circuit breaker + monthly/daily caps in forage(). This ordering is the fix for the
# OpenRouter-bleed backfire: premium spend can never be the first (or a silent) reach.
LOCAL_BACKEND = "local"
LOCAL_MODEL = os.environ.get("JIT_HEAL_LOCAL_MODEL", "qwen2.5-coder:14b")
PREMIUM_BACKEND = "openrouter"
PREMIUM_MODEL = "deepseek/deepseek-v4-flash"  # PAID fallback ONLY — behind every breaker
STATE = os.path.expanduser("~/.jit-healer.state.json")
MIN_INTERVAL = 45            # global de-bounce WatchPaths storms (seconds)
PER_JOB_COOLDOWN = 600       # don't re-heal same job within 10 min
MAX_CONSEC_FAILS = 3         # after 3 consecutive failed heals, give up + log — stop burning tokens
# Benign nonzero exits we never heal: stale nightly + KeepAlive daemons SIGTERM'd on restart.
IGNORE_SUFFIX = ("nightly-update", "deep-canary")  # deep-canary exits 2 on check failure by design
IGNORE_CODES = ()            # -15 handled by pid check below (alive daemon = not a failure)


def _red_jobs():
    out = subprocess.run("launchctl list | grep claude-stack", shell=True,
                         capture_output=True, text=True).stdout
    red = []
    for ln in out.splitlines():
        p = ln.split()
        if len(p) < 3:
            continue
        pid, code, label = p[0], p[1], p[2]
        if pid != "-":
            continue  # has a live pid → running, not failed
        try:
            c = int(code)
        except ValueError:
            continue
        if c == 0 or label.endswith(IGNORE_SUFFIX) or c in IGNORE_CODES:
            continue
        red.append((label, c))
    return red


def _load_state():
    try:
        return json.load(open(STATE))
    except Exception:
        return {}


def _save_state(s):
    try:
        json.dump(s, open(STATE, "w"))
    except Exception:
        pass


def _debounced(state):
    if time.time() - state.get("last", 0) < MIN_INTERVAL:
        return True
    state["last"] = time.time()
    return False


def _err_tail(job, n=2000):
    """Best-effort err-log tail. Tries the conventional Library log first, then the
    in-repo logs.internal/<name>.stderr.log path some jobs (e.g. nocturnal-worker)
    use, so escalations/diagnostics aren't empty."""
    short = job.split(".")[-1]
    candidates = [
        os.path.expanduser(f"~/Library/Logs/claude-stack/{short}.err.log"),
        os.path.expanduser(f"~/claude-stack/logs.internal/{short.replace('-', '_')}.stderr.log"),
    ]
    for p in candidates:
        try:
            with open(p) as f:
                t = f.read()[-n:]
            if t.strip():
                return t
        except Exception:
            continue
    return ""


def _escalate_chronic(state, job, fails):
    """Bounded escalation: when a job exhausts its auto-heal budget, tell the operator ONCE
    instead of looping silently. The flag resets in _job_record when the job heals,
    so each chronic episode escalates exactly once.

    Routed through the central alert gateway as WARN so a single chronic job dedups
    by issue_key and BATCHES with any other chronic jobs into one digest — this is
    what kills the per-job escalation flood when a single shared dependency (e.g. a
    core import breaking) takes many jobs red at the same time. Falls back to the
    direct alert if the gateway can't be imported, so an escalation is never lost."""
    escalated = state.setdefault("escalated", {})
    if escalated.get(job):
        return
    short = job.split(".")[-1]
    tail = _err_tail(job)[-600:]
    subject = f"jit-healer escalation: {short} chronic"
    body = (f"{job} failed {fails}x consecutive auto-heals (cap={MAX_CONSEC_FAILS}). "
            f"Auto-heal stopped to avoid thrash — needs a manual fix.\n\nlast err log:\n{tail}")
    sent = False
    try:
        from core import alert_gateway
        alert_gateway.route("warn", "jit_healer", subject, body,
                            issue_key=f"jit-chronic:{job}")
        sent = True
        log(f"[jit-healer] ESCALATED {job} via alert_gateway (digest)")
    except Exception as e:
        log(f"[jit-healer] gateway unavailable, direct escalate {job}: {e}")
        try:
            from core.alert import alert_alex
            alert_alex(subject, body)
            sent = True
        except Exception as e2:
            log(f"[jit-healer] escalation failed {job}: {e2}")
    if sent:
        escalated[job] = time.time()


def _job_allowed(state, job):
    """Per-job cooldown + consecutive fail cap."""
    jobs = state.setdefault("jobs", {})
    j = jobs.setdefault(job, {"last_heal": 0, "consec_fails": 0})
    if j["consec_fails"] >= MAX_CONSEC_FAILS:
        log(f"[jit-healer] CHRONIC {job} ({j['consec_fails']} consec fails) — skipping, needs manual fix")
        _escalate_chronic(state, job, j["consec_fails"])
        return False
    if time.time() - j["last_heal"] < PER_JOB_COOLDOWN:
        return False
    return True


def _job_record(state, job, healed_ok):
    j = state.setdefault("jobs", {}).setdefault(job, {"last_heal": 0, "consec_fails": 0})
    j["last_heal"] = time.time()
    j["consec_fails"] = 0 if healed_ok else j["consec_fails"] + 1
    if healed_ok:
        state.setdefault("escalated", {}).pop(job, None)


def main():
    red = _red_jobs()
    if not red:
        return  # NO failure → NO LLM call (true JIT, $0)
    state = _load_state()
    if _debounced(state):
        _save_state(state)
        return
    # NOTE: do NOT bail on _budget_tripped() here — the heal is LOCAL-first ($0) and must
    # run even when the OpenRouter breaker is tripped. Paid fallback stays fully gated inside
    # forage() (kill switch + budget breaker), so a tripped budget only blocks the paid reach.
    if _budget_tripped():
        log(f"[jit-healer] {len(red)} red; OpenRouter breaker tripped → local-only heal (paid path will SKIP)")
    # Pick worst job that isn't on cooldown / chronic
    candidates = [(j, c) for j, c in sorted(red, key=lambda x: abs(x[1]), reverse=True)
                  if _job_allowed(state, j)]
    if not candidates:
        _save_state(state)
        return
    job, code = candidates[0]
    short = job.split(".")[-1]

    # A Python SyntaxError/IndentationError can NEVER be fixed by kickstarting the
    # job — burning the 3-heal budget on it is exactly what produced the
    # nocturnal-worker flood (3 wasted premium heals, then a per-job page). Detect
    # it up front: escalate ONCE (digested) with the real traceback, no token burn.
    tail = _err_tail(job)
    if ("SyntaxError" in tail) or ("IndentationError" in tail):
        log(f"[jit-healer] {job} has a Python SYNTAX error — kickstart can't fix it; "
            f"escalating once (digest), not burning heals")
        j = state.setdefault("jobs", {}).setdefault(job, {"last_heal": 0, "consec_fails": 0})
        j["consec_fails"] = MAX_CONSEC_FAILS
        j["last_heal"] = time.time()
        _escalate_chronic(state, job, j["consec_fails"])
        _save_state(state)
        return

    heal_goal = (
        f"The launchd job {job} just FAILED with exit={code}. Read its error log at "
        f"~/Library/Logs/claude-stack/{short}.err.log, diagnose the single root cause, fix it if "
        f"REVERSIBLE (repoint a path, fix the script, restart), then verify by kickstarting the job. "
        f"RESULT: what you fixed.")
    log(f"[jit-healer] FAILURE {job} exit={code} → LOCAL-first heal ({LOCAL_MODEL})")
    try:
        # 1) Local Ollama first — $0, no paid gating. Only if it's unreachable/errored do
        #    we fall to paid OpenRouter (which itself SKIPs when the budget breaker is tripped).
        r = forage("jit-external", heal_goal, backend=LOCAL_BACKEND, model=LOCAL_MODEL)
        if isinstance(r, str) and (r.startswith("ERR:") or r.startswith("SKIP")):
            log(f"[jit-healer] local heal unavailable ({r[:70]}) → paid OpenRouter last-resort (behind breaker)")
            r = forage("jit-external", heal_goal, backend=PREMIUM_BACKEND, model=PREMIUM_MODEL)
        log(f"[jit-healer] {r[:240]}")
        signature = f"launchd_exit_{code}"
        ok = verify_heal(job, settle_seconds=8)
        record_heal(service=job, signature=signature, action="premium_jit_heal", verified=ok,
                    detail=(r or "")[:300])
        _job_record(state, job, ok)
        if not ok:
            log(f"[jit-healer] VERIFY FAILED {job} signature={signature}")
    except Exception as e:
        log(f"[jit-healer] ERR {e}")
        _job_record(state, job, False)
        try:
            record_heal(service=job, signature=f"launchd_exit_{code}",
                        action="premium_jit_heal", verified=False, detail=str(e)[:300])
        except Exception:
            pass
    finally:
        _save_state(state)


if __name__ == "__main__":
    main()
