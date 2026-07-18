#!/usr/bin/env python3
"""posted_ledger.py — the ONE place every pipeline records what it posted, and the
ONE place every pipeline checks before posting again.

Why this exists: persona1 reels were posting the same caption/script for days. Root
cause was a split brain — build_ima_reel.py used a hardcoded caption + a fixed
9-scene pool and a fingerprint that only varied the random scene subset, while the
cross-persona builder (build_reel.py) wrote its own entries, and the content_quality
near-dup gate was never handed a `recent` list for reels. Nothing could see that
"your upline won't show you..." had already gone out 7 times.

This module unifies all of that on the existing on-disk ledger so:
  • EXACT dedup: a content fingerprint already in the ledger is blocked.
  • TOPIC dedup: a normalized topic key (the hook reworded still maps to the same
    key) cannot repost within a cooldown window — kills "same thing, new words".
  • TEXT history: recent_texts() feeds content_quality.gate(recent=...) so the
    deterministic jaccard near-dup check finally has something to compare against.

Deterministic, $0, no LLM. Fail-OPEN on read errors (never crash a render), but
callers that auto-post should fail-CLOSED on a positive dup hit (treat "is this a
dup?" == True as "do not post").
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

# Same physical file build_reel.py already appends to, so reels from EITHER builder
# (and any future pipeline) dedup against each other. Overridable for tests.
LEDGER_FILE = Path(
    os.environ.get(
        "POSTED_LEDGER_FILE",
        str(Path.home() / ".openclaw" / "workspace" / "android-logs" / "posted-content-ledger.json"),
    )
)

DEFAULT_TOPIC_COOLDOWN_DAYS = int(os.environ.get("POSTED_LEDGER_TOPIC_COOLDOWN_DAYS", "21"))
DEFAULT_TEXT_LOOKBACK_DAYS = int(os.environ.get("POSTED_LEDGER_TEXT_LOOKBACK_DAYS", "14"))

# Filler/stopwords stripped when deriving the topic key, so "your upline won't show
# you the follow-up system" and "the upline never shows the follow up system" collapse
# to the same key. Kept small + generic on purpose.
_STOP = {
    "the", "a", "an", "you", "your", "youre", "ur", "us", "i", "im", "me", "my", "we",
    "to", "of", "in", "on", "for", "and", "or", "but", "is", "are", "was", "were",
    "be", "been", "this", "that", "it", "its", "wont", "will", "show", "shows",
    "showing", "here", "hun", "girl", "free", "bio", "now", "get", "got", "go",
    "let", "lets", "with", "from", "after", "before", "every", "some", "all", "any",
    "no", "not", "dont", "do", "does", "did", "what", "how", "why", "when", "who",
    "part", "this", "thing", "things",
    # CTA / hashtag boilerplate that rides on every caption — must never define the
    # topic, or two different angles sharing "free prompts in bio" collapse wrongly,
    # AND the same angle with/without the tail splits.
    "prompt", "prompts", "directsales", "aisales", "dmstrategy", "persona1",
    "follow", "followup",
}

# Common contractions → expanded form so "won't" and "will not" tokenize the same.
_CONTRACTIONS = (
    ("won't", "will not"), ("can't", "can not"), ("n't", " not"),
    ("'re", " are"), ("'ll", " will"), ("'ve", " have"), ("'m", " am"),
    ("'s", ""), ("'d", " would"),
)


def _norm(text: str) -> str:
    text = (text or "").lower()
    for src, dst in _CONTRACTIONS:
        text = text.replace(src, dst)
    # collapse hyphenated compounds ("follow-up" → "followup") before stripping punct
    text = re.sub(r"(?<=\w)-(?=\w)", "", text)
    text = "".join(c if (c.isalnum() or c.isspace()) else " " for c in text)
    return " ".join(text.split())


def content_fingerprint(text: str) -> str:
    """Exact-content hash over normalized text."""
    return hashlib.sha256(_norm(text).encode()).hexdigest()


def topic_key(*parts: str) -> str:
    """Stable key for the TOPIC of a post, robust to rewording.

    Strips stopwords + sorts the remaining significant tokens, so the same idea in
    different words maps to the same key. Used for the topic cooldown.
    """
    norm = _norm(" ".join(p for p in parts if p))
    toks = sorted({t for t in norm.split() if len(t) >= 3 and t not in _STOP})
    # Cap to the strongest 12 tokens so a long script doesn't dilute the key.
    sig = " ".join(toks[:12])
    return hashlib.sha256(sig.encode()).hexdigest()[:16] if sig else ""


def _read() -> list[dict]:
    try:
        data = json.loads(LEDGER_FILE.read_text())
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _parse_ts(entry: dict) -> datetime | None:
    raw = entry.get("at") or entry.get("ts") or entry.get("created_at") or ""
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def posted_fingerprints() -> set[str]:
    return {e.get("fp") for e in _read() if e.get("fp")}


def has_fingerprint(fp: str) -> bool:
    return bool(fp) and fp in posted_fingerprints()


def topic_on_cooldown(key: str, *, days: int = DEFAULT_TOPIC_COOLDOWN_DAYS) -> bool:
    """True if this topic key was posted within the cooldown window — even if the
    exact wording differs. The block that string-dedup can't see."""
    if not key:
        return False
    now = datetime.now(timezone.utc)
    for e in _read():
        if e.get("topic_key") != key:
            continue
        ts = _parse_ts(e)
        if ts is None:
            # undated topic entry → treat as recent (fail-closed for cooldown)
            return True
        if (now - ts).days < days:
            return True
    return False


