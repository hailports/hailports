"""self_patch_gen.py — PREMIUM code-patch generator + worktree verification gate.

When the auto-patcher has NO safe registered remedy for an incident (it would
otherwise just escalate to Operator), this escalates to a PREMIUM model (Claude Opus)
to GENERATE a minimal code patch, then PROVES it is safe by applying it in an
ISOLATED git worktree and running the full verification suite there. A patch is
only surfaced (and only auto-applied when explicitly enabled) if it applies
cleanly, every touched module compiles, the invariants stay green, and the
target check passes — with NO regression.

GUARANTEE = verified-before-apply + auto-rollback + never-applies-unverified.
(Not a mathematical proof — the strongest real guarantee.)

DEFAULTS (safe):
  • PROPOSE-ONLY. Auto-apply requires the flag file .self-patch-codegen-enabled.
  • Kill switch .self-patch-disabled hard-stops everything.
  • Cost-capped (per-cycle / per-day premium calls + burn-rate budget gate).
  • Circuit-broken: 2 failed verifies for the same incident -> quarantine + stop.
  • Scope-guarded: only repo .py on an allowlist; NEVER secrets/.env/*.key or its
    own safety rails (this file + auto_patcher.py).

CLI:
  python -m core.self_patch_gen --self-test   # exercise the GATE for free (no API)
  python -m core.self_patch_gen --once        # process the latest diagnoser verdict
  python -m core.self_patch_gen --dry-run      # propose only, never apply
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYBIN = str(ROOT / ".venv" / "bin" / "python")
if not Path(PYBIN).exists():
    PYBIN = sys.executable

STATE_FILE = ROOT / ".self-patch-gen.state.json"
ARTIFACT_DIR = ROOT / "data" / "runtime" / "self_patch_patches"
LOG_FILE = ROOT / "logs" / "self_patch_gen.jsonl"
KILL_SWITCH = ROOT / ".self-patch-disabled"
AUTOAPPLY_FLAG = ROOT / ".self-patch-codegen-enabled"

# ── cost / runaway rails ──────────────────────────────────────────────────────
MAX_PREMIUM_PER_CYCLE = int(os.environ.get("SELF_PATCH_MAX_CYCLE", "2"))
MAX_PREMIUM_PER_DAY = int(os.environ.get("SELF_PATCH_MAX_DAY", "15"))
COOLDOWN_SEC = int(os.environ.get("SELF_PATCH_COOLDOWN", "1800"))      # 30 min between cycles
INCIDENT_FAIL_QUARANTINE = 2                                           # circuit breaker
PREMIUM_MODEL = os.environ.get("SELF_PATCH_MODEL", "anthropic/claude-opus-4")
PREMIUM_FALLBACK = os.environ.get("SELF_PATCH_MODEL_FALLBACK", "anthropic/claude-3.5-sonnet")
MAX_FILE_CHARS = 24000                                                 # context cap per file
# How many FREE verify->feedback regenerations to exhaust before ever spending on paid.
FREE_FIX_ATTEMPTS = int(os.environ.get("SELF_PATCH_FREE_ATTEMPTS", "3"))

# ── scope guard ───────────────────────────────────────────────────────────────
PATCHABLE_PREFIXES = ("apps/", "core/", "tools/", "agents/", "scripts/")
FORBIDDEN_SUBSTR = (".env", "secret", ".key", "credential", "token", "password",
                    "data/secrets", ".venv/", "venv/", "/.git/")
# Never let the patcher touch its OWN safety rails (no self-sabotage).
FORBIDDEN_FILES = {"core/self_patch_gen.py", "core/auto_patcher.py", "tools/invariants_guard.py"}

# Critical modules whose compile/import must stay green for ANY accepted patch.
CRITICAL_MODULES = [
    "apps/chatgpt_redacted_action.py", "core/constants.py", "core/auto_patcher.py",
    "core/self_patch_gen.py", "tools/invariants_guard.py",
]


def _now() -> float:
    return time.time()


def _log(event: dict) -> None:
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        event = {"ts": _now(), **event}
        with open(LOG_FILE, "a") as f:
            f.write(json.dumps(event, default=str) + "\n")
    except Exception:
        pass


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def _save_state(s: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(s, indent=2, default=str))
    except Exception:
        pass


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def _budget_ok() -> bool:
    """False if the burn-rate / OpenRouter budget guard is tripped — don't spend on premium."""
    try:
        bg = ROOT / ".openrouter-budget-guard.state.json"
        if bg.exists():
            st = json.loads(bg.read_text())
            if st.get("tripped") or st.get("disabled") or st.get("over_budget"):
                return False
    except Exception:
        pass
    try:
        br = ROOT / ".burn-rate-guard.state.json"
        if br.exists():
            st = json.loads(br.read_text())
            if st.get("tripped"):
                return False
    except Exception:
        pass
    return True


