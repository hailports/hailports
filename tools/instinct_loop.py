#!/usr/bin/env python3
"""Confidence-scored instinct loop — in-house continuous-learning for the stack.

Pattern mined from ECC's continuous-learning-v2 (NO foreign code — rebuilt here). The idea it got right:
extract learnings from a session, store each with a 0-1 confidence, and only re-inject the top few
high-confidence ones at the next session start (capped) — so the stack gets SMARTER over time instead
of NOISIER. Everything is local ($0 via Ollama) and deterministic-fallback-safe.

Storage: data/instincts/instincts.jsonl  (one JSON instinct per line)
  {id, text, tags[], confidence 0-1, created, last_seen, hits}

Lifecycle:
  extract   — pull candidate learnings from a session transcript/notes -> score -> upsert (reinforce dupes)
  inject    — emit the top-N high-confidence instincts as a capped SessionStart context block
  decay     — age-decay confidence, prune the stale/low tail (keeps the store from growing unbounded)

CLI:
  python3 tools/instinct_loop.py extract --file <session.txt>        (or --text "...", or stdin)
  python3 tools/instinct_loop.py inject  [--top 6] [--min-conf 0.65] [--max-chars 1500]
  python3 tools/instinct_loop.py decay   [--half-life-days 30] [--floor 0.25]
  python3 tools/instinct_loop.py list    [--top 20]

Wire (SessionStart/Stop hooks or the autoflow loops call these). Firewall: hustle-lane store by default;
pass --store for a separate index (work-GPT keeps its own, per lane separation).
"""
from __future__ import annotations
import argparse, json, os, re, sys, time, hashlib, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))  # so the core.local_reason import (Fable primer) resolves
DEFAULT_STORE = ROOT / "data" / "instincts" / "instincts.jsonl"
OLLAMA = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
# prefer the strongest local model that's installed; fall back down the ladder
MODEL_CANDIDATES = [
    os.environ.get("INSTINCT_MODEL", ""),
    "qwen3:14b", "qwen2.5:7b",
]
NOW = time.time()
DAY = 86400.0


def _models_available() -> set:
    try:
        with urllib.request.urlopen(f"{OLLAMA}/api/tags", timeout=5) as r:
            return {m["name"] for m in json.loads(r.read()).get("models", [])}
    except Exception:
        return set()


def _pick_model() -> str | None:
    avail = _models_available()
    for m in MODEL_CANDIDATES:
        if m and (m in avail or any(a.startswith(m.split(":")[0]) for a in avail)):
            return m
    return next(iter(avail), None) if avail else None


LLM_TIMEOUT = float(os.environ.get("INSTINCT_LLM_TIMEOUT", "120"))


def _llm(prompt: str, model: str) -> str:
    # Prefer the shared Fable-primed local-reasoning entry (extraction is a judgment task, so it
    # reasons at Fable's bar), keeping this job's model choice. Falls back to a bare stdlib call
    # if core isn't importable (e.g. a minimal python) — still local, just unprimed.
    try:
        from core.local_reason import local_generate
        return local_generate(prompt, model=model, reason=True, condensed=True,
                               num_ctx=8192, timeout=LLM_TIMEOUT)
    except Exception:
        pass
    body = json.dumps({"model": model, "prompt": prompt, "stream": False,
                       "options": {"temperature": 0.1, "num_ctx": 8192}}).encode()
    req = urllib.request.Request(f"{OLLAMA}/api/generate", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as r:
        out = json.loads(r.read()).get("response", "")
    # Hermes-4 is a reasoning model — strip any <think>...</think> before parsing
    return re.sub(r"<think>.*?</think>", "", out, flags=re.S).strip()


def _iid(text: str) -> str:
    return hashlib.sha1(re.sub(r"\s+", " ", text.lower()).strip().encode()).hexdigest()[:12]


def _load(store: Path) -> dict:
    d = {}
    if store.exists():
        for line in store.read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    r = json.loads(line); d[r["id"]] = r
                except Exception:
                    pass
    return d


def _save(store: Path, d: dict) -> None:
    store.parent.mkdir(parents=True, exist_ok=True)
    tmp = store.with_suffix(".tmp")
    tmp.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in d.values()) + "\n")
    tmp.replace(store)


_EXTRACT_PROMPT = """You extract durable OPERATING LEARNINGS from an AI agent's work session — the kind
worth remembering for next time (a gotcha, a working approach, a decision + its why, a rule).

Return STRICT JSON: a list of at most 6 objects, each:
  {"text": "<one crisp, self-contained learning, imperative or factual>",
   "confidence": <0.0-1.0 — how sure/durable/reusable this is>,
   "tags": ["<1-3 lowercase topic tags>"]}

Rules: only genuinely reusable learnings (skip one-off trivia, restated obvious facts, or anything
session-specific). If nothing durable, return []. No prose, JSON only.

SESSION:
{session}
"""


