#!/usr/bin/env python3
"""Capability crystallization — turn every novel task the AI solves into a permanent
DETERMINISTIC $0 tool, so the stack gets cheaper AND smarter every repeat.

This GENERALIZES core.sf_resolver_library: that module hand-holds ONE proven pattern
(the SF access-diff) into a deterministic resolver. This module automates the whole
loop for ANY task shape the AI solves with a stable tool-call sequence.

THE LOOP (silent self-correction — the stack FIXES, it does not alert):
  1. record_solution(sig, approach, tool_calls, result, lane)
       every time premium AI resolves a NOVEL task, log HOW it solved it (the concrete,
       ordered tool-call sequence) to data/learning/crystallization_candidates.jsonl.
  2. promote(...)  — when the SAME task_signature recurs N times (default 3) with the
       SAME deterministic tool-call sequence (a "stable" solution), GENERATE a callable
       resolver stub into core/crystallized/<name>.py that replays that proven sequence,
       and register it. HUMAN-REVIEW GATE: the stub is STAGED, not auto-activated. A person
       (or a later trusted pass) reviews the generated .py, confirms it's still correct +
       deterministic, then calls activate(name) to flip it live.
  3. match(task, lane) — next time the task recurs, return the crystallized deterministic
       resolver (no premium AI). No live match -> None -> caller falls back to AI (which,
       once it solves it again, feeds step 1 -> eventually promotes -> never AI again).

Every promotion retires a recurring premium-AI call: the task gets cheaper (deterministic
$0) and the stack gets smarter (one more thing it never has to re-reason).

LANE-AWARE: candidates + resolvers are tagged work|hustle|personal and NEVER cross. A
crystallized work resolver can never satisfy a hustle task (and vice-versa) — the hard
lane firewall holds at match time (registry is keyed by lane::signature).

Deterministic-first, additive, fail-soft. Touches no live service. The generated stub is
INERT Python (a proven sequence + a pure resolve()); it is data the caller chooses to
execute, it does not execute anything itself.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

if __package__ in (None, ""):  # allow `python core/capability_crystallization.py`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import BASE_DIR

# ---------------------------------------------------------------------------
# Config / store
# ---------------------------------------------------------------------------

PROMOTE_THRESHOLD = 3          # a signature must recur this many times (same sequence)
LANES = {"work", "hustle", "personal"}
DEFAULT_LANE = "hustle"

STATUS_STAGED = "staged"       # generated, awaiting human review — NOT served by match()
STATUS_ACTIVE = "active"       # human-approved — served deterministically

_lock = threading.Lock()

# HARD LANE FIREWALL. /home/user/claude-stack auto-mirrors to GitHub, so a
# WORK-lane record (real CompanyA Monday BOARD_ID XPHONEX, Outlook/SF calls) landing
# under it would break CompanyA's air-gap. Work-lane state lives ONLY under CompanyA-local.
HUSTLE_ROOT = BASE_DIR.resolve()  # == /home/user/claude-stack (GitHub-mirrored)
WORK_ROOT = (Path("~/.openclaw/workspace/CompanyA-local").expanduser()).resolve()


class LaneFirewallError(RuntimeError):
    """Raised (never silently swallowed) when a work-lane write would land in a GitHub-
    mirrored hustle path — the CompanyA air-gap must fail CLOSED and loud."""


@dataclass
class Store:
    """Where the crystallization state lives. Swappable so the smoke test (and any
    caller) can run against a throwaway dir without touching the live stack."""
    candidates_log: Path
    registry_path: Path
    crystallized_dir: Path

    def ensure(self) -> "Store":
        self.candidates_log.parent.mkdir(parents=True, exist_ok=True)
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        self.crystallized_dir.mkdir(parents=True, exist_ok=True)
        init = self.crystallized_dir / "__init__.py"
        if not init.exists():
            init.write_text(
                '"""Auto-generated crystallized deterministic resolvers.\n'
                "Each module replays a proven tool-call sequence so a recurring task never\n"
                'again spends premium AI. Human-reviewed before activation."""\n'
            )
        return self


