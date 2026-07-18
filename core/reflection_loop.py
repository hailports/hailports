#!/usr/bin/env python3
"""Nightly REFLECTION — "what did I learn today, what patterns repeated" → durable lessons.

The last mile of the retrieval brain. Phase 1 gave the stack CLEAN signal:
  - clean_capture   → tagged operator/machine exchange ledger (exchanges.jsonl)
  - reaction_grader → graded outcomes (hit/miss/correction) (graded_outcomes.jsonl)
  - error_memory    → recurring-crash signatures + fixes (never-twice layer)
  - alert_gateway   → issue lifecycle (recur / resolve)

None of that becomes a DURABLE LESSON on its own. This loop closes it: once a night it
reads the day's real signal and DETERMINISTICALLY surfaces only repetition/outcome that
actually happened —
  * the SAME correction landing twice        → a PREFER/AVOID lesson (never correct twice)
  * the SAME error signature seen ≥2×         → a recurring-failure lesson (never-twice tie-in)
  * an alert issue that went active → resolved → a "how it got fixed" lesson worth keeping
  * the SAME kind of ask handled well ≥2×     → a working-pattern lesson worth crystallizing
Then a $0 LOCAL model (ollama) phrases each into a durable one-liner (with a deterministic
template fallback if the model is down), and (dry=False) writes it via redacted_memory.remember
so the live prompt path recalls + injects it later.

HONESTY (hard): a lesson is NEVER invented. Every candidate MUST carry ≥1 evidence ref
tracing to a real repeated correction / repeated error / real resolution, or it is dropped.
Single, one-off events never become lessons.

LANE FIREWALL (hard): work ⟂ hustle. Each lane reads ONLY its own sources and writes ONLY
to its own sink:
  - work   sources: graded_outcomes + exchanges (operator grades on the work-GPT / iMessage)
           sink:    redacted_memory.remember(kind="reflection_lesson")   [the work brain]
  - hustle sources: error_memory + alert_gateway (stack-infra failures/resolutions)
           sink:    data/hustle/reflection_lessons.jsonl                [hustle store]
A work reflect never touches a hustle source or sink, and vice-versa.

Additive + fail-soft. Reads offline ledgers; the sink writes are idempotent (a per-lesson
key hash in reflection_loop_state.json prevents re-writing the same lesson on the next night).
Never touches any live service or hot path.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

if __package__ in (None, ""):  # allow `python core/reflection_loop.py`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import BASE_DIR

# ---- lanes ----------------------------------------------------------------
WORK = "work"
HUSTLE = "hustle"

# ---- source ledgers (per lane) --------------------------------------------
EX_LEDGER = BASE_DIR / "data" / "learning" / "exchanges.jsonl"          # work
GRADED = BASE_DIR / "data" / "learning" / "graded_outcomes.jsonl"       # work
ERR_MEM = BASE_DIR / "data" / "hustle" / "error_memory.json"            # hustle
ALERT_STATE = BASE_DIR / "data" / "alert_gateway_state.json"            # hustle

# ---- sinks / outputs ------------------------------------------------------
HUSTLE_LESSONS = BASE_DIR / "data" / "hustle" / "reflection_lessons.jsonl"
DIGEST_OUT = BASE_DIR / "data" / "learning" / "reflection_digest.md"
STATE = BASE_DIR / "data" / "learning" / "reflection_loop_state.json"

# repetition threshold — a lesson requires the pattern to have happened at least twice
MIN_REPEAT = 2
# a chronic unresolved issue must have re-fired across at least this span to count
CHRONIC_SPAN_S = 3 * 24 * 3600

LOCAL_MODEL = "qwen2.5:7b"
OLLAMA_URL = "http://localhost:11434/api/generate"

_STOP = set(
    "the a an is are was were be been being to of and or for in on at it that this these those "
    "i you we he she they my your our their me us him her them do does did done not no yes can "
    "could would should will shall may might must with without from into over under again more "
    "most some any all each every no nor so than then there here what which who whom whose why "
    "how when where s t re ll ve d m o pull redo fix it that wrong actually instead please".split()
)


# ============================================================================
# small io helpers
# ============================================================================
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iter_jsonl(path: Path):
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except Exception:
                continue


def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _parse_ts(val) -> datetime | None:
    """Parse an iso8601 string OR an epoch float/int into an aware UTC datetime."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        try:
            return datetime.fromtimestamp(float(val), tz=timezone.utc)
        except Exception:
            return None
    s = str(val).strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        try:  # bare epoch-in-string
            return datetime.fromtimestamp(float(s), tz=timezone.utc)
        except Exception:
            return None


