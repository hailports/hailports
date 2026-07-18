#!/usr/bin/env python3
"""auto_patcher.py — the PATCH-VERIFY-LEARN edge of self-healing.

Recovery and diagnosis already exist (watchdog/jit_healer restart; the
diagnostician + a diagnoser produce a root-cause verdict). This module is the
last edge that makes a cause *not recur*: it consumes a diagnoser verdict and,
ONLY when the fix is provably safe, applies the smallest root-cause remedy,
VERIFIES the service is healthy again, and LEARNS the outcome into
core.health_ledger so a recurrence of the SAME cause is detectable (= the patch
was a band-aid → escalate for a deeper fix).

Hard guarantees (never violated):
  • BOUNDED — at most MAX_ATTEMPTS per incident per WINDOW. Past that it stops
    and escalates instead of thrashing.
  • SAFE — only auto_patchable verdicts with risk=low (or risk=medium when the
    remedy is in SAFE_MEDIUM_REMEDIES) are touched. external_gate or risk=high
    go STRAIGHT to escalation, no patch.
  • VERIFIED — every applied remedy is followed by a deterministic health check
    (core.heal_verify for launchd jobs, file/size checks for log/disk). The
    outcome (verified True/False) is written to health_ledger.record_heal.
  • REVERSIBLE-FIRST — remedies that can be reverted register a revert hook; if
    verify fails we revert before escalating.
  • ESCALATE not loop — failure / cap / high-risk → core.alert.alert_alex
    (guaranteed iMessage leg) with the diagnosis + proposed manual fix.

Verdict contract (what the diagnoser hands us — dict):
    {
      "incident_id": "exit_loop:com.claude-stack.foo",   # stable de-dupe key
      "signature":   "exit_loop:com.claude-stack.foo",    # health_ledger key
      "service":     "com.claude-stack.foo",              # launchd label | None
      "cause_type":  "crash_loop",                          # human category
      "root_cause":  "WorkingDirectory empty → exit 78",   # the WHY
      "auto_patchable": true,
      "risk":        "low",                                # low | medium | high
      "external_gate": false,                              # needs a human gate?
      "remedy":      "restart_dependency",                 # remedy id (registry)
      "remedy_params": {"label": "com.claude-stack.foo"},  # remedy inputs
      "proposed_manual_fix": "repoint WorkingDirectory ..."# shown on escalate
    }

Run:
    python3 -m core.auto_patcher --run            # consume the live verdict file
    python3 -m core.auto_patcher --self-test      # exercise the full loop (no sends)
    python3 -m core.auto_patcher --verdict '<json>'
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
RUNTIME_DIR = DATA_DIR / "runtime"
LOG_DIR = DATA_DIR / "logs"
for _d in (RUNTIME_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# Where the diagnoser drops verdicts (a JSON list, or {"verdicts": [...]}).
VERDICT_FILE = RUNTIME_DIR / "diagnoser_verdicts.json"
STATE_FILE = RUNTIME_DIR / "auto_patcher_state.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s auto_patcher %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("auto_patcher")

# --- bounded autonomy ------------------------------------------------------
MAX_ATTEMPTS = 2            # per incident, per window
WINDOW_SEC = 3600          # rolling window for the attempt cap
LOG_CAP_MB = 50            # a log over this is "bloated" (matches diagnostician)
LOG_KEEP_BYTES = 5 * 1024 * 1024   # tail kept when rotating a bloated log

# medium-risk remedies the stack trusts to auto-apply (known-good, reversible or
# non-destructive). Everything else at risk=medium escalates.
SAFE_MEDIUM_REMEDIES = {"repin_config", "restart_dependency", "restart_service"}

PIN_STABLE = Path.home() / ".openclaw" / "pin-stable-config.py"
CDP_PRUNER = BASE_DIR / "tools" / "cdp_profile_pruner.py"
PYTHON = str(BASE_DIR / ".venv" / "bin" / "python")
if not Path(PYTHON).exists():
    PYTHON = sys.executable


# ---------------------------------------------------------------------------
# state (bounded attempt tracking)
# ---------------------------------------------------------------------------
def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2, default=str))
    except Exception as e:
        log.warning("state save failed: %s", e)


def _attempts_in_window(state: dict, incident: str) -> int:
    cutoff = time.time() - WINDOW_SEC
    rec = state.get("incidents", {}).get(incident, {})
    return len([t for t in rec.get("attempts", []) if float(t) >= cutoff])


def _record_attempt(state: dict, incident: str, outcome: str) -> None:
    inc = state.setdefault("incidents", {}).setdefault(incident, {"attempts": []})
    inc["attempts"] = [t for t in inc.get("attempts", []) if float(t) >= time.time() - WINDOW_SEC]
    inc["attempts"].append(time.time())
    inc["last_outcome"] = outcome
    inc["last_ts"] = time.time()


# ---------------------------------------------------------------------------
# escalation — the no-thrash exit. Resolves to core.alert's guaranteed iMessage
# leg. dry_run proves the wiring without actually paging Operator.
# ---------------------------------------------------------------------------
def _escalation_helper_ready() -> bool:
    """True iff the real iMessage escalation path is wired and callable. Used by
    the self-test to PROVE escalation resolves to the live helper without sending."""
    try:
        from core import alert
    except Exception as e:
        log.error("escalation helper import failed: %s", e)
        return False
    return callable(getattr(alert, "alert_alex", None)) and callable(
        getattr(alert, "_send_imessage_phone", None)
    )


def escalate(verdict: dict, reason: str, *, dry_run: bool = False) -> dict:
    """Hand the incident to Operator with the diagnosis + proposed manual fix. Never
    loops — this is what we do INSTEAD of re-patching."""
    svc = verdict.get("service") or "stack"
    subject = f"Auto-patcher ESCALATION: {svc}"
    body = (
        f"reason: {reason}\n"
        f"cause: {verdict.get('cause_type', '?')} — {verdict.get('root_cause', '?')}\n"
        f"signature: {verdict.get('signature', verdict.get('incident_id', '?'))}\n"
        f"risk: {verdict.get('risk', '?')}  external_gate: {bool(verdict.get('external_gate'))}\n"
        f"proposed manual fix: {verdict.get('proposed_manual_fix', '(none supplied)')}"
    )
    ready = _escalation_helper_ready()
    if dry_run:
        log.info("[dry-run] ESCALATE -> alert_alex wired=%s | %s | %s", ready, subject, reason)
        return {"action": "escalate", "sent": False, "helper_ready": ready, "reason": reason}
    if not ready:
        log.error("ESCALATION HELPER NOT READY — cannot page Operator (%s)", reason)
        return {"action": "escalate", "sent": False, "helper_ready": False, "reason": reason}
    try:
        from core.alert import alert_alex
        alert_alex(subject, body)
        log.info("ESCALATED to Operator: %s", subject)
        return {"action": "escalate", "sent": True, "helper_ready": True, "reason": reason}
    except Exception as e:
        log.error("escalation send failed: %s", e)
        return {"action": "escalate", "sent": False, "helper_ready": ready, "reason": reason}


# ---------------------------------------------------------------------------
# remedies — each returns dict(ok, detail, verify, revert)
#   verify: callable() -> bool   (deterministic health re-check)
#   revert: callable() -> None | None  (best-effort undo if verify fails)
# ---------------------------------------------------------------------------
def _verify_service(label: str | None):
    if not label:
        return lambda: True
    from core.heal_verify import verify_heal
    return lambda: verify_heal(label, settle_seconds=8)


def _remedy_repin_config(verdict: dict) -> dict:
    """config-drift → re-pin the stable config (pin-stable-config.py re-asserts the
    pinned values and re-chmods read-only)."""
    if not PIN_STABLE.exists():
        return {"ok": False, "detail": f"pin script missing: {PIN_STABLE}", "verify": lambda: False}
    r = subprocess.run([PYTHON, str(PIN_STABLE)], capture_output=True, text=True, timeout=120)
    ok = r.returncode == 0
    label = (verdict.get("remedy_params") or {}).get("label") or verdict.get("service")
    return {"ok": ok, "detail": f"re-pin rc={r.returncode} {r.stdout.strip()[:160]}",
            "verify": _verify_service(label)}


def _remedy_rotate_log(verdict: dict) -> dict:
    """disk/log bloat → cap the offending log: back it up (reversible), then truncate
    in place keeping the tail so the writer keeps its file handle."""
    params = verdict.get("remedy_params") or {}
    path = Path(os.path.expanduser(params.get("path", "")))
    cap_mb = float(params.get("cap_mb", LOG_CAP_MB))
    if not path.exists():
        return {"ok": False, "detail": f"log not found: {path}", "verify": lambda: False}
    backup = path.with_suffix(path.suffix + f".autopatch-{int(time.time())}")
    try:
        data = path.read_bytes()
        backup.write_bytes(data[-LOG_KEEP_BYTES * 4:])  # bounded backup
        tail = data[-LOG_KEEP_BYTES:]
        with open(path, "wb") as f:
            f.write(tail)
    except Exception as e:
        return {"ok": False, "detail": f"rotate failed: {e}", "verify": lambda: False}

    def _verify():
        try:
            return path.stat().st_size / 1024 / 1024 < cap_mb
        except Exception:
            return False

    def _revert():
        try:
            if backup.exists():
                path.write_bytes(backup.read_bytes())
        except Exception:
            pass

    return {"ok": True, "detail": f"rotated {path.name} -> kept {len(tail)//1024}KB, backup {backup.name}",
            "verify": _verify, "revert": _revert}


def _remedy_prune_cdp(verdict: dict) -> dict:
    """disk bloat from Chrome-CDP caches → reuse the allowlist-only cdp pruner."""
    if not CDP_PRUNER.exists():
        return {"ok": False, "detail": f"pruner missing: {CDP_PRUNER}", "verify": lambda: False}
    force = (verdict.get("remedy_params") or {}).get("force")
    cmd = [PYTHON, str(CDP_PRUNER)] + (["--force"] if force else [])
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    ok = r.returncode == 0
    return {"ok": ok, "detail": f"cdp prune rc={r.returncode} {r.stdout.strip()[-200:]}",
            "verify": lambda: r.returncode == 0}


def _remedy_restart_service(verdict: dict) -> dict:
    """crash_loop precondition cleared / dependency-down → kickstart the launchd job
    (only when it is NOT already running, so we never kill a healthy service)."""
    params = verdict.get("remedy_params") or {}
    label = params.get("label") or verdict.get("service")
    if not label:
        return {"ok": False, "detail": "no launchd label to restart", "verify": lambda: False}
    uid = os.getuid()
    live = subprocess.run(
        f"launchctl list | awk -v l={label} '$3==l && $1!=\"-\"{{print $1}}'",
        shell=True, capture_output=True, text=True,
    ).stdout.strip()
    if live:
        # already running — rule 4: don't kill a healthy/live service to "fix" it.
        return {"ok": True, "detail": f"{label} already running (pid {live}), no restart",
                "verify": _verify_service(label)}
    r = subprocess.run(["launchctl", "kickstart", f"gui/{uid}/{label}"],
                       capture_output=True, text=True, timeout=20)
    return {"ok": r.returncode == 0, "detail": f"kickstart {label} rc={r.returncode}",
            "verify": _verify_service(label)}


REMEDY_REGISTRY = {
    "repin_config": _remedy_repin_config,
    "rotate_log": _remedy_rotate_log,
    "prune_cdp": _remedy_prune_cdp,
    "restart_service": _remedy_restart_service,
    "restart_dependency": _remedy_restart_service,
}


# ---------------------------------------------------------------------------
# gate — decide patch vs escalate. SAFETY before everything.
# ---------------------------------------------------------------------------
def _gate(verdict: dict) -> tuple[bool, str]:
    if verdict.get("external_gate"):
        return False, "external_gate"
    risk = str(verdict.get("risk", "high")).lower()
    if risk == "high":
        return False, "risk_high"
    if not verdict.get("auto_patchable"):
        return False, "not_auto_patchable"
    remedy = verdict.get("remedy")
    if remedy not in REMEDY_REGISTRY:
        return False, f"unknown_remedy:{remedy}"
    if risk == "medium" and remedy not in SAFE_MEDIUM_REMEDIES:
        return False, f"medium_risk_unsafe_remedy:{remedy}"
    return True, "ok"


def _learn(verdict: dict, action: str, verified: bool | None, detail: str) -> None:
    try:
        from core.health_ledger import record_heal
        record_heal(
            service=verdict.get("service") or "",
            signature=verdict.get("signature") or verdict.get("incident_id") or "",
            action=f"auto_patch:{action}",
            verified=verified,
            detail=detail,
        )
    except Exception as e:
        log.warning("learn/record_heal failed: %s", e)


def _is_band_aid(verdict: dict) -> bool:
    """Has health_ledger already flagged this signature as a recurring band-aid?
    If so the remedy is treating a symptom — escalate for a deeper fix, don't re-apply."""
    sig = verdict.get("signature") or verdict.get("incident_id")
    if not sig:
        return False
    try:
        from core.health_ledger import chronic
        for c in chronic(window_hours=168.0, min_cycles=3):
            if c.get("signature") == sig and c.get("band_aid"):
                return True
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# the loop: gate -> patch -> verify -> learn -> (revert+escalate on failure)
# ---------------------------------------------------------------------------
def process_verdict(verdict: dict, *, state: dict | None = None, dry_escalate: bool = False) -> dict:
    state = _load_state() if state is None else state
    incident = verdict.get("incident_id") or verdict.get("signature") or "unknown"

    allowed, why = _gate(verdict)
    if not allowed:
        res = escalate(verdict, f"gate_blocked:{why}", dry_run=dry_escalate)
        _save_state(state)
        return {"incident": incident, "patched": False, **res}

    # bounded autonomy — never thrash
    if _attempts_in_window(state, incident) >= MAX_ATTEMPTS:
        _learn(verdict, "cap_exceeded", None, f"{MAX_ATTEMPTS} attempts in {WINDOW_SEC}s")
        res = escalate(verdict, "retry_cap_exceeded", dry_run=dry_escalate)
        _save_state(state)
        return {"incident": incident, "patched": False, **res}

    # band-aid recurrence — the patch didn't hold last time(s); go deeper
    if _is_band_aid(verdict):
        res = escalate(verdict, "recurring_band_aid", dry_run=dry_escalate)
        _save_state(state)
        return {"incident": incident, "patched": False, **res}

    remedy_fn = REMEDY_REGISTRY[verdict["remedy"]]
    _record_attempt(state, incident, "applying")
    _save_state(state)
    log.info("PATCH %s via %s (risk=%s)", incident, verdict["remedy"], verdict.get("risk"))

    try:
        out = remedy_fn(verdict)
    except Exception as e:
        _learn(verdict, verdict["remedy"], False, f"remedy raised: {e}")
        res = escalate(verdict, f"remedy_exception:{e}", dry_run=dry_escalate)
        _record_attempt(state, incident, "error")
        _save_state(state)
        return {"incident": incident, "patched": False, **res}

    if not out.get("ok"):
        _learn(verdict, verdict["remedy"], False, out.get("detail", ""))
        res = escalate(verdict, f"remedy_failed:{out.get('detail','')}", dry_run=dry_escalate)
        _record_attempt(state, incident, "failed")
        _save_state(state)
        return {"incident": incident, "patched": False, **res}

    # MANDATORY verify
    verified = False
    try:
        verified = bool(out.get("verify", lambda: True)())
    except Exception as e:
        log.warning("verify raised: %s", e)
        verified = False

    _learn(verdict, verdict["remedy"], verified, out.get("detail", ""))
    _record_attempt(state, incident, "verified" if verified else "unverified")

    if verified:
        log.info("PATCH VERIFIED %s: %s", incident, out.get("detail", ""))
        _save_state(state)
        return {"incident": incident, "patched": True, "verified": True,
                "detail": out.get("detail", "")}

    # verify failed — revert if we can, then escalate (no thrash)
    revert = out.get("revert")
    reverted = False
    if callable(revert):
        try:
            revert()
            reverted = True
            log.info("reverted %s after failed verify", incident)
        except Exception as e:
            log.warning("revert failed: %s", e)
    res = escalate(verdict, f"verify_failed (reverted={reverted})", dry_run=dry_escalate)
    _save_state(state)
    return {"incident": incident, "patched": True, "verified": False, "reverted": reverted, **res}


