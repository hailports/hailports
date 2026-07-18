"""Agent relay — shared message bus between Claude Code (MacBook) and Codex (Mini).

Both agents read/write ~/claude-stack/data/agent_inbox.json.
Claude Code accesses it via SSH. Codex accesses it directly.

Usage:
    python3 core/agent_relay.py post --from claude_code --to codex --subject "..." --body "..."
    python3 core/agent_relay.py read --for codex
    python3 core/agent_relay.py ack <message_id>
"""
from __future__ import annotations
import argparse, json, os, uuid
from datetime import datetime, timezone
from pathlib import Path

INBOX = Path(os.path.expanduser("~/claude-stack/data/agent_inbox.json"))


def _load() -> list:
    """Return the message list. The on-disk bus is a bare JSON array (the shape
    Codex and other writers use directly); tolerate a legacy {"messages":[...]}
    dict too. We do NOT rewrite other writers' messages here — normalization is
    display-only in read()."""
    data = None
    if INBOX.exists():
        try:
            data = json.loads(INBOX.read_text())
        except Exception:
            data = None
    if isinstance(data, dict):
        return data.get("messages", []) or []
    if isinstance(data, list):
        return data
    return []


def _save(msgs: list) -> None:
    """Persist in the on-disk list shape (matches Codex's direct reader)."""
    INBOX.parent.mkdir(parents=True, exist_ok=True)
    tmp = INBOX.with_suffix(".tmp")
    tmp.write_text(json.dumps(msgs, indent=2))
    tmp.replace(INBOX)


def post(from_: str, to: str, subject: str, body: str) -> str:
    msgs = _load()
    msg_id = str(uuid.uuid4())[:8]
    msg = {
        "id": msg_id,
        "from": from_,
        "to": to,
        "subject": subject,
        "body": body,
        "ts": datetime.now(timezone.utc).isoformat(),
        "status": "unread",
    }
    msgs.append(msg)
    _save(msgs)
    print("posted [" + msg_id + "] to " + to + ": " + subject)
    return msg_id


def read(for_: str, unread_only: bool = True) -> list:
    msgs = [m for m in _load() if isinstance(m, dict) and m.get("to") == for_]
    if unread_only:
        msgs = [m for m in msgs if m.get("status") != "acked"]
    for m in msgs:
        sender = m.get("from", "?")
        ts = str(m.get("ts", ""))[:16]
        status = m.get("status", "unread")
        print("[" + str(m.get("id", "--------")) + "] from=" + sender + " ts=" + ts + " status=" + status)
        print("  subject: " + str(m.get("subject", "")))
        print("  body: " + str(m.get("body", ""))[:300])
        print()
    if not msgs:
        print("no unread messages for " + for_)
    return msgs


def ack(msg_id: str) -> None:
    msgs = _load()
    for m in msgs:
        if isinstance(m, dict) and m.get("id") == msg_id:
            m["status"] = "acked"
            m["acked_at"] = datetime.now(timezone.utc).isoformat()
            _save(msgs)
            print("acked " + msg_id)
            return
    print("message " + msg_id + " not found")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("post")
    p.add_argument("--from", dest="from_", required=True)
    p.add_argument("--to", required=True)
    p.add_argument("--subject", required=True)
    p.add_argument("--body", default="")

    r = sub.add_parser("read")
    r.add_argument("--for", dest="for_", required=True)
    r.add_argument("--all", action="store_true")

    a = sub.add_parser("ack")
    a.add_argument("id")

    args = parser.parse_args()
    if args.cmd == "post":
        post(args.from_, args.to, args.subject, args.body)
    elif args.cmd == "read":
        read(args.for_, unread_only=not args.all)
    elif args.cmd == "ack":
        ack(args.id)
    else:
        parser.print_help()
