#!/usr/bin/env python3
"""Build a sanitized rolling digest of recent local AI chats for memorySearch."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()
OUT = HOME / ".openclaw/workspace/CompanyA-local/digests/RECENT_CHAT_CONTEXT.md"
STATE = HOME / ".openclaw/workspace/CompanyA-local/digests/RECENT_CHAT_CONTEXT.state.json"
DEFAULT_DAYS = 7
DEFAULT_MAX_FILES = 400
DEFAULT_MAX_ITEMS = 6000
MAX_EXCERPT = 700
MAX_DIGEST_BYTES = 6_000_000

SECRET_RX = [
    re.compile(r"(?<![A-Za-z0-9])sk-or-[A-Za-z0-9_\-]{12,}"),
    re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_\-]{12,}"),
    re.compile(r"(?<![A-Za-z0-9])xai-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"ghp_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{12,}"),
    re.compile(r"(?i)((?:api[_-]?key|token|secret|password|passwd|access[_-]?token|authorization)[\"']?\s*[:=]\s*[\"']?)([^\s\"',}]{6,})"),
    re.compile(r"\b[A-Za-z0-9+/]{56,}={0,2}\b"),
]

NOISE_RX = [
    re.compile(r"^# AGENTS\.md instructions for "),
    re.compile(r"^<environment_context>"),
    re.compile(r"^\[cron:[^\]]+\]"),
    re.compile(r"^Current time: "),
    re.compile(r"I['’]ve looked through the memory for", re.I),
    re.compile(r"Could you provide more context", re.I),
    re.compile(r"no direct link to a report explicitly stored in memory", re.I),
    re.compile(r"done verbally, in a different system not integrated", re.I),
    re.compile(r"we made some high level reporting.*spend.*calls.*automations", re.I),
]

IMPORTANT_RX = re.compile(
    r"(?i)\b("
    r"remember|fix|broken|blocked|changelog|stack map|openclaw|claw chat|codex|claude|"
    r"null bytes?|nul|cancer thread|thread|context brain|hailports|boss babe|shop|"
    r"website|cms|gumroad|revenue|CompanyA|outlook|salesforce|monday|little bird|zoom"
    r")\b"
)


@dataclass
class Item:
    ts: float
    source: str
    session: str
    role: str
    text: str


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def clean_text(value: object) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    text = text.replace("\x00", "")
    text = "".join(ch if ch in "\n\t" or ord(ch) >= 32 else " " for ch in text)
    for rx in SECRET_RX:
        if rx.groups >= 2:
            text = rx.sub(lambda m: m.group(1) + "***REDACTED***", text)
        else:
            text = rx.sub("***REDACTED***", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def text_from_content(content: object) -> str:
    if isinstance(content, str):
        return clean_text(content)
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            kind = part.get("type")
            if kind in {"tool_result", "tool_use", "image", "input_image"}:
                continue
            text = part.get("text") or part.get("content")
            if isinstance(text, str):
                parts.append(text)
        return clean_text("\n".join(parts))
    return ""


def should_skip(text: str, role: str) -> bool:
    if not text or len(text) < 4:
        return True
    if len(text) > 200_000:
        return True
    if any(rx.search(text) for rx in NOISE_RX):
        return True
    if "tool-use-id" in text and "output-file" in text and "<status>" in text:
        return True
    if role == "assistant" and text.startswith("{") and '"cmd"' in text:
        return True
    return False


def excerpt(text: str, limit: int = MAX_EXCERPT) -> str:
    text = re.sub(r"\s+", " ", clean_text(text)).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def file_ts(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def recent_files(root: Path, pattern: str, days: int, max_files: int) -> list[Path]:
    cutoff = datetime.now().timestamp() - days * 86400
    if not root.exists():
        return []
    files = []
    for path in root.rglob(pattern):
        name = path.name
        if ".trajectory" in name or name.endswith(".state.json"):
            continue
        try:
            st = path.stat()
        except OSError:
            continue
        if st.st_mtime >= cutoff and path.is_file():
            files.append(path)
    return sorted(files, key=file_ts, reverse=True)[:max_files]


def parse_jsonl(path: Path):
    try:
        with path.open("rb") as fh:
            for raw in fh:
                if b"\x00" in raw:
                    raw = raw.replace(b"\x00", b"")
                try:
                    yield json.loads(raw.decode("utf-8", errors="replace"))
                except Exception:
                    continue
    except Exception:
        return


def item_ts(obj: dict, path: Path) -> float:
    raw = obj.get("timestamp") or obj.get("created_at") or obj.get("ts")
    if isinstance(raw, (int, float)):
        return float(raw / 1000 if raw > 10_000_000_000 else raw)
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return file_ts(path)


def source_session(path: Path, source: str) -> str:
    name = path.name
    if source == "codex":
        return name.replace("rollout-", "").replace(".jsonl", "")[:48]
    return name.replace(".jsonl", "")[:48]


def parse_openclaw(path: Path) -> list[Item]:
    out: list[Item] = []
    for obj in parse_jsonl(path):
        if not isinstance(obj, dict):
            continue
        msg = obj.get("message")
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role not in {"user", "assistant"}:
            continue
        text = text_from_content(msg.get("content"))
        if should_skip(text, role):
            continue
        out.append(Item(item_ts(obj, path), "openclaw", source_session(path, "openclaw"), role, text))
    return out


def parse_codex(path: Path) -> list[Item]:
    out: list[Item] = []
    for obj in parse_jsonl(path):
        if not isinstance(obj, dict) or obj.get("type") != "response_item":
            continue
        payload = obj.get("payload") or {}
        if not isinstance(payload, dict) or payload.get("type") != "message":
            continue
        role = payload.get("role")
        if role not in {"user", "assistant"}:
            continue
        phase = payload.get("phase")
        if role == "assistant" and phase not in {None, "final_answer"}:
            continue
        text = text_from_content(payload.get("content"))
        if should_skip(text, role):
            continue
        out.append(Item(item_ts(obj, path), "codex", source_session(path, "codex"), role, text))
    return out


def parse_claude(path: Path) -> list[Item]:
    out: list[Item] = []
    for obj in parse_jsonl(path):
        if not isinstance(obj, dict):
            continue
        msg = obj.get("message")
        role = None
        content = None
        if isinstance(msg, dict):
            role = msg.get("role")
            content = msg.get("content")
        if role not in {"user", "assistant"}:
            typ = obj.get("type")
            if typ in {"user", "assistant"}:
                role = typ
                content = msg.get("content") if isinstance(msg, dict) else obj.get("content")
        if role not in {"user", "assistant"}:
            continue
        text = text_from_content(content)
        if should_skip(text, role):
            continue
        out.append(Item(item_ts(obj, path), "claude", source_session(path, "claude"), role, text))
    return out


def gather(days: int, max_files: int) -> tuple[list[Item], dict[str, int]]:
    roots = [
        ("openclaw", HOME / ".openclaw/agents/main/sessions", parse_openclaw),
        ("codex", HOME / ".codex/sessions", parse_codex),
        ("claude", HOME / ".claude/projects", parse_claude),
    ]
    items: list[Item] = []
    counts = {"openclaw_files": 0, "codex_files": 0, "claude_files": 0}
    for source, root, parser in roots:
        files = recent_files(root, "*.jsonl", days, max_files)
        counts[f"{source}_files"] = len(files)
        for path in files:
            items.extend(parser(path))
    return items, counts


def high_signal(items: list[Item], limit: int = 35) -> list[Item]:
    seen: set[str] = set()
    out: list[Item] = []
    for item in sorted(items, key=lambda i: i.ts, reverse=True):
        if item.role != "user" or not IMPORTANT_RX.search(item.text):
            continue
        key = excerpt(item.text, 180).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
        if len(out) >= limit:
            break
    return out


def conversation_blocks(items: list[Item], max_items: int) -> list[str]:
    lines: list[str] = []
    # User intent is safer long-term memory than assistant responses. Old wrong
    # assistant answers can outrank the corrected source index and poison recall.
    recent = [i for i in sorted(items, key=lambda i: i.ts, reverse=True) if i.role == "user"][:max_items]
    for item in recent:
        dt = datetime.fromtimestamp(item.ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        lines.append(f"### {dt} - {item.source}/{item.session}")
        lines.append(f"- {item.role.title()}: {excerpt(item.text)}")
        lines.append("")
    return lines


def build_digest(days: int, max_files: int, max_items: int) -> tuple[str, dict[str, int]]:
    items, counts = gather(days, max_files)
    items = [i for i in items if i.text]
    high = high_signal(items)
    lines = [
        "# Recent Chat Context",
        "",
        f"Generated: {utc_now()}",
        f"Window: last {days} days",
        "Purpose: Sanitized rolling local digest for OpenClaw memorySearch. Raw transcripts are not injected.",
        "Sanitizers: NUL bytes removed, control bytes stripped, secret-looking values redacted, tool noise skipped.",
        "",
        "## Source Counts",
        f"- OpenClaw files scanned: {counts.get('openclaw_files', 0)}",
        f"- Codex files scanned: {counts.get('codex_files', 0)}",
        f"- Claude files scanned: {counts.get('claude_files', 0)}",
        f"- Chat items indexed in digest: {min(len(items), max_items)} of {len(items)} extracted",
        "",
        "## High-Signal Recent User Requests",
    ]
    if high:
        for item in high:
            dt = datetime.fromtimestamp(item.ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            lines.append(f"- {dt} [{item.source}] {excerpt(item.text, 420)}")
    else:
        lines.append("- No high-signal user requests found in the scan window.")
    lines.extend(["", "## Recent Chat Excerpts"])
    lines.extend(conversation_blocks(items, max_items))
    text = "\n".join(lines).strip() + "\n"
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_DIGEST_BYTES:
        text = encoded[:MAX_DIGEST_BYTES].decode("utf-8", errors="ignore")
        text = text.rsplit("\n### ", 1)[0].rstrip() + "\n\n_Truncated at safe digest size cap._\n"
    return text, counts | {"items": len(items), "high_signal": len(high)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS)
    ap.add_argument("--max-files", type=int, default=DEFAULT_MAX_FILES)
    ap.add_argument("--max-items", type=int, default=DEFAULT_MAX_ITEMS)
    args = ap.parse_args()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    text, counts = build_digest(args.days, args.max_files, args.max_items)
    if "\x00" in text:
        raise RuntimeError("digest still contains NUL byte")
    OUT.write_text(text, encoding="utf-8")
    os.chmod(OUT, 0o640)
    STATE.write_text(json.dumps({"generated": utc_now(), **counts}, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
