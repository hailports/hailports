"""Deterministic overnight activity rundown for chat surfaces.

This keeps "what did the machine do all night?" out of the generic LLM/tool
path. It reads local state and recent logs only, so the answer is fast and
does not spend model tokens.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from core import BASE_DIR, mountain_now


ACTIVITY_RE = re.compile(
    r"\b("
    r"what\s+did\s+(the\s+)?machine\s+do|"
    r"what\s+happened\s+(overnight|last\s+night)|"
    r"overnight\s+(activity|report|rundown|summary)|"
    r"all\s+night|"
    r"what\s+ran\s+(overnight|last\s+night)"
    r")\b",
    re.I,
)


def is_activity_rundown_request(text: str) -> bool:
    return bool(ACTIVITY_RE.search(str(text or "")))


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def _read_text(path: Path, max_chars: int = 8000) -> str:
    try:
        text = path.read_text(errors="replace")
    except Exception:
        return ""
    if len(text) > max_chars:
        return text[-max_chars:]
    return text


def _mtime_label(path: Path) -> str:
    try:
        dt = datetime.fromtimestamp(path.stat().st_mtime, mountain_now().tzinfo)
        return dt.strftime("%I:%M %p").lstrip("0")
    except Exception:
        return "unknown time"


def _changed_files(base: Path, start: datetime, limit: int = 14) -> list[Path]:
    roots = [
        base / "data" / "hustle",
        base / "data" / "runtime",
        base / "data" / "logs",
        base / "products" / "content",
        base / "products" / "outreach",
    ]
    out: list[Path] = []
    start_ts = start.timestamp()
    for root in roots:
        if not root.exists():
            continue
        try:
            iterator = root.rglob("*") if root.name != "logs" else root.glob("*")
            for path in iterator:
                if not path.is_file():
                    continue
                try:
                    if path.stat().st_mtime >= start_ts:
                        out.append(path)
                except Exception:
                    continue
        except Exception:
            continue
    return sorted(out, key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)[:limit]


def _line_is_recent(line: str, since: datetime) -> bool:
    match = re.search(r"\b(20\d{2}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})", line)
    if not match:
        return True
    try:
        dt = datetime.strptime(" ".join(match.groups()), "%Y-%m-%d %H:%M:%S")
        dt = dt.replace(tzinfo=mountain_now().tzinfo)
        return dt >= since
    except Exception:
        return True


def _latest_lines(path: Path, tokens: tuple[str, ...], limit: int = 6, since: datetime | None = None) -> list[str]:
    text = _read_text(path, max_chars=50000)
    rows = []
    for raw in text.splitlines():
        line = raw.strip()
        if line and any(token.lower() in line.lower() for token in tokens):
            if since is not None and not _line_is_recent(line, since):
                continue
            rows.append(line)
    return rows[-limit:]


def build_activity_rundown_text(base_dir: Path | str = BASE_DIR, prompt: str = "") -> str:
    base = Path(base_dir)
    now = mountain_now()
    start = now - timedelta(hours=10)
    hustle = base / "data" / "hustle"
    logs = base / "data" / "logs"

    coordinator = _read_json(hustle / "revenue_coordinator_state.json", {})
    session_status = _read_json(hustle / "session_status.json", {})
    dashboard = _read_json(hustle / "revenue_dashboard.json", {})
    health = _read_json(hustle / "revenue_health.json", {})
    overnight_keeper = _read_json(base / "data" / "runtime" / "overnight_keeper_state.json", {})
    strategy_requests = sorted((hustle / "strategy_requests").glob("*.json"), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)[:8]

    pipeline = coordinator.get("last_pipeline") if isinstance(coordinator, dict) else {}
    metrics = coordinator.get("last_metrics") if isinstance(coordinator, dict) else {}
    actions = coordinator.get("last_actions") if isinstance(coordinator, dict) and isinstance(coordinator.get("last_actions"), list) else []
    today = dashboard.get("today") if isinstance(dashboard, dict) and isinstance(dashboard.get("today"), dict) else {}
    summary = health.get("summary") if isinstance(health, dict) and isinstance(health.get("summary"), dict) else {}

    lines = [
        f"Overnight machine rundown ({start.strftime('%I:%M %p').lstrip('0')} - {now.strftime('%I:%M %p').lstrip('0')})",
        "",
    ]

    if isinstance(overnight_keeper, dict) and overnight_keeper:
        lines.append(
            "Keeper: "
            f"admin_ai_running={overnight_keeper.get('admin_ai_running', 'unknown')}, "
            f"stack_running={overnight_keeper.get('stack_running', 'unknown')}, "
            f"mode={overnight_keeper.get('phase') or overnight_keeper.get('mode') or 'unknown'}."
        )

    if pipeline or metrics:
        lines.append(
            "Revenue coordinator: "
            f"{pipeline.get('total_prospects', metrics.get('prospects_total', 0))} prospects, "
            f"{pipeline.get('active_sequences', metrics.get('prospects_activated', 0))} active, "
            f"{pipeline.get('by_status', {}).get('queued', 0) if isinstance(pipeline.get('by_status'), dict) else 0} queued, "
            f"{metrics.get('emails_sent', pipeline.get('emails_sent', 0))} emails sent."
        )
        if actions:
            lines.append("Last coordinator actions: " + ", ".join(str(a) for a in actions[:6]) + ".")

    if today:
        lines.append(
            "Revenue dashboard: "
            f"revenue={today.get('total_revenue') or today.get('gumroad_revenue') or '$0'}, "
            f"emails_sent={today.get('emails_sent', 0)}, "
            f"opens={today.get('opens', 0)}, clicks={today.get('clicks', 0)}."
        )
    if summary:
        lines.append(
            "Health: "
            f"{summary.get('healthy_agents', summary.get('healthy', 'unknown'))}/{summary.get('total', summary.get('loaded_agents', summary.get('loaded', 'unknown')))} healthy, "
            f"{summary.get('dead', 0)} dead, {summary.get('stuck', 0)} stuck, {summary.get('erroring', 0)} erroring."
        )

    ok_sessions = []
    blocked_sessions = []
    if isinstance(session_status, dict):
        for name, row in sorted(session_status.items()):
            if isinstance(row, dict) and row.get("ok") is True:
                ok_sessions.append(name)
            elif isinstance(row, dict) and row.get("human_required"):
                blocked_sessions.append(name)
    if ok_sessions or blocked_sessions:
        lines.append("Authenticated channels: " + (", ".join(ok_sessions[:8]) if ok_sessions else "none verified") + ".")
        if blocked_sessions:
            lines.append("Needs human auth: " + ", ".join(blocked_sessions[:8]) + ".")

    if strategy_requests:
        lines.extend(["", "Strategy work queued overnight:"])
        for path in strategy_requests[:5]:
            item = _read_json(path, {})
            if not isinstance(item, dict):
                continue
            label = item.get("request") or item.get("summary") or path.stem
            lines.append(f"- {_mtime_label(path)}: {item.get('brand', 'revenue')}/{item.get('channel', 'general')} - {str(label)[:130]}")

    signals = []
    for path in (
        logs / "revenue-coordinator.log",
        logs / "revenue-watchdog.err.log",
        logs / "revenue-strategist.err.log",
        logs / "overnight-keeper.log",
        logs / "browser-zombie-killer.log",
    ):
        signals.extend(_latest_lines(path, ("starting", "complete", "queued", "sent", "error", "warning", "restarted"), limit=4, since=start))
    if signals:
        lines.extend(["", "Recent log signals:"])
        for line in signals[-8:]:
            lines.append(f"- {line[:180]}")

    changed = _changed_files(base, start, limit=10)
    if changed:
        lines.extend(["", "Files touched recently:"])
        for path in changed[:10]:
            try:
                rel = path.relative_to(base)
            except Exception:
                rel = path
            lines.append(f"- {_mtime_label(path)} {rel}")

    lines.extend([
        "",
        "Fast/cost note: this is a local state/log rundown. No paid LLM call was needed.",
    ])
    return "\n".join(lines).strip()
