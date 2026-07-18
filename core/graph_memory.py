#!/usr/bin/env python3
"""graph_memory.py — ENTITY GRAPH memory so recall TRAVERSES instead of keyword-matching.

The problem this closes: work_rag (semantic) and work_context_index (FTS) both answer by
SIMILARITY — "what did we decide about Blake's access" pulls chunks that *sound like* the
question. But the real answer lives across three linked facts (person Blake -> ticket SF-1234
-> a perm-set decision) that may never co-occur in one chunk. This module builds a small
typed graph over the WORK corpus so recall WALKS the relation (person -> ticket -> decision)
and returns the connected facts with their source refs, deterministically.

STORE: data/learning/graph_memory.sqlite
  nodes(id, type, name, lane, created_at)   types: person|ticket|case|account|project|decision|meeting
  edges(src, dst, rel, ts, source_ref)      directed relations, one row per (src,dst,rel,source_ref)
  seen(source_ref, hash)                     incremental watermark per source document

HARD LANE FIREWALL: every node carries a lane (work|hustle|personal). An edge may NEVER link
nodes of different lanes — _add_edge refuses it (fail-closed). Ingest here reads ONLY the work
corpus and tags lane="work"; traverse() confines its walk to a single lane. A work query cannot
surface a hustle/personal node because (a) no edge crosses lanes and (b) the BFS drops any
neighbor whose lane != the seed lane. Two independent guarantees.

CORPUS (pull, never re-scrape): reuses core.work_rag._iter_corpus() (tools.work_context_index
docs + core.redacted_memory) and core.clean_capture's exchange ledger. Entity + relation
extraction is DETERMINISTIC (regex / NER-lite): SF-#### / case refs, person names (possessive +
full-name + roster), account names, and decision statements ("decided/closed/resolved/...").
Only the final answer MAY be smoothed by the local model — the graph itself never is.

    core/../.venv/bin/python -m core.graph_memory              # run the smoke proof (default)
    core/../.venv/bin/python -m core.graph_memory ingest       # build the real work graph (incremental)
    core/../.venv/bin/python -m core.graph_memory ask "what did we decide about Blake's access"

STAGED — NOT wired into any live retrieval path. Ingest is read-only over existing stores;
nothing here bounces a service. The traverse() entry point is the wire-in surface for the
clone/GPT once the proof is reviewed.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

GRAPH_DB = BASE_DIR / "data" / "learning" / "graph_memory.sqlite"

WORK, HUSTLE, PERSONAL = "work", "hustle", "personal"
_LANES = {WORK, HUSTLE, PERSONAL}

PERSON, TICKET, CASE, ACCOUNT, PROJECT, DECISION, MEETING = (
    "person", "ticket", "case", "account", "project", "decision", "meeting")
_TYPES = {PERSON, TICKET, CASE, ACCOUNT, PROJECT, DECISION, MEETING}


class LaneFirewallError(ValueError):
    """Raised when an edge would link two lanes — a HARD firewall violation, never a soft merge."""


# ─────────────────────────────────────────────────────────────────────── store
def _db(path: Path = GRAPH_DB) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE IF NOT EXISTS nodes (id TEXT PRIMARY KEY, type TEXT, name TEXT, "
                "lane TEXT, created_at REAL)")
    con.execute("CREATE TABLE IF NOT EXISTS edges (src TEXT, dst TEXT, rel TEXT, ts REAL, "
                "source_ref TEXT, PRIMARY KEY(src, dst, rel, source_ref))")
    con.execute("CREATE INDEX IF NOT EXISTS ix_edges_src ON edges(src)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_edges_dst ON edges(dst)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_edges_ref ON edges(source_ref)")
    con.execute("CREATE TABLE IF NOT EXISTS seen (source_ref TEXT PRIMARY KEY, hash TEXT)")
    return con


def _node_id(lane: str, type_: str, name: str) -> str:
    """Deterministic, dedupe-stable id. Free-text nodes (decisions) hash their body so the id
    stays short and identical text collapses to one node."""
    norm = re.sub(r"\s+", " ", name).strip().lower()
    if type_ == DECISION:
        norm = hashlib.sha1(norm.encode("utf-8", "ignore")).hexdigest()[:12]
    return f"{lane}::{type_}::{norm}"


def _upsert_node(con, lane: str, type_: str, name: str) -> str:
    if lane not in _LANES:
        raise ValueError(f"bad lane {lane!r}")
    if type_ not in _TYPES:
        raise ValueError(f"bad type {type_!r}")
    nid = _node_id(lane, type_, name)
    con.execute("INSERT OR IGNORE INTO nodes(id, type, name, lane, created_at) VALUES (?,?,?,?,?)",
                (nid, type_, name.strip(), lane, time.time()))
    return nid


def _add_edge(con, src: str, dst: str, rel: str, source_ref: str, ts: float = 0.0) -> None:
    """Insert a directed edge. HARD firewall: refuses to link two different lanes."""
    sl = con.execute("SELECT lane FROM nodes WHERE id=?", (src,)).fetchone()
    dl = con.execute("SELECT lane FROM nodes WHERE id=?", (dst,)).fetchone()
    if not sl or not dl:
        raise ValueError("both endpoints must be upserted before linking")
    if sl[0] != dl[0]:
        raise LaneFirewallError(
            f"refused cross-lane edge {src} ({sl[0]}) -> {dst} ({dl[0]}): the graph never "
            f"links work to hustle/personal.")
    con.execute("INSERT OR IGNORE INTO edges(src, dst, rel, ts, source_ref) VALUES (?,?,?,?,?)",
                (src, dst, rel, ts, source_ref))


# ─────────────────────────────────────────────────────────── deterministic extraction (NER-lite)
_RE_TICKET = re.compile(r"\bSF[-\s]?(\d{2,6})\b", re.I)
_RE_CASE = re.compile(r"\bcase[-\s#]*?(\d{5,10})\b", re.I)
_RE_POSSESSIVE = re.compile(r"\b([A-Z][a-z]{2,})'s\b")
_RE_FULLNAME = re.compile(r"\b([A-Z][a-z]+)\s+([A-Z][a-z]+)\b")
_RE_ACCOUNT = re.compile(r"\baccount[:\s]+([A-Z][A-Za-z0-9&'.\- ]{2,40})", re.I)
_RE_PROJECT = re.compile(r"\bproject[:\s]+([A-Z][A-Za-z0-9&'.\- ]{2,40})", re.I)
_RE_SENT = re.compile(r"[^.!?\n]+[.!?\n]?")

_DECISION_KW = re.compile(
    r"\b(decid\w*|decision|resolv\w*|closed|approv\w*|grant\w*|agreed|will\s+use|"
    r"chose|selected|assigned|reverted?|shipped|deployed)\b", re.I)

# words that pass the "Two Capitalized Words" shape but are NOT people
_NAME_STOP = {
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december", "the", "salesforce", "perm",
    "permission", "portal", "account", "project", "case", "ticket", "valley",
    "partners", "connector", "rebate", "machine", "attribution", "experience",
    "community", "sandbox", "prod", "production", "local", "tests", "user",
    "opportunity", "controller", "set", "sets", "org", "scanner",
}


def _persons(text: str, roster: set[str] | None = None) -> set[str]:
    """Deterministic person extraction, precision-biased:
      1. possessive  ("Blake's access"      -> Blake)
      2. full name   ("Vijay Kumar"         -> Vijay Kumar)   minus a stoplist
      3. roster      (exact known first/full names, catches single-token names)"""
    out: set[str] = set()
    for m in _RE_POSSESSIVE.finditer(text):
        w = m.group(1)
        if w.lower() not in _NAME_STOP:
            out.add(w)
    for m in _RE_FULLNAME.finditer(text):
        a, b = m.group(1), m.group(2)
        if a.lower() in _NAME_STOP or b.lower() in _NAME_STOP:
            continue
        out.add(f"{a} {b}")
    if roster:
        for name in roster:
            if re.search(rf"\b{re.escape(name)}\b", text):
                out.add(name)
    return out


def _decisions(text: str) -> list[str]:
    """Sentences that assert a decision. Trimmed + de-noised; each becomes a decision node."""
    out = []
    for m in _RE_SENT.finditer(text):
        s = m.group(0).strip()
        if 12 <= len(s) <= 300 and _DECISION_KW.search(s):
            out.append(re.sub(r"\s+", " ", s))
    return out


def _extract(text: str, roster: set[str] | None = None) -> dict:
    """All entities in one doc: persons, tickets, cases, accounts, projects, decisions."""
    return {
        "persons": _persons(text, roster),
        "tickets": {f"SF-{m.group(1)}" for m in _RE_TICKET.finditer(text)},
        "cases": {f"Case #{m.group(1)}" for m in _RE_CASE.finditer(text)},
        "accounts": {m.group(1).strip() for m in _RE_ACCOUNT.finditer(text)},
        "projects": {m.group(1).strip() for m in _RE_PROJECT.finditer(text)},
        "decisions": _decisions(text),
    }


def _roster_from_memory() -> set[str]:
    """Seed the person roster from redacted_memory's `entities` field (already work-lane, $0).
    Best-effort — a missing/altered store just yields an empty roster."""
    roster: set[str] = set()
    try:
        from core import redacted_memory
        for m in redacted_memory.recent(limit=5000):
            ents = m.get("entities")
            if isinstance(ents, str):
                try:
                    ents = json.loads(ents)
                except Exception:
                    ents = [ents]
            for e in ents or []:
                e = str(e).strip()
                if re.fullmatch(r"[A-Z][a-z]+(?: [A-Z][a-z]+)?", e) and e.lower() not in _NAME_STOP:
                    roster.add(e)
    except Exception:
        pass
    return roster


# ─────────────────────────────────────────────────────────────────────── ingest
def _ingest_doc(con, lane: str, source_ref: str, text: str, roster, ts: float = 0.0) -> int:
    """Extract one document into the graph. Returns edges added. Decisions become nodes linked
    (about) tickets/cases and (involves) persons; persons link (involved_in) tickets."""
    ents = _extract(text, roster)
    person_ids = [_upsert_node(con, lane, PERSON, p) for p in ents["persons"]]
    ticket_ids = [_upsert_node(con, lane, TICKET, t) for t in ents["tickets"]]
    case_ids = [_upsert_node(con, lane, CASE, c) for c in ents["cases"]]
    acct_ids = [_upsert_node(con, lane, ACCOUNT, a) for a in ents["accounts"]]
    proj_ids = [_upsert_node(con, lane, PROJECT, p) for p in ents["projects"]]
    ref_targets = ticket_ids + case_ids + acct_ids + proj_ids

    n = 0
    for pid in person_ids:
        for tid in ref_targets:
            _add_edge(con, pid, tid, "involved_in", source_ref, ts); n += 1
    for did_text in ents["decisions"]:
        did = _upsert_node(con, lane, DECISION, did_text)
        for tid in ref_targets:
            _add_edge(con, did, tid, "about", source_ref, ts); n += 1
        for pid in person_ids:
            _add_edge(con, did, pid, "involves", source_ref, ts); n += 1
    return n


def ingest(docs=None, *, incremental: bool = True, lane: str = WORK, db_path: Path = GRAPH_DB,
           roster: set[str] | None = None) -> dict:
    """Build the entity graph. `docs` = iterable of (source_ref, text[, ts]); default pulls the
    real WORK corpus. Incremental via a per-doc content hash (unchanged docs skipped, changed docs
    have their prior edges dropped + re-derived). ingest() is READ-ONLY over existing stores."""
    if lane != WORK and docs is None:
        raise LaneFirewallError("the corpus ingester is work-lane only; pass explicit docs for a test lane")
    con = _db(db_path)
    if roster is None:
        roster = _roster_from_memory() if docs is None else set()
    if not incremental:
        con.execute("DELETE FROM edges"); con.execute("DELETE FROM nodes"); con.execute("DELETE FROM seen")
        con.commit()

    src = _iter_work_corpus() if docs is None else docs
    added_docs = skipped = edges = 0
    for row in src:
        source_ref, text = row[0], row[1]
        ts = float(row[2]) if len(row) > 2 and row[2] else 0.0
        text = (text or "").strip()
        if not text:
            continue
        h = hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()
        prev = con.execute("SELECT hash FROM seen WHERE source_ref=?", (source_ref,)).fetchone()
        if prev and prev[0] == h:
            skipped += 1
            continue
        con.execute("DELETE FROM edges WHERE source_ref=?", (source_ref,))
        edges += _ingest_doc(con, lane, source_ref, text, roster, ts)
        con.execute("INSERT OR REPLACE INTO seen(source_ref, hash) VALUES (?,?)", (source_ref, h))
        added_docs += 1
        if added_docs % 300 == 0:
            con.commit()
    con.commit()
    stats = {
        "ok": True, "docs_ingested": added_docs, "unchanged": skipped, "edges_added": edges,
        "nodes_total": con.execute("SELECT COUNT(*) FROM nodes").fetchone()[0],
        "edges_total": con.execute("SELECT COUNT(*) FROM edges").fetchone()[0],
        "db": str(db_path),
    }
    con.close()
    return stats


def _iter_work_corpus():
    """(source_ref, text, ts) over the work corpus. Reuses work_rag._iter_corpus (wci docs +
    redacted_memory) and appends the clean_capture work exchange ledger. No re-scraping."""
    try:
        from core import work_rag
        for source, ref, title, url, ts, text in work_rag._iter_corpus():
            yield (f"{source}:{ref}", (f"{title}\n{text}" if title else text), ts)
    except Exception as e:
        print(f"[graph_memory] work_rag corpus read failed: {e}", file=sys.stderr)
    try:
        from core import clean_capture
        if clean_capture.LEDGER.exists():
            with clean_capture.LEDGER.open() as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    txt = (rec.get("text") or "").strip()
                    if txt:
                        yield (f"exchange:{rec.get('id')}", txt, 0.0)
    except Exception as e:
        print(f"[graph_memory] clean_capture read failed: {e}", file=sys.stderr)


# ─────────────────────────────────────────────────────────────────────── traverse
def _seed_nodes(con, query: str, lane: str, roster: set[str] | None) -> list[dict]:
    """Find the query's anchor entities that actually exist as nodes IN THIS LANE."""
    ents = _extract(query, roster)
    wanted = (
        [(PERSON, p) for p in ents["persons"]]
        + [(TICKET, t) for t in ents["tickets"]]
        + [(CASE, c) for c in ents["cases"]]
        + [(ACCOUNT, a) for a in ents["accounts"]]
        + [(PROJECT, p) for p in ents["projects"]]
    )
    seeds = []
    for type_, name in wanted:
        nid = _node_id(lane, type_, name)
        row = con.execute("SELECT id, type, name, lane FROM nodes WHERE id=?", (nid,)).fetchone()
        if row:
            seeds.append({"id": row[0], "type": row[1], "name": row[2], "lane": row[3]})
    return seeds


