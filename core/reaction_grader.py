#!/usr/bin/env python3
"""Reaction grader — the reward signal the learning stack was missing.

The stack logged every exchange with outcome="answered" (derived from the machine's
OWN response), so nothing ever knew whether an output was good, missed, or corrected.
Without a reward signal, none of the eight downstream learners could actually improve.

This closes that. It now reads the CLEAN substrate (core.clean_capture.pair_exchanges),
which yields {request, response, reaction} triples where direction is tagged AT CAPTURE
TIME — so machine pushes (scoreboards/alerts/digests) are excluded from operator turns
BEFORE grading, instead of being guessed at with a regex. The reaction (Operator's next
genuine inbound in the thread) is the grade on the machine's handling of the request.

  - CORRECTION  ("no", "not that", "actually", "redo", "i said", "wrong", "instead")
                -> the prior output was wrong. Extract a durable PREFER/AVOID rule and
                   remember() it (kind="correction_rule") so it is recalled + injected
                   into future prompts. This is "never correct the same thing twice."
  - MISS/RE-ASK (reaction is a rephrase of the same ask within the window)
                -> the machine didn't land it. Logged as a negative outcome.
  - HIT         ("perfect", "thanks", "nice", "ship it", "yes go", "exactly")
                -> positive. Feeds the hit-rate scoreboard.
  - NEUTRAL     new topic / no signal -> ignored (not every turn is a grade).

Why clean substrate matters (the before/after this fixes): the OLD path walked
conversation.db turns and had to gate machine pushes out with a `_looks_machine` regex.
A push logged under role="user" between the answer and Operator's correction shifted the
role window, so the correction was never graded — the grader produced ~1 real signal.
clean_capture.pair_exchanges SKIPS pushes when reconstructing triples, so the correction
lands as the reaction it actually is.

Additive + fail-soft: reads the append-only exchanges ledger offline, writes a graded-
outcome ledger + correction rules into the memory the live prompt path already recalls.
Never touches engine.py's hot path. Idempotent via a reaction-timestamp watermark.
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from core import BASE_DIR
from core import redacted_memory
from core import clean_capture

LEDGER = BASE_DIR / "data" / "learning" / "graded_outcomes.jsonl"
STATE = BASE_DIR / "data" / "learning" / "reaction_grader_state.json"
RULES = BASE_DIR / "data" / "learning" / "correction_rules.md"  # injected block
LEDGER.parent.mkdir(parents=True, exist_ok=True)

# how close in time the reaction must follow the response to count as a grade
PAIR_WINDOW_S = 45 * 60

_CORRECTION = re.compile(
    r"\b(no+|nope|not (that|it|what|right)|that'?s (not|wrong)|wrong|actually|redo|re-?do|"
    r"i (said|meant|asked)|instead|don'?t|stop|isn'?t (right|what)|incorrect|fix (it|that)|"
    r"try again|not quite|that isn'?t)\b",
    re.I,
)
_POSITIVE = re.compile(
    r"\b(perfect|thanks|thank you|nice|great|awesome|exactly|ship it|yes,? (go|do|send|ship)|"
    r"love it|beautiful|nailed it|that'?s it|correct|works|good (job|call|stuff)|clean)\b",
    re.I,
)
_STOP = set("the a an is are to of and or for in on at it that this be i you we my your our with "
            "can could would should do did go get got make made just now then so ok okay please".split())


def _tokens(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if w not in _STOP and len(w) > 2}


def _similar(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _classify(prev_text: str, next_text: str, gap_s: float) -> tuple[str, float]:
    """Grade the machine's handling of prev_text using Operator's next_text reaction."""
    nxt = (next_text or "").strip()
    if not nxt or gap_s > PAIR_WINDOW_S:
        return "neutral", 0.0
    # a correction marker in the first clause is the strongest signal
    head = nxt[:120]
    if _CORRECTION.search(head):
        return "correction", 0.9
    if _POSITIVE.search(head):
        return "hit", 0.8
    sim = _similar(prev_text, nxt)
    if sim >= 0.5:
        # re-asking the same thing a different way => the machine missed it
        return "miss", sim
    return "neutral", sim


def _load_state(state_path: Path) -> dict:
    try:
        st = json.loads(state_path.read_text())
    except Exception:
        st = {}
    st.setdefault("watermark_ts", "")
    for k in ("hits", "misses", "corrections", "graded"):
        st.setdefault(k, 0)
    return st


def _save_state(st: dict, state_path: Path) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(st, indent=1))


_TRIVIAL = re.compile(r"^(hi|hey|hello|yo|ok|okay|k|ty|thx|\W*|https?://\S+)$", re.I)


def _is_trivial(text: str) -> bool:
    t = (text or "").strip()
    return len(t) < 4 or bool(_TRIVIAL.match(t)) or t == "￼"


def _ts(iso: str) -> float:
    try:
        return datetime.fromisoformat((iso or "").replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _extract_rule(prev_text: str, correction_text: str) -> str:
    """Turn a correction into a one-line durable PREFER/AVOID rule."""
    corr = correction_text.strip().rstrip(".")
    ask = prev_text.strip()[:120]
    return f"When asked about \"{ask}\" — Operator corrected with: \"{corr[:200]}\". Do it his way next time."


def _write_rules_block(rules: list[str], rules_path: Path) -> None:
    """Rewrite the injected corrections block (most-recent-first, capped)."""
    existing = []
    if rules_path.exists():
        existing = [ln.strip() for ln in rules_path.read_text().splitlines()
                    if ln.strip().startswith("- ")]
    merged = [f"- {r}" for r in rules] + existing
    seen, out = set(), []
    for ln in merged:
        if ln not in seen:
            seen.add(ln)
            out.append(ln)
        if len(out) >= 40:
            break
    rules_path.parent.mkdir(parents=True, exist_ok=True)
    rules_path.write_text(
        "# Learned corrections (auto — Operator's own fixes; treat as hard preferences)\n\n"
        + "\n".join(out) + "\n"
    )


def run(
    dry: bool = False,
    limit: int | None = None,
    source_path: Path | None = None,
    state_path: Path | None = None,
    rules_path: Path | None = None,
    remember_fn=None,
) -> dict:
    """Grade every new clean triple. Idempotent via a reaction-ts watermark.

    Params exist so the smoke test can drive a throwaway ledger/state without
    touching the live substrate or redacted_memory."""
    source_path = source_path or clean_capture.LEDGER
    state_path = state_path or STATE
    rules_path = rules_path or RULES
    remember_fn = remember_fn if remember_fn is not None else redacted_memory.remember

    st = _load_state(state_path)
    wm = st["watermark_ts"]
    triples = clean_capture.pair_exchanges(path=source_path)  # most-recent-first

    graded, new_rules, max_ts = [], [], wm
    processed = 0
    for t in triples:
        react = t.get("reaction")
        if react is None:
            continue  # ungraded pair (no reaction yet)
        req, ans = t["request"], t["response"]
        r_ts = react.get("ts") or ""
        if r_ts <= wm:
            continue  # already graded (watermark is strictly-greater)
        if limit and processed >= limit:
            break
        processed += 1
        if r_ts > max_ts:
            max_ts = r_ts
        if _is_trivial(react.get("text", "")) or _is_trivial(req.get("text", "")):
            continue
        gap = _ts(r_ts) - _ts(ans.get("ts", ""))
        label, conf = _classify(req.get("text", ""), react.get("text", ""), gap)
        if label == "neutral":
            continue
        rec = {
            "graded_at": datetime.now(timezone.utc).isoformat(),
            "at": r_ts,
            "channel": react.get("channel", ""),
            "thread_id": react.get("thread_id", ""),
            "reaction_id": react.get("id", ""),
            "label": label,
            "confidence": round(conf, 2),
            "asked": (req.get("text") or "")[:280],
            "answer": (ans.get("text") or "")[:200],
            "reaction": (react.get("text") or "")[:280],
        }
        graded.append(rec)
        if label == "hit":
            st["hits"] += 1
        elif label == "miss":
            st["misses"] += 1
        elif label == "correction":
            st["corrections"] += 1
            new_rules.append(_extract_rule(req.get("text", ""), react.get("text", "")))
        st["graded"] += 1

    if not dry:
        if graded:
            # graded ledger stays alongside the state file so the smoke run stays isolated
            ledger_out = LEDGER if state_path is STATE else (state_path.parent / "graded_outcomes.jsonl")
            ledger_out.parent.mkdir(parents=True, exist_ok=True)
            with ledger_out.open("a") as f:
                for rec in graded:
                    f.write(json.dumps(rec) + "\n")
        if new_rules:
            _write_rules_block(new_rules, rules_path)
            for rule in new_rules:
                try:
                    remember_fn(
                        kind="correction_rule",
                        title=rule[:200],
                        body=rule,
                        source="reaction_grader",
                    )
                except Exception:
                    pass
        st["watermark_ts"] = max_ts
        _save_state(st, state_path)

    total_signal = st["hits"] + st["misses"] + st["corrections"]
    hit_rate = round(st["hits"] / total_signal, 3) if total_signal else None
    return {
        "new_graded": len(graded),
        "new_corrections": len(new_rules),
        "rules": new_rules,
        "cum": {k: st[k] for k in ("hits", "misses", "corrections", "graded")},
        "hit_rate": hit_rate,
        "watermark_ts": st.get("watermark_ts"),
    }


# ─────────────────────────────── smoke test ───────────────────────────────

def _old_dirty_grade(turns: list[dict]) -> int:
    """Replica of the OLD dirty-source algorithm: role-window walk over conversation
    turns where a machine push was logged under role='user'. Returns corrections found.
    Used only to prove the before/after — NOT part of the live path."""
    corrections = 0
    for i in range(2, len(turns)):
        req, ans, react = turns[i - 2], turns[i - 1], turns[i]
        if req["role"] != "user" or ans["role"] != "assistant" or react["role"] != "user":
            continue
        if _CORRECTION.search((react["text"] or "")[:120]):
            corrections += 1
    return corrections


def _smoke() -> int:
    """Seed a synthetic CLEAN thread (with a machine push interleaved before Operator's
    correction), grade it off the clean substrate, and prove the correction becomes a
    durable rule — where the old dirty-source path missed it. Fully isolated: throwaway
    ledger/state/rules, a collecting remember_fn (redacted_memory is never touched)."""
    import tempfile

    tmp = Path(tempfile.mkdtemp())
    src = tmp / "exchanges.jsonl"
    state = tmp / "reaction_grader_state.json"
    rules = tmp / "correction_rules.md"
    ch, th = "imessage", "smoke-thread"

    cc = clean_capture
    cc.capture(ch, th, cc.OPERATOR_INBOUND, "pull the monday board for this sprint", True, path=src)
    cc.capture(ch, th, cc.MACHINE_RESPONSE, "here are the 12 open items ...", False, path=src)
    # the poison record: a scoreboard the machine pushed him, between answer and correction
    cc.capture(ch, th, cc.MACHINE_PUSH, "🔔 revenue scoreboard: checkouts 0, views 14", False, path=src)
    cc.capture(ch, th, cc.OPERATOR_INBOUND, "no, i meant the NEXT sprint, redo it", True, path=src)
    cc.capture(ch, th, cc.MACHINE_RESPONSE, "got it — next sprint has 5 items ...", False, path=src)
    cc.capture(ch, th, cc.OPERATOR_INBOUND, "perfect, thanks", True, path=src)

    remembered: list[dict] = []

    def collect_remember(**kw):
        remembered.append(kw)
        return {"id": len(remembered)}

    # ---- BEFORE: the old dirty-source path (push mislabeled role='user') ----
    dirty_turns = [
        {"role": "user", "text": "pull the monday board for this sprint"},
        {"role": "assistant", "text": "here are the 12 open items ..."},
        {"role": "user", "text": "🔔 revenue scoreboard: checkouts 0, views 14"},  # push as user
        {"role": "user", "text": "no, i meant the NEXT sprint, redo it"},
        {"role": "assistant", "text": "got it — next sprint has 5 items ..."},
        {"role": "user", "text": "perfect, thanks"},
    ]
    before_corrections = _old_dirty_grade(dirty_turns)

    # ---- AFTER: the clean substrate path ----
    out = run(source_path=src, state_path=state, rules_path=rules, remember_fn=collect_remember)
    after_corrections = out["new_corrections"]

    fails = []
    if before_corrections != 0:
        fails.append(f"expected OLD dirty path to miss the correction (0), got {before_corrections}")
    if after_corrections != 1:
        fails.append(f"expected clean path to extract 1 correction, got {after_corrections}")
    if out["cum"]["hits"] != 1:
        fails.append(f"expected 1 hit (perfect, thanks), got {out['cum']['hits']}")
    if not remembered or remembered[0].get("kind") != "correction_rule":
        fails.append("correction rule was NOT remembered as kind=correction_rule")
    if not rules.exists() or "NEXT sprint" not in rules.read_text():
        fails.append("durable correction rule not written to the injected rules block")
    # idempotency: a second run over the same ledger grades nothing new
    out2 = run(source_path=src, state_path=state, rules_path=rules, remember_fn=collect_remember)
    if out2["new_graded"] != 0 or out2["new_corrections"] != 0:
        fails.append(f"watermark not idempotent: 2nd run graded {out2['new_graded']}")

    if fails:
        print("SMOKE FAIL:")
        for m in fails:
            print("  -", m)
        rc = 1
    else:
        print("SMOKE OK — clean substrate turns the correction into a durable rule.")
        print(f"  BEFORE (dirty role-walk, push mislabeled user): corrections found = {before_corrections}  (MISSED)")
        print(f"  AFTER  (clean_capture triples, push excluded):   corrections found = {after_corrections}  (CAUGHT)")
        print(f"  graded: {out['cum']}  hit_rate={out['hit_rate']}")
        print(f"  durable rule -> {remembered[0]['title']}")
        print(f"  2nd run (idempotency): new_graded={out2['new_graded']}")
        rc = 0

    try:
        for p in (src, state, rules, state.parent / "graded_outcomes.jsonl"):
            if p.exists():
                p.unlink()
        tmp.rmdir()
    except Exception:
        pass
    return rc


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="analyze without writing")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--smoke", action="store_true", help="run the isolated smoke test")
    a = ap.parse_args()
    if a.smoke:
        return _smoke()
    print(json.dumps(run(dry=a.dry, limit=a.limit), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