# HUSTLE lane (+ personal) — the GitHub-mirrored repo is fine for these.
DEFAULT_STORE = Store(
    candidates_log=BASE_DIR / "data" / "learning" / "crystallization_candidates.jsonl",
    registry_path=BASE_DIR / "data" / "learning" / "crystallized_registry.json",
    crystallized_dir=BASE_DIR / "core" / "crystallized",
)

# WORK lane — rooted under CompanyA-local, NEVER under the GitHub-mirrored repo.
WORK_STORE = Store(
    candidates_log=WORK_ROOT / "crystallization" / "crystallization_candidates.jsonl",
    registry_path=WORK_ROOT / "crystallization" / "crystallized_registry.json",
    crystallized_dir=WORK_ROOT / "crystallization" / "crystallized",
)


def _store(store: Optional[Store], lane: Any = None) -> Store:
    """Pick the store. An explicit `store` (e.g. the smoke's throwaway dir) always wins.
    Otherwise route by lane: work -> CompanyA-local, everything else -> the hustle repo."""
    if store is not None:
        return store
    return WORK_STORE if _norm_lane(lane) == "work" else DEFAULT_STORE


def _guard_lane_store(lane: Any, st: Store) -> None:
    """Fail CLOSED: refuse to persist a work-lane record to any path under the GitHub-
    mirrored hustle repo. Raises LaneFirewallError so the fault ESCALATES, never heals silent."""
    if _norm_lane(lane) != "work":
        return
    for p in (st.candidates_log, st.registry_path, st.crystallized_dir):
        rp = Path(p).resolve()
        if rp == HUSTLE_ROOT or HUSTLE_ROOT in rp.parents:
            raise LaneFirewallError(
                f"work-lane write blocked: {rp} is under the GitHub-mirrored hustle repo "
                f"{HUSTLE_ROOT} — work-lane state must live under {WORK_ROOT}"
            )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Signature + sequence normalization
# ---------------------------------------------------------------------------

def _norm_sig(sig: Any) -> str:
    """Canonicalize a task signature so trivial variants group together."""
    s = "" if sig is None else str(sig)
    return re.sub(r"\s+", " ", s).strip().lower()


def _extract_signature(task: Any) -> str:
    """Pull a signature out of whatever the caller passes to match()."""
    if isinstance(task, dict):
        for k in ("task_signature", "signature", "sig", "text", "request"):
            if task.get(k):
                return str(task[k])
        return ""
    return "" if task is None else str(task)


def _extract_lane(task: Any) -> Optional[str]:
    if isinstance(task, dict):
        lane = task.get("lane")
        if lane in LANES:
            return lane
    return None


def _norm_lane(lane: Any) -> str:
    l = str(lane or "").strip().lower()
    return l if l in LANES else DEFAULT_LANE


def _norm_tool_calls(tool_calls: Any) -> list[dict]:
    """Normalize a tool-call sequence into an ordered list of {tool, args} dicts.
    Accepts a list of dicts, a list of (tool, args) tuples, or a list of bare strings."""
    out: list[dict] = []
    for c in (tool_calls or []):
        if isinstance(c, dict):
            tool = c.get("tool") or c.get("name") or c.get("fn") or ""
            args = c.get("args", c.get("arguments", {}))
        elif isinstance(c, (list, tuple)):
            tool = c[0] if len(c) > 0 else ""
            args = c[1] if len(c) > 1 else {}
        else:
            tool = str(c)
            args = {}
        out.append({"tool": str(tool), "args": args if isinstance(args, dict) else {"_": args}})
    return out


def _seq_hash(tool_calls: list[dict]) -> str:
    """Stable hash of the (tool, arg-KEYS) shape of a sequence. Arg VALUES are ignored:
    two runs that call the same tools in the same order with the same arg-keys but
    different live values are the SAME deterministic capability. That shape-stability is
    exactly what makes a task safe to crystallize."""
    shape = [[c["tool"], sorted((c.get("args") or {}).keys())] for c in tool_calls]
    return hashlib.sha1(json.dumps(shape, sort_keys=True).encode("utf-8")).hexdigest()[:12]