def traverse(query: str, *, lane: str = WORK, max_hops: int = 2, db_path: Path = GRAPH_DB,
             roster: set[str] | None = None, summarize: bool = False) -> dict:
    """Answer by WALKING the graph. Seeds on the query's entities, BFS up to max_hops WITHIN one
    lane, and returns the connected subgraph + a deterministic answer (decisions/tickets/people
    with source refs). The BFS drops any neighbor whose lane != the seed lane — a second, redundant
    firewall on top of the no-cross-lane-edge invariant. Set summarize=True to smooth the answer via
    the local model ($0, best-effort); the graph facts are unchanged either way."""
    if lane not in _LANES:
        raise LaneFirewallError(f"bad lane {lane!r}")
    con = _db(db_path)
    if roster is None:
        roster = _roster_from_memory() if db_path == GRAPH_DB else set()
    seeds = _seed_nodes(con, query, lane, roster)

    visited: dict[str, dict] = {}
    sub_edges: list[dict] = []
    frontier = []
    for s in seeds:
        visited[s["id"]] = s
        frontier.append((s["id"], 0))

    while frontier:
        nid, depth = frontier.pop(0)
        if depth >= max_hops:
            continue
        rows = con.execute(
            "SELECT src, dst, rel, source_ref FROM edges WHERE src=? OR dst=?", (nid, nid)).fetchall()
        for src, dst, rel, ref in rows:
            other = dst if src == nid else src
            row = con.execute("SELECT id, type, name, lane FROM nodes WHERE id=?", (other,)).fetchone()
            if not row or row[3] != lane:      # FIREWALL: never step into another lane
                continue
            sub_edges.append({"src": src, "dst": dst, "rel": rel, "source_ref": ref})
            if other not in visited:
                visited[other] = {"id": row[0], "type": row[1], "name": row[2], "lane": row[3]}
                frontier.append((other, depth + 1))
    con.close()

    decisions = [n for n in visited.values() if n["type"] == DECISION]
    tickets = [n for n in visited.values() if n["type"] in (TICKET, CASE)]
    people = [n for n in visited.values() if n["type"] == PERSON]
    # de-dup edges
    seen_e = set(); uniq_edges = []
    for e in sub_edges:
        k = (e["src"], e["dst"], e["rel"], e["source_ref"])
        if k not in seen_e:
            seen_e.add(k); uniq_edges.append(e)

    answer = _synthesize(seeds, decisions, tickets, uniq_edges, summarize)
    return {
        "lane": lane, "query": query,
        "seeds": [s["name"] for s in seeds],
        "nodes": list(visited.values()),
        "edges": uniq_edges,
        "decisions": decisions, "tickets": tickets, "people": people,
        "answer": answer,
    }