def _content_words(text: str) -> set:
    """Significant content words of a message (lowercased, stopwords + short tokens dropped)."""
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {w for w in words if len(w) > 2 and w not in _STOP}


def _topic_key(text: str, n: int = 6) -> str:
    """Stable label for a cluster: the sorted content words, first n."""
    ws = _content_words(text)
    return " ".join(sorted(ws)[:n]) if ws else ""


# minimum shared significant words for two messages to be "the same subject"
_OVERLAP_MIN = 3


def _cluster_by_overlap(items: list[dict], text_of) -> list[list[dict]]:
    """Greedily group records whose content-word sets share ≥_OVERLAP_MIN words (or Jaccard
    ≥0.5 for short messages). Deterministic (input order), robust to rewordings of one subject."""
    clusters: list[dict] = []  # {"words": set, "recs": list}
    for it in items:
        ws = _content_words(text_of(it))
        if not ws:
            continue
        best = None
        for cl in clusters:
            shared = len(ws & cl["words"])
            union = len(ws | cl["words"]) or 1
            if shared >= _OVERLAP_MIN or (shared / union) >= 0.5:
                best = cl
                break
        if best is None:
            clusters.append({"words": set(ws), "recs": [it]})
        else:
            best["words"] |= ws
            best["recs"].append(it)
    return [cl["recs"] for cl in clusters]


# ============================================================================
# deterministic pattern detectors  (each returns candidate dicts w/ evidence)
# ============================================================================
def _candidate(lane, category, key, count, evidence, confidence, seed_title, seed_body):
    return {
        "lane": lane,
        "category": category,
        "key": key,
        "count": count,
        "evidence": evidence,            # list of {source, ref, detail} — never empty
        "confidence": round(confidence, 2),
        "seed_title": seed_title,        # deterministic phrasing (model may refine)
        "seed_body": seed_body,
    }


def _detect_from_graded(records: list[dict]) -> list[dict]:
    """WORK: cluster graded outcomes by topic; a label repeated ≥MIN_REPEAT is a pattern.
    correction≥2 = never-correct-twice lesson; miss≥2 = recurring blind spot;
    hit≥2 = a working pattern worth crystallizing."""
    by_label: dict[str, list[dict]] = {}
    for r in records:
        label = r.get("label")
        if label not in ("correction", "miss", "hit"):
            continue
        if not _content_words(r.get("asked", "")):
            continue
        by_label.setdefault(label, []).append(r)

    out = []
    for label, recs_l in by_label.items():
        for recs in _cluster_by_overlap(recs_l, lambda r: r.get("asked", "")):
            if len(recs) < MIN_REPEAT:
                continue
            key = _topic_key(recs[-1].get("asked", ""))
            evidence = [{
                "source": "graded_outcomes.jsonl",
                "ref": r.get("at") or r.get("graded_at") or "",
                "detail": f"asked={r.get('asked','')[:80]!r} reaction={r.get('reaction','')[:60]!r}",
            } for r in recs]
            subject = recs[-1].get("asked", "")[:120]
            if label == "correction":
                out.append(_candidate(
                    WORK, "repeated_correction", key, len(recs), evidence, 0.9,
                    f"corrected {len(recs)}× on: {subject}",
                    f"Operator corrected the same thing {len(recs)} times re: {subject!r}. "
                    f"Latest correction: {recs[-1].get('reaction','')[:160]!r}. "
                    f"Do it his way the first time next time.",
                ))
            elif label == "miss":
                out.append(_candidate(
                    WORK, "recurring_miss", key, len(recs), evidence, 0.85,
                    f"missed/re-asked {len(recs)}× on: {subject}",
                    f"The same ask about {subject!r} didn't land {len(recs)} times "
                    f"(re-asked). Blind spot to close.",
                ))
            else:  # hit
                out.append(_candidate(
                    WORK, "successful_pattern", key, len(recs), evidence, 0.7,
                    f"handled well {len(recs)}× on: {subject}",
                    f"The approach to {subject!r} landed cleanly {len(recs)} times. "
                    f"Keep doing it that way.",
                ))
    return out