def _resolver_name(lane: str, norm_sig: str, seq_hash: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", norm_sig).strip("_")[:48] or "task"
    return f"cr_{lane}_{slug}_{seq_hash}"


# ---------------------------------------------------------------------------
# 1. record_solution — log a novel AI-resolved task
# ---------------------------------------------------------------------------

def record_solution(task_signature, approach, tool_calls, result,
                    lane=DEFAULT_LANE, meta=None, store: Optional[Store] = None) -> Optional[dict]:
    """Append one AI-resolved novel task + HOW it was solved. Fail-soft — EXCEPT a lane-
    firewall violation, which escalates loud (a work record must never reach the hustle repo)."""
    try:
        ln = _norm_lane(lane)
        st = _store(store, ln)
        _guard_lane_store(ln, st)
        st.ensure()
        seq = _norm_tool_calls(tool_calls)
        rec = {
            "at": _now(),
            "lane": ln,
            "signature": str(task_signature or ""),
            "norm_sig": _norm_sig(task_signature),
            "approach": str(approach or ""),
            "tool_calls": seq,
            "seq_hash": _seq_hash(seq),
            "result": result,
            "meta": meta or {},
        }
        with _lock:
            with st.candidates_log.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec
    except LaneFirewallError:
        raise  # never swallow an air-gap violation — escalate loud
    except Exception:
        return None


def _iter_candidates(store: Store):
    p = store.candidates_log
    if not p.exists():
        return
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except Exception:
                continue


# ---------------------------------------------------------------------------
# 2. crystallization_ready + promote
# ---------------------------------------------------------------------------

def crystallization_ready(signature, lane=DEFAULT_LANE, threshold=PROMOTE_THRESHOLD,
                        store: Optional[Store] = None) -> Optional[dict]:
    """Has (lane, signature) recurred `threshold`+ times with ONE STABLE sequence?

    Groups this signature's candidates by seq_hash. Ready only if the DOMINANT sequence
    alone has >= threshold occurrences — divergent sequences mean the solution isn't
    deterministic yet and must NOT be crystallized (a wrong crystallization would replay
    a bad sequence forever). Returns the proven sequence + a representative result, or None.
    """
    ln = _norm_lane(lane)
    st = _store(store, ln)
    ns = _norm_sig(signature)
    by_hash: dict[str, list[dict]] = {}
    for rec in _iter_candidates(st):
        if rec.get("lane") != ln or rec.get("norm_sig") != ns:
            continue
        by_hash.setdefault(rec.get("seq_hash") or "", []).append(rec)
    if not by_hash:
        return None
    seq_hash, recs = max(by_hash.items(), key=lambda kv: len(kv[1]))
    if len(recs) < int(threshold):
        return None
    proven = recs[-1]  # most recent instance of the stable sequence
    return {
        "ready": True,
        "lane": ln,
        "signature": proven.get("signature", ""),
        "norm_sig": ns,
        "seq_hash": seq_hash,
        "occurrences": len(recs),
        "sequence": proven.get("tool_calls", []),
        "sample_result": proven.get("result"),
        "approach": proven.get("approach", ""),
    }


def promote(candidate=None, signature=None, lane=None, threshold=PROMOTE_THRESHOLD,
            store: Optional[Store] = None) -> dict:
    """Generate a STAGED deterministic resolver stub for a recurring, stable task.

    `candidate` may be a dict from record_solution (carries signature + lane), or pass
    `signature`/`lane` explicitly. Confirms readiness via crystallization_ready first —
    never crystallizes an unproven / divergent solution.

    HUMAN-REVIEW GATE: the stub lands STAGED. It is NOT served by match() until a human
    calls activate(name). Returns a dict describing the outcome (never raises).
    """
    if isinstance(candidate, dict):
        signature = signature or candidate.get("signature")
        lane = lane or candidate.get("lane")
    ln = _norm_lane(lane)
    st = _store(store, ln)
    _guard_lane_store(ln, st)  # fail closed BEFORE any write if work-lane -> hustle repo
    st.ensure()

    ready = crystallization_ready(signature, lane=ln, threshold=threshold, store=st)
    if not ready:
        return {
            "promoted": False,
            "reason": "not crystallization-ready (needs a stable sequence recurring "
                      f">= {threshold}x in lane '{ln}')",
            "signature": signature,
            "lane": ln,
        }

    name = _resolver_name(ln, ready["norm_sig"], ready["seq_hash"])
    key = f"{ln}::{ready['norm_sig']}"

    # Registry read-modify-write is a single critical section: activate() mutates the same
    # file under _lock, so a lock-free RMW here would lose that update. Hold _lock across
    # the whole load->check->write->save so promote/activate/re-promote can't clobber.
    with _lock:
        reg = _load_registry(st)
        existing = reg["resolvers"].get(key)
        if existing and existing.get("seq_hash") == ready["seq_hash"]:
            # already crystallized on the same proven sequence — idempotent, don't churn it.
            return {
                "promoted": False,
                "already": True,
                "status": existing.get("status"),
                "name": existing.get("name"),
                "stub_path": existing.get("stub_path"),
                "reason": "already crystallized on this sequence",
            }

        stub_path = st.crystallized_dir / f"{name}.py"
        stub_path.write_text(_render_stub(name, ready))

        entry = {
            "name": name,
            "lane": ln,
            "signature": ready["signature"],
            "norm_sig": ready["norm_sig"],
            "seq_hash": ready["seq_hash"],
            "occurrences": ready["occurrences"],
            "stub_path": str(stub_path),
            "status": STATUS_STAGED,
            "promoted_at": _now(),
            "activated_at": None,
        }
        reg["resolvers"][key] = entry
        _save_registry(st, reg)
    return {
        "promoted": True,
        "crystallization_ready": True,
        "status": STATUS_STAGED,
        "name": name,
        "lane": ln,
        "stub_path": str(stub_path),
        "seq_hash": ready["seq_hash"],
        "occurrences": ready["occurrences"],
        "note": "STAGED for human review — call activate() to serve it deterministically",
    }


def _render_stub(name: str, ready: dict) -> str:
    seq_json = json.dumps(ready["sequence"], indent=2, ensure_ascii=False)
    res_json = json.dumps(ready["sample_result"], indent=2, ensure_ascii=False)
    return f'''#!/usr/bin/env python3
"""CRYSTALLIZED deterministic resolver (auto-generated — STAGED, review before activating).

signature   : {ready["signature"]!r}
lane        : {ready["lane"]}
seq_hash    : {ready["seq_hash"]}
occurrences : {ready["occurrences"]} (stable sequence recurred this many times)
approach    : {ready.get("approach", "")!r}

This replays a PROVEN, deterministic tool-call SHAPE (tool + arg-KEYS + order) so this
recurring task never again spends premium AI. The proven arg VALUES are only a template from
the run that trained it — resolve(task) fills each step's values from the CURRENT task so a
value-varying task (e.g. a different board item / recipient) is not served stale inputs.
Review SEQUENCE below, confirm it's still correct + deterministic, then:
    from core import capability_crystallization as cc
    cc.activate({name!r})

The module is INERT: resolve() returns the plan; it executes nothing itself. The caller runs
the tool_calls (all deterministic) and gets the crystallized result — $0, no AI.
"""
import json

SIGNATURE = {ready["signature"]!r}
LANE = {ready["lane"]!r}
SEQ_HASH = {ready["seq_hash"]!r}

_SEQUENCE_JSON = r"""{seq_json}"""
_RESULT_TEMPLATE_JSON = r"""{res_json}"""

SEQUENCE = json.loads(_SEQUENCE_JSON)          # proven ordered tool calls (KEY-shape is law)
RESULT_TEMPLATE = json.loads(_RESULT_TEMPLATE_JSON)  # shape of the proven result


def _current_overrides(task, n):
    """Per-step arg VALUES from the CURRENT task, keyed by step index. Accepts task with
    `tool_calls`/`args_by_step` (a per-step list of {{args}} or dicts) or a flat `args` dict
    applied to every step. Returns a list[dict] of length n (empty dicts where nothing given)."""
    out = [{{}} for _ in range(n)]
    if not isinstance(task, dict):
        return out
    steps = task.get("tool_calls") or task.get("args_by_step")
    if isinstance(steps, list):
        for i in range(min(n, len(steps))):
            s = steps[i]
            a = s.get("args", s) if isinstance(s, dict) else {{}}
            if isinstance(a, dict):
                out[i] = dict(a)
    flat = task.get("args")
    if isinstance(flat, dict):
        for i in range(n):
            merged = dict(flat)
            merged.update(out[i])  # explicit per-step wins over the flat default
            out[i] = merged
    return out


def resolve(task=None):
    """Deterministic replay. Keeps the PROVEN key-shape/sequence but fills arg values from
    the CURRENT `task` (falling back to the trained template only where the task is silent),
    so value-varying tasks aren't served stale inputs. Returns the plan; executes nothing."""
    overrides = _current_overrides(task, len(SEQUENCE))
    calls = []
    all_current = True
    for i, c in enumerate(SEQUENCE):
        proven_args = dict(c.get("args") or {{}})
        ov = overrides[i]
        args = {{}}
        for k in proven_args:  # PROVEN keys are law; only their values may change
            if k in ov:
                args[k] = ov[k]
            else:
                args[k] = proven_args[k]  # stale template value — task didn't supply this key
                all_current = False
        calls.append({{"tool": c["tool"], "args": args}})
    return {{
        "via": "crystallized",
        "resolver": {name!r},
        "signature": SIGNATURE,
        "lane": LANE,
        "seq_hash": SEQ_HASH,
        "tool_calls": calls,
        "values_from_task": all_current,  # False -> some values fell back to the trained template
        "result_template": RESULT_TEMPLATE,
        "deterministic": True,
    }}
'''


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def _load_registry(store: Store) -> dict:
    p = store.registry_path
    if p.exists():
        try:
            data = json.loads(p.read_text())
            data.setdefault("resolvers", {})
            return data
        except Exception:
            pass
    return {"resolvers": {}}


def _save_registry(store: Store, reg: dict) -> None:
    store.registry_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = store.registry_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(reg, indent=2, ensure_ascii=False))
    tmp.replace(store.registry_path)


