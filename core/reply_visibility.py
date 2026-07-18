"""Reply visibility — single source of truth for inbound reply analytics.

Aggregates the data the reply detector already collects (replies.json,
auto_replies.jsonl, sent.jsonl) into one structured summary that can feed:
  • the vibe_hustle dashboard (counts, recent items, hot leads)
  • a CLI digest for ad-hoc review (`python -m core.reply_visibility`)
  • an opt-in Telegram digest (`--send-telegram`)

No outbound sending happens by default. The CLI prints; Telegram is opt-in.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

BASE = Path(os.path.expanduser("~/claude-stack"))
OUTREACH = BASE / "products" / "outreach"
HUSTLE = BASE / "data" / "hustle"

REPLIES_FILE_REL = ("products", "outreach", "replies.json")
AUTO_REPLIES_FILE_REL = ("products", "outreach", "auto_replies.jsonl")
SENT_FILE_RELS = (
    ("products", "outreach", "sent.jsonl"),
    ("data", "hustle", "outreach_sent.jsonl"),
    ("data", "hustle", "outbound_sends.jsonl"),
)
STATE_FILE_REL = ("data", "hustle", "reply_detector_state.json")

CLASSIFICATION_LABELS = ("positive", "neutral", "auto_response", "bounce", "unsubscribe", "unknown")
ACTIONABLE_CLASSES = {"positive", "neutral"}


def _read_json(path: Path, fallback: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text())
    except Exception:
        pass
    return fallback


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _clean_email(value: Any) -> str:
    return str(value or "").strip().strip("<>,.;:").lower()


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    # ISO 8601 first
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        pass
    # RFC 2822 (e.g. "Wed, 03 May 2026 19:22:14 -0700")
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(raw)
        if dt is not None:
            return dt
    except Exception:
        pass
    # Fallback: best-effort common formats
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw[:19], fmt).replace(tzinfo=timezone.utc)
        except Exception:
            continue
    return None


def _reply_dt(reply: dict) -> datetime | None:
    return (
        _parse_dt(reply.get("detected_at"))
        or _parse_dt(reply.get("date"))
    )


def _reply_dedupe_key(reply: dict) -> tuple[str, str, str, str]:
    return (
        _clean_email(reply.get("from") or reply.get("reply_sender") or ""),
        str(reply.get("subject") or "").strip().lower(),
        str(reply.get("date") or "").strip(),
        str(reply.get("source") or "").strip().lower(),
    )


def load_replies(base: Path | None = None) -> list[dict]:
    """Load deduplicated replies, normalized and sorted newest-first."""
    base = base or BASE
    raw = _read_json(base.joinpath(*REPLIES_FILE_REL), [])
    if not isinstance(raw, list):
        return []
    seen: set[tuple[str, str, str, str]] = set()
    out: list[dict] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        key = _reply_dedupe_key(row)
        if key in seen:
            continue
        seen.add(key)
        item = dict(row)
        item["_dt"] = _reply_dt(item)
        out.append(item)
    out.sort(key=lambda r: r.get("_dt") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return out


def load_auto_replies(base: Path | None = None) -> list[dict]:
    base = base or BASE
    rows = _read_jsonl(base.joinpath(*AUTO_REPLIES_FILE_REL))
    for r in rows:
        r["_dt"] = _parse_dt(r.get("ts"))
    rows.sort(key=lambda r: r.get("_dt") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return rows


def _sent_recipient(row: dict) -> str:
    for key in ("to", "email", "destination", "recipient"):
        value = _clean_email(row.get(key, ""))
        if value:
            return value
    return ""


def load_sent_recipients(base: Path | None = None) -> set[str]:
    base = base or BASE
    sent: set[str] = set()
    for rel in SENT_FILE_RELS:
        for row in _read_jsonl(base.joinpath(*rel)):
            recipient = _sent_recipient(row)
            if recipient:
                sent.add(recipient)
    return sent


def _classify(value: Any) -> str:
    cls = str(value or "unknown").strip().lower()
    return cls if cls in CLASSIFICATION_LABELS else "unknown"


def _summarize_window(replies: list[dict], window: timedelta, now: datetime) -> dict[str, int]:
    cutoff = now - window
    bucket: Counter[str] = Counter()
    for r in replies:
        dt = r.get("_dt")
        if dt and dt >= cutoff:
            bucket[_classify(r.get("classification"))] += 1
    out = {label: bucket.get(label, 0) for label in CLASSIFICATION_LABELS}
    out["total"] = sum(out.values())
    return out


def _is_auto_replied(reply: dict, auto_replies: list[dict]) -> bool:
    sender = _clean_email(reply.get("from"))
    subject = str(reply.get("subject") or "").strip().lower()
    if not sender:
        return False
    for ar in auto_replies:
        if _clean_email(ar.get("to")) != sender:
            continue
        ar_subject = str(ar.get("subject") or "").strip().lower()
        # auto-replies prefix "Re: " — strip for fuzzy match
        ar_norm = re.sub(r"^re:\s*", "", ar_subject)
        sub_norm = re.sub(r"^re:\s*", "", subject)
        if ar_norm == sub_norm or sub_norm in ar_norm or ar_norm in sub_norm:
            if ar.get("sent"):
                return True
    return False


def _short_preview(text: Any, limit: int = 240) -> str:
    raw = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(raw) <= limit:
        return raw
    return raw[: max(0, limit - 1)].rstrip() + "…"


def _serialize_reply(reply: dict, *, auto_replied: bool, body_chars: int = 240) -> dict:
    dt = reply.get("_dt")
    return {
        "from": _clean_email(reply.get("from")),
        "subject": str(reply.get("subject") or "").strip(),
        "classification": _classify(reply.get("classification")),
        "sequence": str(reply.get("original_sequence") or "").strip() or "unknown",
        "original_subject": str(reply.get("original_subject") or "").strip(),
        "preview": _short_preview(reply.get("body_preview"), body_chars),
        "detected_at": dt.isoformat() if dt else "",
        "source": str(reply.get("source") or "").strip(),
        "auto_replied": auto_replied,
    }


def summarize(base: Path | None = None, *, recent_limit: int = 20) -> dict:
    """Aggregate everything into one dashboard- and CLI-friendly payload."""
    base = base or BASE
    replies = load_replies(base)
    auto_replies = load_auto_replies(base)
    state = _read_json(base.joinpath(*STATE_FILE_REL), {}) or {}

    now = datetime.now(timezone.utc)

    counts = Counter(_classify(r.get("classification")) for r in replies)
    counts_full = {label: counts.get(label, 0) for label in CLASSIFICATION_LABELS}
    counts_full["total"] = sum(counts_full.values())

    auto_replies_sent = sum(1 for r in auto_replies if r.get("sent"))
    auto_replies_attempted = len(auto_replies)
    auto_replies_failed = auto_replies_attempted - auto_replies_sent

    recent = []
    for r in replies[:recent_limit]:
        recent.append(_serialize_reply(r, auto_replied=_is_auto_replied(r, auto_replies)))

    hot_leads = []
    pending_review = []
    for r in replies:
        cls = _classify(r.get("classification"))
        ser = _serialize_reply(r, auto_replied=_is_auto_replied(r, auto_replies), body_chars=400)
        if cls == "positive":
            hot_leads.append(ser)
        elif cls == "neutral" and not ser["auto_replied"]:
            pending_review.append(ser)

    last_scan = state.get("last_scan") or ""
    last_source = state.get("last_source") or ""
    imap_ok = bool(state.get("imap_credentials_configured"))
    imap_failed_at = state.get("imap_auth_failed_at") or ""

    return {
        "generated_at": now.isoformat(),
        "counts": counts_full,
        "windows": {
            "last_24h": _summarize_window(replies, timedelta(days=1), now),
            "last_7d": _summarize_window(replies, timedelta(days=7), now),
            "last_30d": _summarize_window(replies, timedelta(days=30), now),
        },
        "auto_replies": {
            "attempted": auto_replies_attempted,
            "sent": auto_replies_sent,
            "failed": auto_replies_failed,
        },
        "detector": {
            "last_scan": last_scan,
            "last_source": last_source,
            "imap_credentials_configured": imap_ok,
            "imap_auth_failed_at": imap_failed_at,
            "apple_mail_fallback": bool(state.get("apple_mail_fallback_enabled", True)),
        },
        "recent": recent,
        "hot_leads": hot_leads[:25],
        "pending_review": pending_review[:25],
    }


def format_text_digest(summary: dict, *, recent_limit: int = 8) -> str:
    """Plain-text digest for terminal or Telegram."""
    counts = summary["counts"]
    w24 = summary["windows"]["last_24h"]
    w7 = summary["windows"]["last_7d"]
    auto = summary["auto_replies"]
    detector = summary["detector"]

    lines: list[str] = []
    lines.append(f"Reply Visibility — {summary['generated_at'][:19]}Z")
    lines.append(
        f"Last scan: {detector.get('last_scan') or 'never'} via {detector.get('last_source') or 'n/a'}"
    )
    if not detector.get("imap_credentials_configured"):
        lines.append("⚠ IMAP credentials not configured — using Apple Mail fallback")
    elif detector.get("imap_auth_failed_at"):
        lines.append(f"⚠ IMAP auth failed at {detector['imap_auth_failed_at']}")
    lines.append("")
    lines.append(f"Totals (all-time):  positive={counts['positive']}  neutral={counts['neutral']}  "
                 f"auto={counts['auto_response']}  bounce={counts['bounce']}  unsub={counts['unsubscribe']}  "
                 f"unknown={counts['unknown']}  → {counts['total']} replies")
    lines.append(f"Last 24h:           positive={w24['positive']}  neutral={w24['neutral']}  "
                 f"bounce={w24['bounce']}  unsub={w24['unsubscribe']}  → {w24['total']}")
    lines.append(f"Last 7d:            positive={w7['positive']}  neutral={w7['neutral']}  "
                 f"bounce={w7['bounce']}  unsub={w7['unsubscribe']}  → {w7['total']}")
    lines.append("")
    lines.append(
        f"Auto-replies: {auto['sent']}/{auto['attempted']} sent, {auto['failed']} failed"
    )

    hot = summary.get("hot_leads") or []
    if hot:
        lines.append("")
        lines.append(f"HOT LEADS ({len(hot)}):")
        for r in hot[:recent_limit]:
            tag = " (auto-replied)" if r.get("auto_replied") else " (NEEDS REPLY)"
            lines.append(f"  • {r['from']}  ←  {r['original_subject'] or r['subject']}{tag}")
            if r.get("preview"):
                lines.append(f"      {r['preview']}")

    pending = summary.get("pending_review") or []
    if pending:
        lines.append("")
        lines.append(f"NEUTRAL — REVIEW ({len(pending)}):")
        for r in pending[:recent_limit]:
            lines.append(f"  • {r['from']}  ←  {r['original_subject'] or r['subject']}")
            if r.get("preview"):
                lines.append(f"      {r['preview']}")

    recent = summary.get("recent") or []
    if recent:
        lines.append("")
        lines.append(f"RECENT ({min(len(recent), recent_limit)} of {len(recent)}):")
        for r in recent[:recent_limit]:
            ts = (r.get("detected_at") or "")[:19]
            lines.append(f"  [{r['classification']:<13}] {ts}  {r['from']}  ←  {r['subject'][:60]}")

    return "\n".join(lines)


def format_telegram_digest(summary: dict) -> str:
    """Compact Telegram-friendly digest. Hot leads are the lede."""
    counts = summary["counts"]
    w24 = summary["windows"]["last_24h"]
    auto = summary["auto_replies"]
    hot = summary.get("hot_leads") or []
    pending = summary.get("pending_review") or []

    lines: list[str] = []
    lines.append("[REPLIES] daily visibility digest")
    lines.append(
        f"24h: {w24['positive']} positive · {w24['neutral']} neutral · "
        f"{w24['bounce']} bounce · {w24['unsubscribe']} unsub · {w24['total']} total"
    )
    lines.append(
        f"All time: {counts['total']} replies; auto-replied {auto['sent']}/{auto['attempted']}"
    )
    if hot:
        lines.append("")
        lines.append(f"🔥 Hot leads ({len(hot)}):")
        for r in hot[:8]:
            tag = " · auto-replied" if r.get("auto_replied") else " · NEEDS REPLY"
            lines.append(f"  • {r['from']}{tag}")
    if pending:
        lines.append("")
        lines.append(f"📭 Neutral, no auto-reply yet ({len(pending)}):")
        for r in pending[:5]:
            lines.append(f"  • {r['from']} — {(r.get('subject') or '')[:60]}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Show visibility into inbound replies (reply detector pipeline)."
    )
    parser.add_argument(
        "--format",
        choices=("text", "json", "telegram"),
        default="text",
        help="Output format (default: text).",
    )
    parser.add_argument("--limit", type=int, default=15, help="Max recent replies to surface.")
    parser.add_argument(
        "--send-telegram",
        action="store_true",
        help="Also push the digest to the primary Telegram chat (opt-in).",
    )
    args = parser.parse_args(argv)

    summary = summarize(recent_limit=max(args.limit, 1))

    if args.format == "json":
        print(json.dumps(summary, indent=2, default=str))
    elif args.format == "telegram":
        print(format_telegram_digest(summary))
    else:
        print(format_text_digest(summary, recent_limit=max(args.limit, 1)))

    if args.send_telegram:
        try:
            from core import telegram as tg
        except Exception as exc:
            print(f"telegram unavailable: {exc}", file=sys.stderr)
            return 1
        chunks = tg.send_to_alex(format_telegram_digest(summary))
        print(f"telegram: sent {chunks} chunk(s)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
