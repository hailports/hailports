#!/usr/bin/env python3
"""Voice learner — make drafts read more like Operator over time (feeds work_reply_voice).

work_reply_voice is a STATIC register gate: it knows the rules Operator told us once. This is
the LEARNING half. Ground truth of Operator's voice isn't a doc — it's the EDIT: what the clone
drafted vs. what Operator actually sent. Diff the two and the deletions/insertions ARE his voice
("he always cuts 'just' and 'i wanted to'"). Mine that continuously, plus his sent-email
finals and his genuine operator inbounds, into a learned-voice profile the drafters read.

THE DISCIPLINE THAT MATTERS (why this isn't just a diff logger):
  - Don't overfit to one email. A single deletion is a one-off, not a voice rule. A phrase
    only becomes a RULE when it's edited the same way across >= MIN_OCCURRENCES DISTINCT
    pairs AND isn't contradicted (removed 3x, never added) — confidence-gated.
  - Lane-aware. Work voice != hustle voice. Operator's work register is lowercase-to-directors,
    first-name, and keeps his natural lmk/w//b/c/qs/&/+ shorthand; only u/ur/thx are too
    texty. So each lane has its own profile, and the work lane never learns to ADD those
    three banned forms even if noisy finals suggest it (register guard).
  - Feed systematic misses back. When a remove-rule gets promoted (a repeated voice miss),
    push it into reaction_grader's injected corrections block + memory, so the live prompt
    path recalls "cut 'just'" the same way it recalls Operator's other corrections.

ARTIFACT: data/learning/voice_profile.json — cumulative, per-lane. work_reply_voice + the
drafters read `rules` (promoted PREFER/AVOID) and the descriptive stats (greeting/sign-off/
sentence-length/shorthand-per-surface). Additive, fail-soft, idempotent (per-pair hash
dedup so re-runs never double-count). Reads offline ledgers; never sends, never bounces a
service, carries no AI/stack trace.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):  # allow `python core/voice_learner.py` (direct-script run)
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import BASE_DIR
from core import work_reply_voice as wrv

LANE_WORK = "work"
LANE_HUSTLE = "hustle"
LANE_CASUAL = "casual"
_LANES = (LANE_WORK, LANE_HUSTLE, LANE_CASUAL)

PROFILE = BASE_DIR / "data" / "learning" / "voice_profile.json"
# staged (clone-draft -> Operator-final-sent) edit pairs, one JSON record per line:
#   {"lane": "work", "draft": "...", "final": "...", "ts": "<iso>"}
DRAFT_EDITS = BASE_DIR / "data" / "learning" / "draft_edits.jsonl"
# sent-email finals (no draft) that still teach descriptive voice (shorthand/greeting/len):
#   glob of *_sent.jsonl records carrying a "body"/"text"/"final" field.
SENT_GLOBS = [BASE_DIR / "data" / "hustle"]
# where promoted voice misses are fed for prompt-time recall (shared with reaction_grader):
CORRECTIONS = BASE_DIR / "data" / "learning" / "correction_rules.md"

MIN_OCCURRENCES = 2      # >= this many DISTINCT pairs before a phrase is a rule (anti-one-off)
MIN_CONFIDENCE = 0.6     # support / (support + contradiction)
MAX_PHRASE_TOKENS = 4    # cap learned phrase length; longer = context, not voice
MAX_SEEN = 8000          # cap the pair-dedup memory

# Too-texty tokens (post-tokenizer form) the WORK lane must never LEARN TO ADD. Operator's
# lmk/w//b/c/qs/&/+ are valid work shorthand and remain learnable.
_WORK_ADD_SUPPRESS = frozenset("u ur thx".split())

_SHORTHAND_OBSERVED = (
    (r"\blmk\b", "lmk"),
    (r"\bw/", "w/"),
    (r"\bb/c\b", "b/c"),
    (r"\bqs\b", "qs"),
    (r"&", "&"),
    (r"\+", "+"),
    *((pat, tok) for pat, _exp, tok in wrv.SHORTHAND),
)

# Structural tokens that carry no voice signal on their own — excluded from SINGLE-TOKEN
# rules so a repeated article/preposition can't masquerade as a learned preference. Filler
# Operator genuinely cuts ("just", "actually", "honestly", "wanted") is deliberately NOT here.
_STRUCT_STOP = frozenset(
    "the a an to of and or in on at is are be for with my your our their it that this these "
    "those i you we they he she as by from but so if then than".split()
)

_TOKEN = re.compile(r"[a-z0-9']+|[&+]")
_SENT_SPLIT = re.compile(r"[.!?]+(?:\s|$)")
_GREET = re.compile(r"^\s*(hey|hi|hello|yo|morning|thanks)\b", re.I)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall((text or "").lower())


def _pair_hash(lane: str, draft: str, final: str) -> str:
    return hashlib.sha1(f"{lane}|{draft}|{final}".encode("utf-8")).hexdigest()[:16]


def _span_phrases(span: list[str]) -> list[str]:
    """A phrasal edit yields BOTH the full contiguous phrase and its atomic tokens, so a
    filler word Operator removes in varying contexts ('just wanted to' one time, 'just flag it'
    the next) is still counted by its common atom 'just' — while the intact phrase 'i wanted
    to' is also kept as a unit. De-duped; the full span capped at MAX_PHRASE_TOKENS."""
    out: list[str] = []
    if 1 < len(span) <= MAX_PHRASE_TOKENS:
        out.append(" ".join(span))
    out.extend(span)  # atomic tokens (structural ones are filtered at promotion time)
    seen, uniq = set(), []
    for p in out:
        if p and p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def _diff_edits(draft: str, final: str) -> tuple[list[str], list[str]]:
    """Token-level diff -> (removed_phrases, added_phrases), each de-duped within the pair
    so `count` means DISTINCT pairs (the anti-one-off guarantee), never repeats in one draft."""
    a, b = _tokens(draft), _tokens(final)
    removed, added = [], []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag in ("delete", "replace") and i2 > i1:
            removed.extend(_span_phrases(a[i1:i2]))
        if tag in ("insert", "replace") and j2 > j1:
            added.extend(_span_phrases(b[j1:j2]))
    return sorted(set(removed)), sorted(set(added))


