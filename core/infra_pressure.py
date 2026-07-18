#!/usr/bin/env python3
"""Observation-only resource pressure feed for autonomous stack scheduling.

This deliberately does not stop, restart, or unload anything. It creates a
small JSON signal that agents and dashboards can read before starting expensive
browser or local-LLM work.
"""

from __future__ import annotations

from core.constants import LOCAL_MODEL
import argparse
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


BASE = Path(os.environ.get("CLAUDE_STACK_DIR") or Path.home() / "claude-stack")
RUNTIME_DIR = BASE / "data" / "runtime"
LOG_DIR = BASE / "data" / "logs"
STATE_PATH = RUNTIME_DIR / "infra_pressure_state.json"
LOG_PATH = LOG_DIR / "infra-pressure.jsonl"
BROWSER_LOCK_PATH = RUNTIME_DIR / "browser_pool.lock"

OLLAMA_QUEUE_HEALTH_URL = os.environ.get("OLLAMA_QUEUE_HEALTH_URL", "http://127.0.0.1:11435/health")
MAX_LOG_BYTES = int(os.environ.get("INFRA_PRESSURE_MAX_LOG_BYTES", str(5 * 1024 * 1024)))

redacted_MARKERS = (
    "CompanyA",
    "salesforce",
    "sf_",
    "closed_selected_redacted",
)

NON_redacted_WORK_MARKERS = (
    "dm",
    "outreach",
    "bio",
    "prospector",
    "blog",
    "fiverr",
    "etsy",
    "tiktok",
    "gumroad",
    "kdp",
    "affiliate",
)

CORE_SERVICE_LABELS = (
    "com.claude-stack.webui-guardian",
    "com.claude-stack.webui-backend-a",
    "com.claude-stack.webui-backend-b",
    "com.claude-stack.ollama-queue",
    "com.claude-stack.ollama",
    "com.claude-stack.ollama-watchdog",
    "com.claude-stack.watchdog",
    "com.claude-stack.healer",
    "com.claude-stack.diagnostician",
)

OPTIONAL_GENERIC_LABELS = (
    "com.claude-stack.dm-outreach",
    "com.claude-stack.bio-harvester",
    "com.claude-stack.social-prospector",
    "com.claude-stack.auto-blogger",
)


def now() -> float:
    return time.time()


def iso(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts or now()))


def run(argv: list[str], timeout: float = 4.0) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            argv,
            cwd=str(BASE),
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 124, "", str(exc)


def http_json(url: str, timeout: float = 2.0) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            body = response.read(256_000)
        data = json.loads(body.decode("utf-8", errors="replace"))
        return data if isinstance(data, dict) else {"raw": data}
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": str(exc)}


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {"raw": data}
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        return {"error": str(exc)}


def pid_alive(pid: Any) -> bool:
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return False
    if pid_int <= 0:
        return False
    try:
        os.kill(pid_int, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def parse_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def load_state() -> dict[str, Any]:
    code, out, err = run(["sysctl", "-n", "vm.loadavg"], timeout=2)
    loads: list[float] = []
    if code == 0:
        cleaned = out.replace("{", "").replace("}", "").strip()
        loads = [parse_float(part) for part in cleaned.split()[:3]]
    cpu_count = os.cpu_count() or 1
    return {
        "ok": code == 0,
        "load_1m": loads[0] if len(loads) > 0 else None,
        "load_5m": loads[1] if len(loads) > 1 else None,
        "load_15m": loads[2] if len(loads) > 2 else None,
        "cpu_count": cpu_count,
        "error": err if code != 0 else None,
    }


def memory_state() -> dict[str, Any]:
    code, out, err = run(["vm_stat"], timeout=2)
    if code != 0:
        return {"ok": False, "error": err}

    page_size = 4096
    pages: dict[str, int] = {}
    for line in out.splitlines():
        if "page size of" in line:
            parts = [part for part in line.split() if part.isdigit()]
            if parts:
                page_size = int(parts[0])
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        digits = "".join(ch for ch in value if ch.isdigit())
        if digits:
            pages[key.strip()] = int(digits)

    free_pages = (
        pages.get("Pages free", 0)
        + pages.get("Pages inactive", 0)
        + pages.get("Pages speculative", 0)
    )
    wired_pages = pages.get("Pages wired down", 0)
    compressed_pages = pages.get("Pages occupied by compressor", 0)
    available_gb = round(free_pages * page_size / (1024**3), 2)
    pressure_pages = wired_pages + compressed_pages
    pressure_gb = round(pressure_pages * page_size / (1024**3), 2)
    return {
        "ok": True,
        "available_gb_estimate": available_gb,
        "wired_plus_compressed_gb": pressure_gb,
        "page_size": page_size,
    }


def disk_state() -> dict[str, Any]:
    try:
        usage = shutil.disk_usage(BASE)
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "path": str(BASE),
        "free_gb": round(usage.free / (1024**3), 2),
        "used_pct": round((usage.used / usage.total) * 100, 1) if usage.total else None,
    }


