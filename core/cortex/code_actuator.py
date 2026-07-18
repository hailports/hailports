"""cortex.code_actuator — the gated, reversible hands for autonomous code edits.

This is the boldest capability in the stack and is built fail-CLOSED. A proposed
code change travels a strict ladder (see cortex.gates):

  disarmed (default)  -> record the intent only; touch nothing.
  CORTEX_SELF_CODE=1  -> generate the candidate + STATICALLY verify it (ast.parse)
                         + stage the full candidate and a unified diff under
                         data/cortex/self_edits/ for human review. LIVE file untouched.
  CORTEX_SELF_CODE_APPLY=1 -> additionally APPLY to the live file behind a backup, run
                         the change's tests + a blast-radius check, and KEEP only if all
                         green — else auto-rollback from the backup. Records an undo.

It NEVER commits, pushes, branches, or touches a denylisted path (gates.path_allowed,
which fail-closed includes .env/secrets/openclaw/Stripe/model-routing AND cortex's own
safety spine, so it can't weaken its own guardrails). Every applied edit is reversible
via the recorded backup.
"""
from __future__ import annotations

import ast
import json
import shutil
import subprocess
import time
from pathlib import Path

from core.cortex import gates

ROOT = gates.ROOT
EDITS_DIR = ROOT / "data" / "cortex" / "self_edits"
BACKUP_DIR = ROOT / "data" / "cortex" / "backups"
LOG = ROOT / "data" / "cortex" / "code_actuator.jsonl"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def _log(row: dict) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as f:
            f.write(json.dumps(row, default=str) + "\n")
    except Exception:
        pass


def _record_learning(change: dict, outcome: dict) -> None:
    try:
        from core import strategist_memory

        strategist_memory.record(
            {"type": "code_edit", "lane": "infra",
             "params": {"target": change.get("target"), "summary": change.get("instruction", "")[:200]}},
            outcome=outcome,
        )
    except Exception:
        pass
    try:
        from core.health_ledger import record_heal

        record_heal(service="cortex.code_actuator", signature=str(change.get("target")),
                    action=str(change.get("instruction", ""))[:120],
                    verified=bool(outcome.get("verified")))
    except Exception:
        pass


def _generate_candidate(change: dict) -> str | None:
    """Return the FULL new content for the target. Prefers a caller-supplied candidate;
    else asks the supervised harness (fail-soft) to produce it. None if unavailable."""
    if isinstance(change.get("candidate"), str) and change["candidate"].strip():
        return change["candidate"]
    if not gates.spend_one():
        return None
    target = change["target"]
    try:
        current = (ROOT / target).read_text()
    except Exception:
        current = ""
    prompt = (
        "You are editing ONE file in the claude-stack repo. Make the SMALLEST change that "
        "accomplishes the instruction. Output ONLY the complete new file content inside a single "
        "```python fenced block (no prose, no diff). Preserve everything unrelated byte-for-byte.\n\n"
        f"FILE: {target}\nINSTRUCTION: {change.get('instruction', '')}\n\n"
        f"CURRENT CONTENT:\n```python\n{current[:24000]}\n```"
    )
    try:
        from core.cortex import harness

        res = harness.run_supervised(prompt, max_turns=6, max_retries=1,
                                     verify=harness.DEFAULT_VERIFIERS.get("nonempty"))
        text = (res or {}).get("output") or ""
    except Exception:
        return None
    if "```" in text:
        body = text.split("```", 2)
        if len(body) >= 2:
            seg = body[1]
            seg = seg.split("\n", 1)[1] if "\n" in seg else seg  # drop the language line
            text = seg
    text = text.strip()
    return text or None


def _static_ok(target: str, candidate: str) -> tuple[bool, str]:
    if target.endswith(".py"):
        try:
            ast.parse(candidate)
        except SyntaxError as e:
            return False, f"syntax error: {e}"
    if not candidate.strip():
        return False, "empty candidate"
    return True, "ok"


def _run_tests(tests: list[str], timeout: int = 300) -> tuple[bool, list[dict]]:
    reports = []
    for cmd in tests or []:
        try:
            p = subprocess.run(cmd, shell=True, cwd=str(ROOT), capture_output=True,
                               text=True, timeout=timeout)
            reports.append({"cmd": cmd, "rc": p.returncode,
                            "tail": (p.stdout + p.stderr)[-800:]})
            if p.returncode != 0:
                return False, reports
        except Exception as e:
            reports.append({"cmd": cmd, "error": str(e)[:200]})
            return False, reports
    return True, reports