def _sentence_stats(text: str) -> tuple[int, int]:
    """(sentence_count, word_count) for a running average — descriptive, not gated."""
    parts = [s for s in _SENT_SPLIT.split(text or "") if s.strip()]
    words = len(re.findall(r"[a-z0-9']+", (text or "").lower()))
    return max(len(parts), 1 if words else 0), words


def _greeting(text: str) -> str | None:
    for line in (text or "").splitlines():
        if line.strip():
            m = _GREET.match(line)
            return m.group(1).lower() if m else "(none)"
    return None


def _signoff(text: str) -> str | None:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return None
    low = lines[-1].lower()
    for so in wrv.SIGNOFFS:
        if low.startswith(so.rstrip(", ")):
            return so.strip().rstrip(",")
    return "(none)"


def _shorthand_used(text: str) -> list[str]:
    """Which natural or too-texty shorthand tokens appear in Operator's FINAL text."""
    hits = []
    for pat, tok in _SHORTHAND_OBSERVED:
        if re.search(pat, text or "", re.IGNORECASE):
            hits.append(tok)
    return hits


def _blank_lane() -> dict:
    return {
        "removes": {}, "adds": {},
        "greetings": {}, "signoffs": {},
        "sent_len": {"sentences": 0, "words": 0},
        "shorthand_used": {},
        "pairs": 0, "finals": 0,
        "rules": [],
    }


def _load_profile(path: Path) -> dict:
    try:
        p = json.loads(path.read_text())
    except Exception:
        p = {}
    p.setdefault("updated_at", None)
    p.setdefault("seen_pairs", [])
    lanes = p.setdefault("lanes", {})
    for ln in _LANES:
        lanes.setdefault(ln, _blank_lane())
        for k, v in _blank_lane().items():
            lanes[ln].setdefault(k, v)
    return p