def browser_lock_state() -> dict[str, Any]:
    if not BROWSER_LOCK_PATH.exists():
        return {"present": False}

    stat = BROWSER_LOCK_PATH.stat()
    meta = read_json(BROWSER_LOCK_PATH)
    started = parse_float(meta.get("started_at") or meta.get("timestamp") or stat.st_mtime, stat.st_mtime)
    age = max(0, now() - started)
    owner = str(meta.get("owner") or meta.get("agent") or "unknown")
    owner_lower = owner.lower()
    is_redacted = any(marker in owner_lower for marker in redacted_MARKERS)
    is_generic = any(marker in owner_lower for marker in NON_redacted_WORK_MARKERS)
    pid = meta.get("pid")

    return {
        "present": True,
        "path": str(BROWSER_LOCK_PATH),
        "owner": owner,
        "pid": pid,
        "pid_alive": pid_alive(pid),
        "age_seconds": round(age, 1),
        "started_at": iso(started),
        "is_redacted_related": is_redacted,
        "looks_like_generic_revenue_work": is_generic,
        "metadata": meta,
    }


def ollama_queue_state() -> dict[str, Any]:
    health = http_json(OLLAMA_QUEUE_HEALTH_URL)
    if health.get("ok") is False and "error" in health:
        return {"ok": False, "url": OLLAMA_QUEUE_HEALTH_URL, "error": health["error"]}

    queue_depth = (
        health.get("queue_depth")
        or health.get("pending")
        or health.get("pending_jobs")
        or health.get("queued")
        or 0
    )
    oldest_wait = (
        health.get("oldest_wait_seconds")
        or health.get("oldest_wait_s")
        or health.get("max_wait_seconds")
        or 0
    )
    model = health.get("model") or health.get("local_model") or health.get("active_model")
    return {
        "ok": True,
        "url": OLLAMA_QUEUE_HEALTH_URL,
        "queue_depth": parse_int(queue_depth),
        "oldest_wait_seconds": parse_float(oldest_wait),
        "model": model,
        "raw": health,
    }


def launchctl_services() -> dict[str, Any]:
    code, out, err = run(["launchctl", "list"], timeout=5)
    if code != 0:
        return {"ok": False, "error": err}

    rows: dict[str, dict[str, Any]] = {}
    for line in out.splitlines()[1:]:
        parts = line.split(None, 2)
        if len(parts) != 3:
            continue
        pid_raw, status_raw, label = parts
        if not label.startswith("com.claude-stack."):
            continue
        pid = None if pid_raw == "-" else parse_int(pid_raw)
        status = None if status_raw == "-" else parse_int(status_raw)
        rows[label] = {"pid": pid, "status": status, "running": pid is not None}

    def subset(labels: tuple[str, ...]) -> dict[str, Any]:
        return {label: rows.get(label, {"pid": None, "status": None, "running": False}) for label in labels}

    return {
        "ok": True,
        "core": subset(CORE_SERVICE_LABELS),
        "optional_generic": subset(OPTIONAL_GENERIC_LABELS),
        "claude_stack_count": len(rows),
    }


def top_processes(limit: int = 8) -> list[dict[str, Any]]:
    code, out, _ = run(["ps", "-axo", "pid,pcpu,pmem,etime,command"], timeout=4)
    if code != 0:
        return []

    rows: list[dict[str, Any]] = []
    for line in out.splitlines()[1:]:
        parts = line.split(None, 4)
        if len(parts) != 5:
            continue
        pid, pcpu, pmem, etime, command = parts
        if "infra_pressure.py" in command:
            continue
        rows.append(
            {
                "pid": parse_int(pid),
                "cpu_pct": parse_float(pcpu),
                "mem_pct": parse_float(pmem),
                "etime": etime,
                "command": command[:220],
            }
        )
    rows.sort(key=lambda row: (row["cpu_pct"], row["mem_pct"]), reverse=True)
    return rows[:limit]


def redacted_activity_state() -> dict[str, Any]:
    candidates: list[Path] = []
    if RUNTIME_DIR.exists():
        for path in RUNTIME_DIR.iterdir():
            name = path.name.lower()
            if any(marker in name for marker in redacted_MARKERS):
                candidates.append(path)

    files: list[dict[str, Any]] = []
    for path in sorted(candidates, key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)[:12]:
        try:
            stat = path.stat()
        except OSError:
            continue
        files.append(
            {
                "name": path.name,
                "mtime": iso(stat.st_mtime),
                "age_seconds": round(max(0, now() - stat.st_mtime), 1),
                "size_bytes": stat.st_size,
            }
        )

    last_age = files[0]["age_seconds"] if files else None
    return {
        "recent_files": files,
        "last_activity_age_seconds": last_age,
        "recent_activity": last_age is not None and last_age < 6 * 3600,
        "scope_gate": "Only improve autonomous infrastructure when it benefits CompanyA delivery, reliability, or safety.",
    }