def _lane_from_name(name: Any) -> Optional[str]:
    """Resolver names are cr_<lane>_<slug>_<hash>; recover the lane to pick its store."""
    parts = str(name or "").split("_")
    return parts[1] if len(parts) >= 2 and parts[1] in LANES else None


def activate(name, store: Optional[Store] = None, lane: Any = None) -> dict:
    """HUMAN-REVIEW GATE release valve: flip a staged resolver live. After this, match()
    serves it deterministically. Returns the updated entry (or an error dict)."""
    st = _store(store, lane if lane is not None else _lane_from_name(name))
    with _lock:
        reg = _load_registry(st)
        for key, entry in reg["resolvers"].items():
            if entry.get("name") == name:
                entry["status"] = STATUS_ACTIVE
                entry["activated_at"] = _now()
                _save_registry(st, reg)
                return {"activated": True, "name": name, "status": STATUS_ACTIVE}
    return {"activated": False, "reason": f"no resolver named {name!r}"}


def list_resolvers(store: Optional[Store] = None) -> list[dict]:
    if store is not None:
        return list(_load_registry(store)["resolvers"].values())
    # no explicit store -> merge both lane stores (work lives in CompanyA-local, hustle in repo)
    out: list[dict] = []
    for st in (DEFAULT_STORE, WORK_STORE):
        out.extend(_load_registry(st)["resolvers"].values())
    return out