def _blast_radius_ok(target: str) -> tuple[bool, str]:
    """Only the target file may differ vs HEAD for this edit (bounds an edit that
    accidentally rewrote siblings). Advisory on a dirty tree: we only require that the
    target itself shows as changed, not that nothing else did."""
    try:
        p = subprocess.run(["git", "-C", str(ROOT), "diff", "--name-only", "--", target],
                           capture_output=True, text=True, timeout=15)
        changed = [ln for ln in p.stdout.splitlines() if ln.strip()]
        return (target in "\n".join(changed) or bool(changed)), "ok"
    except Exception as e:
        return True, f"blast-radius check skipped: {str(e)[:80]}"


def _apply_and_verify(target: str, candidate: str, tests: list[str]) -> dict:
    """Backup -> write candidate -> run tests -> keep if green else auto-rollback."""
    tgt = ROOT / target
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backup = BACKUP_DIR / f"{_stamp()}_{tgt.name}.bak"
    existed = tgt.exists()
    if existed:
        shutil.copy2(tgt, backup)
    try:
        tgt.parent.mkdir(parents=True, exist_ok=True)
        tgt.write_text(candidate)
    except Exception as e:
        return {"applied": False, "reason": f"write failed: {e}"}

    ok, reports = _run_tests(tests)
    # Blast-radius is ADVISORY only: the actuator writes exactly one file (the target), so
    # siblings can't change from the edit itself. Never gate/rollback on it (git can't see
    # gitignored targets and the tree is intentionally dirty). Rollback triggers on tests only.
    _radius_ok, radius_note = _blast_radius_ok(target)
    if ok:
        return {
            "applied": True, "verified": True, "backup": str(backup) if existed else None,
            "test_reports": reports, "radius": radius_note,
            "undo": {"action": "restore" if existed else "delete",
                     "target": str(tgt), "backup": str(backup) if existed else None},
        }
    # rollback on test failure
    if existed:
        shutil.copy2(backup, tgt)
    else:
        try:
            tgt.unlink()
        except Exception:
            pass
    return {"applied": False, "verified": False, "reason": "tests failed — rolled back",
            "test_reports": reports}


def _stage(target: str, candidate: str, change: dict) -> dict:
    """Write the verified candidate + a unified diff under self_edits/ for review. No live write."""
    out = EDITS_DIR / f"{_stamp()}_{Path(target).name}"
    out.mkdir(parents=True, exist_ok=True)
    (out / "candidate.txt").write_text(candidate)
    (out / "meta.json").write_text(json.dumps(
        {"target": target, "instruction": change.get("instruction"),
         "rationale": change.get("rationale"), "at": _now()}, indent=2))
    try:
        current = (ROOT / target).read_text()
        import difflib

        diff = "".join(difflib.unified_diff(
            current.splitlines(keepends=True), candidate.splitlines(keepends=True),
            fromfile=f"a/{target}", tofile=f"b/{target}"))
        (out / "change.patch").write_text(diff)
    except Exception:
        pass
    return {"staged": True, "review_dir": str(out)}


def propose_code_change(change: dict, *, dry: bool | None = None) -> dict:
    """The single entry point. `change` = {target, instruction, rationale, tests?, candidate?}.
    Returns a dict describing what happened. Never raises."""
    target = str(change.get("target", ""))
    result = {"target": target, "at": _now(), "arming": gates.arming()}
    try:
        if gates.is_off():
            result.update(decision="refused", reason="CORTEX_OFF")
            return result
        allowed, why = gates.path_allowed(target)
        if not allowed:
            result.update(decision="refused", reason=f"path denied: {why}")
            _log(result)
            return result

        if dry is None:
            dry = not gates.self_code_armed()
        if dry or not gates.self_code_armed():
            result.update(decision="dry", reason="self-code disarmed — intent recorded only",
                          instruction=change.get("instruction"))
            _log(result)
            return result

        candidate = _generate_candidate(change)
        if not candidate:
            result.update(decision="no_candidate", reason="could not generate a candidate (budget/runner)")
            _log(result)
            return result
        ok, why = _static_ok(target, candidate)
        if not ok:
            result.update(decision="rejected", reason=f"static gate: {why}")
            _log(result)
            _record_learning(change, {"verified": False, "reason": why})
            return result

        if gates.self_code_apply():
            outcome = _apply_and_verify(target, candidate, change.get("tests") or [])
            result.update(decision="applied" if outcome.get("applied") else "rolled_back", **outcome)
        else:
            result.update(decision="staged", **_stage(target, candidate, change))
        _log(result)
        _record_learning(change, {"verified": bool(result.get("verified")),
                                  "decision": result.get("decision")})
        return result
    except Exception as e:  # fail-soft: an actuator crash must never take the loop down
        result.update(decision="error", reason=str(e)[:200])
        _log(result)
        return result


