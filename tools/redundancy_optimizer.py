#!/usr/bin/env python3
"""redundancy_optimizer.py — standing autonomous perf/redundancy optimizer.

Keeps the stack lean + fast and ships PROVABLY-SAFE fixes on a schedule WITHOUT Operator
ever seeing a problem. It is silent: it fixes routine waste itself, logs everything to a
reversible ledger, and escalates ONLY a genuinely irreducible owner-action to
data/hustle/ALEX_ACTION_QUEUE.md. It NEVER pages, never texts, never osascripts.

Audit categories (each run):
  1. ollama models  — a model that is (a) NOT a canonical keep, (b) redundant with an
     installed role-equivalent, (c) re-pullable, (d) older than MIN_AGE_DAYS, AND whose
     every code/config/launchd reference can be atomically rewritten to the equivalent →
     removal candidate. This is the marquee: the Hermes-4 breakage (removing the model
     left MODEL_CANDIDATES/JUDGE_MODELS lists in agent_eval.py + instinct_loop.py pointing
     at a gone model) is the exact failure this category's REF-SAFETY prevents.
  2. dead snapshots — *-retired/*-fresh/*.bak/.KILLED/.diag dirs past an age. NOT moved by
     this tool; DELEGATED to tools/cold_data_tiering.py (reuse, no double-reclaim).
  3. regenerable caches — repo __pycache__/.pytest_cache older than an age → pruned
     (Python regenerates them on next import; provably restorable).
  4. perf regressions — DETECT ONLY. Reads tools/perf_guard.py's status; records genuine
     regressions to a flag file for the perf tooling. Never refactors code.

REF-SAFETY (mandatory, the hard-won lesson): before removing ANY model, grep the whole
codebase + configs + LaunchAgents. Referenced → rewrite each reference to the installed
equivalent ATOMICALLY in the same change (then verify zero dangling refs remain) or SKIP.
Never leave a dangling reference.

Runtime is sacrosanct: canonical/installed-equivalent models, .venv/data-runtime/
data-secrets/.colima/.ollama and launchd-referenced paths are refused. Coordinates via a
flock + pgrep check so it never double-reclaims against cold_data_tiering / disk-guardian.

Modes:
  redundancy_optimizer.py               # real run: apply provably-safe fixes, log ledger
  redundancy_optimizer.py --dry-run     # show the exact plan, change nothing
  redundancy_optimizer.py --json        # machine-readable
  redundancy_optimizer.py --selftest    # offline proof: plants a redundant model + ref,
                                        #   proves audit finds it, ref-checks it, and would
                                        #   clean the reference BEFORE removal. No deletes.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HOME = Path.home()
ROOT = Path(__file__).resolve().parent.parent
SELF = Path(__file__).resolve()
LAUNCHAGENTS = HOME / "Library/LaunchAgents"
CONFIG_ROOTS = (LAUNCHAGENTS, HOME / ".claude")

LOG = HOME / "Library/Logs/claude-stack/redundancy-optimizer.log"
LEDGER = ROOT / "data/logs/redundancy_optimizer_ledger.jsonl"
PERF_FLAGS = ROOT / "data/runtime/redundancy_optimizer_perf_flags.jsonl"
LOCK = ROOT / "data/runtime/redundancy_optimizer.lock"
QUEUE = ROOT / "data/hustle/ALEX_ACTION_QUEUE.md"
PERF_STATUS = ROOT / "data/runtime/perf_guard.json"

MIN_AGE_DAYS = int(os.environ.get("REDOPT_MODEL_MIN_AGE_DAYS", "14"))
CACHE_MIN_AGE_DAYS = int(os.environ.get("REDOPT_CACHE_MIN_AGE_DAYS", "10"))
CACHE_MIN_TOTAL_MB = float(os.environ.get("REDOPT_CACHE_MIN_TOTAL_MB", "5"))
PERF_IO_MBS_SAT = float(os.environ.get("PERF_IO_MBS_SAT", "110"))

# Canonical installed equivalents by role (the only models the box is meant to keep).
ROLE_EQUIV = {
    "reasoner": "qwen3:14b",
    "coder": "qwen2.5-coder:14b",
    "general": "qwen2.5:7b",
    "small": "qwen2.5:1.5b",
    "embed": "nomic-embed-text:latest",
}
CANONICAL_KEEP = set(ROLE_EQUIV.values())

# Never touch these path prefixes (runtime/live/secret/lane-separated).
RUNTIME_PREFIXES = tuple(str(p) for p in (
    ROOT / "data/runtime", ROOT / "data/secrets", ROOT / ".git",
    HOME / ".colima", HOME / ".ollama", HOME / ".docker",
))
CACHE_SCAN_ROOTS = (ROOT / "agents", ROOT / "tools", ROOT / "core",
                    ROOT / "apps", ROOT / "scripts")
CACHE_DIR_NAMES = ("__pycache__", ".pytest_cache")

# Sibling reclaim jobs: if any is live, we skip snapshot/cache reclaim (no double-reclaim).
SIBLING_PROCS = ("cold_data_tiering.py", "disk_guardian.sh", "storage_reaper.py",
                 "disk_early_warning", "perf_guard.py")


def ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def note(msg: str) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a") as fh:
            fh.write(f"{ts()} {msg}\n")
    except Exception:
        pass


def human(n: int) -> str:
    return f"{n / 1024**3:.2f} GB" if n >= 1024**3 else f"{n / 1024**2:.1f} MB"


def ledger_append(entry: dict) -> None:
    entry = {"ts": ts(), **entry}
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with open(LEDGER, "a") as fh:
        fh.write(json.dumps(entry) + "\n")


# --------------------------------------------------------------------------- ollama

def classify_role(name: str) -> str:
    n = name.lower()
    if any(t in n for t in ("embed", "nomic", "bge", "minilm", "gte")):
        return "embed"
    if any(t in n for t in ("coder", "starcoder", "deepseek-coder", "codellama", "codegemma")):
        return "coder"
    if any(t in n for t in (":0.5b", ":1b", ":1.5b", ":2b", ":3b", "tinyllama", "phi", "gemma:2")):
        return "small"
    if any(t in n for t in (":7b", ":8b")):
        return "general"
    return "reasoner"


def ollama_installed() -> list[dict]:
    """[{name,size_bytes,age_days}] from `ollama list` (best-effort; [] if ollama absent)."""
    try:
        out = subprocess.run(["ollama", "list"], capture_output=True, text=True, timeout=30).stdout
    except Exception:
        return []
    rows = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 3:
            continue
        name = parts[0]
        # SIZE is two tokens: "9.0 GB"; MODIFIED is the trailing relative age.
        m = re.search(r"([\d.]+)\s*(GB|MB|KB|B)\b", line)
        size_b = 0
        if m:
            v = float(m.group(1))
            size_b = int(v * {"GB": 1024**3, "MB": 1024**2, "KB": 1024, "B": 1}[m.group(2)])
        rows.append({"name": name, "size_bytes": size_b, "age_days": _parse_age(line)})
    return rows


def _parse_age(line: str) -> float | None:
    m = re.search(r"(\d+)\s+(second|minute|hour|day|week|month|year)s?\s+ago", line)
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    return n * {"second": 1/86400, "minute": 1/1440, "hour": 1/24,
                "day": 1, "week": 7, "month": 30, "year": 365}[unit]


CLAUDE_HOME = (HOME / ".claude").resolve()
TRANSCRIPT_DIRS = {"projects", "subagents", "workflows"}


def _is_claude_transcript(f: Path) -> bool:
    """True for ~/.claude conversation history (session/subagent/workflow transcripts, *.jsonl).
    These are append-only logs, NEVER rewriteable references — a model name only appears
    because a session once discussed it. Rewriting them = history corruption; and because
    they keep growing, a post-rewrite verify re-grep would re-find the name and self-block
    the removal forever. So they are excluded from the reference corpus entirely."""
    try:
        rp = f.resolve()
        rel = rp.relative_to(CLAUDE_HOME)
    except (ValueError, OSError):
        return False
    return rp.suffix == ".jsonl" or bool(set(rel.parts) & TRANSCRIPT_DIRS)


def _grep_refs(token: str, grep_root: Path | None) -> set[Path]:
    """Files that reference `token`. grep_root set → hermetic recursive grep (selftest).
    Else PRODUCTION: repo tracked+untracked (git grep --untracked) PLUS a targeted gitignored-
    code sweep over the code roots (a ref hiding in a gitignored code file is the Hermes dangling-
    ref class; we cover code dirs cheaply rather than dragging git grep through the huge data/
    tree) + LaunchAgents + ~/.claude configs. Excludes self+ledger+log and transcripts. Erring
    conservative — a stray match only makes us SKIP a removal, never break one."""
    files: set[Path] = set()
    if grep_root is not None:
        try:
            r = subprocess.run(["grep", "-rlF", "--", token, str(grep_root)],
                               capture_output=True, text=True, timeout=30)
            files.update(Path(p) for p in r.stdout.splitlines() if p.strip())
        except Exception:
            pass
    else:
        try:
            r = subprocess.run(["git", "grep", "--untracked", "-lF", "--", token],
                               cwd=str(ROOT), capture_output=True, text=True, timeout=30)
            files.update(ROOT / p for p in r.stdout.splitlines() if p.strip())
        except Exception:
            pass
        # Targeted gitignored-code sweep: git grep --untracked skips .gitignored files, so a model
        # ref hiding in a gitignored file under a CODE root (the Hermes dangling-ref class) would
        # evade it. Cover exactly the code dirs (cheap, no huge data/ tree) — NOT a blanket
        # --no-exclude-standard, which drags git grep through data/ (3k pages + External symlink)
        # and times out. Erring conservative: a stray match only makes us SKIP a removal.
        try:
            code_roots = [str(ROOT / d) for d in ("core", "tools", "agents", "scripts", "apps") if (ROOT / d).exists()]
            if code_roots:
                r = subprocess.run(["grep", "-rlF", "--include=*.py", "--include=*.sh",
                                    "--include=*.json", "--include=*.js", "--", token, *code_roots],
                                   capture_output=True, text=True, timeout=30)
                files.update(Path(p) for p in r.stdout.splitlines() if p.strip())
        except Exception:
            pass
        for cfg in CONFIG_ROOTS:
            if not cfg.exists():
                continue
            try:
                r = subprocess.run(["grep", "-rlF", "--", token, str(cfg)],
                                   capture_output=True, text=True, timeout=30)
                files.update(Path(p) for p in r.stdout.splitlines() if p.strip())
            except Exception:
                pass
    exclude = {SELF, LEDGER.resolve(), LOG.resolve(), PERF_FLAGS.resolve()}
    return {f for f in files
            if f.resolve() not in exclude and not _is_claude_transcript(f)}


def plan_ref_clean(name: str, equiv: str, grep_root: Path | None = None):
    """Return (plan, skip_reason).
    plan = [(file, old_text, new_text)] atomically rewriting every reference name→equiv.
    Empty plan = no references (free to remove). plan=None = an uncleanable reference exists
    (binary/unreadable/unwritable) → caller MUST skip removal (never leave a dangling ref)."""
    plan = []
    for f in sorted(_grep_refs(name, grep_root)):
        try:
            content = f.read_text()
        except Exception:
            return None, f"unreadable/binary reference in {f}"
        if not os.access(f, os.W_OK):
            return None, f"unwritable reference in {f}"
        if name not in content:
            continue
        plan.append((f, content, content.replace(name, equiv)))
    return plan, None


def audit_ollama(installed: list[dict] | None = None, grep_root: Path | None = None):
    """Return removal candidates. Each: dict(name,size_bytes,equiv,age_days,ref_plan,
    refd_files,skip). Hermetic when installed+grep_root are injected (selftest)."""
    inst = installed if installed is not None else ollama_installed()
    names = {m["name"] for m in inst}
    out = []
    for m in inst:
        name = m["name"]
        if name in CANONICAL_KEEP:
            continue
        equiv = ROLE_EQUIV[classify_role(name)]
        if equiv not in names:
            continue  # no installed equivalent → NOT redundant, keep
        if equiv == name:
            continue
        age = m.get("age_days")
        plan, skip = plan_ref_clean(name, equiv, grep_root)
        refd = [] if plan is None else [str(f) for f, _, _ in plan]
        # age gate: known age must exceed floor; unknown age only qualifies if UNREFERENCED.
        if age is not None and age < MIN_AGE_DAYS:
            skip = skip or f"too fresh ({age:.0f}d < {MIN_AGE_DAYS}d)"
        if age is None and refd:
            skip = skip or "age unknown AND referenced (conservative hold)"
        out.append({
            "name": name, "size_bytes": m.get("size_bytes", 0), "equiv": equiv,
            "age_days": age, "ref_plan": plan, "refd_files": refd, "skip": skip,
            "restore": f"ollama pull {name}",
        })
    return out


def apply_ollama_removal(cand: dict, dry: bool) -> tuple[bool, str]:
    """Atomically ref-clean THEN remove. Verifies zero dangling refs; rolls back on failure.
    Returns (applied, message). Never removes when dry."""
    name, equiv, plan = cand["name"], cand["equiv"], cand["ref_plan"]
    if cand["skip"]:
        return False, f"skip {name}: {cand['skip']}"
    if plan is None:
        return False, f"skip {name}: uncleanable reference"
    if dry:
        refs = ", ".join(cand["refd_files"]) or "none"
        return False, (f"WOULD ref-clean {len(plan)} file(s) [{refs}] {name}->{equiv}, "
                       f"verify no dangling ref, THEN `ollama rm {name}` "
                       f"(restore: {cand['restore']})")
    # 1) apply ref rewrites atomically, remembering originals for rollback.
    backups = []
    try:
        for f, old, new in plan:
            fd, tmp = tempfile.mkstemp(dir=str(f.parent))
            with os.fdopen(fd, "w") as th:
                th.write(new)
            os.replace(tmp, f)
            backups.append((f, old))
    except Exception as e:
        for f, old in backups:
            try:
                f.write_text(old)
            except Exception:
                pass
        return False, f"ref-clean FAILED for {name} ({e}); rolled back, model untouched"
    # 2) verify no dangling reference remains BEFORE deleting the model.
    remaining, _ = plan_ref_clean(name, equiv, None)
    if remaining is None or any(name in f.read_text() for f, _, _ in remaining):
        for f, old in backups:
            f.write_text(old)
        return False, f"dangling ref persisted for {name}; rolled back, model untouched"
    # 3) remove the (re-pullable) model.
    try:
        subprocess.run(["ollama", "rm", name], check=True, capture_output=True, timeout=60)
    except Exception as e:
        return False, f"`ollama rm {name}` failed ({e}); refs already point to {equiv} (safe)"
    ledger_append({"action": "ollama_rm", "model": name, "equiv": equiv,
                   "size_bytes": cand["size_bytes"], "restore": cand["restore"],
                   "ref_clean_files": cand["refd_files"]})
    return True, f"removed {name} (refs->{equiv}), freed {human(cand['size_bytes'])}"


# ----------------------------------------------------------------- snapshots (delegate)

def snapshot_plan():
    """Reuse cold_data_tiering's classifier to LIST provably-cold snapshot dirs. We never
    move them here — the real move is delegated to cold_data_tiering (no double-reclaim)."""
    try:
        sys.path.insert(0, str(ROOT))
        from tools import cold_data_tiering as cdt  # noqa
        dir_moves, dir_blocked = cdt.scan_dirs()
        return ([{"path": str(s), "bytes": z} for _, s, _, z, _ in dir_moves],
                [{"path": str(s), "bytes": z, "reason": r} for s, z, r in dir_blocked])
    except Exception as e:
        note(f"snapshot_plan via cold_data_tiering unavailable: {e}")
        return [], []


def apply_snapshots(dry: bool) -> str:
    if dry:
        return "delegated to cold_data_tiering (dry)"
    if sibling_running():
        return "skipped: a sibling reclaim job is live (no double-reclaim)"
    try:
        subprocess.run([str(ROOT / ".venv/bin/python"),
                        str(ROOT / "tools/cold_data_tiering.py")],
                       cwd=str(ROOT), capture_output=True, timeout=600)
        ledger_append({"action": "delegate_cold_data_tiering",
                       "restore": "see data/logs/cold_data_tiering.tsv (reversible manifest)"})
        return "invoked cold_data_tiering (snapshot/log tiering)"
    except Exception as e:
        return f"cold_data_tiering delegation failed: {e}"


# ---------------------------------------------------------------------- caches

def _dir_bytes(p: Path) -> int:
    t = 0
    for dp, _, files in os.walk(p):
        for fn in files:
            try:
                t += (Path(dp) / fn).stat().st_size
            except OSError:
                pass
    return t


def _newest_mtime(p: Path) -> float:
    newest = 0.0
    for dp, _, files in os.walk(p):
        for fn in files:
            try:
                m = (Path(dp) / fn).stat().st_mtime
            except OSError:
                continue
            newest = max(newest, m)
    return newest


def audit_caches():
    """Stale repo __pycache__/.pytest_cache (regenerated on next import → provably safe)."""
    cutoff = time.time() - CACHE_MIN_AGE_DAYS * 86400
    cands = []
    for root in CACHE_SCAN_ROOTS:
        if not root.exists():
            continue
        for dp, dirs, _ in os.walk(root):
            for d in list(dirs):
                if d not in CACHE_DIR_NAMES:
                    continue
                cd = Path(dp) / d
                s = str(cd)
                if any(s.startswith(pre) for pre in RUNTIME_PREFIXES):
                    continue
                nm = _newest_mtime(cd)
                if nm == 0.0 or nm >= cutoff:
                    continue
                cands.append({"path": s, "bytes": _dir_bytes(cd)})
    return cands


def apply_caches(cands, dry: bool) -> tuple[int, str]:
    total = sum(c["bytes"] for c in cands)
    if total < CACHE_MIN_TOTAL_MB * 1024**2:
        return 0, f"below {CACHE_MIN_TOTAL_MB}MB threshold ({human(total)}) — hold"
    if dry:
        return total, f"WOULD prune {len(cands)} stale cache dir(s), {human(total)} (regenerable)"
    if sibling_running():
        return 0, "skipped: sibling reclaim job live"
    freed = 0
    for c in cands:
        try:
            shutil.rmtree(c["path"])
            freed += c["bytes"]
        except Exception as e:
            note(f"cache prune failed {c['path']}: {e}")
    if freed:
        ledger_append({"action": "cache_prune", "count": len(cands), "freed_bytes": freed,
                       "restore": "regenerated automatically on next import (no action)"})
    return freed, f"pruned {len(cands)} cache dir(s), freed {human(freed)}"


# ------------------------------------------------------------------ perf regressions

def detect_perf_regressions(persist: bool = True) -> list[dict]:
    """DETECT ONLY. Reads perf_guard's status; records genuine regressions to a flag file
    for the perf tooling. Never refactors code. persist=False (dry-run) = observe, no write."""
    flags = []
    try:
        st = json.loads(PERF_STATUS.read_text())
    except Exception:
        return flags
    io = st.get("io_mbs") or st.get("io_mbps") or st.get("disk_mbs")
    if isinstance(io, (int, float)) and io >= PERF_IO_MBS_SAT:
        flags.append({"kind": "disk_io_saturation", "io_mbs": io, "source": "perf_guard"})
    acted = st.get("actions") or st.get("acted")
    if acted:
        flags.append({"kind": "perf_guard_remediated", "detail": acted, "source": "perf_guard"})
    if flags and persist:
        PERF_FLAGS.parent.mkdir(parents=True, exist_ok=True)
        with open(PERF_FLAGS, "a") as fh:
            for f in flags:
                fh.write(json.dumps({"ts": ts(), **f}) + "\n")
    return flags


# --------------------------------------------------------------- escalation (silent)

def escalate(title: str, body: str) -> None:
    """Append an irreducible owner-action to ALEX_ACTION_QUEUE.md. NEVER pages/texts."""
    try:
        QUEUE.parent.mkdir(parents=True, exist_ok=True)
        block = (f"\n---\n\n## {time.strftime('%Y-%m-%d')} — {title}\n\n"
                 f"> staged by redundancy_optimizer (irreducible owner action).\n\n"
                 f"- [ ] {body}\n")
        with open(QUEUE, "a") as fh:
            fh.write(block)
        note(f"escalated to ALEX_ACTION_QUEUE: {title}")
    except Exception as e:
        note(f"escalate failed: {e}")


def sibling_running() -> bool:
    for proc in SIBLING_PROCS:
        try:
            r = subprocess.run(["pgrep", "-f", proc], capture_output=True, text=True, timeout=10)
            if r.returncode == 0 and r.stdout.strip():
                return True
        except Exception:
            pass
    return False


# ------------------------------------------------------------------------- selftest

def selftest() -> int:
    """Offline proof (no ollama, no real deletes): plant a redundant model referenced in
    two files; prove audit finds it, ref-checks it, and would clean the reference BEFORE
    removal — rewriting name->equiv, leaving ZERO dangling refs."""
    fails = []
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        planted = "hermes-4:latest"           # redundant reasoner
        equiv = ROLE_EQUIV["reasoner"]        # qwen3:14b
        f1 = tdp / "agent_eval.py"
        f2 = tdp / "instinct_loop.py"
        f1.write_text(f'MODEL_CANDIDATES = ["{planted}", "{equiv}"]\n')
        f2.write_text(f'JUDGE_MODELS = ["{planted}"]  # judge\n')

        fake_installed = [
            {"name": planted, "size_bytes": 8 * 1024**3, "age_days": 40},
            {"name": equiv, "size_bytes": 9 * 1024**3, "age_days": 4},
            {"name": "qwen2.5-coder:14b", "size_bytes": 9 * 1024**3, "age_days": 4},
        ]

        cands = audit_ollama(installed=fake_installed, grep_root=tdp)
        cand = next((c for c in cands if c["name"] == planted), None)

        # 1) found as a removal candidate
        if cand is None:
            fails.append("audit did NOT flag the planted redundant model")
            _report_selftest(fails)
            return 1
        # canonical keep must NOT be flagged
        if any(c["name"] == equiv for c in cands):
            fails.append("audit wrongly flagged the canonical keep")

        # 2) ref-check found BOTH referencing files
        refset = {Path(p).name for p in cand["refd_files"]}
        if refset != {"agent_eval.py", "instinct_loop.py"}:
            fails.append(f"ref-check found {refset}, expected both files")
        if cand["equiv"] != equiv:
            fails.append(f"equiv resolved to {cand['equiv']}, expected {equiv}")
        if cand["skip"]:
            fails.append(f"clean candidate wrongly marked skip: {cand['skip']}")

        # 3) it WOULD clean the reference before removal (dry path prints ref-clean→verify→rm)
        applied, msg = apply_ollama_removal(cand, dry=True)
        if applied:
            fails.append("dry-run apply reported an actual removal (must not delete)")
        if not ("ref-clean" in msg and "ollama rm" in msg and equiv in msg):
            fails.append(f"dry plan missing ref-clean-before-remove ordering: {msg}")

        # PROVE the mechanism actually works: apply the ref-clean to the temp copies and
        # confirm the dangling name is gone and the equivalent is present — no `ollama rm`.
        for f, old, new in cand["ref_plan"]:
            f.write_text(new)
        residual = plan_ref_clean(planted, equiv, tdp)[0]
        still = [str(f) for f, _, _ in (residual or []) if planted in f.read_text()]
        if still:
            fails.append(f"ref-clean left dangling references in {still}")
        if equiv not in f1.read_text() or planted in f2.read_text():
            fails.append("ref-clean did not rewrite name->equiv in the planted files")

        # negative: a model with NO installed equivalent must NOT be flagged
        no_equiv = audit_ollama(
            installed=[{"name": "llava:34b", "size_bytes": 1, "age_days": 99}], grep_root=tdp)
        if no_equiv:
            fails.append("flagged a model that has no installed equivalent")

    # ---- PRODUCTION-PATH coverage: exercise the REAL git-grep + ~/.claude scan
    # (grep_root=None), not the hermetic grep_root — the two historic breakages live here.
    phantom = "redopt-selftest-phantom:latest"
    repo_ref = ROOT / "_redopt_selftest_untracked_ref.py"    # untracked repo file
    claude_dir = HOME / ".claude/projects/_redopt_selftest"
    transcript = claude_dir / f"session-{os.getpid()}.jsonl"  # ~/.claude transcript
    try:
        repo_ref.write_text(f'MODEL_CANDIDATES = ["{phantom}"]  # untracked repo ref\n')
        claude_dir.mkdir(parents=True, exist_ok=True)
        transcript.write_text(f'{{"role":"user","text":"we discussed {phantom} once"}}\n')
        prod_plan, _ = plan_ref_clean(phantom, ROLE_EQUIV["reasoner"], None)
        prod_files = {Path(f).resolve() for f, _, _ in (prod_plan or [])}
        # DEFECT 1: an UNTRACKED repo reference MUST be caught by the production path.
        if repo_ref.resolve() not in prod_files:
            fails.append("PROD path missed an UNTRACKED repo reference (defect-1 regression)")
        # DEFECT 2: a ~/.claude transcript MUST NEVER be a rewrite target.
        if transcript.resolve() in prod_files:
            fails.append("PROD path planned a rewrite of a ~/.claude transcript (defect-2 regression)")
    finally:
        for p in (repo_ref, transcript):
            try:
                p.unlink()
            except Exception:
                pass
        try:
            claude_dir.rmdir()
        except Exception:
            pass

    _report_selftest(fails)
    return 1 if fails else 0


def _report_selftest(fails):
    if fails:
        print("SELFTEST FAILED:")
        for f in fails:
            print("  - " + f)
    else:
        print("SELFTEST PASSED — planted redundant model found, ref-checked, and its "
              "reference cleaned (name->equiv) BEFORE any removal; zero dangling refs; "
              "no-equivalent model correctly not flagged; no real deletes performed.")


# ----------------------------------------------------------------------------- main

def run(dry: bool, as_json: bool) -> int:
    lock_fh = None
    if not dry:
        try:
            LOCK.parent.mkdir(parents=True, exist_ok=True)
            lock_fh = open(LOCK, "w")
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception:
            print("[redopt] another instance holds the lock — exiting")
            return 0

    ollama_cands = audit_ollama()
    snap_moves, snap_blocked = snapshot_plan()
    cache_cands = audit_caches()
    perf_flags = detect_perf_regressions(persist=not dry)

    actions = []
    for c in ollama_cands:
        applied, msg = apply_ollama_removal(c, dry)
        actions.append(msg)
        # irreducible: a big redundant model held ONLY by an uncleanable (non-repo) ref.
        if not dry and c["skip"] and "uncleanable" in (c["skip"] or "") \
                and c["size_bytes"] > 4 * 1024**3:
            escalate(f"redundant ollama model {c['name']} has an uncleanable reference",
                     f"{c['name']} ({human(c['size_bytes'])}) duplicates {c['equiv']} but a "
                     f"reference outside the repo can't be auto-rewritten. review + retire manually.")

    cache_bytes, cache_msg = apply_caches(cache_cands, dry)
    snap_msg = apply_snapshots(dry) if (snap_moves and not dry) else \
        (f"{len(snap_moves)} snapshot dir(s), {human(sum(s['bytes'] for s in snap_moves))} "
         f"(delegated to cold_data_tiering)" if snap_moves else "no cold snapshots")

    summary = {
        "mode": "dry-run" if dry else "apply",
        "ollama_candidates": [{"name": c["name"], "equiv": c["equiv"],
                               "size": human(c["size_bytes"]), "age_days": c["age_days"],
                               "refs": c["refd_files"], "skip": c["skip"]} for c in ollama_cands],
        "ollama_actions": actions,
        "snapshots": snap_msg,
        "snapshots_blocked": len(snap_blocked),
        "caches": cache_msg,
        "cache_bytes": cache_bytes,
        "perf_regressions": perf_flags,
    }
    note(f"{summary['mode']}: ollama={len(ollama_cands)} cand, "
         f"snap={len(snap_moves)}, cache={human(cache_bytes)}, perf_flags={len(perf_flags)}")

    if as_json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"[redopt] {summary['mode']}  "
              f"(runtime models refused, sibling-coordinated, silent)")
        print(f"  ollama redundancy : {len(ollama_cands)} candidate(s)")
        for m in actions:
            print(f"      - {m}")
        if not ollama_cands:
            print(f"      (all installed models are canonical keeps / referenced — nothing to cut)")
        print(f"  cold snapshots    : {summary['snapshots']}")
        print(f"  regenerable cache : {cache_msg}")
        if snap_blocked:
            print(f"  runtime/hot dirs REFUSED (correct): {len(snap_blocked)}")
        print(f"  perf regressions  : {len(perf_flags)} flagged (detect-only, no code change)")
        for pf in perf_flags:
            print(f"      ! {pf}")
    if lock_fh:
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_UN)
            lock_fh.close()
        except Exception:
            pass
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    return run(dry=args.dry_run, as_json=args.json)


if __name__ == "__main__":
    raise SystemExit(main())