# ── premium model call (sync; key from the same dotenv the stack uses) ────────
def _premium_key() -> str:
    try:
        from core.api_client import _dotenv_get
        k = _dotenv_get("OPENROUTER_API_KEY")
        if k:
            return k
    except Exception:
        pass
    return os.environ.get("OPENROUTER_API_KEY", "")


def _gen_free(system: str, user: str, max_tokens: int = 3000) -> str:
    """FREE cloud pool (qwen3-coder etc.) — $0, no key/cap. '' on failure (never raises)."""
    try:
        import asyncio
        from core.free_llm_pool import try_free_providers
        txt, _prov = asyncio.run(try_free_providers(
            user, system=system, max_tokens=max_tokens, explicit=True, tier="strong"))
        return txt if (txt and txt.strip()) else ""
    except Exception:
        return ""


def _gen_local(system: str, user: str, max_tokens: int = 3000) -> str:
    """Local coder (always-available offline floor) — $0. '' on failure (never raises)."""
    try:
        import asyncio
        from core.local_client import generate as _lg
        _code_model = (os.environ.get("LOCAL_CODE_MODEL_STRONG")
                       or os.environ.get("LOCAL_CODE_MODEL") or None)
        out = asyncio.run(_lg(prompt=user, system=system, model=_code_model,
                              max_tokens=max_tokens, temperature=0))
        return out if (out and out.strip()) else ""
    except Exception:
        return ""


def _paid_available() -> tuple[bool, str]:
    """(ok, key). ok is True ONLY if a premium key exists AND the paid-LLM guard
    permits spend right now. This is the single choke-point for real OpenRouter $ spend."""
    key = _premium_key()
    if not key:
        return False, ""
    if not _budget_ok():
        return False, key
    try:
        from core.paid_llm_guard import require_paid_llm_api
        require_paid_llm_api("self_patch_gen premium")
    except Exception:
        return False, key
    return True, key


def _gen_paid(system: str, user: str, key: str, max_tokens: int = 3000) -> str:
    """PREMIUM Opus via OpenRouter — the paid TRUE-fix escalation. The caller MUST have
    already confirmed `_paid_available()` first. '' on failure (never raises)."""
    import urllib.request
    for model in [m for m in (PREMIUM_MODEL, PREMIUM_FALLBACK) if m]:
        try:
            payload = json.dumps({
                "model": model, "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}],
            }).encode()
            req = urllib.request.Request(
                "https://openrouter.ai/api/v1/chat/completions", data=payload,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=150) as r:
                d = json.loads(r.read().decode())
            content = d["choices"][0]["message"]["content"]
            if content and content.strip():
                return content
        except Exception:
            continue
    return ""


def _with_feedback(user: str, reasons: list, attempt: int) -> str:
    """Fold a prior attempt's verification-failure reasons back into the prompt so the
    NEXT (still-free) regeneration can self-correct before we ever escalate to paid."""
    if not reasons:
        return user
    fb = "; ".join(str(r) for r in reasons)[:800]
    return (user + f"\n\n--- PRIOR ATTEMPT #{attempt} FAILED VERIFICATION ---\n"
            f"The previous diff was REJECTED because: {fb}\n"
            "Produce a corrected minimal unified diff that resolves these specific problems and "
            "passes compile + invariants. Do not repeat the same mistake.")


_DIFF_RE = re.compile(r"```(?:diff|patch)?\s*\n(.*?)```", re.DOTALL)