def _heuristic_extract(text: str) -> list:
    """Deterministic fallback if no local model — pull lines that look like learnings/decisions."""
    out = []
    for ln in text.splitlines():
        s = ln.strip("-*• \t")
        if 25 <= len(s) <= 240 and re.search(
                r"\b(learned|gotcha|always|never|root cause|fix(ed)? =|decided|rule:|because|turns out|"
                r"must|don'?t|note:)\b", s, re.I):
            out.append({"text": s, "confidence": 0.5, "tags": ["heuristic"]})
    return out[:6]


def _extract_candidates(text: str, model: str | None) -> list:
    """Extract learning candidates from a chunk of session text.

    Trust the model when its call succeeds and yields a parseable JSON array — even an
    empty one (a good model saying "nothing durable here" beats the noisy heuristic).
    Only fall back to the keyword heuristic when there's no model or the call errors out.
    """
    text = text.strip()[-16000:]  # cap the tail we feed the model
    if not text:
        return []
    if model:
        try:
            raw = _llm(_EXTRACT_PROMPT.replace("{session}", text), model)
            m = re.search(r"\[.*\]", raw, re.S)
            if m:
                return json.loads(m.group(0))
        except Exception as e:
            print(f"[instinct] llm extract failed ({e}); heuristic fallback", file=sys.stderr)
    return _heuristic_extract(text)


def _ingest(d: dict, cands: list) -> tuple[int, int]:
    """Upsert candidates into the store dict; reinforce dupes. Returns (added, reinforced)."""
    added = reinforced = 0
    for c in cands:
        t = str(c.get("text", "")).strip()
        if not t or len(t) < 12:
            continue
        conf = max(0.0, min(1.0, float(c.get("confidence", 0.5) or 0.5)))
        tags = [str(x).lower() for x in (c.get("tags") or [])][:3]
        iid = _iid(t)
        if iid in d:  # reinforce: nudge confidence up, refresh recency
            r = d[iid]
            r["confidence"] = round(min(1.0, r["confidence"] + 0.08 * (1 - r["confidence"])), 4)
            r["hits"] = r.get("hits", 1) + 1
            r["last_seen"] = NOW
            reinforced += 1
        else:
            d[iid] = {"id": iid, "text": t, "tags": tags, "confidence": round(conf, 4),
                      "created": NOW, "last_seen": NOW, "hits": 1}
            added += 1
    return added, reinforced


def cmd_extract(a):
    store = Path(a.store)
    text = a.text or (Path(a.file).read_text() if a.file else sys.stdin.read())
    if not text.strip():
        print(json.dumps({"ok": False, "error": "empty session"})); return
    model = _pick_model()
    cands = _extract_candidates(text, model)
    d = _load(store)
    added, reinforced = _ingest(d, cands)
    _save(store, d)
    print(json.dumps({"ok": True, "model": model, "candidates": len(cands),
                      "added": added, "reinforced": reinforced, "total": len(d)}))


def _transcript_text(path: Path) -> str:
    """Pull human-readable user+assistant text out of a Claude Code session .jsonl transcript."""
    parts = []
    try:
        lines = path.read_text(errors="ignore").splitlines()
    except Exception:
        return ""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        msg = rec.get("message") if isinstance(rec, dict) else None
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "")
        if role not in ("user", "assistant"):
            continue
        content = msg.get("content")
        if isinstance(content, str):
            txt = content
        elif isinstance(content, list):
            chunks = []
            for b in content:
                if isinstance(b, dict) and b.get("type") == "text" and b.get("text"):
                    chunks.append(str(b["text"]))
            txt = "\n".join(chunks)
        else:
            txt = ""
        txt = txt.strip()
        # skip tool-result noise / system-reminder dumps that aren't learnings
        if txt and not txt.startswith("<local-command") and "caveat" not in txt[:40].lower():
            parts.append(f"{role}: {txt}")
    return "\n\n".join(parts)


