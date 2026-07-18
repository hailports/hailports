#!/usr/bin/env python3
"""Clean-capture substrate — the tagged, append-only interaction ledger.

THE PROBLEM (frontier #1, why nothing could learn):
Every existing store (conversation.db, redacted_memory.request_log) logs machine
pushes/notifications/assistant-dumps under role="user". So the grader (and every
downstream learner: reaction_grader, never-twice, decision modeling) can't tell
Operator's genuine inbound ask from a scoreboard the machine texted him. reaction_grader
today has to GUESS with a `_looks_machine` regex heuristic — which is exactly why it
was useless. This substrate removes the guess: it tags direction AT CAPTURE TIME.

THE LEDGER: data/learning/exchanges.jsonl (append-only, one JSON record per line).
Each record:
  {
    id:          "ex_<12hex>"       # stable content id
    ts:          "<iso8601 utc>",
    channel:     "imessage"|"webui"|"telegram"|...,
    thread_id:   "<thread>",
    direction:   "operator_inbound" | "machine_response" | "machine_push",
    is_operator: bool,               # True ONLY for a genuine operator inbound
    text:        "<message text>",
    prev_ref:    "<id of prior record in this thread, or None>",
    reaction_to: "<id of the machine_response this operator msg reacts to, or None>",
  }

  - operator_inbound = Operator's real ask/correction/approval (is_operator=True).
  - machine_response = the assistant's reply to an operator_inbound (is_operator=False).
  - machine_push     = unsolicited outbound: scoreboards, alerts, digests, notifications
                       (is_operator=False). NEVER counted as an operator turn. This is the
                       exact record class the old grader mistook for Operator's request.

Additive + fail-soft. Reuses core.BASE_DIR + the conversation.py / redacted_memory jsonl
patterns; does NOT reinvent storage and does NOT touch any live hot path. Other code
calls the thin capture() below; wire-in is STAGED (see WIRE-IN block at bottom), applied
by Operator/me at a natural reload, never a live bounce.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):  # allow `python core/clean_capture.py`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import BASE_DIR

LEDGER = BASE_DIR / "data" / "learning" / "exchanges.jsonl"
LEDGER.parent.mkdir(parents=True, exist_ok=True)

OPERATOR_INBOUND = "operator_inbound"
MACHINE_RESPONSE = "machine_response"
MACHINE_PUSH = "machine_push"
_DIRECTIONS = {OPERATOR_INBOUND, MACHINE_RESPONSE, MACHINE_PUSH}

_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mk_id(ts: str, channel: str, thread_id: str, direction: str, text: str) -> str:
    h = hashlib.sha1(f"{ts}|{channel}|{thread_id}|{direction}|{text}".encode("utf-8"))
    return "ex_" + h.hexdigest()[:12]


def _iter_records(path: Path = LEDGER):
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
                continue  # skip a torn/partial line rather than crash a reader


def _thread_tail(channel: str, thread_id: str, path: Path = LEDGER):
    """One scan → (last, last_meaningful) for (channel, thread_id).
    `last` = the actual prior record (for prev_ref + consecutive-dedup).
    `last_meaningful` = the last non-push record (for reaction_to linkage — a machine_push
    interleaved between a response and Operator's reaction must NOT break the grade link)."""
    last = None
    last_meaningful = None
    for rec in _iter_records(path):
        if rec.get("channel") == channel and rec.get("thread_id") == thread_id:
            last = rec
            if rec.get("direction") != MACHINE_PUSH:
                last_meaningful = rec
    return last, last_meaningful


def capture(channel, thread_id, direction, text, is_operator, prev_ref=None, path: Path = LEDGER):
    """Append one TAGGED interaction record. Dedups an identical consecutive record
    (same direction + text) in the same thread. Returns the record dict (or the existing
    one on dedup). Fail-soft: never raises into a caller's hot path.

    is_operator is derived from direction (operator_inbound => True) and cross-checked
    against the passed flag so a miswired caller can't smuggle a push in as an operator turn.
    """
    try:
        channel = str(channel or "")
        thread_id = str(thread_id or "")
        text = "" if text is None else str(text)
        if direction not in _DIRECTIONS:
            direction = OPERATOR_INBOUND if is_operator else MACHINE_PUSH
        # direction is the source of truth for is_operator; ignore a contradicting flag.
        is_op = direction == OPERATOR_INBOUND

        with _lock:
            prev, last_meaningful = _thread_tail(channel, thread_id, path)
            if prev is not None:
                # dedup identical consecutive (double-fire, ret/relay echo)
                if prev.get("direction") == direction and prev.get("text") == text:
                    return prev
                if prev_ref is None:
                    prev_ref = prev.get("id")

            # an operator turn whose last MEANINGFUL predecessor is a machine_response is a
            # REACTION to it (the grade signal) — even if a machine_push was texted in
            # between. Link it so the grader never has to re-derive that.
            reaction_to = None
            if is_op and last_meaningful is not None and \
                    last_meaningful.get("direction") == MACHINE_RESPONSE:
                reaction_to = last_meaningful.get("id")

            ts = _now()
            rec = {
                "id": _mk_id(ts, channel, thread_id, direction, text),
                "ts": ts,
                "channel": channel,
                "thread_id": thread_id,
                "direction": direction,
                "is_operator": is_op,
                "text": text,
                "prev_ref": prev_ref,
                "reaction_to": reaction_to,
            }
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            return rec
    except Exception:
        return None


def pair_exchanges(limit=None, path: Path = LEDGER):
    """Reconstruct clean {request, response, reaction} triples for the grader.

    Per (channel, thread_id), walk records in time order. machine_push records are
    SKIPPED — they never act as an operator turn (the exact bug that made the old grader
    useless). A triple = an operator_inbound (request) + the following machine_response
    (response) + the next operator_inbound (reaction, may be None if none yet).

    request/response are always present; reaction is None when the machine's answer hasn't
    been reacted to yet. Returns most-recent-first, capped to `limit`.
    """
    threads: dict = {}
    for rec in _iter_records(path):
        key = (rec.get("channel"), rec.get("thread_id"))
        threads.setdefault(key, []).append(rec)

    triples = []
    for recs in threads.values():
        recs.sort(key=lambda r: (r.get("ts") or "", r.get("id") or ""))
        pending_req = None
        pending_resp = None
        for rec in recs:
            d = rec.get("direction")
            if d == MACHINE_PUSH:
                continue  # a push is never an operator turn — drop it from pairing
            if d == OPERATOR_INBOUND:
                if pending_req is not None and pending_resp is not None:
                    # this operator turn is the REACTION to the prior req/resp
                    triples.append({
                        "request": pending_req,
                        "response": pending_resp,
                        "reaction": rec,
                    })
                    pending_req = rec
                    pending_resp = None
                else:
                    # no answer yet (or fresh thread) — a re-ask replaces the request
                    pending_req = rec
                    pending_resp = None
            elif d == MACHINE_RESPONSE:
                if pending_req is not None and pending_resp is None:
                    pending_resp = rec  # first answer to the pending request
                # extra responses before a reaction are ignored (keep the first answer)
        # trailing request+response with no reaction yet: still a valid ungraded pair
        if pending_req is not None and pending_resp is not None:
            triples.append({
                "request": pending_req,
                "response": pending_resp,
                "reaction": None,
            })

    triples.sort(key=lambda t: (t["request"].get("ts") or ""), reverse=True)
    if limit:
        triples = triples[: int(limit)]
    return triples


# ============================================================================
# WIRE-IN (STAGED — apply at a natural reload, never a live bounce)
#
# Import once at module top of each caller:
#     from core import clean_capture
#
# (A) core/engine.py :: handle_message — tag Operator's genuine inbound + the reply.
#     At the entry of handle_message(...), right where clean_text is known:
#         clean_capture.capture(frontend, thread_id, clean_capture.OPERATOR_INBOUND,
#                               clean_text, is_operator=True)
#     Immediately before each `return reply` (paired with the assistant save_message,
#     e.g. lines ~232/265/406/456/527):
#         clean_capture.capture(frontend, thread_id, clean_capture.MACHINE_RESPONSE,
#                               reply, is_operator=False)
#
# (B) tools/imsg_bridge.py — inbound poll (SELECT ... is_from_me = 0, ~line 169) is an
#     operator inbound; the outbound send() path is a machine_push (NOT a response unless
#     it is a reply produced by handle_message — those get tagged in (A) instead):
#         # in the inbound loop, per new row:
#         clean_capture.capture("imessage", chat_id, clean_capture.OPERATOR_INBOUND,
#                               text, is_operator=True)
#         # in send_imessage(...) for unsolicited alerts/scoreboards/digests:
#         clean_capture.capture("imessage", chat_id, clean_capture.MACHINE_PUSH,
#                               body, is_operator=False)
#
# That single tag at capture time is the whole point: machine_push can never again be
# mistaken for Operator's ask, so reaction_grader.pair_exchanges → clean_capture.pair_exchanges
# gets a pure request/response/reaction signal with zero heuristic guessing.
# ============================================================================


def _smoke() -> int:
    """Synthetic clean thread + an interleaved machine push; prove pairing is clean.
    Runs against a throwaway ledger — never touches the live exchanges.jsonl or any service."""
    import tempfile

    tmp = Path(tempfile.mkdtemp()) / "exchanges_smoke.jsonl"
    ch, th = "imessage", "smoke-thread"

    # operator ask -> machine response -> [machine push interleaved] -> operator correction
    r1 = capture(ch, th, OPERATOR_INBOUND, "pull the monday board for this sprint", True, path=tmp)
    r2 = capture(ch, th, MACHINE_RESPONSE, "here are the 12 open items ...", False, path=tmp)
    # a scoreboard/alert the machine pushed him — the poison record for the old grader:
    rp = capture(ch, th, MACHINE_PUSH, "🔔 revenue scoreboard: checkouts 0, views 14", False, path=tmp)
    # dedup: fire the exact same push again immediately -> must NOT append a new record
    rp_dup = capture(ch, th, MACHINE_PUSH, "🔔 revenue scoreboard: checkouts 0, views 14", False, path=tmp)
    r3 = capture(ch, th, OPERATOR_INBOUND, "no, i meant the NEXT sprint, redo it", True, path=tmp)
    r4 = capture(ch, th, MACHINE_RESPONSE, "got it — next sprint has 5 items ...", False, path=tmp)
    r5 = capture(ch, th, OPERATOR_INBOUND, "perfect, thanks", True, path=tmp)

    fails = []

    # tagging correctness
    if not (r1["is_operator"] and r3["is_operator"] and r5["is_operator"]):
        fails.append("operator inbounds not tagged is_operator=True")
    if r2["is_operator"] or r4["is_operator"] or rp["is_operator"]:
        fails.append("a machine record was tagged is_operator=True")
    if rp["direction"] != MACHINE_PUSH:
        fails.append("push not tagged machine_push")

    # dedup: rp_dup must be the same record object (no new line)
    if rp_dup.get("id") != rp.get("id"):
        fails.append("consecutive identical push was NOT deduped")
    total_lines = sum(1 for _ in _iter_records(tmp))
    if total_lines != 6:  # r1..r5 + rp, dup skipped
        fails.append(f"expected 6 ledger lines, got {total_lines}")

    # reaction linkage: r3 reacts to r2, r5 reacts to r4
    if r3.get("reaction_to") != r2.get("id"):
        fails.append("correction r3 not linked as reaction_to the response r2")
    if r5.get("reaction_to") != r4.get("id"):
        fails.append("praise r5 not linked as reaction_to the response r4")

    # THE core test: pair_exchanges reconstructs the RIGHT triples and the push is NOT
    # mistaken for an operator turn.
    triples = pair_exchanges(path=tmp)
    if len(triples) != 2:
        fails.append(f"expected 2 triples, got {len(triples)}")
    else:
        # most-recent-first
        t_new, t_old = triples[0], triples[1]
        if t_old["request"]["text"] != "pull the monday board for this sprint":
            fails.append("triple 1 request wrong")
        if t_old["response"]["text"] != "here are the 12 open items ...":
            fails.append("triple 1 response wrong")
        # the reaction to triple 1 must be Operator's correction, NOT the scoreboard push
        if t_old["reaction"]["text"] != "no, i meant the NEXT sprint, redo it":
            fails.append("triple 1 reaction is NOT the operator correction (push leaked in!)")
        if "scoreboard" in (t_old["reaction"]["text"] or ""):
            fails.append("machine push leaked in as the operator reaction — THE bug")
        if t_new["request"]["text"] != "no, i meant the NEXT sprint, redo it":
            fails.append("triple 2 request wrong")
        if t_new["reaction"]["text"] != "perfect, thanks":
            fails.append("triple 2 reaction wrong")

    # no triple may ever carry a push in any slot
    for t in triples:
        for slot in ("request", "response", "reaction"):
            r = t.get(slot)
            if r is not None and r.get("direction") == MACHINE_PUSH:
                fails.append(f"a push appeared in triple slot {slot}")

    if fails:
        print("SMOKE FAIL:")
        for m in fails:
            print("  -", m)
        rc = 1
    else:
        print("SMOKE OK — 2 clean triples reconstructed; machine push excluded from operator "
              "turns; dedup held.")
        for i, t in enumerate(triples):
            print(f"  triple[{i}] req={t['request']['text']!r} -> resp={t['response']['text']!r} "
                  f"-> reaction={(t['reaction'] or {}).get('text')!r}")
        rc = 0

    try:
        tmp.unlink()
        tmp.parent.rmdir()
    except Exception:
        pass
    return rc


if __name__ == "__main__":
    raise SystemExit(_smoke())