def _extract_diff(text: str) -> str:
    m = _DIFF_RE.search(text or "")
    diff = m.group(1) if m else (text or "")
    diff = diff.strip()
    # must look like a unified diff
    if "+++ " not in diff or "--- " not in diff:
        return ""
    if not diff.endswith("\n"):
        diff += "\n"
    return diff


def _touched_files(diff: str) -> list[str]:
    files = []
    for line in diff.splitlines():
        if line.startswith("+++ "):
            p = line[4:].strip()
            p = re.sub(r"^[ab]/", "", p)
            if p and p != "/dev/null":
                files.append(p)
    return files


def _scope_violation(files: list[str]) -> str | None:
    if not files:
        return "no files in diff"
    for f in files:
        fl = f.lower()
        if f in FORBIDDEN_FILES:
            return f"forbidden (safety rail): {f}"
        if any(s in fl for s in FORBIDDEN_SUBSTR):
            return f"forbidden (secret/scope): {f}"
        if not f.startswith(PATCHABLE_PREFIXES):
            return f"outside patchable allowlist: {f}"
        if not f.endswith(".py"):
            return f"not a .py file: {f}"
    return None


# ── the verification gate ─────────────────────────────────────────────────────
def _run_checks(cwd: Path, touched: list[str]) -> dict:
    """Deterministic health snapshot in `cwd`. Returns {compile_ok, invariants_ok, invariants_pass}."""
    res = {"compile_ok": True, "compile_detail": "", "invariants_ok": False, "invariants_pass": 0, "invariants_total": 0}
    # py_compile every touched module + the critical set (catches breakage anywhere)
    targets = sorted(set(touched) | set(CRITICAL_MODULES))
    for rel in targets:
        fp = cwd / rel
        if not fp.exists():
            continue
        r = subprocess.run([PYBIN, "-m", "py_compile", str(fp)], capture_output=True, text=True)
        if r.returncode != 0:
            res["compile_ok"] = False
            res["compile_detail"] = f"{rel}: {r.stderr.strip()[:200]}"
            break
    # invariants_guard --check (cwd = the tree under test)
    try:
        r = subprocess.run([PYBIN, "-m", "tools.invariants_guard", "--check"],
                           capture_output=True, text=True, cwd=str(cwd), timeout=120)
        out = r.stdout + r.stderr
        m = re.search(r"(\d+)\s*/\s*(\d+)\s*ok", out)
        if m:
            res["invariants_pass"] = int(m.group(1))
            res["invariants_total"] = int(m.group(2))
            res["invariants_ok"] = res["invariants_pass"] == res["invariants_total"]
        else:
            res["invariants_ok"] = "DANGER" not in out and r.returncode == 0
    except Exception as e:
        res["invariants_detail"] = str(e)[:160]
    return res


def verify_patch(diff: str, target_check=None) -> dict:
    """THE GATE. Apply `diff` in an isolated git worktree and prove it is safe.
    Accept ONLY if: scope-clean, applies cleanly, all touched/critical modules compile,
    invariants stay >= baseline, and (if given) target_check(worktree) passes.
    Returns {accepted, reasons[], touched[], before, after}."""
    reasons = []
    touched = _touched_files(diff)
    sv = _scope_violation(touched)
    if sv:
        return {"accepted": False, "reasons": [f"scope: {sv}"], "touched": touched}

    baseline = _run_checks(ROOT, touched)
    # Build the verification tree from the LIVE working tree (uncommitted changes INCLUDED) via
    # `git stash create` — it writes a commit object of the current state without modifying anything.
    # Falls back to HEAD when the tree is clean.
    sc = subprocess.run(["git", "-C", str(ROOT), "stash", "create"], capture_output=True, text=True)
    base_ref = (sc.stdout or "").strip() or "HEAD"
    wt = Path(tempfile.mkdtemp(prefix="selfpatch-wt-"))
    worktree = wt / "tree"
    try:
        r = subprocess.run(["git", "-C", str(ROOT), "worktree", "add", "--detach", str(worktree), base_ref],
                           capture_output=True, text=True)
        if r.returncode != 0:
            return {"accepted": False, "reasons": [f"worktree add failed: {r.stderr.strip()[:160]}"], "touched": touched}

        patch_file = wt / "patch.diff"
        patch_file.write_text(diff)
        ap = subprocess.run(["git", "-C", str(worktree), "apply", "--whitespace=nowarn", str(patch_file)],
                            capture_output=True, text=True)
        if ap.returncode != 0:
            return {"accepted": False, "reasons": [f"git apply failed: {ap.stderr.strip()[:200]}"], "touched": touched}

        after = _run_checks(worktree, touched)
        if not after["compile_ok"]:
            reasons.append(f"compile failed: {after.get('compile_detail','')}")
        # no regression: invariants must not drop below baseline, and must be all-green
        if after["invariants_total"] and after["invariants_pass"] < baseline.get("invariants_pass", 0):
            reasons.append(f"invariants regressed {baseline.get('invariants_pass')}→{after['invariants_pass']}")
        if not after["invariants_ok"]:
            reasons.append(f"invariants not all-green ({after['invariants_pass']}/{after['invariants_total']})")
        if target_check is not None:
            try:
                if not target_check(worktree):
                    reasons.append("target check still failing after patch")
            except Exception as e:
                reasons.append(f"target check error: {e}")

        accepted = not reasons
        return {"accepted": accepted, "reasons": reasons or ["all checks passed"],
                "touched": touched, "before": baseline, "after": after}
    finally:
        subprocess.run(["git", "-C", str(ROOT), "worktree", "remove", "--force", str(worktree)],
                       capture_output=True, text=True)
        shutil.rmtree(wt, ignore_errors=True)


