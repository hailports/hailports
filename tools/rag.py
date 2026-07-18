#!/usr/bin/env python3
"""rag.py — local, $0 retrieval-augmented generation over the stack's OWN knowledge.

The pilfer from the "production RAG" play: chat-with-your-docs with reranking +
citations, but self-hosted and pointed at the hustle-lane corpus so any agent (or
Operator) can ask "what did we decide about X" and get a GROUNDED, CITED answer instead
of re-deriving it or missing it (the standing chat-recall gap / TASK #1).

Pipeline: chunk docs -> embed (Ollama nomic-embed-text, local) -> store (sqlite) ->
retrieve (cosine top-K) -> RERANK (local qwen scores relevance) -> answer with
[source] CITATIONS (local qwen). No paid API, no data leaves the mini.

LANE FIREWALL: this indexes the HUSTLE lane only (stack docs + Operator's memory). The
work-GPT gets its OWN separate index (never share) — pass --corpus to point elsewhere.

    python3 tools/rag.py index                 # build/refresh the index (incremental)
    python3 tools/rag.py ask "how do we gate cold email sends?"
    python3 tools/rag.py ask "..." --no-rerank # faster, cosine-only
    python3 tools/rag.py stats
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import struct
import sys
import urllib.request
from pathlib import Path

STACK = Path(os.path.expanduser("~/claude-stack"))
HOME = Path(os.path.expanduser("~"))
DB_PATH = STACK / "data" / "hustle" / "rag" / "index.db"

OLLAMA = os.environ.get("OLLAMA_URL", "http://localhost:11434")
EMBED_MODEL = os.environ.get("RAG_EMBED_MODEL", "nomic-embed-text")
GEN_MODEL = os.environ.get("RAG_GEN_MODEL", "qwen2.5:7b")            # volume / default tier
GEN_MODEL_QUALITY = os.environ.get("RAG_GEN_MODEL_QUALITY", "qwen3:14b")  # used ONLY when headroom


def _headroom_ok() -> bool:
    """True only when the box can afford the bigger model WITHOUT memory pressure — so we
    never OOM a saturated mini. Load < 6 (of 10 cores) AND > 11GB free+inactive RAM."""
    try:
        if os.getloadavg()[0] >= 6.0:
            return False
        import subprocess
        out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5).stdout
        free = inact = 0
        for ln in out.splitlines():
            if "Pages free" in ln:
                free = int(ln.split(":")[1].strip().rstrip("."))
            elif "Pages inactive" in ln:
                inact = int(ln.split(":")[1].strip().rstrip("."))
        return (free + inact) * 16384 / 1e9 > 11.0
    except Exception:
        return False   # unknown -> conservative -> small model


def _quality_model() -> str:
    """The bigger brain for final synthesis when there's headroom, else the 7b."""
    return GEN_MODEL_QUALITY if _headroom_ok() else GEN_MODEL

# HUSTLE-lane corpus (local, Operator's own). NOT the firewalled day-job work digests.
# Claude Code slugifies the home path into its project dir name; derive it so this is portable.
_HOME_SLUG = "-" + str(HOME).strip("/").replace("/", "-")
DEFAULT_CORPUS = [
    HOME / ".claude" / "projects" / _HOME_SLUG / "memory",
    STACK / "digests",
    STACK / "STACK_MAP.md",
    STACK / "SYSTEM_CHANGELOG.md",
    STACK / "CLAUDE.md",
]
EXTS = {".md", ".txt", ".mdx"}
CHUNK_CHARS = 900
CHUNK_OVERLAP = 150


