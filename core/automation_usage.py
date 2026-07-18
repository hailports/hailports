"""Usage ledger for non-LLM automation work.

This tracks real automation activity that does not create Anthropic/local LLM
usage rows but still displaces manual work: Salesforce API calls, outbound email
operations, scheduled scans, and similar service actions.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core import BASE_DIR, mountain_now, _get_stack_tz
from core.constants import COST_RATES
from core.savings_math import estimate_claude_baseline_savings

LOG_DIR = BASE_DIR / "data" / "logs"
LEDGER_PATH = LOG_DIR / "automation_usage.jsonl"
PAUSE_PATH = BASE_DIR / "data" / "runtime" / "outbound_sends_paused.json"

DEFAULT_API_EQUIVALENTS = {
    # These are not labor-value estimates. They are paid-model equivalents:
    # tokens that would normally be spent to plan, execute, and summarize the unit.
    "salesforce_query": {"tier": "haiku", "input_tokens": 900, "output_tokens": 250},
    "salesforce_read": {"tier": "haiku", "input_tokens": 700, "output_tokens": 200},
    "salesforce_describe": {"tier": "haiku", "input_tokens": 1200, "output_tokens": 400},
    "salesforce_tooling_api": {"tier": "sonnet", "input_tokens": 1200, "output_tokens": 450},
    "salesforce_write": {"tier": "sonnet", "input_tokens": 2200, "output_tokens": 800},
    "salesforce_admin_run": {"tier": "sonnet", "input_tokens": 4200, "output_tokens": 1400},
    "outbound_email": {"tier": "sonnet", "input_tokens": 1800, "output_tokens": 650},
    "outbound_email_reply": {"tier": "sonnet", "input_tokens": 2200, "output_tokens": 800},
    "email_draft": {"tier": "haiku", "input_tokens": 1000, "output_tokens": 350},
    "local_llm_generate": {"tier": "haiku", "input_tokens": 500, "output_tokens": 150},
    "local_llm_chat": {"tier": "haiku", "input_tokens": 600, "output_tokens": 200},
    "ollama_server_generate": {"tier": "haiku", "input_tokens": 1000, "output_tokens": 450},
    "ollama_server_chat": {"tier": "haiku", "input_tokens": 1200, "output_tokens": 500},
    "background_activity": {"tier": "haiku", "input_tokens": 500, "output_tokens": 150},
}

OLLAMA_POST_RE = __import__("re").compile(
    r'^\[GIN\]\s+(?P<stamp>\d{4}/\d{2}/\d{2} - \d{2}:\d{2}:\d{2})\s+\|\s+'
    r'(?P<status>\d{3})\s+\|\s+(?P<duration>[^|]+)\|\s+[^|]+\|\s+POST\s+"(?P<path>/api/(?:generate|chat))"'
)
LOG_ACTIVITY_DATE_RE = re.compile(
    r"(?P<iso>\d{4}-\d{2}-\d{2})(?:[ T]\d{2}:\d{2}:\d{2})?"
    r"|(?P<slash>\d{4}/\d{2}/\d{2})"
)
LOG_ACTIVITY_EXCLUDED = {
    "cost.log",
    "webui.log",
    "webui.err.log",
    "webui-backend-a.log",
    "webui-backend-a.err.log",
    "webui-backend-b.log",
    "webui-backend-b.err.log",
}


def _now() -> datetime:
    return mountain_now()


def _parse_ts(ts: Any) -> datetime | None:
    try:
        if isinstance(ts, (int, float)):
            return datetime.fromtimestamp(float(ts), tz=timezone.utc)
        text = str(ts or "").strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_get_stack_tz())
        return parsed
    except Exception:
        return None


def entry_date(entry: dict[str, Any]) -> str:
    raw_date = str(entry.get("date") or "").strip()
    if raw_date:
        return raw_date
    parsed = _parse_ts(entry.get("ts"))
    if parsed is None:
        return "unknown"
    return parsed.astimezone(_get_stack_tz()).strftime("%Y-%m-%d")


def ts_sort_key(value: Any) -> float:
    parsed = _parse_ts(value)
    if parsed is not None:
        return parsed.timestamp()
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def _stable_event_id(entry: dict[str, Any]) -> str:
    raw = json.dumps(
        {
            "ts": entry.get("ts"),
            "source": entry.get("source"),
            "kind": entry.get("kind"),
            "action": entry.get("action"),
            "metadata": entry.get("metadata") or {},
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


def api_equivalent_for(kind: str) -> dict[str, Any]:
    spec = dict(DEFAULT_API_EQUIVALENTS.get(str(kind or ""), DEFAULT_API_EQUIVALENTS["background_activity"]))
    tier = str(spec.get("tier") or "haiku")
    rates = COST_RATES.get(tier, COST_RATES["haiku"])
    input_tokens = int(spec.get("input_tokens") or 0)
    output_tokens = int(spec.get("output_tokens") or 0)
    if str(kind or "").startswith(("local_llm_", "ollama_server_")):
        cost = estimate_claude_baseline_savings(input_tokens=input_tokens, output_tokens=output_tokens)
    else:
        cost = (input_tokens * rates["input"] + output_tokens * rates["output"]) / 1_000_000
    return {
        "tier": tier,
        "model": f"claude-{tier}",
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "unit_cost_usd": round(cost, 6),
    }


def unit_value_for(kind: str) -> float:
    return float(api_equivalent_for(kind)["unit_cost_usd"])


def record_event(
    *,
    source: str,
    kind: str,
    action: str = "",
    count: int = 1,
    unit_value_usd: float | None = None,
    ts: Any = None,
    date: str | None = None,
    metadata: dict[str, Any] | None = None,
    event_id: str | None = None,
    ledger_path: Path | None = None,
) -> dict[str, Any]:
    """Append one automation usage event. Best-effort and side-effect isolated."""
    now = _now()
    raw_ts = ts if ts is not None else now.isoformat(timespec="seconds")
    parsed_count = max(int(count or 1), 1)
    api_equivalent = api_equivalent_for(kind)
    unit = api_equivalent["unit_cost_usd"] if unit_value_usd is None else float(unit_value_usd or 0.0)
    entry = {
        "ts": raw_ts,
        "date": date or entry_date({"ts": raw_ts}),
        "source": str(source or "automation"),
        "kind": str(kind or "automation"),
        "action": str(action or ""),
        "count": parsed_count,
        "api_equivalent_tier": api_equivalent["tier"],
        "api_equivalent_model": api_equivalent["model"],
        "api_equivalent_input_tokens": api_equivalent["input_tokens"],
        "api_equivalent_output_tokens": api_equivalent["output_tokens"],
        "api_equivalent_cost_usd": round(unit, 6),
        "unit_value_usd": round(unit, 6),
        "saved_usd": round(unit * parsed_count, 6),
        "metadata": metadata or {},
    }
    entry["event_id"] = event_id or _stable_event_id(entry)

    path = ledger_path or LEDGER_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")
    return entry


def read_events(log_dir: Path | None = None) -> list[dict[str, Any]]:
    path = (log_dir or LOG_DIR) / "automation_usage.jsonl"
    if not path.exists():
        return []
    by_event_id: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    try:
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                event_id = str(event.get("event_id") or _stable_event_id(event))
                event["event_id"] = event_id
                if event_id not in by_event_id:
                    order.append(event_id)
                # Last write wins so retries can correct count/value without double billing.
                by_event_id[event_id] = event
    except Exception:
        return []
    return [by_event_id[event_id] for event_id in order]


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        for line in path.read_text(errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    except Exception:
        return []
    return rows


def _tail_text_lines(path: Path, *, max_lines: int = 300, max_bytes: int = 256_000) -> list[str]:
    """Read a bounded tail from noisy service logs without loading the whole file."""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(max(0, size - max_bytes), os.SEEK_SET)
            data = handle.read(max_bytes)
        text = data.decode("utf-8", errors="replace")
        return text.splitlines()[-max_lines:]
    except Exception:
        return []


def _log_activity_date(line: str) -> str | None:
    match = LOG_ACTIVITY_DATE_RE.search(line)
    if not match:
        return None
    value = match.group("iso") or match.group("slash") or ""
    return value.replace("/", "-")[:10] or None


def background_log_activity_events(
    log_dir: Path | None = None,
    *,
    max_files: int = 160,
    max_lines_per_file: int = 300,
) -> list[dict[str, Any]]:
    """Infer background machine activity from active agent logs.

    Some revenue and maintenance agents do real work without writing the
    canonical automation ledger. These derived rows are deliberately low-value
    audit activity, not paid API spend or qualified displaced savings.
    """
    root = log_dir or LOG_DIR
    roots = [root]
    hustle = root / "hustle"
    if hustle.is_dir():
        roots.append(hustle)

    candidates: list[Path] = []
    for base in roots:
        try:
            for path in base.iterdir():
                if not path.is_file():
                    continue
                name = path.name
                if name in LOG_ACTIVITY_EXCLUDED:
                    continue
                if not (name.endswith(".log") or name.endswith(".err") or name.endswith(".err.log")):
                    continue
                candidates.append(path)
        except Exception:
            continue
    candidates.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0.0, reverse=True)

    spec = api_equivalent_for("background_activity")
    unit = float(spec["unit_cost_usd"])
    events: list[dict[str, Any]] = []
    for path in candidates[:max_files]:
        lines = _tail_text_lines(path, max_lines=max_lines_per_file)
        date_counts: Counter[str] = Counter()
        for line in lines:
            date = _log_activity_date(line)
            if date:
                date_counts[date] += 1
        if not date_counts:
            try:
                dt = datetime.fromtimestamp(path.stat().st_mtime, tz=_get_stack_tz())
                if (time.time() - path.stat().st_mtime) <= 36 * 3600:
                    date_counts[dt.strftime("%Y-%m-%d")] = 1
            except Exception:
                pass
        if not date_counts:
            continue
        try:
            rel = path.relative_to(root)
        except Exception:
            rel = path
        source = f"log:{rel}"
        for date, raw_count in date_counts.items():
            count = max(1, min(int(raw_count), max_lines_per_file))
            ts = ""
            try:
                ts = datetime.fromtimestamp(path.stat().st_mtime, tz=_get_stack_tz()).isoformat()
            except Exception:
                ts = date
            events.append({
                "ts": ts,
                "date": date,
                "source": source,
                "kind": "background_activity",
                "action": "log_activity",
                "count": count,
                "event_id": _stable_event_id({
                    "ts": date,
                    "source": source,
                    "kind": "background_activity",
                    "action": "log_activity",
                    "metadata": {"path": str(rel)},
                }),
                "api_equivalent_tier": spec["tier"],
                "api_equivalent_model": spec["model"],
                "api_equivalent_input_tokens": spec["input_tokens"],
                "api_equivalent_output_tokens": spec["output_tokens"],
                "api_equivalent_cost_usd": round(unit, 6),
                "unit_value_usd": round(unit, 6),
                "saved_usd": round(unit * count, 6),
                "metadata": {"source_log": str(rel), "derived": True},
            })
    return events


def _float_ts(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    parsed = _parse_ts(value)
    if parsed is not None:
        return parsed.timestamp()
    try:
        text = str(value or "").strip()
        if text:
            return float(text)
    except Exception:
        pass
    return None


def _represented_local_savings_index(log_dir: Path) -> dict[int, list[dict[str, Any]]]:
    """Index canonical savings rows so local LLM audits do not double-count."""
    index: dict[int, list[dict[str, Any]]] = {}
    for name in ("savings.jsonl", "hybrid_savings.jsonl"):
        for row in _read_jsonl(log_dir / name):
            ts = _float_ts(row.get("ts"))
            if ts is None:
                continue
            bucket = int(ts)
            index.setdefault(bucket, []).append(row)
    return index


def _num(row: dict[str, Any], *names: str) -> float:
    for name in names:
        try:
            value = row.get(name)
            if value is not None and value != "":
                return float(value)
        except Exception:
            continue
    return 0.0


def _roughly_same_amount(a: float, b: float) -> bool:
    if a <= 0 or b <= 0:
        return False
    return abs(a - b) <= max(0.00001, max(a, b) * 0.15)


def _roughly_same_size(a: float, b: float) -> bool:
    if a <= 0 or b <= 0:
        return False
    return abs(a - b) <= max(128.0, max(a, b) * 0.12)


def _local_llm_event_represented(event: dict[str, Any], index: dict[int, list[dict[str, Any]]]) -> bool:
    ts = _float_ts(event.get("ts"))
    if ts is None:
        return False
    saved = _num(event, "estimated_saved_usd", "saved_usd", "saved")
    in_chars = _num(event, "input_chars", "in_chars")
    out_chars = _num(event, "output_chars", "out_chars")
    for bucket in range(int(ts) - 2, int(ts) + 3):
        for row in index.get(bucket, []):
            row_ts = _float_ts(row.get("ts"))
            if row_ts is None or abs(row_ts - ts) > 2.0:
                continue
            row_saved = _num(row, "saved", "saved_usd", "estimated_saved_usd")
            row_in = _num(row, "in_chars", "input_chars")
            row_out = _num(row, "out_chars", "output_chars")
            if _roughly_same_amount(saved, row_saved):
                return True
            if _roughly_same_size(in_chars, row_in) and (out_chars <= 0 or row_out <= 0 or _roughly_same_size(out_chars, row_out)):
                return True
    return False


def _normalize_local_llm_source(source: Any, caller: Any = "") -> str:
    text = str(source or caller or "").strip()
    if not text:
        return "local_llm"
    lower = text.lower()
    if "/cellar/python" in lower or "/lib/python" in lower or "asyncio/tasks.py" in lower:
        return "local_llm"
    return text


def _local_datetime_from_ollama_stamp(stamp: str) -> datetime | None:
    try:
        return datetime.strptime(stamp, "%Y/%m/%d - %H:%M:%S").replace(tzinfo=_get_stack_tz())
    except Exception:
        return None


def _local_llm_http_index(log_dir: Path) -> dict[int, list[dict[str, Any]]]:
    index: dict[int, list[dict[str, Any]]] = {}
    for row in _read_jsonl(log_dir / "local_llm_calls.jsonl"):
        ts = _float_ts(row.get("iso_utc") or row.get("ts"))
        if ts is None:
            continue
        kind = str(row.get("kind") or "").strip().lower()
        index.setdefault(int(ts), []).append({"ts": ts, "kind": kind})
    return index


def _ollama_server_event_represented(ts: float, kind: str, index: dict[int, list[dict[str, Any]]]) -> bool:
    short_kind = "chat" if kind.endswith("chat") else "generate"
    for bucket in range(int(ts) - 3, int(ts) + 4):
        for row in index.get(bucket, []):
            if abs(float(row.get("ts") or 0) - ts) <= 3.0 and str(row.get("kind") or "") == short_kind:
                return True
    return False


def ollama_server_api_equivalent_events(log_dir: Path | None = None) -> list[dict[str, Any]]:
    """Count Ollama HTTP requests that bypass core.local_client auditing."""
    root = log_dir or LOG_DIR
    path = root / "ollama.log"
    if not path.exists():
        return []
    local_llm_index = _local_llm_http_index(root)
    events: list[dict[str, Any]] = []
    try:
        lines = path.read_text(errors="replace").splitlines()
    except Exception:
        return []
    for line_no, line in enumerate(lines, start=1):
        match = OLLAMA_POST_RE.search(line)
        if not match:
            continue
        dt = _local_datetime_from_ollama_stamp(match.group("stamp"))
        if dt is None:
            continue
        status = int(match.group("status"))
        endpoint = match.group("path")
        kind = "ollama_server_chat" if endpoint.endswith("/chat") else "ollama_server_generate"
        if _ollama_server_event_represented(dt.timestamp(), kind, local_llm_index):
            continue
        spec = api_equivalent_for(kind)
        unit = float(spec["unit_cost_usd"] if status < 400 else 0.0)
        events.append({
            "ts": dt.isoformat(),
            "date": dt.strftime("%Y-%m-%d"),
            "source": "ollama_server",
            "kind": kind,
            "action": endpoint.rsplit("/", 1)[-1],
            "count": 1,
            "event_id": _stable_event_id({
                "ts": dt.isoformat(),
                "source": "ollama_server",
                "kind": kind,
                "action": endpoint,
                "metadata": {"line": line_no},
            }),
            "api_equivalent_tier": spec["tier"],
            "api_equivalent_model": spec["model"],
            "api_equivalent_input_tokens": spec["input_tokens"],
            "api_equivalent_output_tokens": spec["output_tokens"],
            "api_equivalent_cost_usd": round(unit, 6),
            "unit_value_usd": round(unit, 6),
            "saved_usd": round(unit, 6),
            "metadata": {
                "endpoint": endpoint,
                "status_code": status,
                "duration": match.group("duration").strip(),
                "source_log": "ollama.log",
                "line": line_no,
            },
        })
    return events


def local_llm_api_equivalent_events(log_dir: Path | None = None) -> list[dict[str, Any]]:
    """Return local LLM audit rows not already represented in canonical savings.

    `local_llm_calls.jsonl` is the broad audit stream from core.local_client.
    Some callers also write `savings.jsonl`; those rows are matched by timestamp
    and approximate size/value so the dashboard counts every local inference once.
    """
    root = log_dir or LOG_DIR
    represented = _represented_local_savings_index(root)
    events: list[dict[str, Any]] = []
    for row in _read_jsonl(root / "local_llm_calls.jsonl"):
        if bool(row.get("historical")):
            continue
        if str(row.get("status") or "").strip().lower() != "success":
            continue
        if _local_llm_event_represented(row, represented):
            continue
        kind = "local_llm_chat" if str(row.get("kind") or "").lower() == "chat" else "local_llm_generate"
        input_chars = int(_num(row, "input_chars", "in_chars"))
        visible_output_chars = int(_num(row, "output_chars", "out_chars"))
        raw_output_chars = int(_num(row, "raw_output_chars", "raw_out_chars"))
        generated_tokens = int(_num(row, "eval_count", "output_tokens"))
        output_inferred = False
        output_chars = visible_output_chars
        if output_chars <= 0 and raw_output_chars > 0:
            output_chars = raw_output_chars
            output_inferred = True
        elif output_chars <= 0 and generated_tokens > 0:
            # Qwen can spend the whole response budget inside <think> text that
            # the client strips before returning. Count generated tokens for
            # API-equivalent value without storing the private response text.
            output_chars = generated_tokens * 4
            output_inferred = True
        saved = estimate_claude_baseline_savings(input_chars=input_chars, output_chars=output_chars)
        tier = str(row.get("displaced_tier") or "haiku").strip().lower() or "haiku"
        ts = row.get("iso_utc") or row.get("ts")
        source = _normalize_local_llm_source(row.get("source"), row.get("caller"))
        events.append({
            "ts": ts,
            "date": entry_date({"ts": ts}),
            "source": source,
            "kind": kind,
            "action": str(row.get("kind") or "generate"),
            "count": 1,
            "event_id": row.get("event_id"),
            "api_equivalent_tier": tier,
            "api_equivalent_model": f"claude-{tier}",
            "api_equivalent_input_tokens": max(0, round(input_chars / 4)),
            "api_equivalent_output_tokens": max(0, generated_tokens or round(output_chars / 4)),
            "api_equivalent_cost_usd": round(saved, 6),
            "unit_value_usd": round(saved, 6),
            "saved_usd": round(saved, 6),
            "metadata": {
                "local_model": row.get("model"),
                "route": row.get("route"),
                "caller": row.get("caller"),
                "input_chars": input_chars,
                "output_chars": output_chars,
                "visible_output_chars": visible_output_chars,
                "generated_tokens": generated_tokens,
                "output_inferred": output_inferred,
                "source_log": "local_llm_calls.jsonl",
            },
        })
    return events


def local_machine_api_equivalent_events(log_dir: Path | None = None) -> list[dict[str, Any]]:
    root = log_dir or LOG_DIR
    events = []
    events.extend(local_llm_api_equivalent_events(log_dir=root))
    events.extend(ollama_server_api_equivalent_events(log_dir=root))
    return events


def outbound_sends_paused() -> tuple[bool, str]:
    if str(__import__("os").environ.get("CLAUDE_STACK_ALLOW_OUTBOUND_SENDS") or "").lower() in {"1", "true", "yes"}:
        return False, "override enabled"
    try:
        payload = json.loads(PAUSE_PATH.read_text())
    except Exception:
        return False, ""
    if bool(payload.get("paused", True)):
        reason = str(payload.get("reason") or "outbound sends paused")
        return True, reason
    return False, ""


def outbound_sends_allowed() -> tuple[bool, str]:
    paused, reason = outbound_sends_paused()
    if paused:
        return False, reason
    return True, ""