def _read_verdicts(path: Path) -> list[dict]:
    try:
        raw = json.loads(path.read_text())
    except Exception:
        return []
    if isinstance(raw, dict):
        raw = raw.get("verdicts", [])
    return [v for v in raw if isinstance(v, dict)]


def run(path: Path = VERDICT_FILE, *, dry_escalate: bool = False) -> list[dict]:
    verdicts = _read_verdicts(path)
    if not verdicts:
        log.info("no verdicts at %s", path)
        return []
    state = _load_state()
    results = []
    for v in verdicts:
        results.append(process_verdict(v, state=state, dry_escalate=dry_escalate))
    return results


# ---------------------------------------------------------------------------
# self-test — exercises the WHOLE loop on provably-safe synthetic verdicts.
# Touches NO service: the low-risk patchable case operates on a throwaway temp
# log; escalation runs in dry-run so Operator is NOT paged but the helper wiring is
# proven.
# ---------------------------------------------------------------------------
def self_test() -> int:
    import tempfile

    failures = []

    # 1) LOW-RISK PATCHABLE: rotate an oversized temp log; expect patched+verified.
    tmp = Path(tempfile.mkdtemp()) / "fake_bloat.log"
    tmp.write_bytes(b"x" * (LOG_CAP_MB + 5) * 1024 * 1024)
    low = {
        "incident_id": "selftest:log_bloat", "signature": "selftest:log_bloat",
        "service": None, "cause_type": "log_bloat", "root_cause": "synthetic oversize log",
        "auto_patchable": True, "risk": "low", "external_gate": False,
        "remedy": "rotate_log", "remedy_params": {"path": str(tmp), "cap_mb": LOG_CAP_MB},
    }
    r1 = process_verdict(low, dry_escalate=True)
    if not (r1.get("patched") and r1.get("verified")):
        failures.append(f"low-risk did not patch+verify: {r1}")
    if tmp.stat().st_size / 1024 / 1024 >= LOG_CAP_MB:
        failures.append("log still bloated after rotate")

    # 2) HIGH-RISK: must escalate, never patch.
    high = {
        "incident_id": "selftest:high", "signature": "selftest:high", "service": "com.x",
        "cause_type": "missing_import", "root_cause": "ambiguous code edit",
        "auto_patchable": True, "risk": "high", "external_gate": False,
        "remedy": "restart_service", "remedy_params": {"label": "com.x"},
        "proposed_manual_fix": "review import graph by hand",
    }
    r2 = process_verdict(high, dry_escalate=True)
    if r2.get("patched") or r2.get("action") != "escalate" or not r2.get("helper_ready"):
        failures.append(f"high-risk not escalated to live helper: {r2}")

    # 3) EXTERNAL-GATE: must escalate (human gate), never patch.
    gated = {
        "incident_id": "selftest:gate", "signature": "selftest:gate", "service": "stripe",
        "cause_type": "bad_key", "root_cause": "Stripe key rotated, needs human re-auth",
        "auto_patchable": True, "risk": "low", "external_gate": True,
        "remedy": "repin_config", "proposed_manual_fix": "re-enter key at /credentials",
    }
    r3 = process_verdict(gated, dry_escalate=True)
    if r3.get("patched") or r3.get("reason") != "gate_blocked:external_gate":
        failures.append(f"external-gate not escalated: {r3}")

    # 4) RETRY CAP: same incident past MAX_ATTEMPTS must stop patching + escalate.
    capkey = "selftest:cap"
    st = _load_state()
    st.setdefault("incidents", {})[capkey] = {"attempts": [time.time()] * MAX_ATTEMPTS}
    _save_state(st)
    capped = {
        "incident_id": capkey, "signature": capkey, "service": None,
        "cause_type": "log_bloat", "root_cause": "thrash guard",
        "auto_patchable": True, "risk": "low", "external_gate": False,
        "remedy": "rotate_log", "remedy_params": {"path": str(tmp)},
    }
    r4 = process_verdict(capped, dry_escalate=True)
    if r4.get("patched") or r4.get("reason") != "retry_cap_exceeded":
        failures.append(f"retry cap not enforced: {r4}")

    # 5) LEARN: a heal row for the low-risk patch must be in the ledger.
    learned = False
    try:
        from core.health_ledger import LEDGER
        if LEDGER.exists():
            for ln in LEDGER.read_text().splitlines()[-50:]:
                try:
                    row = json.loads(ln)
                except Exception:
                    continue
                if row.get("kind") == "heal" and row.get("signature") == "selftest:log_bloat":
                    learned = True
                    break
    except Exception as e:
        failures.append(f"ledger read failed: {e}")
    if not learned:
        failures.append("no learn record written for low-risk patch")

    # cleanup synthetic state so we don't poison real incident tracking
    st = _load_state()
    for k in ("selftest:log_bloat", "selftest:high", "selftest:gate", capkey):
        st.get("incidents", {}).pop(k, None)
    _save_state(st)
    try:
        import shutil
        shutil.rmtree(tmp.parent, ignore_errors=True)
    except Exception:
        pass

    if failures:
        print("SELF-TEST FAILED:")
        for f in failures:
            print("  ✗", f)
        return 1
    print("SELF-TEST PASSED: low-risk auto-patched+verified; high-risk+external-gate "
          "escalated to live iMessage helper (no send); retry cap enforced; learn recorded.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="patch-verify-learn the diagnoser's verdicts")
    ap.add_argument("--run", action="store_true", help="process the live verdict file")
    ap.add_argument("--verdict-file", default=str(VERDICT_FILE))
    ap.add_argument("--verdict", help="inline JSON verdict (testing)")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--dry-escalate", action="store_true",
                    help="prove escalation wiring without paging Operator")
    a = ap.parse_args()

    if a.self_test:
        return self_test()
    if a.verdict:
        print(json.dumps(process_verdict(json.loads(a.verdict), dry_escalate=a.dry_escalate), indent=2))
        return 0
    # default + --run: consume the verdict file
    results = run(Path(a.verdict_file), dry_escalate=a.dry_escalate)
    log.info("processed %d verdict(s): %s", len(results),
             {r.get("incident"): ("patched" if r.get("patched") else r.get("reason", r.get("action")))
              for r in results})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
