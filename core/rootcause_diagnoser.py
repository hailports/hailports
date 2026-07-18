#!/usr/bin/env python3
"""rootcause_diagnoser.py — turn a crash incident into a precise ROOT CAUSE.

The self_healer/jit-healer/watchdog can RECOVER a service (restart it), but a
restart treats the symptom. This module reads one crash incident and classifies
the *root cause* into an actionable category with evidence, then proposes a fix
tagged with whether it is safe to auto-patch or must be escalated to Operator.

It deliberately reuses the existing learn loop rather than reinventing it:
  - health_ledger.chronic() supplies the recurrence/band-aid signal. A root
    cause that keeps coming back (or keeps getting "healed" and re-breaking) gets
    its severity escalated and is flagged so a band-aid is replaced with a real fix.

Categories (with auto_patchable disposition):
  TRACEBACK              python error -> file:line / missing import / bad key  (often auto)
  RESOURCE               OOM / disk-full / FD leak                            (auto for log/cache bloat, else escalate)
  CONFIG-DRIFT           a pinned value got clobbered (cross-check pinner)     (auto: re-pin)
  DEPENDENCY             a module/binary/service it needs is down             (auto if local restartable, else escalate)
  SELECTOR/PLATFORM-DRIFT browser DOM/endpoint changed                        (escalate: needs a human eye)
  CRASH-LOOP             rapid respawn -> failing precondition                (escalate: never thrash)
  EXTERNAL-GATE          banned acct / expired cred / platform API change     (ALWAYS escalate)

An incident is a dict (one JSONL line from data/logs/crash_incidents.jsonl):
  {service, ts, exit_code?, log_tail?/traceback?, source?}
Only `service` and some text payload (log_tail/traceback) are required; everything
else is best-effort. The diagnoser is dependency-free and never raises on bad input.

Run:  python3 -m core.rootcause_diagnoser            # diagnose latest incident
      python3 -m core.rootcause_diagnoser --selftest # exercise synthetic incidents
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
LOG_DIR = DATA_DIR / "logs"
INCIDENTS = LOG_DIR / "crash_incidents.jsonl"

LEVEL_RANK = {"info": 0, "warn": 1, "warning": 1, "critical": 2, "crit": 2}

PY_ERROR_TYPES = (
    "ModuleNotFoundError", "ImportError", "NameError", "IndentationError",
    "SyntaxError", "TypeError", "AttributeError", "KeyError", "ValueError",
    "RuntimeError", "FileNotFoundError", "PermissionError",
)
# Network/transport errors are dependency symptoms, not code bugs — they are
# classified by the DEPENDENCY/SELECTOR paths, not localized as a traceback.

# Phrases that prove an EXTERNAL gate the stack must not (and cannot) self-patch.
_EXTERNAL_GATE = (
    "account suspended", "account banned", "account disabled", "your account has been",
    "permanently suspended", "rate limit exceeded", "ratelimit", "429 too many",
    "401 unauthorized", "403 forbidden", "invalid api key", "expired token",
    "expired credential", "authentication failed", "login required",
    "captcha", "verify you are human", "checkpoint required", "payment required",
    "subscription expired", "quota exceeded", "billing",
)

_RESOURCE = {
    "oom": ("out of memory", "oom", "memoryerror", "cannot allocate memory",
            "killed", "sigkill", "signal 9"),
    "disk": ("no space left", "disk full", "enospc", "quota exceeded on disk"),
    "fd": ("too many open files", "emfile", "enfile", "file descriptor"),
}

_SELECTOR = (
    "no such element", "element not found", "selector", "waiting for selector",
    "node is detached", "queryselector", "timeout.*waiting for", "navigation timeout",
    "net::err", "target closed", "cdp", "devtools", "chrome not reachable",
)

_DEPENDENCY = (
    "connection refused", "could not connect", "connection reset",
    "name or service not known", "failed to establish a new connection",
    "command not found", "no such file or directory", "executable", "not running",
    "service unavailable", "502 bad gateway", "503 service",
)


def _now() -> float:
    return time.time()


def _text_of(incident: dict) -> str:
    parts = []
    for k in ("traceback", "log_tail", "stderr", "error", "message", "signature"):
        v = incident.get(k)
        if v:
            parts.append(str(v))
    return "\n".join(parts)


def _lower(s: str) -> str:
    return (s or "").lower()


def _find_py_error(text: str) -> str | None:
    for et in PY_ERROR_TYPES:
        if et in text:
            return et
    return None


def _traceback_locus(text: str) -> str | None:
    """Pull the offending file:line from the last `File "...", line N` frame."""
    frames = re.findall(r'File "([^"]+)", line (\d+)', text)
    if frames:
        f, ln = frames[-1]
        return f"{f}:{ln}"
    return None


def _missing_module(text: str) -> str | None:
    m = re.search(r"No module named ['\"]([^'\"]+)['\"]", text)
    return m.group(1) if m else None


def _bad_key(text: str) -> str | None:
    m = re.search(r"KeyError:\s*['\"]?([^'\"\n]+)", text)
    return m.group(1).strip() if m else None


def _missing_name(text: str) -> str | None:
    m = re.search(r"name ['\"]([^'\"]+)['\"] is not defined", text)
    return m.group(1) if m else None


def _matches(text: str, needles) -> str | None:
    for n in needles:
        if re.search(n, text) if any(c in n for c in ".*[") else (n in text):
            return n
    return None


def _looks_pinned(service: str, text: str) -> bool:
    """A CONFIG-DRIFT signal: the incident mentions a config the pinner owns."""
    hints = ("openclaw.json", "pin-stable-config", "pinned", "config drift",
             "clobbered", "reverted to", "unexpected config value", "baseurl",
             "model id", "primary model")
    return any(h in text for h in hints)


def _recurrence_for(service: str, signature: str):
    """Ask the existing learn loop whether this root cause is chronic / a band-aid.
    Returns (recurrence_cycles, heal_attempts, band_aid). Never raises."""
    try:
        from core.health_ledger import chronic
    except Exception:
        return 0, 0, False
    try:
        for c in chronic(window_hours=168.0, min_cycles=2, min_level="warn"):
            cs = str(c.get("signature", ""))
            svc = str(c.get("service", ""))
            if (signature and signature[:40] in cs) or (service and svc == service and cs):
                return (int(c.get("recurrence_cycles", 0)),
                        int(c.get("heal_attempts", 0)),
                        bool(c.get("band_aid")))
    except Exception:
        pass
    return 0, 0, False


def diagnose(incident: dict) -> dict:
    """Classify one crash incident into a root cause + proposed fix.

    Returns a dict: {category, root_cause, evidence, proposed_fix, risk,
    auto_patchable, severity, recurrence_cycles, heal_attempts, band_aid, service}.
    """
    if not isinstance(incident, dict):
        incident = {"message": str(incident)}
    service = str(incident.get("service") or incident.get("job") or "unknown")
    raw = _text_of(incident)
    text = _lower(raw)
    exit_code = incident.get("exit_code")

    result = {
        "category": "UNKNOWN",
        "root_cause": "Could not classify; insufficient evidence in incident.",
        "evidence": (raw[:300] or f"exit_code={exit_code}"),
        "proposed_fix": "Manual triage required — capture stderr/traceback into the incident.",
        "risk": "high",
        "auto_patchable": False,
        "service": service,
    }

    # --- EXTERNAL-GATE first: never auto-fixable, always escalate. ---
    hit = _matches(text, _EXTERNAL_GATE)
    if hit:
        result.update({
            "category": "EXTERNAL-GATE",
            "root_cause": f"External platform gate hit ({hit!r}): banned account, "
                          f"expired credential, or platform API/policy change.",
            "evidence": _excerpt(raw, hit),
            "proposed_fix": "ESCALATE to Operator: re-auth / rotate credential / appeal "
                            "account / adapt to new platform terms. Not auto-fixable.",
            "risk": "high",
            "auto_patchable": False,
        })
        return _finalize(result, service)

    # --- CONFIG-DRIFT: a pinned value got clobbered. ---
    if _looks_pinned(service, text):
        result.update({
            "category": "CONFIG-DRIFT",
            "root_cause": "A pinned config value drifted/was clobbered "
                          "(pin-stable-config owns the canonical value).",
            "evidence": raw[:300],
            "proposed_fix": "Re-run pin-stable-config to restore the pinned value, "
                            "then verify the service reads it. Safe & reversible.",
            "risk": "low",
            "auto_patchable": True,
        })
        return _finalize(result, service)

    # --- RESOURCE: OOM / disk-full / FD leak. ---
    for kind, needles in _RESOURCE.items():
        if _matches(text, needles):
            if kind == "disk":
                result.update({
                    "category": "RESOURCE",
                    "root_cause": "Disk full — almost always log or cache bloat.",
                    "evidence": _excerpt(raw, "space") or raw[:200],
                    "proposed_fix": "Truncate/rotate the offending logs & clear caches "
                                    "(disk-guardian path). Low risk; data preserved.",
                    "risk": "low",
                    "auto_patchable": True,
                })
            elif kind == "fd":
                result.update({
                    "category": "RESOURCE",
                    "root_cause": "File-descriptor leak — sockets/files not closed.",
                    "evidence": raw[:200],
                    "proposed_fix": "Restart to reclaim FDs now; the leak is a code "
                                    "root cause — flag the owning module for a fix.",
                    "risk": "medium",
                    "auto_patchable": False,
                })
            else:  # oom
                result.update({
                    "category": "RESOURCE",
                    "root_cause": "Process OOM-killed — likely a memory leak or "
                                  "unbounded buffer in the service.",
                    "evidence": raw[:200] or "signal 9 / killed",
                    "proposed_fix": "Restart restores service, but the leak is the root "
                                    "cause — escalate the owning module for a real fix.",
                    "risk": "medium",
                    "auto_patchable": False,
                })
            return _finalize(result, service)

    # --- TRACEBACK: a concrete python error we can localize. ---
    et = _find_py_error(raw)
    if et:
        locus = _traceback_locus(raw)
        mod = _missing_module(raw)
        name = _missing_name(raw)
        key = _bad_key(raw)
        if et in ("ModuleNotFoundError", "ImportError") and mod:
            rc = f"{et}: missing module {mod!r}."
            fix = f"pip install {mod} in .venv (or fix the import path). Verify import."
            risk, auto = "low", True
        elif et == "NameError" and name:
            rc = f"NameError: {name!r} undefined" + (f" at {locus}" if locus else "") + "."
            fix = "Define/import the missing name at the locus. Verify py_compile + run."
            risk, auto = "low", True
        elif et == "KeyError" and key:
            rc = f"KeyError {key!r}" + (f" at {locus}" if locus else "") + \
                 " — missing/renamed config or dict key."
            fix = "Add the missing key (or use .get with a default) at the locus."
            risk, auto = "low", True
        elif et in ("SyntaxError", "IndentationError"):
            rc = f"{et}" + (f" at {locus}" if locus else "") + " — code won't parse."
            fix = "Fix the syntax at the locus; gate on py_compile before reload."
            risk, auto = "low", True
        else:
            rc = f"{et}" + (f" at {locus}" if locus else "") + "."
            fix = "Patch the offending logic at the locus; add a guard; verify by re-run."
            risk, auto = "medium", et in ("TypeError", "AttributeError", "ValueError")
        result.update({
            "category": "TRACEBACK",
            "root_cause": rc,
            "evidence": (locus and _excerpt(raw, "File ")) or raw[-300:],
            "proposed_fix": fix,
            "risk": risk,
            "auto_patchable": auto,
        })
        return _finalize(result, service)

    # --- DEPENDENCY: a thing it needs is down/missing. ---
    dep = _matches(text, _DEPENDENCY)
    if dep:
        target = _dependency_target(raw)
        result.update({
            "category": "DEPENDENCY",
            "root_cause": f"A required dependency is unreachable"
                          + (f" ({target})" if target else "") + f": {dep!r}.",
            "evidence": _excerpt(raw, dep.split("*")[0].strip("[.")) or raw[:200],
            "proposed_fix": (f"Restart/start the dependency"
                             + (f" ({target})" if target else "")
                             + " if it's a local service we own; otherwise escalate."),
            "risk": "medium",
            # Auto only when the dependency is plausibly a local restartable service.
            "auto_patchable": bool(target and _is_local_dep(target)),
        })
        return _finalize(result, service)

    # --- SELECTOR / PLATFORM-DRIFT: browser automation broke. ---
    sel = _matches(text, _SELECTOR)
    if sel:
        result.update({
            "category": "SELECTOR/PLATFORM-DRIFT",
            "root_cause": f"Browser automation target changed (DOM/endpoint): {sel!r}.",
            "evidence": _excerpt(raw, sel.split("*")[0].strip("[.")) or raw[:200],
            "proposed_fix": "ESCALATE: a selector/endpoint needs re-mapping by a human "
                            "(auto-guessing selectors risks acting on the wrong element).",
            "risk": "high",
            "auto_patchable": False,
        })
        return _finalize(result, service)

    # --- CRASH-LOOP: rapid respawn with no clear payload = failing precondition. ---
    restarts = incident.get("restart_count") or incident.get("respawns") or 0
    if (isinstance(restarts, (int, float)) and restarts >= 3) or \
       ("crash loop" in text or "respawn" in text):
        result.update({
            "category": "CRASH-LOOP",
            "root_cause": "Rapid respawn loop — a precondition fails immediately on "
                          "start (bad WorkingDirectory, missing env, exit 78, etc.).",
            "evidence": raw[:200] or f"restart_count={restarts}, exit_code={exit_code}",
            "proposed_fix": "ESCALATE: stop thrashing. Find the failing precondition "
                            "(env/cwd/launchd plist) before any further restart.",
            "risk": "high",
            "auto_patchable": False,
        })
        return _finalize(result, service)

    # launchd exit 78 = config/precondition error: known, classify as crash-loop-ish.
    if str(exit_code) == "78":
        result.update({
            "category": "CRASH-LOOP",
            "root_cause": "launchd exit 78 — config/precondition error (empty "
                          "WorkingDirectory or std path the pre-exec TCC can't open).",
            "evidence": f"exit_code=78; {raw[:160]}",
            "proposed_fix": "Repoint std/exec paths to internal ~/Library/Logs and set "
                            "a valid WorkingDirectory in the plist, then reload. Reversible.",
            "risk": "low",
            "auto_patchable": True,
        })
        return _finalize(result, service)

    return _finalize(result, service)


def _dependency_target(text: str) -> str | None:
    for name in ("ollama", "searxng", "docker", "redis", "postgres", "chrome",
                 "cdp", "gateway", "stripe", "resend", "brevo", "CompanyA-bridge"):
        if name in text.lower():
            return name
    m = re.search(r"127\.0\.0\.1:(\d+)|localhost:(\d+)", text)
    if m:
        return f"localhost:{m.group(1) or m.group(2)}"
    return None


_LOCAL_DEPS = ("ollama", "searxng", "docker", "redis", "gateway", "cdp",
               "CompanyA-bridge", "localhost")


def _is_local_dep(target: str) -> bool:
    t = target.lower()
    return any(d in t for d in _LOCAL_DEPS)


def _excerpt(raw: str, needle: str, width: int = 160) -> str:
    if not needle:
        return raw[:width]
    i = raw.lower().find(needle.lower())
    if i < 0:
        return raw[:width]
    start = max(0, i - 40)
    return raw[start:start + width].strip()


def _signature_of(result: dict) -> str:
    return f"{result['category']}:{result['root_cause']}"[:200]


def _finalize(result: dict, service: str) -> dict:
    """Attach the recurrence/band-aid signal and escalate severity for chronic
    root causes (a band-aid that keeps failing must be flagged for a REAL fix)."""
    sig = _signature_of(result)
    cycles, heals, band_aid = _recurrence_for(service, sig)
    result["recurrence_cycles"] = cycles
    result["heal_attempts"] = heals
    result["band_aid"] = band_aid

    base = "warn"
    if result["risk"] == "high" or not result["auto_patchable"]:
        base = "critical"
    severity = base
    # Chronic recurrence escalates: a recurring fix that keeps re-breaking is a
    # root-cause bug, and we should stop auto-band-aiding it and escalate instead.
    if band_aid or cycles >= 3:
        severity = "critical"
        if result["auto_patchable"]:
            result["auto_patchable"] = False
            result["proposed_fix"] = (
                "ESCALATE: this root cause recurs (" f"{cycles} cycles, {heals} heals)"
                " — the prior auto-fix is a BAND-AID. Needs a real fix, not another "
                "restart. Original suggestion: " + result["proposed_fix"]
            )
    result["severity"] = severity
    result["signature"] = sig
    result["diagnosed_ts"] = _now()
    return result


def _read_incidents(limit: int | None = None) -> list[dict]:
    out = []
    if not INCIDENTS.exists():
        return out
    for line in INCIDENTS.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out[-limit:] if limit else out


def diagnose_latest() -> dict | None:
    incs = _read_incidents(limit=1)
    if not incs:
        return None
    return diagnose(incs[0])


def _selftest() -> int:
    cases = [
        ("python-traceback", {
            "service": "intent_response_engine",
            "exit_code": 1,
            "traceback": ('Traceback (most recent call last):\n'
                          '  File "/Users/a/claude-stack/agents/x.py", line 42, in run\n'
                          '    do_thing(cfg["mode"])\n'
                          'KeyError: \'mode\''),
        }, "TRACEBACK", True),
        ("disk-full", {
            "service": "outlook-noise-cleaner",
            "log_tail": "OSError: [Errno 28] No space left on device while writing log",
        }, "RESOURCE", True),
        ("config-drift", {
            "service": "claw-chat",
            "message": "chat primary model reverted to free gpt-oss; pinned value in "
                       "openclaw.json was clobbered (config drift)",
        }, "CONFIG-DRIFT", True),
        ("banned-account", {
            "service": "ima_reel_poster",
            "log_tail": "POST /upload -> 403 Forbidden: your account has been "
                        "permanently suspended for violating community guidelines",
        }, "EXTERNAL-GATE", False),
        ("dependency-down", {
            "service": "claw-websearch",
            "log_tail": "requests.exceptions.ConnectionError: connection refused to "
                        "127.0.0.1:8890 (searxng)",
        }, "DEPENDENCY", True),
        ("missing-module", {
            "service": "style_clone",
            "traceback": 'File "/x/style_clone.py", line 3, in <module>\n'
                         "ModuleNotFoundError: No module named 'whisper'",
        }, "TRACEBACK", True),
        ("selector-drift", {
            "service": "engage-cdp",
            "log_tail": "Error: waiting for selector `button[data-testid=tweetButton]` "
                        "failed: timeout 30000ms exceeded; node is detached",
        }, "SELECTOR/PLATFORM-DRIFT", False),
        ("crash-loop", {
            "service": "landing-sender",
            "exit_code": 78,
            "restart_count": 6,
        }, "CRASH-LOOP", False),  # exit 78 -> low risk but restart_count forces loop path
    ]
    ok = 0
    for name, inc, want_cat, want_auto in cases:
        d = diagnose(inc)
        cat_ok = d["category"] == want_cat
        auto_ok = d["auto_patchable"] == want_auto
        passed = cat_ok and auto_ok
        ok += passed
        flag = "PASS" if passed else "FAIL"
        print(f"[{flag}] {name}: cat={d['category']} (want {want_cat}) "
              f"auto={d['auto_patchable']} (want {want_auto}) risk={d['risk']} "
              f"sev={d['severity']}")
        if not passed:
            print(f"        root_cause={d['root_cause']}")
            print(f"        fix={d['proposed_fix'][:120]}")
    print(f"\n{ok}/{len(cases)} synthetic incidents diagnosed correctly")
    return 0 if ok == len(cases) else 1


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    d = diagnose_latest()
    if d is None:
        print("[rootcause_diagnoser] no incidents in", INCIDENTS)
    else:
        print(json.dumps(d, indent=2, default=str))