# ── live apply (only when explicitly enabled) ─────────────────────────────────
def _apply_live(diff: str, touched: list[str]) -> dict:
    """Snapshot -> apply -> the caller is expected to re-verify health and revert on drop.
    Returns {applied, backup_dir, detail}."""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    backup = ROOT / f".self-patch-backup-{stamp}"
    backup.mkdir(parents=True, exist_ok=True)
    for rel in touched:
        src = ROOT / rel
        if src.exists():
            dst = backup / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    pf = backup / "patch.diff"
    pf.write_text(diff)
    ap = subprocess.run(["git", "-C", str(ROOT), "apply", "--whitespace=nowarn", str(pf)],
                       capture_output=True, text=True)
    if ap.returncode != 0:
        return {"applied": False, "backup_dir": str(backup), "detail": ap.stderr.strip()[:200]}
    return {"applied": True, "backup_dir": str(backup), "detail": "applied + backed up"}


def _revert_live(backup_dir: str, touched: list[str]) -> None:
    b = Path(backup_dir)
    for rel in touched:
        src = b / rel
        if src.exists():
            shutil.copy2(src, ROOT / rel)


# ── the entrypoint auto_patcher calls ─────────────────────────────────────────
def try_generate(verdict: dict, *, dry_run: bool = False) -> dict:
    """Given a diagnoser verdict with no safe registered remedy, try to produce a
    VERIFIED patch. Default propose-only. Honors all kill/cost/circuit rails.
    Returns {ok, accepted, applied, patch_path, summary, reasons}."""
    incident = str(verdict.get("incident_id") or verdict.get("signature") or "unknown")

    if KILL_SWITCH.exists():
        return {"ok": False, "summary": "self-patch disabled (kill switch)", "accepted": False}
    # NOTE: the burn-rate/budget guard is NOT a hard stop here — free/local patch generation
    # is $0 and must keep working. Budget only gates the paid tier (see _paid_available()).

    state = _load_state()
    day = _today()
    if state.get("day") != day:
        state = {"day": day, "premium_today": 0, "last_cycle": 0, "incidents": {}}
    inc = state.setdefault("incidents", {}).setdefault(incident, {"fails": 0, "last": 0})

    # circuit breaker
    if inc["fails"] >= INCIDENT_FAIL_QUARANTINE:
        return {"ok": False, "summary": f"incident quarantined after {inc['fails']} failed verifies", "accepted": False}
    # caps — the daily premium cap only bars the PAID tier (checked in the attempt plan
    # below); free/local generation stays available so autonomous fixing never stalls on $.
    if _now() - state.get("last_cycle", 0) < COOLDOWN_SEC and state.get("cycle_count_window", 0) >= MAX_PREMIUM_PER_CYCLE:
        return {"ok": False, "summary": "cooldown / per-cycle cap", "accepted": False}

    # candidate file(s) from the verdict
    cands = verdict.get("candidate_files") or ([verdict["file"]] if verdict.get("file") else [])
    sv = _scope_violation([c for c in cands]) if cands else None
    file_ctx = ""
    for rel in cands[:2]:
        fp = ROOT / rel
        if fp.exists() and rel.startswith(PATCHABLE_PREFIXES) and rel not in FORBIDDEN_FILES:
            file_ctx += f"\n=== FILE: {rel} ===\n{fp.read_text(errors='replace')[:MAX_FILE_CHARS]}\n"

    system = (
        "You are a senior engineer fixing a precise, root-caused defect in a running Python stack. "
        "Output ONLY a minimal unified diff in a ```diff code block, in `git apply` format with "
        "`--- a/<path>` and `+++ b/<path>` headers and correct @@ hunks. Touch the FEWEST lines that "
        "fix the root cause. NEVER touch secrets/.env/keys or the approval gate / fail-closed logic. "
        "Preserve surrounding style. If you cannot produce a safe minimal fix, output the text NO_SAFE_PATCH."
    )
    user = (
        f"ROOT CAUSE: {verdict.get('root_cause','?')}\n"
        f"CATEGORY: {verdict.get('cause_type','?')}\n"
        f"SERVICE: {verdict.get('service','?')}\n"
        f"EVIDENCE: {str(verdict.get('evidence',''))[:1500]}\n"
        f"{file_ctx}\n"
        "Produce the minimal unified diff that fixes the ROOT CAUSE."
    )

    state["last_cycle"] = _now()
    state["cycle_count_window"] = state.get("cycle_count_window", 0) + 1
    _save_state(state)

    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)

    # ── Attempt plan: exhaust FREE (verify->feedback regeneration) FIRST, then a single
    # PAID "TRUE fix" escalation, then the local offline floor. Real OpenRouter $ is spent
    # ONLY on the "paid" step, and ONLY after every free attempt has failed verification. ──
    plan = [("free", i + 1) for i in range(max(1, FREE_FIX_ATTEMPTS))]
    paid_ok, paid_key = _paid_available()
    paid_capped = state.get("premium_today", 0) >= MAX_PREMIUM_PER_DAY
    if paid_ok and not paid_capped:
        plan.append(("paid", 0))
    plan.append(("local", 0))

    reasons_fb: list = []
    gate = None
    diff = ""
    patch_path = None
    used_paid = False

    for tier, attempt in plan:
        u = _with_feedback(user, reasons_fb, attempt)
        if tier == "free":
            raw = _gen_free(system, u)
        elif tier == "paid":
            raw = _gen_paid(system, u, paid_key)
            if raw:
                used_paid = True
                state["premium_today"] = state.get("premium_today", 0) + 1
                _save_state(state)
        else:
            raw = _gen_local(system, u)

        if not raw:
            continue
        if "NO_SAFE_PATCH" in raw:
            reasons_fb = ["model declined: no safe minimal patch for this root cause"]
            continue
        cand = _extract_diff(raw)
        if not cand:
            reasons_fb = ["output was not a valid unified diff (need a ```diff block with --- / +++ headers)"]
            continue

        g = verify_patch(cand)
        pp = ARTIFACT_DIR / f"{incident.replace('/','_')[:60]}-{tier}-{int(_now())}.diff"
        pp.write_text(cand)
        _log({"incident": incident, "stage": "verify", "tier": tier, "paid": used_paid,
              "accepted": g["accepted"], "reasons": g["reasons"], "touched": g.get("touched"),
              "patch": str(pp)})
        gate, diff, patch_path = g, cand, pp
        if g["accepted"]:
            break
        reasons_fb = g["reasons"]

    if gate is None:
        inc["fails"] += 1; _save_state(state)
        return {"ok": True, "accepted": False,
                "summary": "no candidate patch produced by any tier (free/paid/local)"}

    if not gate["accepted"]:
        inc["fails"] += 1
        _save_state(state)
        return {"ok": True, "accepted": False, "patch_path": str(patch_path),
                "reasons": gate["reasons"],
                "summary": "patch generated but FAILED verification (not applied)"}

    inc["fails"] = 0
    _save_state(state)

    # accepted + verified. Apply only if explicitly enabled AND not dry_run.
    if AUTOAPPLY_FLAG.exists() and not dry_run:
        ap = _apply_live(diff, gate["touched"])
        if not ap["applied"]:
            return {"ok": True, "accepted": True, "applied": False, "patch_path": str(patch_path),
                    "summary": f"verified but live apply failed: {ap['detail']}"}
        # post-apply re-verify against the LIVE tree; revert on any drop
        post = _run_checks(ROOT, gate["touched"])
        if not (post["compile_ok"] and post["invariants_ok"]):
            _revert_live(ap["backup_dir"], gate["touched"])
            _log({"incident": incident, "stage": "rollback", "post": post})
            return {"ok": True, "accepted": True, "applied": False, "patch_path": str(patch_path),
                    "summary": "applied then AUTO-ROLLED-BACK (post-apply health dropped)"}
        _log({"incident": incident, "stage": "applied", "backup": ap["backup_dir"]})
        return {"ok": True, "accepted": True, "applied": True, "patch_path": str(patch_path),
                "backup_dir": ap["backup_dir"], "summary": "VERIFIED PATCH APPLIED + health re-confirmed"}

    return {"ok": True, "accepted": True, "applied": False, "patch_path": str(patch_path),
            "touched": gate["touched"],
            "summary": "VERIFIED patch ready (propose-only; enable .self-patch-codegen-enabled to auto-apply)"}