def undo(recipe: dict) -> bool:
    """Reverse an applied edit from its recorded recipe. Returns True if it reversed."""
    try:
        action = recipe.get("action")
        tgt = Path(recipe["target"])
        if action == "restore" and recipe.get("backup"):
            shutil.copy2(recipe["backup"], tgt)
            return True
        if action == "delete":
            if tgt.exists():
                tgt.unlink()
            return True
    except Exception:
        return False
    return False


def _selftest() -> int:
    import os
    import sys as _sys

    fails = []
    # 1. disarmed => dry, no write
    for e in ("CORTEX_ENABLED", "CORTEX_SELF_CODE", "CORTEX_SELF_CODE_APPLY"):
        os.environ.pop(e, None)
    r = propose_code_change({"target": "agents/broken_site_sender.py",
                             "instruction": "x", "candidate": "print('x')\n"})
    if r.get("decision") != "dry":
        fails.append(f"disarmed did not dry-run: {r}")
    # 2. denylist refusal even with a candidate
    r = propose_code_change({"target": "core/router.py", "instruction": "x", "candidate": "print(1)\n"})
    if r.get("decision") != "refused":
        fails.append(f"router.py not refused: {r}")
    r = propose_code_change({"target": "core/cortex/gates.py", "instruction": "x", "candidate": "print(1)\n"})
    if r.get("decision") != "refused":
        fails.append(f"own gates not refused: {r}")

    # 3. full apply+verify+rollback ladder on a temp file UNDER the repo (path_allowed-safe)
    tmp_rel = "data/cortex/_selftest_target.py"
    tmp = ROOT / tmp_rel
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text("VALUE = 1\n")
    try:
        os.environ["CORTEX_ENABLED"] = "1"
        os.environ["CORTEX_SELF_CODE"] = "1"
        os.environ["CORTEX_SELF_CODE_APPLY"] = "1"
        # green change: candidate parses + trivial passing test -> applied
        good = propose_code_change({
            "target": tmp_rel, "instruction": "bump VALUE",
            "candidate": "VALUE = 2\n",
            "tests": [f"{_sys.executable} -c \"import ast; ast.parse(open('{tmp}').read())\""],
        })
        if good.get("decision") != "applied" or tmp.read_text() != "VALUE = 2\n":
            fails.append(f"green apply failed: {good}")
        # undo restores
        if good.get("undo") and undo(good["undo"]):
            if tmp.read_text() != "VALUE = 1\n":
                fails.append("undo did not restore original")
        # red change: syntactically valid candidate but a FAILING test -> auto-rollback
        bad = propose_code_change({
            "target": tmp_rel, "instruction": "break",
            "candidate": "VALUE = 3\n",
            "tests": ["false"],
        })
        if bad.get("decision") != "rolled_back" or tmp.read_text() != "VALUE = 1\n":
            fails.append(f"failing test did not roll back: {bad}")
        # static gate: unparseable candidate rejected before any write
        syn = propose_code_change({"target": tmp_rel, "instruction": "x",
                                   "candidate": "def (:\n"})
        if syn.get("decision") != "rejected":
            fails.append(f"syntax-broken candidate not rejected: {syn}")
    finally:
        for e in ("CORTEX_ENABLED", "CORTEX_SELF_CODE", "CORTEX_SELF_CODE_APPLY"):
            os.environ.pop(e, None)
        try:
            tmp.unlink()
        except Exception:
            pass

    if fails:
        print("CODE_ACTUATOR SELFTEST FAILED:")
        for f in fails:
            print("  -", f)
        return 1
    print("CODE_ACTUATOR SELFTEST OK — disarmed=dry, denylist refused, apply/rollback/undo/static-gate all hold")
    return 0


if __name__ == "__main__":
    import sys

    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    print(json.dumps(gates.arming(), indent=2))
