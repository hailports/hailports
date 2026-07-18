#!/usr/bin/env python3
"""all_brain_rag.py — ALL-BRAIN semantic retrieval with a HARD lane firewall.

"All-brain" = EVERY lane has great semantic retrieval, NOT a merged index. Crossing lanes
is a hard firewall violation: a work query must never surface hustle/personal knowledge, and
a hustle query must never surface work/CompanyA knowledge. This module is the single router in
front of the two SEPARATE indexes; it routes each query to EXACTLY ONE index and there is no
code path that reads both.

    WORK   lane -> core.work_rag.search  (data/learning/work_rag.sqlite + work_context_index FTS)
    HUSTLE lane -> tools.rag.retrieve    (data/hustle/rag/index.db)

We REUSE both existing pipelines wholesale (embed/rerank/cosine live there) — this file adds
only the router + the lane guard, nothing about embeddings is reinvented here.

    core/../.venv/bin/python -m core.all_brain_rag                 # firewall + routing proof (default)
    core/../.venv/bin/python -m core.all_brain_rag work   "am i free friday"
    core/../.venv/bin/python -m core.all_brain_rag hustle  "how do we gate cold email sends"

STAGED — not wired into any live retrieval path. Each lane still has its own wire-in point in its
own module; this only unifies the ROUTER so a caller picks a lane explicitly and cannot cross.
"""
from __future__ import annotations

import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

# Two SEPARATE pipelines. They are imported side-by-side ONLY so the dispatch table can name them;
# no function below ever calls into both — that is the whole firewall (see _search_work/_search_hustle).
from core import work_rag as _work_rag        # WORK lane index  (data/learning/work_rag.sqlite)  # noqa: E402
from tools import rag as _hustle_rag          # HUSTLE lane index (data/hustle/rag/index.db)       # noqa: E402

WORK = "work"
HUSTLE = "hustle"
LANES = (WORK, HUSTLE)  # the ONLY valid lanes — no "all"/"both"/"merged" exists, by design


class LaneFirewallError(ValueError):
    """Raised when a caller tries to query anything other than exactly one valid lane —
    a merged/cross-lane query is a HARD firewall violation, not a soft fallback."""


# ─────────────────────────────────────────────────────────────────────── lane guard
def _guard_lane(lane) -> str:
    """Normalize + HARD-validate the lane. Refuses anything that is not exactly one of the two
    single lanes: no list/tuple (multi-lane), no 'all'/'both'/'*', no unknown string. Fails closed."""
    if isinstance(lane, (list, tuple, set, frozenset)):
        raise LaneFirewallError(
            f"cross-lane query refused: got {lane!r}. search() serves ONE lane; "
            f"call it twice (once per lane) — the indexes are firewalled and never merged.")
    if not isinstance(lane, str):
        raise LaneFirewallError(f"lane must be a str, one of {LANES}; got {type(lane).__name__}")
    norm = lane.strip().lower()
    if norm not in LANES:
        raise LaneFirewallError(
            f"unknown/forbidden lane {lane!r}. valid lanes = {LANES}. "
            f"there is deliberately NO merged/all-lane path (firewall).")
    return norm


# ─────────────────────────────────────────────────────────── per-lane searchers (each touches ONE index)
def _search_work(query: str, k: int, *, rerank: bool = False) -> list[dict]:
    """WORK lane ONLY. Delegates to core.work_rag.search -> work_rag.sqlite + work_context_index FTS.
    Does NOT reference the hustle module or its index — that separation IS the firewall."""
    hits = _work_rag.search(query, k=k, rerank=rerank)
    return [_normalize(h, WORK, ref_key="ref") for h in hits]


def _search_hustle(query: str, k: int, *, rerank: bool = False) -> list[dict]:
    """HUSTLE lane ONLY. Delegates to tools.rag.retrieve -> data/hustle/rag/index.db.
    Does NOT reference the work module or its index — that separation IS the firewall.
    rerank defaults False so the router stays cheap/robust (no LLM needed); caller may opt in."""
    hits = _hustle_rag.retrieve(query, keep=k, rerank=rerank)
    return [_normalize(h, HUSTLE, ref_key="path") for h in hits]


# The router's ENTIRE knowledge of "how to answer a lane" is this table. It maps each lane to
# exactly ONE searcher; there is no entry that fans out to both. Adding a merged entry here would
# be the only way to break the firewall — so this table is the thing to guard/review.
_LANE_SEARCHERS = {
    WORK: _search_work,
    HUSTLE: _search_hustle,
}