def _detect_repeated_operator_corrections(exchanges: list[dict]) -> list[dict]:
    """WORK fallback (when graded_outcomes is empty): find operator_inbound turns that are
    reactions to a machine_response and read as corrections, then cluster by topic ≥2."""
    _CORR = re.compile(
        r"\b(no+|nope|not (that|it|right)|wrong|actually|redo|re-?do|i (said|meant)|instead|"
        r"don'?t|stop|incorrect|fix (it|that)|try again|not quite)\b", re.I)
    hits = []
    for r in exchanges:
        if r.get("direction") != "operator_inbound":
            continue
        if not r.get("reaction_to"):
            continue
        text = r.get("text", "")
        if not _CORR.search(text) or not _content_words(text):
            continue
        hits.append(r)

    out = []
    for recs in _cluster_by_overlap(hits, lambda r: r.get("text", "")):
        key = _topic_key(recs[-1].get("text", ""))
        if len(recs) < MIN_REPEAT:
            continue
        evidence = [{
            "source": "exchanges.jsonl",
            "ref": r.get("id") or r.get("ts") or "",
            "detail": f"correction={r.get('text','')[:100]!r}",
        } for r in recs]
        subject = recs[-1].get("text", "")[:120]
        out.append(_candidate(
            WORK, "repeated_correction", key, len(recs), evidence, 0.88,
            f"corrected {len(recs)}× (same subject)",
            f"Operator issued the same correction {len(recs)} times: {subject!r}. "
            f"Apply it up front next time.",
        ))
    return out


def _detect_recurring_errors(err_mem: dict) -> list[dict]:
    """HUSTLE: an error signature seen ≥MIN_REPEAT is a recurring failure (never-twice tie-in)."""
    out = []
    for sig, v in (err_mem or {}).items():
        seen = int(v.get("seen_count", 0) or 0)
        if seen < MIN_REPEAT:
            continue
        agent = v.get("agent", "?")
        err = str(v.get("error", ""))[:160]
        fix = str(v.get("fix", "")).strip()
        evidence = [{
            "source": "error_memory.json",
            "ref": sig[:16],
            "detail": f"agent={agent} seen={seen} auto_fixed={v.get('auto_fixed')} err={err!r}",
        }]
        fix_line = f" Known fix: {fix}" if fix else " No durable fix recorded yet — needs one."
        out.append(_candidate(
            HUSTLE, "recurring_error", sig[:16], seen, evidence,
            0.9 if fix else 0.75,
            f"{agent}: recurring error seen {seen}×",
            f"{agent} has hit {err!r} {seen} times.{fix_line}",
        ))
    return out


