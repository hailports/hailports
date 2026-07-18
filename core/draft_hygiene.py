#!/usr/bin/env python3
"""Inbox upkeep — the janitor organ for Operator's work-lane inbox agent.

Two jobs, both pure logic (zero AI, zero SF-facing prose, never sends):

  1. needs_action  — before the agent spends effort drafting a reply, decide whether
                     the thread STILL needs one. If Operator already answered it (a matching
                     subject shows up in Sent after the inbound landed) or a draft is
                     already staged, the thread is done — don't re-draft over Operator or
                     stack duplicate drafts.

  2. find_abandoned_drafts / gc_abandoned — drafts the agent staged weeks ago that Operator
                     never sent are clearly-never-sending. find_abandoned_drafts only
                     REPORTS them (dry-run list, deletes nothing). gc_abandoned is the
                     separate, gated acting call: it does nothing in dry mode, and even
                     when dry=False it logs every removal (full draft + undo-reason) to a
                     ledger first so any deletion is recoverable.

Subject matching strips Re:/Fwd:/FW: noise so a reply threads to its parent. This module
touches no email content and no Salesforce — it only reasons over subject/timestamp lists.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from core import BASE_DIR

LEDGER = BASE_DIR / "data" / "learning" / "draft_gc.jsonl"
LEDGER.parent.mkdir(parents=True, exist_ok=True)

_PREFIX = re.compile(r"^\s*(re|fwd?|fw)\s*:\s*", re.IGNORECASE)


def _norm_subject(s: str) -> str:
    """Strip repeated Re:/Fwd:/FW: prefixes + collapse whitespace for thread matching."""
    s = (s or "").strip()
    prev = None
    while prev != s:
        prev = s
        s = _PREFIX.sub("", s).strip()
    return re.sub(r"\s+", " ", s).lower()


def _to_epoch(ts) -> float | None:
    """Accept epoch seconds (int/float/numeric str) or ISO-8601; return epoch or None."""
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return float(ts)
    s = str(ts).strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def _subject_of(item) -> str:
    return item.get("subject", "") if isinstance(item, dict) else str(item)


def _ts_of(item, *keys: str):
    if not isinstance(item, dict):
        return None
    for k in keys:
        if item.get(k) is not None:
            return item[k]
    return None


def needs_action(thread_id, inbox_item: dict,
                 sent_subjects: list, draft_subjects: list) -> dict:
    """Does this inbound thread still need a drafted reply?

    sent_subjects / draft_subjects may be plain subject strings or dicts carrying a
    subject + a timestamp ('sent_at'/'received'/'created'/'ts'). When both the sent item
    and the inbound carry a timestamp, a Sent match only counts if it post-dates the
    inbound (an OLD reply doesn't answer a NEW message on the same subject).
    """
    target = _norm_subject(_subject_of(inbox_item))
    if not target:
        return {"action": True, "reason": "no subject to match — draft to be safe"}

    inbound_ts = _to_epoch(_ts_of(inbox_item, "received", "received_at", "ts"))

    for s in sent_subjects or []:
        if _norm_subject(_subject_of(s)) != target:
            continue
        sent_ts = _to_epoch(_ts_of(s, "sent_at", "received", "created", "ts"))
        if inbound_ts is not None and sent_ts is not None and sent_ts < inbound_ts:
            continue  # a reply older than this inbound doesn't cover it
        return {"action": False, "reason": "already replied in sent"}

    for d in draft_subjects or []:
        if _norm_subject(_subject_of(d)) == target:
            return {"action": False, "reason": "draft already staged"}

    return {"action": True}


def find_abandoned_drafts(drafts: list, now_ts, max_age_days: int = 14,
                          recent_inbound_subjects: list | None = None) -> list:
    """DRY-RUN report of drafts too old to plausibly still be sent.

    A draft is a candidate when its age (from 'created'/'last_modified'/'ts') exceeds
    max_age_days AND its thread has no recent inbound activity. Pass
    recent_inbound_subjects (the subjects of currently-unread/recent inbound) to spare
    drafts whose conversation is still live. Deletes nothing — returns candidates only.
    """
    now = _to_epoch(now_ts)
    live = {_norm_subject(_subject_of(x)) for x in (recent_inbound_subjects or [])}
    out: list[dict] = []
    for d in drafts or []:
        ts = _to_epoch(_ts_of(d, "created", "last_modified", "modified", "ts"))
        if now is None or ts is None:
            continue
        age_days = (now - ts) / 86400.0
        if age_days <= max_age_days:
            continue
        subj = _norm_subject(_subject_of(d))
        if subj and subj in live:
            continue  # thread still active — not abandoned
        out.append({
            "draft": d,
            "id": d.get("id") if isinstance(d, dict) else None,
            "subject": _subject_of(d),
            "age_days": round(age_days, 1),
            "reason": f"staged {round(age_days, 1)}d ago (> {max_age_days}d), "
                      f"no recent inbound on this thread — clearly never sending",
        })
    return out


def gc_abandoned(drafts: list, *, dry: bool = True, delete_fn=None) -> list:
    """Act on abandoned-draft candidates. NEVER deletes in dry mode (the default).

    `drafts` is expected to be the candidate list from find_abandoned_drafts (each item a
    {"draft": ..., "reason": ...} record), but bare draft dicts are tolerated. When
    dry=False each removal is logged to data/learning/draft_gc.jsonl with the full draft
    payload + reason (enough to recreate it), and — only if delete_fn is supplied — the
    draft is actually removed via that callback. No delete_fn = log-only (still no data
    loss). Returns a per-draft result list.
    """
    results: list[dict] = []
    for c in drafts or []:
        draft = c.get("draft", c) if isinstance(c, dict) and "draft" in c else c
        reason = c.get("reason", "") if isinstance(c, dict) else ""
        subject = _subject_of(draft)
        did = draft.get("id") if isinstance(draft, dict) else None

        if dry:
            results.append({"removed": False, "dry_run": True,
                            "id": did, "subject": subject, "reason": reason})
            continue

        entry = {
            "at": datetime.now(timezone.utc).isoformat(),
            "id": did,
            "subject": subject,
            "reason": reason,
            "undo": {"note": "recreate as threaded draft via owa_write.js reply",
                     "draft": draft},
        }
        removed, err = False, None
        if delete_fn is not None:
            try:
                delete_fn(draft)
                removed = True
            except Exception as e:
                err = repr(e)
        entry["removed"] = removed
        if err:
            entry["error"] = err
        with LEDGER.open("a") as f:
            f.write(json.dumps(entry) + "\n")
        results.append({"removed": removed, "dry_run": False,
                        "id": did, "subject": subject,
                        "logged": str(LEDGER), **({"error": err} if err else {})})
    return results


if __name__ == "__main__":
    import time
    import tempfile
    from pathlib import Path

    # redirect the ledger to a throwaway file so the smoke test never appends synthetic
    # deletion records to the live data/learning/draft_gc.jsonl
    LEDGER = Path(tempfile.mkdtemp()) / "draft_gc_smoketest.jsonl"

    now = time.time()
    day = 86400.0

    # needs_action -------------------------------------------------------------
    inbound = {"subject": "Re: Blake can't see login-to button", "received": now - 2 * day}

    r1 = needs_action("t1", inbound,
                      sent_subjects=[{"subject": "RE: Blake can't see login-to button",
                                      "sent_at": now - 1 * day}],
                      draft_subjects=[])
    assert r1 == {"action": False, "reason": "already replied in sent"}, r1

    # sent reply predates the inbound -> does NOT cover it
    r2 = needs_action("t1", inbound,
                      sent_subjects=[{"subject": "Blake can't see login-to button",
                                      "sent_at": now - 5 * day}],
                      draft_subjects=[])
    assert r2 == {"action": True}, r2

    r3 = needs_action("t1", inbound, sent_subjects=[],
                      draft_subjects=["Fwd: Blake can't see login-to button"])
    assert r3 == {"action": False, "reason": "draft already staged"}, r3

    r4 = needs_action("t1", inbound, sent_subjects=["something else"],
                      draft_subjects=["unrelated draft"])
    assert r4 == {"action": True}, r4

    # find_abandoned_drafts ----------------------------------------------------
    drafts = [
        {"id": "d1", "subject": "Re: old rebate question", "created": now - 30 * day},
        {"id": "d2", "subject": "Re: recent access request", "created": now - 3 * day},
        {"id": "d3", "subject": "Re: still-live thread", "created": now - 40 * day},
    ]
    abandoned = find_abandoned_drafts(
        drafts, now, max_age_days=14,
        recent_inbound_subjects=[{"subject": "still-live thread"}])
    ids = sorted(x["id"] for x in abandoned)
    assert ids == ["d1"], ids  # d2 too new, d3 still live
    print("abandoned candidates:", json.dumps(abandoned, indent=2, default=str))

    # gc_abandoned dry mode deletes/logs nothing -------------------------------
    before = LEDGER.stat().st_size if LEDGER.exists() else 0
    dry = gc_abandoned(abandoned, dry=True)
    assert all(x["removed"] is False and x["dry_run"] for x in dry), dry
    after_dry = LEDGER.stat().st_size if LEDGER.exists() else 0
    assert before == after_dry, "dry mode must not write the ledger"

    # gc_abandoned dry=False, log-only (no delete_fn) -> logs, doesn't error ----
    acted = gc_abandoned(abandoned, dry=False)
    assert all(x["removed"] is False and not x["dry_run"] for x in acted), acted
    assert LEDGER.stat().st_size > after_dry, "log-only run must append to ledger"

    # dry=False WITH a delete callback marks removed -----------------------------
    seen = []
    acted2 = gc_abandoned(abandoned, dry=False, delete_fn=lambda d: seen.append(d.get("id")))
    assert all(x["removed"] for x in acted2), acted2
    assert seen == ["d1"], seen

    print("OK all smoke assertions passed; ledger:", LEDGER)