def _bump(counter: dict, key: str, field: str, ts: str) -> None:
    slot = counter.setdefault(key, {"support": 0, "contra": 0, "first_seen": ts, "last_seen": ts})
    slot[field] = slot.get(field, 0) + 1
    slot["last_seen"] = ts


def _promote(lane_name: str, lane: dict) -> list[dict]:
    """Recompute promoted rules from cumulative counters. A phrase is a rule only when it
    clears MIN_OCCURRENCES distinct supporting edits AND MIN_CONFIDENCE (not contradicted)."""
    rules: list[dict] = []

    def emit(kind: str, phrase: str, slot: dict) -> None:
        # a single structural token (article/preposition/pronoun) is noise, not voice
        if " " not in phrase and phrase in _STRUCT_STOP:
            return
        support, contra = slot.get("support", 0), slot.get("contra", 0)
        total = support + contra
        conf = support / total if total else 0.0
        if support >= MIN_OCCURRENCES and conf >= MIN_CONFIDENCE:
            rules.append({
                "lane": lane_name, "kind": kind, "phrase": phrase,
                "count": support, "confidence": round(conf, 2),
                "first_seen": slot.get("first_seen"), "last_seen": slot.get("last_seen"),
            })

    for phrase, slot in lane["removes"].items():
        emit("remove", phrase, slot)
    for phrase, slot in lane["adds"].items():
        # WORK register guard: never learn to ADD only the three too-texty forms.
        if lane_name == LANE_WORK and phrase.strip() in _WORK_ADD_SUPPRESS:
            continue
        emit("add", phrase, slot)

    rules.sort(key=lambda r: (r["count"], r["confidence"]), reverse=True)
    return rules


def _corrections_text(rules: list[dict]) -> list[str]:
    """One durable PREFER/AVOID line per promoted rule, in Operator's terms."""
    out = []
    for r in rules:
        if r["kind"] == "remove":
            out.append(f"Voice ({r['lane']}): Operator cuts \"{r['phrase']}\" from drafts "
                       f"({r['count']}x) — leave it out.")
        else:
            out.append(f"Voice ({r['lane']}): Operator prefers \"{r['phrase']}\" "
                       f"({r['count']}x) — use it.")
    return out


def _feed_corrections(rules: list[dict], corrections_path: Path, remember_fn) -> list[str]:
    """Push newly-promoted voice misses into reaction_grader's injected block + memory, so
    the live prompt path recalls them like any other correction. Additive + deduped."""
    lines = _corrections_text(rules)
    if not lines:
        return []
    existing = []
    if corrections_path.exists():
        existing = [ln.strip() for ln in corrections_path.read_text().splitlines()
                    if ln.strip().startswith("- ")]
    seen, merged = set(), []
    for ln in [f"- {t}" for t in lines] + existing:
        if ln not in seen:
            seen.add(ln)
            merged.append(ln)
        if len(merged) >= 60:
            break
    corrections_path.parent.mkdir(parents=True, exist_ok=True)
    header = "# Learned corrections (auto — Operator's own fixes; treat as hard preferences)\n\n"
    corrections_path.write_text(header + "\n".join(merged) + "\n")
    if remember_fn is not None:
        for t in lines:
            try:
                remember_fn(kind="voice_rule", title=t[:200], body=t, source="voice_learner")
            except Exception:
                pass
    return lines


def _iter_draft_edits(path: Path):
    if not path or not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        draft, final = rec.get("draft"), rec.get("final")
        if draft and final:
            yield (rec.get("lane") or LANE_WORK), str(draft), str(final)


def _iter_sent_finals(globs):
    """Final-only sent text (no draft) — teaches descriptive stats/shorthand, not edit rules."""
    for base in globs or []:
        base = Path(base)
        if not base.exists():
            continue
        for fp in base.glob("*_sent.jsonl"):
            try:
                lines = fp.read_text(encoding="utf-8").splitlines()
            except Exception:
                continue
            lane = LANE_HUSTLE  # everything under data/hustle is the hustle lane by construction
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                body = rec.get("body") or rec.get("final") or rec.get("text")
                if body and isinstance(body, str) and len(body) > 12:
                    yield lane, body