def cmd_harvest(a):
    """Mine the most-recent Claude Code session transcripts for durable learnings (scheduled job)."""
    store = Path(a.store)
    tdir = Path(a.transcripts_dir).expanduser()
    wm = Path(a.watermark) if a.watermark else store.parent / ".harvest_watermark"
    since = 0.0
    if wm.exists():
        try:
            since = float(wm.read_text().strip())
        except Exception:
            since = 0.0
    files = sorted((f for f in tdir.glob("*.jsonl") if f.stat().st_mtime > since),
                   key=lambda f: f.stat().st_mtime)[-a.max_files:]
    if not files:
        print(json.dumps({"ok": True, "harvested": 0, "note": "no transcripts newer than watermark"}))
        return
    model = _pick_model()
    d = _load(store)
    total_added = total_reinf = 0
    newest = since
    for f in files:
        newest = max(newest, f.stat().st_mtime)
        text = _transcript_text(f)
        if not text.strip():
            continue
        cands = _extract_candidates(text, model)
        added, reinforced = _ingest(d, cands)
        total_added += added; total_reinf += reinforced
    _save(store, d)
    if a.prune:  # age-decay + drop the stale tail so the store stays bounded
        kept = {}
        for iid, r in d.items():
            age = (NOW - r.get("last_seen", r.get("created", NOW))) / DAY
            r["confidence"] = round(r["confidence"] * (0.5 ** (age / max(1.0, a.half_life_days))), 4)
            if r["confidence"] >= a.floor:
                r["last_seen"] = NOW; kept[iid] = r
        _save(store, kept); d = kept
    wm.parent.mkdir(parents=True, exist_ok=True)
    wm.write_text(str(newest))
    print(json.dumps({"ok": True, "model": model, "files": len(files),
                      "added": total_added, "reinforced": total_reinf, "total": len(d)}))


def cmd_inject(a):
    store = Path(a.store)
    d = _load(store)
    # effective confidence = stored confidence decayed by recency (so stale learnings fade from injection)
    def eff(r):
        age = (NOW - r.get("last_seen", r.get("created", NOW))) / DAY
        return r["confidence"] * (0.5 ** (age / max(1.0, a.half_life_days)))
    ranked = sorted((r for r in d.values() if eff(r) >= a.min_conf),
                    key=lambda r: (eff(r), r.get("hits", 1)), reverse=True)
    picked, buf, used = [], [], 0
    for r in ranked[: a.top * 2]:
        line = f"- {r['text']}"
        if used + len(line) + 1 > a.max_chars:
            break
        picked.append(r); buf.append(line); used += len(line) + 1
        if len(picked) >= a.top:
            break
    if a.json:
        print(json.dumps({"ok": True, "count": len(picked),
                          "instincts": [{"text": r["text"], "confidence": round(eff(r), 3)} for r in picked]}))
    else:
        if picked:
            print("## Learned instincts (high-confidence, from prior sessions)")
            print("\n".join(buf))


def cmd_decay(a):
    store = Path(a.store)
    d = _load(store)
    kept = {}
    pruned = 0
    for iid, r in d.items():
        age = (NOW - r.get("last_seen", r.get("created", NOW))) / DAY
        r["confidence"] = round(r["confidence"] * (0.5 ** (age / max(1.0, a.half_life_days))), 4)
        if r["confidence"] >= a.floor:
            r["last_seen"] = NOW  # decay applied; reset the clock so it's not double-decayed
            kept[iid] = r
        else:
            pruned += 1
    _save(store, kept)
    print(json.dumps({"ok": True, "kept": len(kept), "pruned": pruned}))


def cmd_list(a):
    d = _load(Path(a.store))
    ranked = sorted(d.values(), key=lambda r: (r["confidence"], r.get("hits", 1)), reverse=True)
    for r in ranked[: a.top]:
        print(f"  {r['confidence']:.2f} x{r.get('hits',1):<2} [{','.join(r.get('tags',[]))}] {r['text']}")
    print(f"({len(d)} instincts total)")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--store", default=str(DEFAULT_STORE))
    sub = p.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("extract"); e.add_argument("--file"); e.add_argument("--text"); e.set_defaults(fn=cmd_extract)
    h = sub.add_parser("harvest")
    h.add_argument("--transcripts-dir", dest="transcripts_dir",
                   default=str(Path.home() / ".claude" / "projects" / "-Users-user"))
    h.add_argument("--watermark", default="")
    h.add_argument("--max-files", dest="max_files", type=int, default=3)
    h.add_argument("--prune", action="store_true")
    h.add_argument("--half-life-days", dest="half_life_days", type=float, default=45.0)
    h.add_argument("--floor", type=float, default=0.2)
    h.set_defaults(fn=cmd_harvest)
    i = sub.add_parser("inject"); i.add_argument("--top", type=int, default=6); i.add_argument("--min-conf", dest="min_conf", type=float, default=0.65)
    i.add_argument("--max-chars", dest="max_chars", type=int, default=1500); i.add_argument("--half-life-days", dest="half_life_days", type=float, default=30.0)
    i.add_argument("--json", action="store_true"); i.set_defaults(fn=cmd_inject)
    dc = sub.add_parser("decay"); dc.add_argument("--half-life-days", dest="half_life_days", type=float, default=30.0); dc.add_argument("--floor", type=float, default=0.25); dc.set_defaults(fn=cmd_decay)
    ls = sub.add_parser("list"); ls.add_argument("--top", type=int, default=20); ls.set_defaults(fn=cmd_list)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