# ------------------------------------------------------------------ ollama
def _post(path: str, payload: dict, timeout: int = 120) -> dict:
    req = urllib.request.Request(f"{OLLAMA}{path}", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def embed(text: str) -> list[float] | None:
    try:
        return _post("/api/embeddings", {"model": EMBED_MODEL, "prompt": text[:8000]}).get("embedding")
    except Exception as exc:  # noqa: BLE001
        print(f"  embed failed ({exc})", file=sys.stderr)
        return None


def generate(prompt: str, *, num_predict: int = 400, temperature: float = 0.2, model: str | None = None) -> str:
    try:
        return _post("/api/generate", {"model": model or GEN_MODEL, "prompt": prompt, "stream": False,
                                       "options": {"num_predict": num_predict, "temperature": temperature}},
                     timeout=240).get("response", "").strip()
    except Exception as exc:  # noqa: BLE001
        return f"(local model unavailable: {exc})"


# ------------------------------------------------------------------ vectors (no deps beyond numpy)
def _pack(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def _unpack(blob: bytes) -> list[float]:
    return list(struct.unpack(f"{len(blob)//4}f", blob))


# ------------------------------------------------------------------ store
def _db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    # busy_timeout: retrieve() shares this helper, and its CREATE TABLE IF NOT EXISTS DDL takes a
    # write lock even on the read path. When the index refresher holds the write lock, a concurrent
    # retrieve used to fail instantly with "database is locked" (returned []). Wait up to 3s for the
    # lock to clear instead of erroring — bounded so it can never stall a caller for long.
    con = sqlite3.connect(DB_PATH, timeout=3.0)
    con.execute("PRAGMA busy_timeout=3000")
    con.execute("CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, sha TEXT, chunks INT)")
    con.execute("CREATE TABLE IF NOT EXISTS chunks (id INTEGER PRIMARY KEY, path TEXT, ord INT, "
                "text TEXT, emb BLOB)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_chunks_path ON chunks(path)")
    return con


def _chunk(text: str) -> list[str]:
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) <= CHUNK_CHARS:
        return [text] if text else []
    out, i = [], 0
    while i < len(text):
        end = min(i + CHUNK_CHARS, len(text))
        if end < len(text):                       # break on a paragraph/sentence boundary near the end
            piece = text[i:end]
            cut = max(piece.rfind("\n\n"), piece.rfind(". "))
            if cut > CHUNK_CHARS * 0.5:
                end = i + cut + 1
        out.append(text[i:end].strip())
        if end >= len(text):
            break
        i = max(end - CHUNK_OVERLAP, i + CHUNK_CHARS // 2)   # ALWAYS advance (no runaway)
    return [c for c in out if c]


def _iter_files(corpus: list[Path]):
    for p in corpus:
        if p.is_file() and p.suffix in EXTS:
            yield p
        elif p.is_dir():
            for f in sorted(p.rglob("*")):
                if f.is_file() and f.suffix in EXTS and "MEMORY_ARCHIVE" not in f.name:
                    yield f


def cmd_index(corpus: list[Path]) -> int:
    con = _db()
    seen, added, skipped = set(), 0, 0
    for f in _iter_files(corpus):
        rel = str(f)
        seen.add(rel)
        try:
            raw = f.read_text(errors="replace")
        except Exception:
            continue
        sha = hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()
        row = con.execute("SELECT sha FROM files WHERE path=?", (rel,)).fetchone()
        if row and row[0] == sha:
            skipped += 1
            continue
        con.execute("DELETE FROM chunks WHERE path=?", (rel,))
        pieces = _chunk(raw)
        n = 0
        for ordi, piece in enumerate(pieces):
            emb = embed(f"{f.name}\n{piece}")
            if not emb:
                continue
            con.execute("INSERT INTO chunks(path, ord, text, emb) VALUES (?,?,?,?)",
                        (rel, ordi, piece, _pack(emb)))
            n += 1
        con.execute("INSERT OR REPLACE INTO files(path, sha, chunks) VALUES (?,?,?)", (rel, sha, n))
        con.commit()
        added += 1
        print(f"  indexed {f.name} ({n} chunks)")
    # prune deleted files
    for (rel,) in con.execute("SELECT path FROM files").fetchall():
        if rel not in seen:
            con.execute("DELETE FROM chunks WHERE path=?", (rel,))
            con.execute("DELETE FROM files WHERE path=?", (rel,))
    con.commit()
    tot = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    print(f"\nindex: {added} files (re)embedded, {skipped} unchanged · {tot} chunks total -> {DB_PATH}")
    return 0


# ------------------------------------------------------------------ retrieve + rerank + answer
def _cosine_topk(qvec: list[float], k: int) -> list[tuple[float, str, str]]:
    con = _db()
    rows = con.execute("SELECT path, text, emb FROM chunks").fetchall()
    if not rows:
        return []
    scored = []
    try:
        import numpy as np
        q = np.array(qvec, dtype="float32")
        q /= (np.linalg.norm(q) + 1e-9)
        for path, text, emb in rows:
            v = np.frombuffer(emb, dtype="float32")
            s = float(np.dot(q, v) / (np.linalg.norm(v) + 1e-9))
            scored.append((s, path, text))
    except ImportError:
        # pure-python fallback so `rag.py ask` works under ANY interpreter (e.g. a numpy-less system
        # python3), not just the .venv — same cosine math, one pass per row.
        import array
        import math
        qn = math.sqrt(sum(x * x for x in qvec)) + 1e-9
        qu = [x / qn for x in qvec]
        for path, text, emb in rows:
            v = array.array("f")
            v.frombytes(emb)
            dot = 0.0
            vn = 0.0
            for a, b in zip(qu, v):
                dot += a * b
                vn += b * b
            scored.append((dot / (math.sqrt(vn) + 1e-9), path, text))
    scored.sort(key=lambda x: -x[0])
    return scored[:k]


def _rerank(question: str, cands: list[tuple[float, str, str]], keep: int) -> list[tuple[float, str, str]]:
    """Local LLM relevance rerank (0-10) — the 'reranking' half of the play."""
    rescored = []
    for s, path, text in cands:
        prompt = (f"Question: {question}\n\nPassage:\n{text[:1200]}\n\n"
                  "How relevant is this passage to answering the question? "
                  "Reply with ONLY an integer 0-10.")
        out = generate(prompt, num_predict=4, temperature=0.0)
        m = re.search(r"\d+", out)
        r = min(10, int(m.group())) if m else 0
        rescored.append((r + s, path, text))  # blend rerank score with cosine as tiebreak
    rescored.sort(key=lambda x: -x[0])
    return rescored[:keep]


# ── PUBLIC SERVICE API — importable by any agent: `from tools.rag import ask, retrieve` ──
def retrieve(query: str, *, k: int = 20, keep: int = 6, rerank: bool = True) -> list[dict]:
    """Return the most relevant corpus chunks for a query: [{source, text, score}]. $0/local.
    Agents call this to GROUND their reasoning in the stack's own knowledge before acting."""
    qv = embed(query)
    if not qv:
        return []
    cands = _cosine_topk(qv, k)
    if not cands:
        return []
    top = _rerank(query, cands, keep) if rerank else cands[:keep]
    return [{"source": Path(p).name, "path": p, "text": t, "score": round(sc, 3)} for sc, p, t in top]


def ask(question: str, *, k: int = 20, keep: int = 5, rerank: bool = True) -> dict:
    """Grounded, CITED answer to a question over the corpus. Returns
    {answer, sources:[{n, source, score}]}. Importable service entrypoint."""
    hits = retrieve(question, k=k, keep=keep, rerank=rerank)
    if not hits:
        return {"answer": "(no indexed knowledge / Ollama down — run: rag.py index)", "sources": []}
    ctx = "\n\n".join(f"[{i+1}] ({h['source']})\n{h['text']}" for i, h in enumerate(hits))
    prompt = (
        "Answer the question using ONLY the sources below. Cite every claim with its [n] tag. "
        "If the sources don't answer it, say so plainly — do not invent.\n\n"
        f"SOURCES:\n{ctx}\n\nQUESTION: {question}\n\nANSWER (with [n] citations):"
    )
    answer = generate(prompt, num_predict=500, temperature=0.2, model=_quality_model())  # 14b iff headroom
    return {"answer": answer.strip(),
            "sources": [{"n": i + 1, "source": h["source"], "score": h["score"]}
                        for i, h in enumerate(hits)]}


def cmd_ask(question: str, *, k: int = 20, keep: int = 5, rerank: bool = True) -> int:
    res = ask(question, k=k, keep=keep, rerank=rerank)
    print("\n" + res["answer"] + "\n\n— sources —")
    for s in res["sources"]:
        print(f"  [{s['n']}] {s['source']}  (score {s['score']})")
    return 0


def cmd_stats() -> int:
    con = _db()
    nf = con.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    nc = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    print(f"RAG index: {nf} files · {nc} chunks · db={DB_PATH}")
    for path, ch in con.execute("SELECT path, chunks FROM files ORDER BY chunks DESC LIMIT 8"):
        print(f"  {ch:>4}  {Path(path).name}")
    return 0


def ingest_url(url: str) -> int:
    """ADDITIVE: fetch a URL as clean markdown (local, via core.web_extract) and
    index it into the same corpus DB, keyed by the URL. Opt-in by invocation;
    the default `index` corpus is untouched."""
    sys.path.insert(0, str(STACK))
    from core import web_extract
    d = web_extract.extract(url)
    md = d.get("markdown") or ""
    if not md.strip():
        print(f"ingest-url: no content from {url} (status={d.get('status')})")
        return 1
    con = _db()
    con.execute("DELETE FROM chunks WHERE path=?", (url,))
    n = 0
    for ordi, piece in enumerate(_chunk(md)):
        emb = embed(f"{d.get('title') or url}\n{piece}")
        if not emb:
            continue
        con.execute("INSERT INTO chunks(path, ord, text, emb) VALUES (?,?,?,?)",
                    (url, ordi, piece, _pack(emb)))
        n += 1
    sha = hashlib.sha1(md.encode("utf-8", "replace")).hexdigest()
    con.execute("INSERT OR REPLACE INTO files(path, sha, chunks) VALUES (?,?,?)", (url, sha, n))
    con.commit()
    print(f"ingest-url: indexed {url} ({n} chunks, path={d.get('path')})")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    pi = sub.add_parser("index"); pi.add_argument("--corpus", nargs="*")
    pa = sub.add_parser("ask"); pa.add_argument("question")
    pa.add_argument("--k", type=int, default=20); pa.add_argument("--keep", type=int, default=5)
    pa.add_argument("--no-rerank", action="store_true")
    pu = sub.add_parser("ingest-url"); pu.add_argument("url")
    sub.add_parser("stats")
    args = ap.parse_args()

    if args.cmd == "index":
        corpus = [Path(os.path.expanduser(c)) for c in args.corpus] if args.corpus else DEFAULT_CORPUS
        return cmd_index(corpus)
    if args.cmd == "ingest-url":
        return ingest_url(args.url)
    if args.cmd == "ask":
        return cmd_ask(args.question, k=args.k, keep=args.keep, rerank=not args.no_rerank)
    if args.cmd == "stats":
        return cmd_stats()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