def learn(
    pairs=None,
    finals=None,
    profile_path: Path | None = None,
    draft_edits_path: Path | None = None,
    sent_globs=None,
    corrections_path: Path | None = None,
    remember_fn=None,
    feed: bool = True,
) -> dict:
    """Fold new (draft, final) edit pairs + sent finals into the cumulative voice profile.

    Params exist so the smoke test drives throwaway files and never touches the live profile,
    correction block, or memory. Idempotent: each (lane, draft, final) is hashed and skipped
    if already folded in, so re-runs never double-count.
    """
    profile_path = profile_path or PROFILE
    draft_edits_path = draft_edits_path if draft_edits_path is not None else DRAFT_EDITS
    sent_globs = sent_globs if sent_globs is not None else SENT_GLOBS
    corrections_path = corrections_path or CORRECTIONS

    prof = _load_profile(profile_path)
    seen = set(prof.get("seen_pairs") or [])
    ts = _now()

    # gather edit pairs: explicit arg first (smoke/caller), else the staged ledger
    pair_src = list(pairs) if pairs is not None else list(_iter_draft_edits(draft_edits_path))
    final_src = list(finals) if finals is not None else list(_iter_sent_finals(sent_globs))

    new_pairs = 0
    for item in pair_src:
        lane, draft, final = ((LANE_WORK,) + item) if len(item) == 2 else item
        lane = lane if lane in _LANES else LANE_WORK
        h = _pair_hash(lane, draft, final)
        if h in seen:
            continue
        seen.add(h)
        new_pairs += 1
        L = prof["lanes"][lane]
        L["pairs"] += 1

        removed, added = _diff_edits(draft, final)
        for ph in removed:
            if ph.strip():
                _bump(L["removes"], ph, "support", ts)
                if ph in L["adds"]:          # contradiction bookkeeping (both directions)
                    _bump(L["adds"], ph, "contra", ts)
        for ph in added:
            if ph.strip():
                _bump(L["adds"], ph, "support", ts)
                if ph in L["removes"]:
                    _bump(L["removes"], ph, "contra", ts)

        # descriptive stats come from the FINAL (what Operator actually sent)
        _absorb_final(L, lane, final)

    new_finals = 0
    for item in final_src:
        lane, text = item if len(item) == 2 else (LANE_HUSTLE, item)
        lane = lane if lane in _LANES else LANE_HUSTLE
        h = _pair_hash(lane, "", text)
        if h in seen:
            continue
        seen.add(h)
        new_finals += 1
        L = prof["lanes"][lane]
        L["finals"] += 1
        _absorb_final(L, lane, text)

    promoted_all: list[dict] = []
    for lane in _LANES:
        rules = _promote(lane, prof["lanes"][lane])
        prof["lanes"][lane]["rules"] = rules
        promoted_all.extend(rules)

    prof["seen_pairs"] = list(seen)[-MAX_SEEN:]
    prof["updated_at"] = ts

    fed: list[str] = []
    if feed:
        fed = _feed_corrections(promoted_all, corrections_path, remember_fn)

    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_text(json.dumps(prof, indent=1, ensure_ascii=False))

    return {
        "new_pairs": new_pairs,
        "new_finals": new_finals,
        "rules": promoted_all,
        "rules_by_lane": {ln: prof["lanes"][ln]["rules"] for ln in _LANES},
        "corrections_fed": fed,
        "updated_at": ts,
    }