# ---------------------------------------------------------------------------
# 3. match — deterministic-first lookup
# ---------------------------------------------------------------------------

@dataclass
class CrystallizedResolver:
    """A live, callable deterministic resolver. __call__(task) -> proven plan (no AI)."""
    name: str
    lane: str
    signature: str
    seq_hash: str
    status: str
    stub_path: str
    _resolve: Callable = field(repr=False, default=None)

    def resolve(self, task=None) -> dict:
        return self._resolve(task)

    def __call__(self, task=None) -> dict:
        return self._resolve(task)


def _load_stub(stub_path: str, name: str):
    spec = importlib.util.spec_from_file_location(f"core.crystallized.{name}", stub_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def match(task, lane=None, include_staged=False,
        store: Optional[Store] = None) -> Optional[CrystallizedResolver]:
    """Return a crystallized deterministic resolver for `task`, else None (caller -> AI).

    LANE-AWARE, FAIL-CLOSED: resolves the lane from the `lane` arg or the task itself. If the
    lane CANNOT be determined, returns None — a resolver is NEVER served without a known lane
    (a work resolver must never leak to an unlabeled task). A signature only matches within its
    own lane, served from that lane's own store.

    By default only ACTIVE (human-approved) resolvers are served — a STAGED stub does NOT
    short-circuit AI until a human activates it. Pass include_staged=True to see staged
    matches (e.g. to preview one), clearly labeled via .status.
    """
    ns = _norm_sig(_extract_signature(task))
    if not ns:
        return None
    want_lane = _norm_lane(lane) if lane else _extract_lane(task)
    if want_lane is None:
        return None  # fail CLOSED: no known lane -> never serve across the firewall

    st = _store(store, want_lane)
    reg = _load_registry(st)
    candidates = []
    for entry in reg["resolvers"].values():
        if entry.get("norm_sig") != ns:
            continue
        if not include_staged and entry.get("status") != STATUS_ACTIVE:
            continue
        if include_staged and entry.get("status") not in (STATUS_ACTIVE, STATUS_STAGED):
            continue
        if entry.get("lane") != want_lane:
            continue
        candidates.append(entry)

    if not candidates:
        return None
    # prefer active over staged if both somehow match
    candidates.sort(key=lambda e: 0 if e.get("status") == STATUS_ACTIVE else 1)
    entry = candidates[0]

    try:
        mod = _load_stub(entry["stub_path"], entry["name"])
    except Exception:
        return None
    return CrystallizedResolver(
        name=entry["name"],
        lane=entry["lane"],
        signature=entry.get("signature", ""),
        seq_hash=entry.get("seq_hash", ""),
        status=entry.get("status", ""),
        stub_path=entry["stub_path"],
        _resolve=getattr(mod, "resolve"),
    )


# ---------------------------------------------------------------------------
# Smoke test — no live services, no AI. Runs against a throwaway store.
# ---------------------------------------------------------------------------

def _smoke() -> int:
    import shutil
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="crystallize_smoke_"))
    store = Store(
        candidates_log=tmp / "candidates.jsonl",
        registry_path=tmp / "registry.json",
        crystallized_dir=tmp / "crystallized",
    ).ensure()

    fails: list[str] = []

    SIG = "summarize the weekly monday board and post a digest"
    LANE = "work"
    # the PROVEN deterministic tool-call sequence (same shape every time; live values vary)
    proven_calls = [
        {"tool": "monday.read_board", "args": {"board_id": "XPHONEX"}},
        {"tool": "text.summarize", "args": {"style": "digest"}},
        {"tool": "monday.post_update", "args": {"item_id": "X", "body": "..."}},
    ]

    # ---- record the SAME task 3x (AI solved it each time, stable sequence) ----
    for i in range(3):
        rec = record_solution(
            task_signature=SIG,
            approach="read board -> summarize -> post digest",
            tool_calls=proven_calls,
            result={"posted": True, "items": 12 + i},  # live values differ, shape stable
            lane=LANE,
            store=store,
        )
        if rec is None:
            fails.append(f"record_solution #{i} returned None")

    # a DIFFERENT signature in a DIFFERENT lane — must not contaminate readiness
    record_solution("draft a cold email for a broken site", "compose", [
        {"tool": "web.probe", "args": {"url": "x"}},
        {"tool": "email.draft", "args": {"to": "y"}},
    ], {"drafted": True}, lane="hustle", store=store)

    # ---- readiness: only 3x-stable signature is ready ----
    ready = crystallization_ready(SIG, lane=LANE, store=store)
    if not ready or not ready.get("ready"):
        fails.append("crystallization_ready did NOT flag the 3x-recurring task")
    elif ready["occurrences"] != 3:
        fails.append(f"expected 3 occurrences, got {ready['occurrences']}")

    not_ready = crystallization_ready("draft a cold email for a broken site",
                                    lane="hustle", store=store)
    if not_ready is not None:
        fails.append("single-occurrence task should NOT be crystallization-ready")

    # ---- match BEFORE promote: nothing crystallized yet ----
    if match(SIG, lane=LANE, store=store) is not None:
        fails.append("match() returned a resolver before any promotion")

    # ---- promote: emits a STAGED stub matching the proven sequence ----
    out = promote(signature=SIG, lane=LANE, store=store)
    if not out.get("promoted"):
        fails.append(f"promote() did not promote: {out}")
    if out.get("status") != STATUS_STAGED:
        fails.append(f"promoted stub should be STAGED not {out.get('status')}")
    stub_path = out.get("stub_path", "")
    if not stub_path or not Path(stub_path).exists():
        fails.append("promote() did not emit a resolver stub file")

    # the emitted stub must replay the PROVEN sequence (shape-for-shape)
    if stub_path and Path(stub_path).exists():
        mod = _load_stub(stub_path, out["name"])
        got = [(c["tool"], sorted(c["args"].keys())) for c in mod.SEQUENCE]
        want = [(c["tool"], sorted(c["args"].keys())) for c in _norm_tool_calls(proven_calls)]
        if got != want:
            fails.append(f"stub SEQUENCE does not match the proven sequence: {got} != {want}")
        if mod.SEQ_HASH != ready["seq_hash"]:
            fails.append("stub seq_hash mismatch")

    # ---- human-review gate: staged stub is NOT served by default match() ----
    if match(SIG, lane=LANE, store=store) is not None:
        fails.append("HUMAN GATE BROKEN: staged stub served by default match()")
    # but is previewable with include_staged
    prev = match(SIG, lane=LANE, include_staged=True, store=store)
    if prev is None or prev.status != STATUS_STAGED:
        fails.append("include_staged match() should preview the staged resolver")

    # ---- activate (simulate human review) then match() returns it ----
    act = activate(out["name"], store=store)
    if not act.get("activated"):
        fails.append(f"activate() failed: {act}")

    resolver = match(SIG, lane=LANE, store=store)
    if resolver is None:
        fails.append("match() returned None AFTER activation")
    else:
        if resolver.status != STATUS_ACTIVE:
            fails.append("matched resolver not active")
        plan = resolver.resolve({"task_signature": SIG, "lane": LANE})
        if plan.get("via") != "crystallized" or not plan.get("deterministic"):
            fails.append("resolver.resolve() did not return a deterministic crystallized plan")
        got = [(c["tool"], sorted(c["args"].keys())) for c in plan.get("tool_calls", [])]
        want = [(c["tool"], sorted(c["args"].keys())) for c in _norm_tool_calls(proven_calls)]
        if got != want:
            fails.append("resolver plan tool_calls != proven sequence")

    # ---- lane firewall: same signature, WRONG lane -> no match ----
    if match(SIG, lane="hustle", store=store) is not None:
        fails.append("LANE FIREWALL BROKEN: work resolver matched a hustle request")

    # ---- FAIL CLOSED: no known lane -> never serve (even with an active work resolver) ----
    if match(SIG, store=store) is not None:
        fails.append("FAIL-OPEN: match() with NO lane served a resolver (must fail closed)")
    if match({"task_signature": SIG}, store=store) is not None:
        fails.append("FAIL-OPEN: match() served a lane-less task dict (must fail closed)")

    # ---- AIR-GAP: work-lane default routing lands under CompanyA-local, NEVER the repo ----
    work_st = _store(None, "work")  # == WORK_STORE
    for p in (work_st.candidates_log, work_st.registry_path, work_st.crystallized_dir):
        rp = Path(p).resolve()
        if HUSTLE_ROOT == rp or HUSTLE_ROOT in rp.parents:
            fails.append(f"AIR-GAP BREAK: work store path under the GitHub-mirrored repo: {rp}")
        if not (WORK_ROOT == rp or WORK_ROOT in rp.parents):
            fails.append(f"work store path is NOT under CompanyA-local: {rp}")

    # ---- the hard guard REFUSES a work record into a repo-rooted store (raise, not heal) ----
    repo_store = Store(
        candidates_log=BASE_DIR / "data" / "learning" / "crystallization_candidates.jsonl",
        registry_path=BASE_DIR / "data" / "learning" / "crystallized_registry.json",
        crystallized_dir=BASE_DIR / "core" / "crystallized",
    )
    try:
        record_solution("some work task", "x", proven_calls, {"ok": True},
                        lane="work", store=repo_store)
        fails.append("GUARD FAILED: work record into the GitHub-mirrored repo was allowed")
    except LaneFirewallError:
        pass  # correct: escalated loud, did not silently heal
    # promote() must fail closed the same way
    try:
        promote(signature="some work task", lane="work", store=repo_store)
        fails.append("GUARD FAILED: promote() into the GitHub-mirrored repo was allowed")
    except LaneFirewallError:
        pass
    # and a HUSTLE record into the repo store is fine (the repo IS the hustle lane).
    # Guard-only check (no write) so the smoke never touches the live repo candidates file.
    try:
        _guard_lane_store("hustle", repo_store)
    except LaneFirewallError:
        fails.append("GUARD OVER-BLOCKED: hustle record into the repo store must be allowed")

    # ---- resolve() fills values from the CURRENT task, not the trained template ----
    if resolver is not None:
        live = resolver.resolve({
            "task_signature": SIG, "lane": LANE,
            "tool_calls": [
                {"tool": "monday.read_board", "args": {"board_id": "XPHONEX"}},
                {"tool": "text.summarize", "args": {"style": "digest"}},
                {"tool": "monday.post_update", "args": {"item_id": "999", "body": "fresh"}},
            ],
        })
        posted = next((c for c in live["tool_calls"] if c["tool"] == "monday.post_update"), {})
        if posted.get("args", {}).get("item_id") != "999":
            fails.append("resolve() ignored the CURRENT task's arg values (stale replay)")
        if not live.get("values_from_task"):
            fails.append("resolve() should flag values_from_task=True when task supplied them")

    # ---- idempotent re-promote on the same sequence (registry RMW under _lock) ----
    again = promote(signature=SIG, lane=LANE, store=store)
    if again.get("promoted"):
        fails.append("re-promote on identical sequence should be idempotent (no churn)")
    if not again.get("already"):
        fails.append("re-promote should report already-crystallized (idempotent)")

    if fails:
        print("SMOKE FAIL:")
        for m in fails:
            print("  -", m)
        rc = 1
    else:
        print("SMOKE OK — novel task recorded 3x -> crystallization_ready -> promote() emitted")
        print("           a STAGED resolver stub replaying the proven sequence; human gate held")
        print("           (staged NOT served); after activate() match() returned the")
        print("           deterministic $0 resolver; lane firewall + idempotency held.")
        print(f"  resolver: {resolver.name}  lane={resolver.lane}  seq_hash={resolver.seq_hash}")
        print(f"  plan tool_calls: {[c['tool'] for c in resolver.resolve()['tool_calls']]}")
        rc = 0

    try:
        shutil.rmtree(tmp, ignore_errors=True)
    except Exception:
        pass
    return rc


if __name__ == "__main__":
    raise SystemExit(_smoke())