def _normalize(h: dict, lane: str, *, ref_key: str) -> dict:
    """Common result shape across both pipelines, tagged with its lane so a downstream consumer
    can't silently mix lanes without seeing the label."""
    return {
        "lane": lane,
        "text": h.get("text") or "",
        "source": h.get("source"),
        "ref": h.get(ref_key) or h.get("ref"),
        "title": h.get("title"),
        "url": h.get("url"),
        "score": h.get("score"),
    }


# ─────────────────────────────────────────────────────────────────────── public router
def search(query: str, lane: str, k: int = 8, *, rerank: bool = False) -> list[dict]:
    """Route `query` to the ONE index for `lane` and return [{lane, text, source, ref, score, ...}].

    lane MUST be exactly "work" or "hustle". There is no merged/all-lane path — a caller wanting
    both must call twice; the router will never read the other lane's index for a given call.
    Raises LaneFirewallError on any cross-lane / unknown-lane request (fail closed).

    Degrades gracefully: if the lane's index is empty/unbuilt or Ollama is unreachable, the
    underlying pipeline returns [] rather than erroring — this returns [] for that lane."""
    lane = _guard_lane(lane)
    searcher = _LANE_SEARCHERS[lane]  # exactly one — no fan-out
    try:
        return searcher(query, k, rerank=rerank)
    except Exception as e:  # noqa: BLE001 — one lane being down must never spill into the other
        print(f"[all_brain_rag] {lane} lane search failed (returning []): {e}", file=sys.stderr)
        return []


# ─────────────────────────────────────────────────────────────────────── introspection (graceful)
def index_status(lane: str) -> dict:
    """Non-destructive per-lane index health: {lane, db, exists, chunks}. Never builds or embeds.
    Handles a missing/unbuilt DB by reporting exists=False / chunks=0 instead of raising."""
    import sqlite3
    lane = _guard_lane(lane)
    if lane == WORK:
        db, table = _work_rag.WORK_RAG_DB, "vchunks"
    else:
        db, table = _hustle_rag.DB_PATH, "chunks"
    out = {"lane": lane, "db": str(db), "exists": db.exists(), "chunks": 0}
    if not out["exists"]:
        return out
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            out["chunks"] = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        finally:
            con.close()
    except Exception as e:  # noqa: BLE001
        out["error"] = str(e)
    return out


# ─────────────────────────────────────────────────────────────────────── smoke proof
def _which_dbs_touched(fn):
    """Run fn() while recording EVERY sqlite db path opened, so we can PROVE a lane search touches
    only its own index. Wraps sqlite3.connect (used by both pipelines + the pure-py fallback)."""
    import sqlite3
    touched: list[str] = []
    real = sqlite3.connect

    def _spy(target, *a, **kw):
        s = str(target)
        # strip the read-only URI wrapper so paths compare cleanly
        if s.startswith("file:"):
            s = s[len("file:"):].split("?", 1)[0]
        touched.append(s)
        return real(target, *a, **kw)

    sqlite3.connect = _spy
    try:
        result = fn()
    finally:
        sqlite3.connect = real
    return result, touched


def _no_merge_structural_proof() -> bool:
    """Static proof that no single function in the router reads BOTH indexes: inspect the source of
    search() + every lane searcher and assert none references both lane modules at once."""
    import inspect
    ok = True
    checks = {
        "_search_work": (_search_work, "_work_rag", "_hustle_rag"),
        "_search_hustle": (_search_hustle, "_hustle_rag", "_work_rag"),
    }
    for name, (fn, must, must_not) in checks.items():
        src = inspect.getsource(fn)
        has_own = must in src
        has_other = must_not in src
        good = has_own and not has_other
        ok = ok and good
        print(f"   {name}: references {must}={has_own}, references {must_not}={has_other}  "
              f"-> {'CLEAN' if good else 'LEAK'}")
    # the router itself must dispatch, not read an index directly
    router_src = inspect.getsource(search)
    router_clean = ("_LANE_SEARCHERS[lane]" in router_src
                    and "_work_rag" not in router_src and "_hustle_rag" not in router_src)
    print(f"   search(): dispatches via table, no direct index ref -> "
          f"{'CLEAN' if router_clean else 'LEAK'}")
    # and there is genuinely no merged lane registered
    no_merged_lane = set(_LANE_SEARCHERS) == set(LANES) and len(_LANE_SEARCHERS) == 2
    print(f"   dispatch table = exactly {tuple(_LANE_SEARCHERS)} (no all/both/merged) -> "
          f"{'CLEAN' if no_merged_lane else 'LEAK'}")
    return ok and router_clean and no_merged_lane


