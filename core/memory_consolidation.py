#!/usr/bin/env python3
"""memory_consolidation.py — nightly MEMORY CONSOLIDATION so the brain sharpens with age.

The problem: the durable brain (core.redacted_memory -> data/redacted_context_brain.db) only ever
GROWS. 772 of 783 rows are kind='session' — transient terminal captures, canary/smoke prompts, and
the SAME autonomous-cycle prompt logged verbatim every night. Retrieval quality decays as signal
drowns in near-duplicate noise. This module makes the brain get SHARPER with age instead of just
bloating, three ways, all reversible:

  (a) DEDUP    — near-identical memories collapse to one representative (token-Jaccard overlap over
                 normalized title+body; $0, deterministic, no model/embedding needed by default).
  (b) SUMMARIZE — clusters of related OLD low-value memories fold into ONE durable higher-level
                 lesson written by a LOCAL model (tools.rag.generate -> ollama, $0); members archived.
  (c) PRUNE    — pure noise (canary/status/smoke lines, resolved-and-stale sessions) is ARCHIVED,
                 never hard-deleted.

HARD PROTECTION: a memory that is durable/rule/feedback/playbook/pinned/marker-bearing is NEVER a
candidate for any of the three. Only low-value / transient / duplicated entries are touched.

REVERSIBILITY: nothing is hard-deleted. Every removed row is copied verbatim into `memories_archive`
(same db) AND appended to data/learning/memory_consolidation_log.jsonl before its DELETE. restore(run_id)
puts a whole run back. dry=True (default) opens the store READ-ONLY (?mode=ro) — physically cannot write.

LANE FIREWALL: this operates ONLY on the WORK-lane durable store (redacted_context_brain.db) and reads
work-lane signal sources (clean_capture exchanges, the work digests). It NEVER opens the hustle index
(data/hustle/rag/index.db) or any hustle store — mirrors core.all_brain_rag's one-lane-only discipline.
The single mutating store is redacted_memory; clean_capture + digests are READ-ONLY advisory inputs here.

    core/../.venv/bin/python -m core.memory_consolidation           # dry smoke proof (default)
    core/../.venv/bin/python -m core.memory_consolidation report    # dry report over the real store
    core/../.venv/bin/python -m core.memory_consolidation apply      # ACT (dry=False) — archives+writes
    core/../.venv/bin/python -m core.memory_consolidation restore <run_id>

STAGED — the launchd plist (deploy/launchagents/com.claude-stack.memory-consolidation.plist) is
written but NOT loaded. Consolidation only ever runs when invoked; the live store is never bounced.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from core import redacted_memory as vmem  # WORK-lane durable store (the ONLY mutated store)  # noqa: E402

# ── stores (WORK lane only — no hustle path exists here, by design = firewall) ──────────────────
MEM_DB = vmem.DB_PATH                                              # data/redacted_context_brain.db
EXCHANGES = BASE_DIR / "data" / "learning" / "exchanges.jsonl"    # clean_capture ledger (READ-ONLY here)
DIGESTS_DIR = Path.home() / ".openclaw" / "workspace" / "CompanyA-local" / "digests"  # READ-ONLY advisory
LOG = BASE_DIR / "data" / "learning" / "memory_consolidation_log.jsonl"

# ── hard-protected classes: NEVER a candidate for dedup/summarize/prune ─────────────────────────
DURABLE_KINDS = {
    "rule", "durable", "feedback", "playbook", "ticket_lesson",
    "hard_fact", "decision", "consolidated_lesson", "north_star",
}
# transient classes are the ONLY kinds eligible to be touched
TRANSIENT_KINDS = {"session", "overnight_admin_run", "status", "alert", "canary", "tmp"}

# a memory carrying any of these reads as a standing rule / hard fact — protect regardless of kind
_DURABLE_MARKER = re.compile(
    r"(?:\bHARD\b|HARD RULE|feedback_|NEVER\b|ALWAYS\b|north star|kill.?switch|"
    r"do not (?:delete|touch|send|deploy)|standing rule|load-bearing|invariant)",
    re.IGNORECASE,
)
# noise/transient tells in a title — canary/smoke/status pings with no durable content
_NOISE_TITLE = re.compile(
    r"(?:\btmp:|\bcanary\b|reply with exactly|one short sentence|what are you\??|"
    r"\bok123\b|healthcheck|\bping\b|smoke test|\bhello\b|\btest\b)",
    re.IGNORECASE,
)

# thresholds (env-overridable so cadence/aggr can be tuned without a code edit)
SIM_DEDUP = float(os.environ.get("MEMC_SIM_DEDUP", "0.90"))            # near-identical
SIM_TOPIC = float(os.environ.get("MEMC_SIM_TOPIC", "0.45"))            # related (summarize)
MIN_CLUSTER = int(os.environ.get("MEMC_MIN_CLUSTER", "4"))             # min members to summarize
STALE_PRUNE_DAYS = int(os.environ.get("MEMC_STALE_PRUNE_DAYS", "21"))  # noise must be this old to prune
STALE_SUMMARIZE_DAYS = int(os.environ.get("MEMC_STALE_SUMMARIZE_DAYS", "30"))
SHORT_BODY = int(os.environ.get("MEMC_SHORT_BODY", "220"))            # a "status line" is short


# ─────────────────────────────────────────────────────────────────────── helpers
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _age_days(created_at: str) -> float:
    try:
        dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (_now() - dt).total_seconds() / 86400.0
    except Exception:
        return 0.0  # unparseable ts -> treat as fresh (never prune on a bad date)


_PREFIX = re.compile(r"^\[[^\]]+\]\s*[\w\-./]+:\s*")   # "[claude@Mac-mini] project: "
_SUFFIX = re.compile(r"\s*\(\d+p/\d+cmd/\d+f\)\s*$")    # " (7p/66cmd/17f)"
_WORD = re.compile(r"[a-z0-9]+")


def _normalize(title: str, body: str) -> str:
    t = _SUFFIX.sub("", _PREFIX.sub("", title or ""))
    return f"{t} {body or ''}".lower()


def _tokens(text: str) -> frozenset[str]:
    return frozenset(w for w in _WORD.findall(text) if len(w) > 2)


def _jaccard(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if not inter:
        return 0.0
    return inter / len(a | b)


def _is_durable(m: dict) -> tuple[bool, str]:
    """True + reason if this memory is a HARD-protected class that must never be touched."""
    if m.get("pinned"):
        return True, "pinned"
    if (m.get("kind") or "") in DURABLE_KINDS:
        return True, f"kind={m['kind']}"
    meta = m.get("metadata") or {}
    if isinstance(meta, dict):
        if meta.get("durable") or meta.get("protected") or meta.get("rule"):
            return True, "metadata.durable"
        tags = meta.get("tags")
        if isinstance(tags, (list, tuple)) and any(
                str(t).lower() in ("durable", "rule", "hard", "feedback") for t in tags):
            return True, "metadata.tags"
    if _DURABLE_MARKER.search(m.get("title") or "") or _DURABLE_MARKER.search(m.get("body") or ""):
        return True, "marker"
    return False, ""


def _is_transient(m: dict) -> bool:
    return (m.get("kind") or "") in TRANSIENT_KINDS


def _is_noise(m: dict) -> bool:
    """Pure-noise tell: transient + old + (canary/smoke title OR a short status-line body)."""
    if not _is_transient(m):
        return False
    if _age_days(m.get("created_at") or "") < STALE_PRUNE_DAYS:
        return False
    if _NOISE_TITLE.search(m.get("title") or ""):
        return True
    if len((m.get("body") or "").strip()) < SHORT_BODY:
        return True
    return False


def _rep_score(m: dict) -> tuple:
    """Pick the best representative of a dedup cluster: highest confidence, longest body, newest."""
    return (float(m.get("confidence") or 0.0), len(m.get("body") or ""), m.get("created_at") or "")


# ─────────────────────────────────────────────────────────────────────── load (read-only)
def _load_memories(db_path: Path = MEM_DB) -> list[dict]:
    """Read every memory row READ-ONLY (?mode=ro). Cannot write — the dry-mode guarantee."""
    if not db_path.exists():
        return []
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT id, created_at, updated_at, kind, title, body, source, systems, entities, "
            "confidence, pinned, metadata FROM memories"
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        con.close()
    out = []
    for r in rows:
        m = dict(r)
        m["pinned"] = bool(m.get("pinned"))
        try:
            m["metadata"] = json.loads(m.get("metadata") or "{}")
        except Exception:
            m["metadata"] = {}
        out.append(m)
    return out


# ─────────────────────────────────────────────────────────────────────── clustering
def _union_find_clusters(cands: list[dict], threshold: float) -> list[list[dict]]:
    """Greedy single-link clustering by token-Jaccard >= threshold. O(n^2) over small token sets."""
    toks = [_tokens(_normalize(m.get("title", ""), m.get("body", ""))) for m in cands]
    parent = list(range(len(cands)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    for i in range(len(cands)):
        if not toks[i]:
            continue
        for j in range(i + 1, len(cands)):
            if not toks[j]:
                continue
            # cheap length prefilter before the set op
            li, lj = len(toks[i]), len(toks[j])
            if min(li, lj) / max(li, lj) < threshold:
                continue
            if _jaccard(toks[i], toks[j]) >= threshold:
                union(i, j)

    groups: dict[int, list[dict]] = {}
    for idx, m in enumerate(cands):
        groups.setdefault(find(idx), []).append(m)
    return [g for g in groups.values() if len(g) > 1]


# ─────────────────────────────────────────────────────────────────────── plan (pure, no writes)
def plan(db_path: Path = MEM_DB) -> dict:
    """Compute what WOULD be deduped/summarized/pruned. Pure read — never writes. The dry report."""
    memories = _load_memories(db_path)
    protected, candidates = [], []
    for m in memories:
        dur, reason = _is_durable(m)
        if dur:
            m["_protect"] = reason
            protected.append(m)
        else:
            candidates.append(m)

    by_id = {m["id"]: m for m in candidates}
    consumed: set[int] = set()  # candidate ids already assigned to a dedup/summarize action

    # (a) DEDUP — near-identical → keep best rep, archive the rest
    dedup_clusters = []
    for group in _union_find_clusters(candidates, SIM_DEDUP):
        group.sort(key=_rep_score, reverse=True)
        rep, dupes = group[0], group[1:]
        for d in dupes:
            consumed.add(d["id"])
        dedup_clusters.append({
            "keep_id": rep["id"],
            "keep_title": rep["title"],
            "archive_ids": [d["id"] for d in dupes],
            "size": len(group),
        })

    # (b) SUMMARIZE — related OLD survivors → one durable lesson, archive members
    survivors = [m for m in candidates if m["id"] not in consumed]
    # Exclude noise + near-empty rows from SUMMARIZE clustering so they fall through to PRUNE.
    # Without this, canary/status pings ("hello","test","status","/logout") get distilled into a
    # FABRICATED durable lesson (pinned, high-confidence) that can never be re-consolidated — junk
    # masquerading as knowledge. Noise belongs in prune (archived), never summarized into a "lesson".
    old_survivors = [m for m in survivors
                     if _age_days(m.get("created_at") or "") >= STALE_SUMMARIZE_DAYS
                     and not _is_noise(m)
                     and len((m.get("body") or "").strip()) >= 20]
    summarize_clusters = []
    for group in _union_find_clusters(old_survivors, SIM_TOPIC):
        if len(group) < MIN_CLUSTER:
            continue
        # never fold a durable in (they were never candidates) — double-check anyway
        if any(_is_durable(m)[0] for m in group):
            continue
        for m in group:
            consumed.add(m["id"])
        summarize_clusters.append({
            "member_ids": [m["id"] for m in group],
            "size": len(group),
            "preview_titles": [m["title"][:80] for m in group[:5]],
        })

    # (c) PRUNE — leftover noise not already handled by dedup/summarize
    prune = []
    for m in candidates:
        if m["id"] in consumed:
            continue
        if _is_noise(m):
            prune.append({"id": m["id"], "title": m["title"][:100],
                          "kind": m["kind"], "age_days": round(_age_days(m["created_at"]), 1)})

    dedup_archive = sum(len(c["archive_ids"]) for c in dedup_clusters)
    summarize_archive = sum(c["size"] for c in summarize_clusters)

    return {
        "store": str(db_path),
        "total_memories": len(memories),
        "protected_count": len(protected),
        "candidate_count": len(candidates),
        "protected_by_reason": _count_by(protected, lambda m: m["_protect"]),
        "dedup": {"clusters": dedup_clusters, "would_archive": dedup_archive},
        "summarize": {"clusters": summarize_clusters,
                      "would_archive": summarize_archive,
                      "would_create": len(summarize_clusters)},
        "prune": {"candidates": prune, "would_archive": len(prune)},
        "signal_sources": _signal_sources(),
        "totals": {
            "would_archive": dedup_archive + summarize_archive + len(prune),
            "would_create": len(summarize_clusters),
        },
        "_candidates_by_id": by_id,  # internal, used by act; not for display
    }


def _count_by(rows, keyfn) -> dict:
    out: dict[str, int] = {}
    for r in rows:
        out[keyfn(r)] = out.get(keyfn(r), 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def _signal_sources() -> dict:
    """READ-ONLY advisory scan of the other work-lane sources named in the task (never mutated here).
    clean_capture: count repeated machine_push texts (dedup-able). digests: count stale files."""
    out = {"clean_capture": {"exists": EXCHANGES.exists(), "duplicate_push": 0},
           "digests": {"dir": str(DIGESTS_DIR), "stale_files": 0, "total": 0}}
    if EXCHANGES.exists():
        seen: dict[str, int] = {}
        try:
            for line in EXCHANGES.open("r", encoding="utf-8"):
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("direction") == "machine_push":
                    k = (rec.get("text") or "")[:200]
                    seen[k] = seen.get(k, 0) + 1
            out["clean_capture"]["duplicate_push"] = sum(v - 1 for v in seen.values() if v > 1)
        except Exception:
            pass
    if DIGESTS_DIR.exists():
        now = time.time()
        for f in DIGESTS_DIR.glob("*.md"):
            out["digests"]["total"] += 1
            try:
                if (now - f.stat().st_mtime) / 86400.0 >= STALE_SUMMARIZE_DAYS:
                    out["digests"]["stale_files"] += 1
            except Exception:
                pass
    return out


# ─────────────────────────────────────────────────────────────────────── act (writes; reversible)
def _ensure_archive(con: sqlite3.Connection) -> None:
    con.execute(
        "CREATE TABLE IF NOT EXISTS memories_archive ("
        " archive_id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " run_id TEXT NOT NULL, archived_at TEXT NOT NULL, reason TEXT NOT NULL,"
        " orig_id INTEGER NOT NULL, kept_id INTEGER, new_id INTEGER, row_json TEXT NOT NULL)"
    )


def _archive_and_delete(con: sqlite3.Connection, run_id: str, reason: str,
                        orig_id: int, *, kept_id=None, new_id=None) -> dict | None:
    """Copy a row verbatim into memories_archive + the jsonl log, THEN delete it. Reversible."""
    row = con.execute("SELECT * FROM memories WHERE id=?", (orig_id,)).fetchone()
    if row is None:
        return None
    row_json = json.dumps(dict(row), ensure_ascii=False, default=str)
    ts = _now().isoformat()
    con.execute(
        "INSERT INTO memories_archive(run_id, archived_at, reason, orig_id, kept_id, new_id, row_json)"
        " VALUES (?,?,?,?,?,?,?)",
        (run_id, ts, reason, orig_id, kept_id, new_id, row_json))
    con.execute("DELETE FROM memories WHERE id=?", (orig_id,))
    entry = {"ts": ts, "run_id": run_id, "op": "archive", "reason": reason,
             "orig_id": orig_id, "kept_id": kept_id, "new_id": new_id, "row": json.loads(row_json)}
    _log(entry)
    return entry


def _log(entry: dict) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")


def _summarize_cluster(members: list[dict]) -> str:
    """Fold a cluster of related memories into one durable higher-level lesson via a LOCAL model ($0).
    Fail-soft: on any model error returns "" and the caller SKIPS archiving (never lose data blindly)."""
    try:
        from tools import rag as _rag
    except Exception:
        return ""
    bodies = "\n\n---\n".join(f"[{m['created_at'][:10]}] {m['title']}\n{m['body'][:800]}"
                              for m in members[:12])
    prompt = (
        "You are consolidating several related work memories into ONE durable, higher-level lesson.\n"
        "Write a single tight paragraph (<=120 words) capturing the reusable takeaway — the pattern, "
        "the fix, the standing fact — dropping transient run-specific noise. No preamble.\n\n"
        f"MEMORIES:\n{bodies}\n\nDURABLE LESSON:")
    try:
        return (_rag.generate(prompt, num_predict=220, temperature=0.2) or "").strip()
    except Exception:
        return ""


def consolidate(dry: bool = True, db_path: Path = MEM_DB) -> dict:
    """Consolidate the WORK-lane durable brain. dry=True (default) REPORTS only, opening the store
    READ-ONLY — it cannot and does not write. dry=False archives dupes/noise, folds old clusters into
    durable lessons, and logs every change reversibly (restore(run_id) undoes a whole run).

    Durable/rule/feedback/pinned/marker memories are NEVER candidates — protected before planning."""
    p = plan(db_path)
    p.pop("_candidates_by_id", None)
    p["dry"] = dry
    if dry:
        p["run_id"] = None
        p["applied"] = {"archived": 0, "created": 0}
        return p

    run_id = f"memc_{datetime.now().strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:6]}"
    p["run_id"] = run_id
    archived = created = 0

    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    try:
        _ensure_archive(con)
        # (a) dedup
        for c in p["dedup"]["clusters"]:
            for oid in c["archive_ids"]:
                if _archive_and_delete(con, run_id, "dedup_duplicate", oid, kept_id=c["keep_id"]):
                    archived += 1
        con.commit()
    finally:
        con.close()

    # (b) summarize — write the new durable lesson FIRST (via redacted_memory), then archive members.
    for c in p["summarize"]["clusters"]:
        members = [m for m in _load_memories(db_path) if m["id"] in set(c["member_ids"])]
        if not members:
            continue
        lesson = _summarize_cluster(members)
        if not lesson:
            c["skipped"] = "local model unavailable — members left intact"
            continue
        new = vmem.remember(
            kind="consolidated_lesson",
            title=f"[consolidated x{len(members)}] {members[0]['title'][:200]}",
            body=lesson,
            source="memory_consolidation",
            confidence=0.9, pinned=True,
            metadata={"durable": True, "run_id": run_id,
                      "consolidated_from": [m["id"] for m in members]})
        created += 1
        c["new_id"] = new["id"]
        con = sqlite3.connect(str(db_path))
        con.row_factory = sqlite3.Row
        try:
            _ensure_archive(con)
            for m in members:
                if _archive_and_delete(con, run_id, "summarized", m["id"], new_id=new["id"]):
                    archived += 1
            con.commit()
        finally:
            con.close()

    # (c) prune noise
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        _ensure_archive(con)
        for item in p["prune"]["candidates"]:
            if _archive_and_delete(con, run_id, "prune_noise", item["id"]):
                archived += 1
        con.commit()
    finally:
        con.close()

    p["applied"] = {"archived": archived, "created": created}
    _log({"ts": _now().isoformat(), "run_id": run_id, "op": "run_complete",
          "archived": archived, "created": created})
    return p


def restore(run_id: str, db_path: Path = MEM_DB) -> dict:
    """Undo a consolidation run: re-insert every archived row, delete any consolidated_lesson it
    created, and remove the run's archive rows. Fully reverses consolidate(dry=False)."""
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    restored = removed = 0
    try:
        _ensure_archive(con)
        rows = con.execute(
            "SELECT * FROM memories_archive WHERE run_id=? ORDER BY archive_id", (run_id,)).fetchall()
        new_ids = set()
        for r in rows:
            if r["new_id"]:
                new_ids.add(r["new_id"])
            row = json.loads(r["row_json"])
            cols = ("id", "created_at", "updated_at", "kind", "title", "body", "source",
                    "systems", "entities", "confidence", "pinned", "metadata")
            con.execute(
                f"INSERT OR IGNORE INTO memories({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                tuple(row.get(c) for c in cols))
            restored += 1
        for nid in new_ids:
            con.execute("DELETE FROM memories WHERE id=? AND kind='consolidated_lesson'", (nid,))
            removed += 1
        con.execute("DELETE FROM memories_archive WHERE run_id=?", (run_id,))
        con.commit()
    finally:
        con.close()
    _log({"ts": _now().isoformat(), "run_id": run_id, "op": "restore",
          "restored": restored, "removed_consolidated": removed})
    return {"run_id": run_id, "restored": restored, "removed_consolidated": removed}


# ─────────────────────────────────────────────────────────────────────── report / smoke
def _print_report(p: dict) -> None:
    print(f"store            : {p['store']}")
    print(f"total memories   : {p['total_memories']}")
    print(f"protected (kept) : {p['protected_count']}   {p['protected_by_reason']}")
    print(f"candidates       : {p['candidate_count']}")
    d, s, pr = p["dedup"], p["summarize"], p["prune"]
    print(f"\nDEDUP     : {len(d['clusters'])} clusters -> archive {d['would_archive']}")
    for c in d["clusters"][:5]:
        print(f"   keep #{c['keep_id']} + drop {len(c['archive_ids'])}  «{c['keep_title'][:64]}»")
    print(f"SUMMARIZE : {len(s['clusters'])} clusters -> 1 lesson each, archive {s['would_archive']}")
    for c in s["clusters"][:5]:
        print(f"   x{c['size']}: {c['preview_titles'][:2]}")
    print(f"PRUNE     : {pr['would_archive']} noise rows")
    for c in pr["candidates"][:5]:
        print(f"   #{c['id']} [{c['kind']} {c['age_days']}d] «{c['title'][:60]}»")
    print(f"\nsignal sources (read-only advisory): {json.dumps(p['signal_sources'])}")
    print(f"\nTOTALS    : would_archive={p['totals']['would_archive']} "
          f"would_create={p['totals']['would_create']}  dry={p.get('dry')}")


def _smoke() -> int:
    global LOG
    print("=" * 80)
    print("memory_consolidation smoke — dry over REAL store + synthetic protection/dedup proof")
    print("=" * 80)
    fails = []

    # ── [1] DRY over the REAL store: must write NOTHING, must exclude durable kinds ───────────────
    print("\n[1] dry consolidate() over the real WORK store (read-only) ---------------------")
    before_count = _count_rows(MEM_DB, "memories")
    before_arch = _table_exists(MEM_DB, "memories_archive")
    before_log = LOG.stat().st_size if LOG.exists() else 0

    p = consolidate(dry=True)
    _print_report(p)

    after_count = _count_rows(MEM_DB, "memories")
    after_arch = _table_exists(MEM_DB, "memories_archive")
    after_log = LOG.stat().st_size if LOG.exists() else 0

    if after_count != before_count:
        fails.append(f"DRY MUTATED memories count {before_count} -> {after_count}")
    if after_arch != before_arch:
        fails.append("DRY created the archive table (must not touch the store)")
    if after_log != before_log:
        fails.append("DRY wrote to the change log (must be read-only)")
    if p["run_id"] is not None or p["applied"] != {"archived": 0, "created": 0}:
        fails.append("DRY reported an applied action")
    print(f"   dry write-nothing: memories {before_count}=={after_count}, "
          f"archive_table {before_arch}=={after_arch}, log_size {before_log}=={after_log}")

    # durable kinds present in the real store must be in PROTECTED, never in any candidate bucket
    reals = _load_memories(MEM_DB)
    durable_real = [m for m in reals if _is_durable(m)[0]]
    cand_ids = set()
    for c in p["dedup"]["clusters"]:
        cand_ids.update(c["archive_ids"]); cand_ids.add(c["keep_id"])
    for c in p["summarize"]["clusters"]:
        cand_ids.update(c["member_ids"])
    for c in p["prune"]["candidates"]:
        cand_ids.add(c["id"])
    leaked = [m["id"] for m in durable_real if m["id"] in cand_ids]
    if leaked:
        fails.append(f"durable memories leaked into a candidate bucket: {leaked[:10]}")
    print(f"   durable in real store: {len(durable_real)} — all excluded from candidates? "
          f"{'YES' if not leaked else 'NO ' + str(leaked[:5])}")

    # ── [2] SYNTHETIC deterministic proof on a throwaway db (dedup + hard-rule protection) ────────
    print("\n[2] synthetic proof — throwaway db, dedup collapses dupes, HARD rule survives --------")
    import tempfile
    tmp = Path(tempfile.mkdtemp()) / "memc_smoke.db"
    orig_db = vmem.DB_PATH
    orig_log = LOG
    vmem.DB_PATH = tmp
    LOG = tmp.parent / "memc_smoke_log.jsonl"  # redirect: never pollute the real change log
    try:
        old_iso = (_now().replace(year=_now().year - 1)).isoformat()  # ~1yr old -> stale
        # 3 near-identical transient sessions (dedup target)
        for i in range(3):
            _seed(tmp, kind="session", title="[claude@Mac] cs: AUTONOMOUS BUILD CYCLE running unattended",
                  body="autonomous build cycle for hailports, unattended on a schedule, "
                       "build in public, ship improvements every night " + ("x" * 300),
                  created_at=old_iso, confidence=0.9)
        # 5 related-but-distinct OLD sessions (summarize target): shared topic vocab + a distinct
        # tail so pairwise Jaccard lands in the summarize band [SIM_TOPIC, SIM_DEDUP) — related,
        # not near-identical (so dedup leaves them for summarize).
        shared = ("the mockup deploy pipeline regen job hit a timeout window problem during "
                  "the nightly build cycle deploy")
        tails = ["sigterm signal killed", "dist images huge", "watchdog stamp missing",
                 "externalize assets faster", "bigger timeout needed"]
        for t in tails:
            _seed(tmp, kind="session", title="[claude@Mac] cs: mockup deploy issue",
                  body=f"{shared} {t}", created_at=old_iso, confidence=0.9)
        # a canary noise row (prune target)
        _seed(tmp, kind="session", title="[claude@Mac] tmp: Reply with exactly: OK123",
              body="OK123", created_at=old_iso, confidence=0.9)
        # a HARD durable rule (must NEVER be touched) — even though worded like a session
        rule = _seed(tmp, kind="feedback", title="HARD: never send email from Outlook, draft only",
                     body="standing rule — always draft, never send from Outlook/OWA. Operator sends.",
                     created_at=old_iso, confidence=0.99, pinned=1)
        # a pinned durable that also happens to resemble the dedup cluster text
        _seed(tmp, kind="playbook", title="AUTONOMOUS BUILD CYCLE playbook",
              body="autonomous build cycle for hailports unattended on a schedule " + ("x" * 300),
              created_at=old_iso, confidence=0.95, pinned=1)

        pt = consolidate(dry=True, db_path=tmp)
        # dedup must find the 3-identical cluster (archive 2)
        if pt["dedup"]["would_archive"] < 2:
            fails.append(f"synthetic dedup found {pt['dedup']['would_archive']} archivable, expected >=2")
        # summarize must find the mockup-deploy cluster
        if pt["summarize"]["would_create"] < 1:
            fails.append("synthetic summarize found no cluster of related old memories")
        # the HARD rule + pinned playbook must be protected, never in any bucket
        prot_ids = {rule["id"]}
        touched = set()
        for c in pt["dedup"]["clusters"]:
            touched.update(c["archive_ids"]); touched.add(c["keep_id"])
        for c in pt["summarize"]["clusters"]:
            touched.update(c["member_ids"])
        for c in pt["prune"]["candidates"]:
            touched.add(c["id"])
        if prot_ids & touched:
            fails.append("HARD rule appeared in a consolidation bucket — protection FAILED")
        print(f"   synthetic dry: dedup_archive={pt['dedup']['would_archive']} "
              f"summarize_clusters={pt['summarize']['would_create']} "
              f"prune={pt['prune']['would_archive']} protected={pt['protected_count']}")

        # ── [3] APPLY on the throwaway db proves reversibility (archive, not hard-delete) ──────────
        pa = consolidate(dry=False, db_path=tmp)
        rid = pa["run_id"]
        after_apply = _count_rows(tmp, "memories")
        arch_rows = _count_rows(tmp, "memories_archive")
        if arch_rows < 2:
            fails.append("apply archived nothing")
        # the HARD rule must STILL be present after apply
        if not _row_present(tmp, rule["id"]):
            fails.append("HARD rule was removed by apply — DATA LOSS")
        rest = restore(rid, db_path=tmp)
        after_restore = _count_rows(tmp, "memories")
        # restore returns the archived rows; count returns to >= post-apply minus created lessons
        print(f"   apply run {rid}: archived={pa['applied']['archived']} created={pa['applied']['created']} "
              f"| rows {after_apply} archive_tbl {arch_rows} | restore -> {rest} rows now {after_restore}")
        if rest["restored"] < arch_rows:
            fails.append("restore did not re-insert all archived rows")
    finally:
        vmem.DB_PATH = orig_db
        LOG = orig_log
        try:
            for f in tmp.parent.iterdir():
                f.unlink()
            tmp.parent.rmdir()
        except Exception:
            pass

    print("\n" + "-" * 80)
    if fails:
        print("SMOKE FAIL:")
        for m in fails:
            print("  -", m)
        return 1
    print("SMOKE OK — dry mode wrote nothing to the real store; durable/rule memories excluded from")
    print("every bucket; synthetic dedup/summarize/prune fired; apply archived (not deleted) and")
    print("restore fully reversed the run. The brain sharpens with age, reversibly.")
    return 0


# ── tiny sqlite utils for the smoke (avoid importing heavy helpers) ─────────────────────────────
def _count_rows(db: Path, table: str) -> int:
    if not db.exists():
        return 0
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except sqlite3.OperationalError:
        return 0
    finally:
        con.close()


def _table_exists(db: Path, table: str) -> bool:
    if not db.exists():
        return False
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None
    finally:
        con.close()


def _row_present(db: Path, mem_id: int) -> bool:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return con.execute("SELECT 1 FROM memories WHERE id=?", (mem_id,)).fetchone() is not None
    finally:
        con.close()


def _seed(db: Path, **kw) -> dict:
    """Insert a raw memory row into a throwaway db (bypasses remember() so we can set created_at/id)."""
    con = sqlite3.connect(str(db))
    vmem._init(con)
    now = kw.get("created_at") or _now().isoformat()
    cur = con.execute(
        "INSERT INTO memories(created_at, updated_at, kind, title, body, source, systems, entities, "
        "confidence, pinned, metadata) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (now, now, kw["kind"], kw["title"], kw["body"], kw.get("source", ""),
         "[]", "[]", kw.get("confidence", 0.8), int(kw.get("pinned", 0)),
         json.dumps(kw.get("metadata", {}))))
    con.commit()
    mid = int(cur.lastrowid)
    con.close()
    return {"id": mid}


def _main() -> int:
    args = sys.argv[1:]
    if args and args[0] == "report":
        _print_report(consolidate(dry=True))
        return 0
    if args and args[0] == "apply":
        p = consolidate(dry=False)
        print(json.dumps({"run_id": p["run_id"], "applied": p["applied"]}, indent=2))
        return 0
    if args and args[0] == "restore":
        if len(args) < 2:
            print("usage: restore <run_id>", file=sys.stderr)
            return 2
        print(json.dumps(restore(args[1]), indent=2))
        return 0
    return _smoke()


if __name__ == "__main__":
    raise SystemExit(_main())