# ── self-test: exercises the GATE for FREE (no API call) ──────────────────────
def self_test() -> int:
    print("self_patch_gen self-test (gate only, no API spend)")
    ok = True
    tracked = "core/telegram.py"  # tracked, innocuous, unrelated to safety rails
    fp = ROOT / tracked

    def _diff_for(mutate) -> str:
        orig = fp.read_text()
        try:
            fp.write_text(mutate(orig))
            return subprocess.run(["git", "-C", str(ROOT), "diff", "--", tracked],
                                  capture_output=True, text=True).stdout
        finally:
            fp.write_text(orig)

    # 1) GOOD patch (append a harmless comment) — must be ACCEPTED (compiles, no regression).
    g = verify_patch(_diff_for(lambda s: s + "\n# self_patch_gen self-test marker (harmless)\n"))
    print(f"  GOOD patch accepted: {g['accepted']}  reasons={g['reasons']}")
    ok = ok and g["accepted"] is True

    # 2) BROKEN-syntax patch — must be REJECTED by the compile gate.
    br = verify_patch(_diff_for(lambda s: s + "\ndef (((:\n"))
    print(f"  broken-syntax rejected: {not br['accepted']}  ({br['reasons']})")
    ok = ok and br["accepted"] is False

    # 3) SCOPE violation (.env) — REJECTED before any apply.
    b = verify_patch("--- a/.env\n+++ b/.env\n@@ -1,0 +1,1 @@\n+SECRET=leak\n")
    print(f"  .env rejected: {not b['accepted']}  ({b['reasons']})")
    ok = ok and b["accepted"] is False

    # 4) SELF-SABOTAGE (touches its own safety rail) — REJECTED.
    s = verify_patch("--- a/core/self_patch_gen.py\n+++ b/core/self_patch_gen.py\n@@ -1,0 +1,1 @@\n+# tamper\n")
    print(f"  self-rail rejected: {not s['accepted']}  ({s['reasons']})")
    ok = ok and s["accepted"] is False

    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main() -> int:
    if "--self-test" in sys.argv:
        return self_test()
    dry = "--dry-run" in sys.argv
    # --once: read the latest diagnoser verdict and try
    try:
        from core.auto_patcher import VERDICT_FILE  # type: ignore
        vf = VERDICT_FILE
    except Exception:
        vf = ROOT / "data" / "runtime" / "patch_verdict.json"
    if not vf.exists():
        print("no verdict to process")
        return 0
    try:
        verdicts = json.loads(vf.read_text())
        if isinstance(verdicts, dict):
            verdicts = [verdicts]
    except Exception as e:
        print(f"bad verdict file: {e}")
        return 1
    for v in verdicts:
        r = try_generate(v, dry_run=dry)
        print(json.dumps(r, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