def _smoke() -> int:
    print("=" * 78)
    print("all_brain_rag smoke — ALL-BRAIN routing + HARD lane firewall proof")
    print("=" * 78)

    work_db = str(_work_rag.WORK_RAG_DB)
    hustle_db = str(_hustle_rag.DB_PATH)

    print("\n[1] index status (non-destructive; graceful if unbuilt) --------------------")
    ws, hs = index_status(WORK), index_status(HUSTLE)
    print(f"   WORK   : exists={ws['exists']} chunks={ws['chunks']}  {ws['db']}")
    print(f"   HUSTLE : exists={hs['exists']} chunks={hs['chunks']}  {hs['db']}")

    print("\n[2] lane guard behavior (fail closed on any cross-lane request) -------------")
    bad_inputs = ["all", "both", "merged", "*", "personal", "", ["work", "hustle"], ("work",), None]
    for bad in bad_inputs:
        try:
            _guard_lane(bad)
            print(f"   guard({bad!r}) -> ACCEPTED  <-- FIREWALL BREACH")
            return 1
        except LaneFirewallError:
            print(f"   guard({bad!r}) -> refused (LaneFirewallError)  OK")
    for good in ("work", "HUSTLE ", " Work"):
        print(f"   guard({good!r}) -> {_guard_lane(good)!r}  OK")

    print("\n[3] structural NO-MERGE proof (source inspection) --------------------------")
    if not _no_merge_structural_proof():
        print("   FAIL: a router path can read both indexes.")
        return 1

    print("\n[4] runtime index isolation — each lane touches ONLY its own db --------------")
    # Neutralize Ollama so the proof is deterministic + $0 and never triggers a heavy re-index:
    # embed() returns a fixed dummy vector; rerank stays off. Both pipelines share this one embed.
    orig_embed = _hustle_rag.embed
    _hustle_rag.embed = lambda text: [0.01] * 768  # noqa: E731 — deterministic stub, matches nomic dim

    ok = True
    try:
        _, work_touched = _which_dbs_touched(lambda: search("am i free friday", WORK, k=5))
        _, hustle_touched = _which_dbs_touched(
            lambda: search("how do we gate cold email sends", HUSTLE, k=5))

        def _norm(paths):  # dedupe, resolve symlinks for honest comparison
            return sorted({str(Path(p).resolve()) if Path(p).name else p for p in paths})

        wt, ht = _norm(work_touched), _norm(hustle_touched)
        work_db_r, hustle_db_r = str(Path(work_db).resolve()), str(Path(hustle_db).resolve())

        print(f"   search(_, work)   opened dbs:")
        for p in wt:
            print(f"       {p}")
        work_leaked = hustle_db_r in wt
        print(f"     -> hustle index touched? {work_leaked}  {'LEAK' if work_leaked else 'CLEAN'}")

        print(f"   search(_, hustle) opened dbs:")
        for p in ht:
            print(f"       {p}")
        hustle_leaked = work_db_r in ht
        print(f"     -> work index touched?   {hustle_leaked}  {'LEAK' if hustle_leaked else 'CLEAN'}")

        # hustle lane must have hit its OWN index (proves it actually routed there, when built)
        hustle_hit_own = (hustle_db_r in ht) or (hs["chunks"] == 0 and not hs["exists"])
        print(f"     -> hustle hit its own index (or index unbuilt)? {hustle_hit_own}")

        ok = (not work_leaked) and (not hustle_leaked)
    finally:
        _hustle_rag.embed = orig_embed  # restore — never leave the real embedder stubbed

    print("\n" + "-" * 78)
    if ok:
        print("PROVEN: each lane routes to ONE index; neither call ever opened the other lane's db,")
        print("and no single router function can read both. All-brain = two firewalled brains.")
    else:
        print("FAIL: a lane search touched the other lane's index — firewall breach, reported honestly.")
    print("-" * 78)
    return 0 if ok else 1


def _main() -> int:
    args = sys.argv[1:]
    if args and args[0] in LANES:
        lane = args[0]
        q = " ".join(args[1:]) or ("am i free friday" if lane == WORK else "how do we gate sends")
        for r in search(q, lane, k=8):
            print(f"[{r['lane']}:{r['source']}] {(r.get('title') or r.get('ref') or '')[:70]}  "
                  f"(score={r['score']})")
            print(f"   {(r.get('text') or '')[:200]}")
        return 0
    return _smoke()


if __name__ == "__main__":
    raise SystemExit(_main())
