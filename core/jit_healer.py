#!/usr/bin/env python3
"""jit_healer.py — EVENT-DRIVEN self-heal for external launchd jobs. Fired by launchd
WatchPaths the moment any claude-stack job writes to its error log. It does a cheap
DETERMINISTIC check for an actually-failed job; only THEN does it spend a premium LLM
call to fix it. No real failure → no call. True JIT: failure triggers healing, nothing
is scheduled. Premium spend is guarded by the same budget breaker the hive uses.

Loaded by com.claude-stack.jit-healer (WatchPaths on ~/Library/Logs/claude-stack).
"""
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, "/home/user/claude-stack")
from core.bee import forage, log, _budget_tripped  # noqa: E402
from core.heal_verify import verify_heal  # noqa: E402
from core.health_ledger import record_heal  # noqa: E402

PREMIUM_BACKEND = "openrouter"
PREMIUM_MODEL = "deepseek/deepseek-v4-flash"  # was qwen-2.5-72b — cheap, fast, no policy
STATE = "/home/user/.jit-healer.state.json"
MIN_INTERVAL = 45            # global de-bounce WatchPaths storms (seconds)
PER_JOB_COOLDOWN = 600       # don't re-heal same job within 10 min
MAX_CONSEC_FAILS = 3         # after 3 consecutive failed heals, give up + log — stop burning tokens
# Benign nonzero exits we never heal: stale nightly + KeepAlive daemons SIGTERM'd on restart.
IGNORE_SUFFIX = ("nightly-update", "deep-canary")  # deep-canary exits 2 on check failure by design
IGNORE_CODES = ()            # -15 handled by pid check below (alive daemon = not a failure)

# RAIL: lanes we must NEVER hand to an LLM auto-restart. The screens/remote-view guard is
# governed by the no-live-rebuild-while-viewer-connected rail (a premium "restart" could
# rebuild a display while a viewer is connected); cold-send / broadcast agents could fire a
# real outbound send. These are forced ALERT-ONLY: they short-circuit straight to a
# (digested) escalation and are never passed to forage(). Matched on the short label
# (job.split(".")[-1]). Over-scoping here is a SAFE failure mode — it only makes a job
# alert-only, it never causes an unwanted auto-action.
AUTO_HEAL_DENY_EXACT = ("imessage", "break-glass-imsg")
AUTO_HEAL_DENY_SUFFIX = ("-remote-guard", "-sender")
AUTO_HEAL_DENY_SUBSTR = ("display", "broadcast", "cross-publish", "persona1-x")

# Deterministic infra/daemon lanes that are safe to bounce with a plain `launchctl kickstart`
# BEFORE spending a premium LLM call. A clean kickstart that comes back green is a $0 heal and
# avoids the token burn entirely; if it doesn't recover, we fall through to the premium path.
KICKSTART_SAFE = ("webui-backend-a", "work-context-index", "rag-refresh",
                  "work-rag-refresh", "local-model-bootstrap")


def _auto_heal_denied(job):
    """True if job is a display/remote-view guard or a cold-send/broadcast lane that must
    never be LLM-auto-restarted. Checked BEFORE forage() so the deny always wins."""
    short = job.split(".")[-1]
    if short in AUTO_HEAL_DENY_EXACT:
        return True
    if short.endswith(AUTO_HEAL_DENY_SUFFIX):
        return True
    return any(s in short for s in AUTO_HEAL_DENY_SUBSTR)


def _kickstart_safe(job):
    """True if job is a deterministic infra daemon we can bounce for $0 before any LLM call."""
    return job.split(".")[-1] in KICKSTART_SAFE


def _launchctl_kickstart(job):
    """Cheap, deterministic restart of an infra daemon. Returns True only if the job comes
    back green (verified running) after the bounce — otherwise the caller falls through to
    the premium heal path."""
    try:
        subprocess.run(f"launchctl kickstart -k gui/{os.getuid()}/{job}", shell=True,
                       capture_output=True, text=True, timeout=30)
    except Exception as e:
        log(f"[jit-healer] kickstart error {job}: {e}")
        return False
    return verify_heal(job, settle_seconds=8)


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
    """Bounded escalation: when a job exhausts its auto-heal budget, tell Operator ONCE
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
    if _budget_tripped():
        log(f"[jit-healer] {len(red)} red but spend breaker tripped → no premium call")
        _save_state(state)
        return
    # Pick worst job that isn't on cooldown / chronic
    candidates = [(j, c) for j, c in sorted(red, key=lambda x: abs(x[1]), reverse=True)
                  if _job_allowed(state, j)]
    if not candidates:
        _save_state(state)
        return
    job, code = candidates[0]
    short = job.split(".")[-1]

    # RAIL (checked BEFORE forage so it always wins): a display/remote-view guard or a
    # cold-send/broadcast lane must never be handed to an LLM auto-restart — that could
    # rebuild a display while a viewer is connected (violates
    # no-live-rebuild-while-viewer-connected) or fire a real outbound send. Force
    # alert-only: escalate ONCE (digested), never forage.
    if _auto_heal_denied(job):
        log(f"[jit-healer] {job} is on the auto-heal DENY list (display/send lane) — "
            f"alert-only, NOT auto-restarting")
        j = state.setdefault("jobs", {}).setdefault(job, {"last_heal": 0, "consec_fails": 0})
        j["consec_fails"] = MAX_CONSEC_FAILS
        j["last_heal"] = time.time()
        _escalate_chronic(state, job, j["consec_fails"])
        _save_state(state)
        return

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

    # Deterministic infra/daemon lane: try a plain launchctl kickstart FIRST. If it comes
    # back green that's a $0 heal — no premium LLM call needed. Only fall through to the
    # premium forage path if the cheap bounce didn't bring it back.
    if _kickstart_safe(job):
        log(f"[jit-healer] {job} is a deterministic infra lane — trying $0 launchctl kickstart first")
        if _launchctl_kickstart(job):
            log(f"[jit-healer] {job} recovered via kickstart — no premium call needed")
            try:
                record_heal(service=job, signature=f"launchd_exit_{code}",
                            action="launchctl_kickstart", verified=True,
                            detail="cheap deterministic kickstart recovery")
            except Exception:
                pass
            _job_record(state, job, True)
            _save_state(state)
            return
        log(f"[jit-healer] {job} kickstart didn't recover it — falling through to premium heal")

    log(f"[jit-healer] FAILURE {job} exit={code} → premium JIT heal")
    try:
        r = forage("jit-external",
                   f"The launchd job {job} just FAILED with exit={code}. Read its error log at "
                   f"~/Library/Logs/claude-stack/{short}.err.log, diagnose the single root cause, fix it if "
                   f"REVERSIBLE (repoint a path, fix the script, restart), then verify by kickstarting the job. "
                   f"RESULT: what you fixed.", backend=PREMIUM_BACKEND, model=PREMIUM_MODEL)
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