def _detect_alert_lessons(alert_state: dict, since: datetime) -> list[dict]:
    """HUSTLE: resolved issues (active→False) = a successful resolution worth crystallizing;
    long-recurring active issues = a chronic pattern worth naming."""
    out = []
    issues = (alert_state or {}).get("issues", {}) or {}
    for iid, v in issues.items():
        first = _parse_ts(v.get("first_seen"))
        last = _parse_ts(v.get("last_seen"))
        if last is None or last < since:
            continue  # nothing happened with this issue in the window
        src = v.get("source", "?")
        subj = str(v.get("subject", ""))[:140]
        sev = v.get("severity", "")
        if not v.get("active", True):
            # a real transition: it fired, then stopped firing = it got resolved
            evidence = [{
                "source": "alert_gateway_state.json",
                "ref": iid[:24],
                "detail": f"source={src} severity={sev} resolved (last_seen={last.date()}) subj={subj!r}",
            }]
            out.append(_candidate(
                HUSTLE, "successful_resolution", iid[:24], 1, evidence, 0.7,
                f"resolved: {subj}",
                f"The issue {subj!r} (from {src}) went active then cleared. "
                f"Record what fixed it so the next occurrence resolves fast.",
            ))
        elif first and (last - first).total_seconds() >= CHRONIC_SPAN_S:
            span_days = int((last - first).total_seconds() // 86400)
            evidence = [{
                "source": "alert_gateway_state.json",
                "ref": iid[:24],
                "detail": f"source={src} severity={sev} still active, re-firing {span_days}d "
                          f"(first={first.date()} last={last.date()}) subj={subj!r}",
            }]
            out.append(_candidate(
                HUSTLE, "recurring_issue", iid[:24], span_days, evidence, 0.8,
                f"chronic {span_days}d: {subj}",
                f"{subj!r} (from {src}) has re-fired for {span_days} days without resolving. "
                f"Chronic — needs a root-cause fix, not another alert.",
            ))
    return out


# ============================================================================
# $0 local summarizer (fail-soft to the deterministic seed)
# ============================================================================
def _ollama(prompt: str, timeout: float = 30.0) -> str:
    payload = json.dumps({
        "model": LOCAL_MODEL,
        "prompt": f"/no_think\n{prompt}",
        "stream": False,
        "options": {"temperature": 0.2, "num_predict": 120, "top_p": 0.9},
    }).encode()
    req = urllib.request.Request(OLLAMA_URL, data=payload,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            txt = json.loads(resp.read().decode()).get("response", "")
    except Exception:
        return ""
    return txt.split("</think>")[-1].strip().strip('"').strip()


def _phrase_lesson(cand: dict, use_model: bool) -> tuple[str, str]:
    """Return (title, body). The deterministic seed is authoritative; the local model may
    only tighten the wording — it is fed ONLY the observed evidence, so it cannot fabricate.
    On any model miss we keep the seed verbatim."""
    title = cand["seed_title"].strip()[:200]
    body = cand["seed_body"].strip()
    if not use_model:
        return title, body
    ev = "; ".join(e["detail"] for e in cand["evidence"][:4])
    prompt = (
        "You are compressing an OBSERVED repeated pattern into ONE durable lesson line for "
        "an operator's memory. Use ONLY the facts below — invent nothing, add no advice not "
        "implied by the facts. Output ONE plain sentence, lowercase, under 30 words.\n\n"
        f"pattern: {cand['category']} (happened {cand['count']}x)\n"
        f"draft: {body}\n"
        f"evidence: {ev}\n\nlesson:"
    )
    refined = _ollama(prompt)
    if refined and 8 <= len(refined) <= 300:
        body = refined
    return title, body


# ============================================================================
# state / dedup
# ============================================================================
def _lesson_hash(cand: dict) -> str:
    return hashlib.sha1(f"{cand['lane']}|{cand['category']}|{cand['key']}".encode()).hexdigest()[:16]


def _load_state() -> dict:
    return _load_json(STATE, {"written": {}})


def _save_state(st: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, indent=1), encoding="utf-8")


# ============================================================================
# main entry
# ============================================================================
def _lane_sources(lane: str, since: datetime) -> list[dict]:
    if lane == WORK:
        graded = [r for r in _iter_jsonl(GRADED)
                  if (_parse_ts(r.get("at") or r.get("graded_at")) or since) >= since]
        cands = _detect_from_graded(graded)
        if not cands:  # fall back to the raw tagged ledger if the grader hasn't run yet
            ex = [r for r in _iter_jsonl(EX_LEDGER)
                  if (_parse_ts(r.get("ts")) or since) >= since]
            cands = _detect_repeated_operator_corrections(ex)
        return cands
    if lane == HUSTLE:
        cands = _detect_recurring_errors(_load_json(ERR_MEM, {}))
        cands += _detect_alert_lessons(_load_json(ALERT_STATE, {}), since)
        return cands
    raise ValueError(f"unknown lane {lane!r}")


def reflect(lane: str = WORK, dry: bool = True, hours: int = 24,
            use_model: bool = True) -> dict:
    """Surface the day's repeated corrections / recurring errors / resolutions for one lane,
    phrase them into durable lessons, and (dry=False) write them to that lane's sink.

    Returns {lane, dry, window_hours, digest, lessons:[...], wrote}.
    Every returned lesson carries ≥1 evidence ref. dry=True writes nothing.
    """
    if lane not in (WORK, HUSTLE):
        raise ValueError(f"unknown lane {lane!r}")
    since = _now() - timedelta(hours=hours)

    cands = _lane_sources(lane, since)
    # honesty gate: drop anything without real evidence (belt-and-suspenders — detectors
    # already guarantee it, but never let an evidence-less lesson through)
    cands = [c for c in cands if c.get("evidence")]
    # strongest first
    cands.sort(key=lambda c: (c["confidence"], c["count"]), reverse=True)

    st = _load_state()
    written = st.get("written", {})

    lessons = []
    wrote = 0
    for c in cands:
        h = _lesson_hash(c)
        already = h in written
        title, body = _phrase_lesson(c, use_model=use_model)
        lesson = {
            "hash": h,
            "lane": c["lane"],
            "category": c["category"],
            "count": c["count"],
            "confidence": c["confidence"],
            "title": title,
            "body": body,
            "evidence": c["evidence"],
            "already_written": already,
        }
        if not dry and not already:
            _write_lesson(lesson)
            written[h] = {"at": _now().isoformat(), "title": title}
            wrote += 1
            lesson["written"] = True
        lessons.append(lesson)

    if not dry:
        st["written"] = written
        st["last_run"] = {"at": _now().isoformat(), "lane": lane, "wrote": wrote}
        _save_state(st)

    digest = _build_digest(lane, lessons, since, hours)
    if not dry and lessons:
        _append_digest(digest)

    return {
        "lane": lane,
        "dry": dry,
        "window_hours": hours,
        "digest": digest,
        "lessons": lessons,
        "wrote": wrote,
    }


def _write_lesson(lesson: dict) -> None:
    """Route to the lane's sink. HARD firewall: work→work brain, hustle→hustle store only."""
    src = "reflection_loop:" + ",".join(sorted({e["source"] for e in lesson["evidence"]}))
    if lesson["lane"] == WORK:
        from core import redacted_memory
        redacted_memory.remember(
            kind="reflection_lesson",
            title=lesson["title"],
            body=lesson["body"],
            source=src,
            confidence=lesson["confidence"],
            metadata={"category": lesson["category"], "count": lesson["count"],
                      "evidence": lesson["evidence"]},
        )
    else:  # HUSTLE — never touches redacted_memory
        HUSTLE_LESSONS.parent.mkdir(parents=True, exist_ok=True)
        rec = {"written_at": _now().isoformat(), "kind": "reflection_lesson",
               "source": src, **{k: lesson[k] for k in
                                 ("category", "count", "confidence", "title", "body", "evidence")}}
        with HUSTLE_LESSONS.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _build_digest(lane: str, lessons: list[dict], since: datetime, hours: int) -> str:
    day = _now().strftime("%Y-%m-%d")
    lines = [f"## today I learned — {day} ({lane} lane, last {hours}h)"]
    fresh = [l for l in lessons if not l["already_written"]]
    if not lessons:
        lines.append("- (no repeated corrections, recurring errors, or resolutions today — "
                     "nothing durable to add)")
        return "\n".join(lines)
    lines.append(f"- {len(lessons)} pattern(s) surfaced, {len(fresh)} new this run:")
    for l in lessons:
        tag = "NEW" if not l["already_written"] else "seen"
        lines.append(f"  - [{tag}] ({l['category']} x{l['count']}) {l['body']}")
    return "\n".join(lines)


def _append_digest(text: str) -> None:
    DIGEST_OUT.parent.mkdir(parents=True, exist_ok=True)
    with DIGEST_OUT.open("a", encoding="utf-8") as f:
        f.write(text + "\n\n")


def run_all(dry: bool = True, hours: int = 24, use_model: bool = True) -> dict:
    """Reflect over BOTH lanes (each firewalled to its own sources+sink)."""
    return {lane: reflect(lane=lane, dry=dry, hours=hours, use_model=use_model)
            for lane in (WORK, HUSTLE)}


# ============================================================================
# smoke test — runs over recent REAL data, then a fixture proving the honesty gate.
# ============================================================================
def _smoke() -> int:
    fails: list[str] = []

    print("=" * 78)
    print("PART 1 — reflect() over RECENT REAL DATA (dry, no model, wide window)")
    print("=" * 78)
    # snapshot the work-brain memory count BEFORE — dry must not change it
    def _mem_count() -> int:
        try:
            from core import redacted_memory
            with redacted_memory._connect() as c:
                return c.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        except Exception:
            return -1

    mem_before = _mem_count()
    hustle_lines_before = sum(1 for _ in _iter_jsonl(HUSTLE_LESSONS))

    real_total = 0
    for lane in (WORK, HUSTLE):
        # wide window so genuinely recurring real signal (e.g. long-lived alerts) shows up
        res = reflect(lane=lane, dry=True, hours=24 * 60, use_model=False)
        n = len(res["lessons"])
        real_total += n
        print(f"\n[{lane}] {n} candidate lesson(s) from real data:")
        print("  " + res["digest"].replace("\n", "\n  "))
        for l in res["lessons"]:
            # HONESTY PROOF: every lesson traces to ≥1 real evidence ref
            if not l["evidence"]:
                fails.append(f"[{lane}] a lesson has NO evidence — fabricated")
            print(f"    · {l['title']}")
            for e in l["evidence"][:3]:
                print(f"        ← {e['source']} :: ref={e['ref']} :: {e['detail']}")

    mem_after_dry = _mem_count()
    hustle_lines_after_dry = sum(1 for _ in _iter_jsonl(HUSTLE_LESSONS))
    if mem_before != -1 and mem_after_dry != mem_before:
        fails.append(f"DRY WROTE TO WORK BRAIN: memories {mem_before} -> {mem_after_dry}")
    if hustle_lines_after_dry != hustle_lines_before:
        fails.append("DRY WROTE TO HUSTLE SINK: reflection_lessons.jsonl line count changed")
    print(f"\n  work-brain memories unchanged by dry run: {mem_before} == {mem_after_dry} "
          f"({'OK' if mem_before == mem_after_dry else 'FAIL'})")
    print(f"  hustle sink unchanged by dry run: {hustle_lines_before} == "
          f"{hustle_lines_after_dry} "
          f"({'OK' if hustle_lines_before == hustle_lines_after_dry else 'FAIL'})")

    print("\n" + "=" * 78)
    print("PART 2 — FIXTURE: detectors fire on repetition, and NEVER on a one-off")
    print("=" * 78)
    # graded fixture: one topic corrected TWICE (=lesson) + one corrected ONCE (=no lesson)
    graded_fixture = [
        {"at": _now().isoformat(), "label": "correction",
         "asked": "assign the monday storyboard ticket to the offshore team",
         "reaction": "no, offshore doesn't own storyboard, reassign it"},
        {"at": _now().isoformat(), "label": "correction",
         "asked": "put the storyboard monday ticket on offshore again",
         "reaction": "wrong again, storyboard is NOT offshore"},
        {"at": _now().isoformat(), "label": "correction",
         "asked": "draft the quarterly pipeline email to leadership",
         "reaction": "actually cc finance too"},   # one-off → must NOT become a lesson
    ]
    g = _detect_from_graded(graded_fixture)
    corr = [c for c in g if c["category"] == "repeated_correction"]
    if len(corr) != 1:
        fails.append(f"expected exactly 1 repeated_correction, got {len(corr)}")
    else:
        c = corr[0]
        if c["count"] != 2:
            fails.append(f"repeated_correction count should be 2, got {c['count']}")
        if len(c["evidence"]) != 2:
            fails.append("repeated_correction should carry 2 evidence refs")
        print(f"  ✓ repeated correction detected (x{c['count']}) w/ {len(c['evidence'])} evidence refs")
    # the one-off correction (pipeline email) must NOT have produced a lesson
    if any("pipeline" in c["key"] or "leadership" in c["key"] for c in g):
        fails.append("FABRICATION: a one-off correction became a lesson")
    else:
        print("  ✓ one-off correction produced NO lesson (no fabrication)")

    # error fixture: seen 3x (=lesson) vs seen 1x (=no lesson)
    err_fixture = {
        "abc123deadbeef": {"agent": "outreach_cron", "seen_count": 3,
                           "error": "ModuleNotFoundError: No module named 'products'",
                           "fix": "touch products/__init__.py", "auto_fixed": True},
        "onceonly000000": {"agent": "some_job", "seen_count": 1,
                           "error": "transient timeout", "fix": ""},
    }
    e = _detect_recurring_errors(err_fixture)
    if len(e) != 1 or e[0]["count"] != 3:
        fails.append(f"recurring_error detector wrong: {[(x['key'], x['count']) for x in e]}")
    else:
        print(f"  ✓ recurring error detected (seen x{e[0]['count']}) traces to error_memory.json")
    if any(x["key"].startswith("onceonly") for x in e):
        fails.append("FABRICATION: a seen-once error became a lesson")

    # alert fixture: one resolved (active False) + one chronic active
    now = _now()
    alert_fixture = {"issues": {
        "resolved_x": {"active": False, "source": "storage_reaper",
                       "subject": "disk pressure cleared", "severity": "warn",
                       "first_seen": (now - timedelta(days=2)).timestamp(),
                       "last_seen": (now - timedelta(hours=1)).timestamp()},
        "chronic_y": {"active": True, "source": "pipeline_health",
                      "subject": "docsapp sent 0 emails", "severity": "info",
                      "first_seen": (now - timedelta(days=9)).timestamp(),
                      "last_seen": (now - timedelta(hours=2)).timestamp()},
        "stale_z": {"active": False, "source": "old", "subject": "ancient",
                    "first_seen": (now - timedelta(days=400)).timestamp(),
                    "last_seen": (now - timedelta(days=390)).timestamp()},  # outside window
    }}
    a = _detect_alert_lessons(alert_fixture, since=now - timedelta(hours=24))
    cats = sorted(c["category"] for c in a)
    if cats != ["recurring_issue", "successful_resolution"]:
        fails.append(f"alert detector categories wrong: {cats}")
    else:
        print("  ✓ resolved alert → successful_resolution; chronic active alert → recurring_issue")
    if any(c["key"] == "stale_z" for c in a):
        fails.append("FABRICATION: an out-of-window alert became a lesson")
    else:
        print("  ✓ out-of-window alert produced NO lesson")

    # every fixture candidate must carry evidence
    for c in g + e + a:
        if not c.get("evidence"):
            fails.append(f"candidate {c['category']}/{c['key']} has no evidence")

    print("\n" + "=" * 78)
    if fails:
        print("SMOKE FAIL:")
        for m in fails:
            print("  -", m)
        return 1
    print("SMOKE OK — real-data reflect ran; dry wrote 0 to both sinks; detectors fire only on "
          f"real repetition/outcome (every lesson evidence-backed); {real_total} real candidate(s) shown.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="nightly reflection → durable lessons")
    ap.add_argument("--lane", choices=[WORK, HUSTLE, "all"], default="all")
    ap.add_argument("--write", action="store_true", help="actually write lessons (default dry)")
    ap.add_argument("--hours", type=int, default=24, help="lookback window")
    ap.add_argument("--no-model", action="store_true", help="skip local model, seed phrasing only")
    ap.add_argument("--smoke", action="store_true", help="run smoke test")
    a = ap.parse_args()
    if a.smoke:
        return _smoke()
    dry = not a.write
    use_model = not a.no_model
    if a.lane == "all":
        out = run_all(dry=dry, hours=a.hours, use_model=use_model)
        for lane, res in out.items():
            print(res["digest"])
            print(f"  ({res['wrote']} written)\n")
    else:
        res = reflect(lane=a.lane, dry=dry, hours=a.hours, use_model=use_model)
        print(res["digest"])
        print(f"  ({res['wrote']} written)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main() if "--smoke" not in sys.argv else _smoke())