def compute_pressure(snapshot: dict[str, Any]) -> dict[str, Any]:
    score = 0
    reasons: list[str] = []
    recommendations: list[str] = []

    load = snapshot["resources"]["load"]
    load_1m = load.get("load_1m")
    cpu_count = load.get("cpu_count") or 1
    if load_1m is not None and load_1m > cpu_count * 1.5:
        score += 2
        reasons.append(f"1m load {load_1m:.2f} is high for {cpu_count} CPU cores")
    elif load_1m is not None and load_1m > cpu_count:
        score += 1
        reasons.append(f"1m load {load_1m:.2f} is above CPU core count")

    memory = snapshot["resources"]["memory"]
    available_gb = memory.get("available_gb_estimate")
    if available_gb is not None and available_gb < 2:
        score += 2
        reasons.append(f"estimated available memory is low: {available_gb} GB")
    elif available_gb is not None and available_gb < 4:
        score += 1
        reasons.append(f"estimated available memory is constrained: {available_gb} GB")

    disk = snapshot["resources"]["disk"]
    free_gb = disk.get("free_gb")
    if free_gb is not None and free_gb < 5:
        score += 2
        reasons.append(f"disk free space is low: {free_gb} GB")
    elif free_gb is not None and free_gb < 15:
        score += 1
        reasons.append(f"disk free space is getting tight: {free_gb} GB")

    ollama = snapshot["resources"]["ollama_queue"]
    queue_depth = parse_int(ollama.get("queue_depth"))
    oldest_wait = parse_float(ollama.get("oldest_wait_seconds"))
    if queue_depth >= 8 or oldest_wait >= 240:
        score += 2
        reasons.append(f"30B queue is backed up: depth={queue_depth}, oldest_wait={oldest_wait:.0f}s")
        recommendations.append("defer_non_redacted_llm_work")
    elif queue_depth >= 3 or oldest_wait >= 90:
        score += 1
        reasons.append(f"30B queue is busy: depth={queue_depth}, oldest_wait={oldest_wait:.0f}s")
        recommendations.append("prefer_short_redacted_llm_jobs")

    browser = snapshot["resources"]["browser_lock"]
    if browser.get("present"):
        age = parse_float(browser.get("age_seconds"))
        owner = browser.get("owner") or "unknown"
        if age >= 900:
            score += 1
            reasons.append(f"browser lock held for {age:.0f}s by {owner}")
            recommendations.append("review_browser_lock_if_new_browser_work_blocks")
        if browser.get("looks_like_generic_revenue_work") and not browser.get("is_redacted_related"):
            recommendations.append("do_not_start_more_generic_browser_work_until_lock_clears")

    services = snapshot["services"]
    if services.get("ok"):
        core = services.get("core", {})
        down = [label.rsplit(".", 1)[-1] for label, state in core.items() if not state.get("running")]
        if down:
            score += min(2, len(down))
            reasons.append("core services not running: " + ", ".join(down[:4]))
            recommendations.append("inspect_core_launchagents_before_new_work")

    CompanyA = snapshot["CompanyA"]
    if not CompanyA.get("recent_activity"):
        recommendations.append("refresh_redacted_state_before_acting")

    if not recommendations:
        recommendations.append("normal_operation")

    if score >= 6:
        level = "high"
    elif score >= 3:
        level = "watch"
    else:
        level = "ok"

    return {
        "level": level,
        "score": score,
        "reasons": reasons,
        "recommendations": recommendations,
        "defer": {
            "non_redacted_llm": "defer_non_redacted_llm_work" in recommendations,
            "non_redacted_browser": "do_not_start_more_generic_browser_work_until_lock_clears" in recommendations,
        },
        "enforcement": "observe_only",
    }


def build_snapshot() -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": iso(),
        "stack_dir": str(BASE),
        "mode": "observe_only",
        "policy": {
            "resident_model": LOCAL_MODEL,
            "model_lock": "dynamic local model; do not evict the active model for side work",
            "redacted_scope_gate": "Work should benefit CompanyA delivery, reliability, or safety.",
            "do_not_stop_active_agents_for_scope_gate": True,
        },
        "resources": {
            "load": load_state(),
            "memory": memory_state(),
            "disk": disk_state(),
            "ollama_queue": ollama_queue_state(),
            "browser_lock": browser_lock_state(),
        },
        "services": launchctl_services(),
        "CompanyA": redacted_activity_state(),
        "top_processes": top_processes(),
    }
    snapshot["pressure"] = compute_pressure(snapshot)
    return snapshot


def rotate_log_if_needed() -> None:
    try:
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > MAX_LOG_BYTES:
            rotated = LOG_PATH.with_suffix(LOG_PATH.suffix + ".1")
            if rotated.exists():
                rotated.unlink()
            LOG_PATH.rename(rotated)
    except OSError:
        return


def write_snapshot(snapshot: dict[str, Any]) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n")
    tmp.replace(STATE_PATH)
    rotate_log_if_needed()
    with LOG_PATH.open("a") as handle:
        handle.write(json.dumps(snapshot, sort_keys=True) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Write an observation-only infrastructure pressure snapshot.")
    parser.add_argument("--once", action="store_true", help="Run once and exit.")
    parser.add_argument("--print", action="store_true", help="Print the snapshot JSON.")
    args = parser.parse_args()

    snapshot = build_snapshot()
    write_snapshot(snapshot)
    if args.print:
        print(json.dumps(snapshot, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
