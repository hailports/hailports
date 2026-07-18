#!/usr/bin/env python3
"""Question pattern learner — anticipate what gets asked, keep it pre-warmed.

Records every fast-cache ask, AND mines real chat history (Claude Code + openclaw
sessions) to classify the user's questions into topics. Produces a ranking by
frequency + time-of-day so the warmer can pre-build hot topics before they're
asked (anticipation).
"""
from __future__ import annotations

import json
import re
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "data/runtime/fastcache"
QLOG = CACHE_DIR / "_questions.jsonl"
PATTERNS = CACHE_DIR / "_patterns.json"
LOCAL_TZ = ZoneInfo("America/Chicago")

CLAUDE_TX = Path.home() / ".claude/projects/-Users-user"
OPENCLAW_SESSIONS = Path.home() / ".openclaw/agents/main/sessions/sessions.json"

# Keyword rules → topic (maps free-text questions to a cache topic).
TOPIC_RULES = [
    ("revenue", re.compile(r"\brevenue\b|\bsales?\b|\bmrr\b|\bstripe\b|\bmoney\b|\bearn|\bincome\b|\bleads?\b|\bgumroad\b|\bprofit\b", re.I)),
    ("inbox", re.compile(r"\binbox\b|\bemail|\bunread\b|\boutlook\b|\bneed.*reply\b|\bwho emailed\b", re.I)),
    ("work_context", re.compile(r"\bwork\b|\bcontext\b|\bsprint\b|\bsalesforce\b|\bzoom\b|\bmeeting|\blittle ?bird\b|\bmonday\b|\bticket|\bagenda\b|\bstandup\b|\bcalendar\b|\bredacted\b", re.I)),
]


def _classify(text: str):
    for topic, rx in TOPIC_RULES:
        if rx.search(text or ""):
            return topic
    return None


def record(topic: str) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with QLOG.open("a") as fh:
        fh.write(json.dumps({"ts": time.time(), "hour": datetime.now(LOCAL_TZ).hour,
                             "topic": topic, "src": "ask"}) + "\n")


def _iter_recent_user_turns(days: int = 30):
    """Yield (epoch, text) user messages from Claude transcripts + openclaw."""
    cutoff = time.time() - days * 86400
    # Claude Code JSONL transcripts
    if CLAUDE_TX.exists():
        for f in CLAUDE_TX.glob("*.jsonl"):
            try:
                if f.stat().st_mtime < cutoff:
                    continue
                for line in f.read_text(errors="replace").splitlines():
                    try:
                        o = json.loads(line)
                    except Exception:
                        continue
                    if o.get("type") != "user":
                        continue
                    msg = o.get("message", {})
                    content = msg.get("content")
                    text = content if isinstance(content, str) else " ".join(
                        c.get("text", "") for c in content if isinstance(c, dict)) if isinstance(content, list) else ""
                    ts = o.get("timestamp")
                    try:
                        epoch = datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp() if ts else f.stat().st_mtime
                    except Exception:
                        epoch = f.stat().st_mtime
                    if text and not text.startswith("<") and epoch >= cutoff:
                        yield epoch, text
            except Exception:
                continue


def learn_from_history(days: int = 30) -> dict:
    """Mine chat history → topic frequency + hour histogram. Persists patterns."""
    counts = Counter()
    hours = defaultdict(Counter)
    scanned = 0
    for epoch, text in _iter_recent_user_turns(days):
        scanned += 1
        topic = _classify(text)
        if not topic:
            continue
        counts[topic] += 1
        hours[topic][datetime.fromtimestamp(epoch, LOCAL_TZ).hour] += 1
    # fold in the explicit ask-log too
    if QLOG.exists():
        for line in QLOG.read_text(errors="replace").splitlines():
            try:
                o = json.loads(line)
                counts[o["topic"]] += 1
                hours[o["topic"]][o.get("hour", 0)] += 1
            except Exception:
                continue
    ranked = [t for t, _ in counts.most_common()]
    hot = ranked[:3]
    out = {
        "generated_at": time.time(),
        "generated_iso": datetime.now(LOCAL_TZ).isoformat(),
        "scanned_turns": scanned, "days": days,
        "counts": dict(counts),
        "ranked": ranked, "hot": hot,
        "hours": {t: dict(h) for t, h in hours.items()},
    }
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    PATTERNS.write_text(json.dumps(out, indent=2))
    return out


def rank() -> dict:
    p = None
    try:
        p = json.loads(PATTERNS.read_text())
    except Exception:
        pass
    if not p:
        p = learn_from_history()
    return p


def topics_for_hour(hour: int | None = None) -> list[str]:
    """Topics most likely to be asked this hour (anticipation), hot first."""
    if hour is None:
        hour = datetime.now(LOCAL_TZ).hour
    p = rank()
    by_hour = []
    for t, h in (p.get("hours") or {}).items():
        # JSON object keys are strings; score this hour +/- 1
        score = sum(int(h.get(str(hh), 0)) for hh in (hour - 1, hour, hour + 1))
        if score:
            by_hour.append((t, score))
    by_hour.sort(key=lambda x: -x[1])
    ordered = [t for t, _ in by_hour]
    for t in p.get("hot", []):  # always include the hot set
        if t not in ordered:
            ordered.append(t)
    return ordered


if __name__ == "__main__":
    import sys
    out = learn_from_history()
    print(json.dumps({k: out[k] for k in ("scanned_turns", "counts", "ranked", "hot")}, indent=2))