def _absorb_final(lane_obj: dict, lane_name: str, final: str) -> None:
    sents, words = _sentence_stats(final)
    lane_obj["sent_len"]["sentences"] += sents
    lane_obj["sent_len"]["words"] += words
    g = _greeting(final)
    if g:
        lane_obj["greetings"][g] = lane_obj["greetings"].get(g, 0) + 1
    s = _signoff(final)
    if s:
        lane_obj["signoffs"][s] = lane_obj["signoffs"].get(s, 0) + 1
    # shorthand-per-surface records Operator's natural forms while also surfacing u/ur/thx drift.
    for tok in _shorthand_used(final):
        lane_obj["shorthand_used"][tok] = lane_obj["shorthand_used"].get(tok, 0) + 1


def avg_sentence_len(lane: str, profile_path: Path | None = None) -> float | None:
    prof = _load_profile(profile_path or PROFILE)
    sl = prof["lanes"].get(lane, {}).get("sent_len", {})
    return round(sl["words"] / sl["sentences"], 1) if sl.get("sentences") else None


# ─────────────────────────────── smoke test ───────────────────────────────

def _smoke() -> int:
    """Synthetic (clone-draft, Operator-final) pairs with a CONSISTENT edit — Operator always removes
    'just' and 'i wanted to'. Prove learn() promotes those to voice rules while a phrase edited
    only ONCE stays a one-off; prove natural work shorthand remains learnable while the
    u/ur/thx guard holds, plus lane separation, the reaction_grader feed, and re-run
    idempotency. Fully isolated."""
    import tempfile

    tmp = Path(tempfile.mkdtemp())
    prof = tmp / "voice_profile.json"
    corr = tmp / "correction_rules.md"
    remembered: list[dict] = []

    def collect(**kw):
        remembered.append(kw)

    # 3 WORK pairs: clone opens with "just wanted..." + "i wanted to check"; Operator cuts both.
    # each also drops a DIFFERENT one-off phrase, so no one-off can reach the promotion bar.
    work_pairs = [
        (LANE_WORK,
         "hey Ravi, just wanted to reach out. i wanted to check the deploy status per the sync yesterday",
         "hey Ravi, reach out. check the deploy status"),
        (LANE_WORK,
         "just wanted to flag the perm set. i wanted to confirm the sandbox refresh landed ok honestly",
         "flag the perm set. confirm the sandbox refresh landed"),
        (LANE_WORK,
         "hi team, just wanted to share the numbers. i wanted to note the DPP bands actually shifted",
         "hi team, share the numbers. note the DPP bands shifted"),
    ]
    # Natural "&" is valid on the work lane and should become a learned ADD rule.
    work_amp = [
        (LANE_WORK, "sync the board and the tickets", "sync the board & the tickets"),
        (LANE_WORK, "email Vijay and Ravi today", "email Vijay & Ravi today"),
    ]
    # Too-texty "thx" must still be suppressed even when noisy work finals repeat it.
    work_thx = [
        (LANE_WORK, "checked the view", "checked the view thx"),
        (LANE_WORK, "checked the edit", "checked the edit thx"),
    ]
    # The same natural "&" edit remains valid in the hustle lane too.
    hustle_amp = [
        (LANE_HUSTLE, "the scan and the rebuild are ready", "the scan & the rebuild are ready"),
        (LANE_HUSTLE, "grades and mockups shipped", "grades & mockups shipped"),
    ]

    all_pairs = work_pairs + work_amp + work_thx + hustle_amp
    out = learn(pairs=all_pairs,
                profile_path=prof, corrections_path=corr, remember_fn=collect)

    work_rules = {(r["kind"], r["phrase"]) for r in out["rules_by_lane"][LANE_WORK]}
    hustle_rules = {(r["kind"], r["phrase"]) for r in out["rules_by_lane"][LANE_HUSTLE]}

    fails = []

    # THE core assertion: the repeated edits are rules; the one-offs are not.
    if ("remove", "just") not in work_rules:
        fails.append("'just' not promoted to a work remove-rule despite 3 consistent edits")
    if ("remove", "i wanted to") not in work_rules:
        fails.append("'i wanted to' not promoted despite 3 consistent edits")
    for oneoff in ("per the sync yesterday", "honestly", "actually"):
        if any(p == oneoff for _k, p in work_rules):
            fails.append(f"one-off phrase '{oneoff}' was wrongly promoted to a rule")

    # count/confidence sanity on the headline rule
    just_rule = next((r for r in out["rules_by_lane"][LANE_WORK]
                      if r["kind"] == "remove" and r["phrase"] == "just"), None)
    if not just_rule or just_rule["count"] < MIN_OCCURRENCES:
        fails.append("'just' remove-rule missing MIN_OCCURRENCES support")
    elif just_rule["confidence"] < MIN_CONFIDENCE:
        fails.append("'just' remove-rule below confidence floor")

    # Natural work shorthand remains learnable, while the too-texty guard still holds.
    if ("add", "&") not in work_rules:
        fails.append("work lane did NOT learn the valid '&' add")
    if ("add", "thx") in work_rules:
        fails.append("work lane learned to ADD banned 'thx' — register guard failed")
    if _WORK_ADD_SUPPRESS != frozenset({"u", "ur", "thx"}):
        fails.append(f"work shorthand guard drifted: {sorted(_WORK_ADD_SUPPRESS)}")
    # The same natural edit remains independently learnable in the hustle lane.
    if ("add", "&") not in hustle_rules:
        fails.append("hustle lane did NOT learn the '&' add — lane separation broken")

    # reaction_grader feed: the promoted voice miss became a durable correction line + memory
    if not corr.exists() or "cuts \"just\"" not in corr.read_text():
        fails.append("promoted voice miss not fed to the correction block")
    if not any(r.get("kind") == "voice_rule" for r in remembered):
        fails.append("voice rule not remembered for prompt-time recall")

    # idempotency: re-running over the SAME pairs folds in nothing new and doesn't inflate counts
    out2 = learn(pairs=all_pairs,
                 profile_path=prof, corrections_path=corr, remember_fn=collect)
    if out2["new_pairs"] != 0:
        fails.append(f"re-run not idempotent: folded {out2['new_pairs']} pairs again")
    just_rule2 = next((r for r in out2["rules_by_lane"][LANE_WORK]
                       if r["kind"] == "remove" and r["phrase"] == "just"), None)
    if just_rule2 and just_rule2["count"] != just_rule["count"]:
        fails.append("re-run double-counted the 'just' support")

    if fails:
        print("SMOKE FAIL:")
        for m in fails:
            print("  -", m)
        rc = 1
    else:
        print("SMOKE OK — consistent edits become voice rules; one-offs don't.")
        print(f"  work rules:   {sorted(work_rules)}")
        print(f"  hustle rules: {sorted(hustle_rules)}")
        print(f"  'just' -> count={just_rule['count']} conf={just_rule['confidence']} (promoted)")
        print(f"  work '&' learned = {('add','&') in work_rules}; "
              f"work 'thx' suppressed = {('add','thx') not in work_rules}")
        print(f"  fed to reaction_grader: {out['corrections_fed'][:2]}")
        print(f"  idempotent re-run: new_pairs={out2['new_pairs']}")
        rc = 0

    try:
        for p in (prof, corr):
            if p.exists():
                p.unlink()
        tmp.rmdir()
    except Exception:
        pass
    return rc


def main() -> int:
    ap = argparse.ArgumentParser(description="learn Operator's voice from draft->sent edits")
    ap.add_argument("--smoke", action="store_true", help="run the isolated smoke test")
    ap.add_argument("--no-feed", action="store_true", help="don't push to the corrections block")
    a = ap.parse_args()
    if a.smoke:
        return _smoke()
    try:
        from core import redacted_memory
        remember_fn = redacted_memory.remember
    except Exception:
        remember_fn = None
    res = learn(feed=not a.no_feed, remember_fn=remember_fn)
    print(json.dumps({
        "new_pairs": res["new_pairs"],
        "new_finals": res["new_finals"],
        "promoted_rules": len(res["rules"]),
        "rules": res["rules"][:20],
        "corrections_fed": res["corrections_fed"][:10],
    }, indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