def _synthesize(seeds, decisions, tickets, edges, summarize: bool) -> str:
    """Deterministic-first answer. Only if summarize=True AND the local model is reachable do we
    smooth the wording — the facts (decisions + refs) are always the deterministic ones."""
    if not seeds:
        return "no anchor entity from the question is in the graph yet."
    if not decisions and not tickets:
        return f"{', '.join(s['name'] for s in seeds)} is known, but no linked decision/ticket found."
    refs_for = lambda nid: sorted({e["source_ref"] for e in edges if nid in (e["src"], e["dst"])})
    lines = []
    for d in decisions:
        srefs = refs_for(d["id"])
        tail = f"  [src: {', '.join(srefs)}]" if srefs else ""
        lines.append(f"- decided: {d['name']}{tail}")
    if tickets:
        lines.append(f"- linked tickets/cases: {', '.join(sorted(t['name'] for t in tickets))}")
    deterministic = f"re: {', '.join(s['name'] for s in seeds)}\n" + "\n".join(lines)

    if summarize:
        try:
            from tools import rag as _rag
            prompt = ("Summarize the decision below in one plain sentence. Do not add anything "
                      "not stated.\n\n" + deterministic)
            smoothed = _rag.generate(prompt, num_predict=120).strip()
            if smoothed:
                return smoothed + "\n\n(sources)\n" + deterministic
        except Exception:
            pass
    return deterministic