def recent_texts(*, days: int = DEFAULT_TEXT_LOOKBACK_DAYS, account: str | None = None,
                 persona: str | None = None) -> list[str]:
    """Recent post text (caption/script) to hand to content_quality.gate(recent=...)
    so its jaccard near-dup check has real history. Optionally scope to one account/
    persona."""
    now = datetime.now(timezone.utc)
    out: list[str] = []
    for e in _read():
        ts = _parse_ts(e)
        if ts is not None and (now - ts).days >= days:
            continue
        if account and e.get("account") not in (account, None) and e.get("account") != account:
            if e.get("account") is not None:
                continue
        if persona and e.get("persona") and e.get("persona") != persona:
            continue
        for k in ("script", "caption", "text", "voiceover_text"):
            v = e.get(k)
            if v:
                out.append(str(v))
    return [t for t in out if t]


def is_duplicate(text: str, *, topic_parts: list[str] | None = None,
                 cooldown_days: int = DEFAULT_TOPIC_COOLDOWN_DAYS) -> tuple[bool, str]:
    """Single decision used by auto-posting pipelines. Returns (is_dup, reason).

    Blocks on EITHER an exact fingerprint match OR a topic-key cooldown hit.
    Fail-CLOSED is the caller's job: if this returns True, do not post.
    """
    fp = content_fingerprint(text)
    if has_fingerprint(fp):
        return True, f"exact-duplicate fingerprint {fp[:12]} already posted"
    key = topic_key(*(topic_parts or [text]))
    if topic_on_cooldown(key, days=cooldown_days):
        return True, f"topic {key} on {cooldown_days}d cooldown (reworded-dup)"
    return False, ""


def record(text: str, *, account: str = "", persona: str = "", video: str = "",
           caption: str = "", topic_parts: list[str] | None = None,
           extra: dict | None = None) -> dict:
    """Append a post to the shared ledger. Stores the fingerprint, the topic key,
    and the full text so future exact/topic/jaccard dedup all work. Best-effort:
    never raises (a ledger-write failure must not kill a verified post)."""
    entry = {
        "fp": content_fingerprint(text),
        "topic_key": topic_key(*(topic_parts or [text])),
        "at": datetime.now(timezone.utc).isoformat(),
    }
    if account:
        entry["account"] = account
    if persona:
        entry["persona"] = persona
    if video:
        entry["video"] = video
    entry["caption"] = (caption or text)[:200]
    entry["script"] = text[:600]
    if extra:
        entry.update(extra)
    try:
        LEDGER_FILE.parent.mkdir(parents=True, exist_ok=True)
        data = _read()
        data.append(entry)
        LEDGER_FILE.write_text(json.dumps(data, indent=2))
    except Exception:
        pass
    return entry


def _cli() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Shared posted-content ledger dedup check.")
    ap.add_argument("--text", default="", help="content text/script to check")
    ap.add_argument("--topic", action="append", default=[], help="topic part (repeatable)")
    ap.add_argument("--cooldown-days", type=int, default=DEFAULT_TOPIC_COOLDOWN_DAYS)
    ap.add_argument("--record", action="store_true", help="record after a non-dup check")
    ap.add_argument("--account", default="")
    ap.add_argument("--persona", default="")
    ap.add_argument("--video", default="")
    args = ap.parse_args()
    if not args.text:
        import sys
        args.text = sys.stdin.read()
    dup, reason = is_duplicate(args.text, topic_parts=args.topic or None,
                               cooldown_days=args.cooldown_days)
    print(json.dumps({"duplicate": dup, "reason": reason,
                      "fp": content_fingerprint(args.text),
                      "topic_key": topic_key(*(args.topic or [args.text]))}))
    if dup:
        return 1  # exit 1 == duplicate → caller's shell `if` treats as block
    if args.record:
        record(args.text, account=args.account, persona=args.persona, video=args.video,
               topic_parts=args.topic or None)
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