# ─────────────────────────────────────────────────────────────────────── smoke proof
def _smoke() -> int:
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="graphmem_")) / "smoke.sqlite"
    print(f"[smoke] db={tmp}")

    # synthetic WORK docs — Blake + Mercedes + a perm-set decision + SF-1234
    roster = {"Blake", "Mercedes"}
    work_docs = [
        ("email:001",
         "Mercedes flagged that Blake still can't see the SP Portal accounts. "
         "This is tracked under SF-1234."),
        ("meeting:002",
         "On the access review call we decided to grant Blake the SP_Portal_Machine_Attribution "
         "permission set to fix SF-1234. Mercedes will assign it in prod."),
    ]
    stats = ingest(work_docs, db_path=tmp, lane=WORK, roster=roster)
    print(f"[smoke] ingest(work): {stats}")

    # a HUSTLE node in the SAME db — must stay invisible to a work query
    con = _db(tmp)
    _upsert_node(con, HUSTLE, PERSON, "Blake")          # same name, different lane
    _upsert_node(con, HUSTLE, DECISION, "decided to raise the Gumroad price to $99")
    con.commit(); con.close()

    ok = True

    # 1) firewall at the EDGE layer: linking work->hustle must be refused
    con = _db(tmp)
    wid = _node_id(WORK, PERSON, "Blake"); hid = _node_id(HUSTLE, PERSON, "Blake")
    try:
        _add_edge(con, wid, hid, "same_person", "smoke")
        print("[smoke] FAIL: cross-lane edge was allowed"); ok = False
    except LaneFirewallError:
        print("[smoke] PASS: cross-lane edge refused (edge firewall)")
    con.commit(); con.close()

    # 2) traverse the decision from the person, 2 hops
    res = traverse("what did we decide about Blake's access", lane=WORK, db_path=tmp, roster=roster)
    print(f"[smoke] seeds={res['seeds']}")
    print(f"[smoke] answer:\n{res['answer']}")
    dec_txt = " ".join(d["name"] for d in res["decisions"]).lower()
    tks = {t["name"] for t in res["tickets"]}
    if "perm" not in dec_txt and "permission" not in dec_txt and "grant" not in dec_txt:
        print("[smoke] FAIL: linked perm-set decision not returned"); ok = False
    else:
        print("[smoke] PASS: linked perm-set decision returned")
    if "SF-1234" in tks:
        print("[smoke] PASS: linked ticket SF-1234 returned via traversal")
    else:
        print(f"[smoke] FAIL: SF-1234 not linked; tickets={tks}"); ok = False

    # 3) firewall at the TRAVERSE layer: no hustle node in a work result, ever
    lanes = {n["lane"] for n in res["nodes"]}
    if lanes <= {WORK}:
        print(f"[smoke] PASS: work traversal returned only work nodes (lanes={lanes})")
    else:
        print(f"[smoke] FAIL: work traversal leaked lanes={lanes}"); ok = False
    if any("gumroad" in n["name"].lower() for n in res["nodes"]):
        print("[smoke] FAIL: hustle decision leaked into work result"); ok = False

    print("[smoke] RESULT:", "ALL PASS" if ok else "FAILURES ABOVE")
    return 0 if ok else 1


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "smoke"
    if arg == "ingest":
        print(json.dumps(ingest(incremental="--full" not in sys.argv), indent=2))
    elif arg == "ask":
        q = " ".join(sys.argv[2:]) or "what did we decide about Blake's access"
        res = traverse(q, summarize="--summarize" in sys.argv)
        print(f"seeds: {res['seeds']}\n\n{res['answer']}")
    else:
        sys.exit(_smoke())
