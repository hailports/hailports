#!/usr/bin/env python3
"""crash_supervisor.py — capture rich crash CONTEXT at the moment of failure.

The keystone gap memory flags: today a restart throws the evidence away
("diagnostic.jsonl threw away per-service detail"), so the diagnoser has nothing
real to work with. This module is the black-box flight recorder: when a
watchdog/healer is about to restart a down/hung service, it snapshots an
INCIDENT first — exit code, the tail of stdout/stderr + any traceback, port/
health state, and resource context (RAM/CPU/open-FDs/disk-free, recent OOM) —
then appends it to an append-only ledger AND registers it with health_ledger so
the existing recurrence/band-aid machinery can see it.

Bounded by design:
  * Dedup/rate-limit — a crash-loop bumps a recurrence counter in state, it does
    NOT write 10k rows. One row per signature per window; the row carries the
    running count.
  * Never raises into a caller — every public entrypoint is best-effort. A
    failure to capture must never block a restart.

Public API:
  capture_incident(label, ...) -> dict   # call right before a restart
Run: python3 -m core.crash_supervisor --label com.claude-stack.foo --simulate
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import time
from pathlib import Path

from core import BASE_DIR
from core.health_ledger import record_heal

INCIDENTS = BASE_DIR / "data" / "hustle" / "crash_incidents.jsonl"
STATE = BASE_DIR / "data" / "runtime" / "crash_supervisor_state.json"
LAUNCHD_LOG_DIR = Path.home() / "Library" / "Logs" / "claude-stack"
STACK_LOG_DIR = BASE_DIR / "data" / "logs"

TAIL_LINES = 60            # last N log lines kept per stream
DEDUP_WINDOW_S = 600       # same signature inside this window => bump count, no new row
HEALTH_TIMEOUT_S = 3

INCIDENTS.parent.mkdir(parents=True, exist_ok=True)
STATE.parent.mkdir(parents=True, exist_ok=True)

_TRACEBACK_RE = re.compile(r"Traceback \(most recent call last\):")
_ERROR_LINE_RE = re.compile(
    r"(Error|Exception|Traceback|Fatal|panic|SIGSEGV|SIGABRT|killed)", re.IGNORECASE
)


def _load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {"signatures": {}}


def _save_state(state: dict) -> None:
    try:
        STATE.write_text(json.dumps(state, indent=2))
    except Exception:
        pass


def _short(label: str) -> str:
    return str(label or "").split(".")[-1]


def _default_log_paths(label: str) -> list[Path]:
    s = _short(label)
    cands = [
        LAUNCHD_LOG_DIR / f"{s}.err.log",
        LAUNCHD_LOG_DIR / f"{s}.out.log",
        STACK_LOG_DIR / f"{s}.err.log",
        STACK_LOG_DIR / f"{s}.log",
    ]
    return [p for p in cands if p.exists()]


def _tail(path: Path, n: int = TAIL_LINES) -> str:
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            # Read at most the last ~64KB — cheap, avoids loading huge logs.
            f.seek(max(0, size - 64 * 1024))
            data = f.read().decode("utf-8", "replace")
        return "\n".join(data.splitlines()[-n:])
    except Exception:
        return ""


def _extract_traceback(text: str) -> str:
    """Return the last traceback block in the text, if any."""
    if not text:
        return ""
    idx = text.rfind("Traceback (most recent call last):")
    if idx == -1:
        return ""
    return text[idx:][:2000]


def _last_error_line(text: str) -> str:
    """The most signal-rich line — for the dedup signature."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    for ln in reversed(lines):
        if _ERROR_LINE_RE.search(ln):
            return ln[:200]
    return lines[-1][:200] if lines else ""


def _launchctl_status(label: str) -> tuple[str | None, int | None]:
    """(pid_or_None, last_exit_code_or_None) from `launchctl list`."""
    try:
        out = subprocess.run(["launchctl", "list"], capture_output=True,
                             text=True, timeout=10).stdout
    except Exception:
        return None, None
    for ln in out.splitlines():
        parts = ln.split()
        if len(parts) >= 3 and parts[2] == label:
            pid = parts[0] if parts[0] != "-" else None
            try:
                code = int(parts[1])
            except ValueError:
                code = None
            return pid, code
    return None, None


def _resource_context() -> dict:
    ctx: dict = {}
    try:
        import psutil
        vm = psutil.virtual_memory()
        ctx["ram_percent"] = vm.percent
        ctx["ram_available_mb"] = round(vm.available / 1024 / 1024, 1)
        ctx["cpu_percent"] = psutil.cpu_percent(interval=0.0)
        ctx["load_avg"] = [round(x, 2) for x in os.getloadavg()]
    except Exception:
        try:
            ctx["load_avg"] = [round(x, 2) for x in os.getloadavg()]
        except Exception:
            pass
    try:
        du = shutil.disk_usage(str(BASE_DIR))
        ctx["disk_free_gb"] = round(du.free / 1024 / 1024 / 1024, 2)
        ctx["disk_percent_used"] = round((du.used / du.total) * 100, 1)
    except Exception:
        pass
    return ctx


def _proc_fd_count(pid: str | None) -> int | None:
    if not pid:
        return None
    try:
        import psutil
        return psutil.Process(int(pid)).num_fds()
    except Exception:
        return None


def _recent_oom(window_minutes: int = 5) -> bool | None:
    """Best-effort: was there a recent low-memory / jetsam event? Guarded by a
    short timeout — `log show` can be slow, so a miss returns None, not a hang."""
    try:
        r = subprocess.run(
            ["log", "show", "--style", "compact", "--last", f"{window_minutes}m",
             "--predicate",
             'eventMessage CONTAINS[c] "low memory" OR eventMessage CONTAINS[c] "jetsam"'],
            capture_output=True, text=True, timeout=HEALTH_TIMEOUT_S,
        )
        return bool(r.stdout.strip())
    except Exception:
        return None


def _port_health(port: int | None, health_url: str | None) -> dict:
    out: dict = {}
    if port:
        try:
            with socket.create_connection(("127.0.0.1", int(port)), timeout=2):
                out["port_open"] = True
        except Exception:
            out["port_open"] = False
    if health_url:
        try:
            import urllib.request
            with urllib.request.urlopen(health_url, timeout=HEALTH_TIMEOUT_S) as resp:
                out["health_status"] = resp.status
        except Exception as e:
            out["health_status"] = f"error:{type(e).__name__}"
    return out


def _signature(label: str, exit_code, err_line: str) -> str:
    return f"{label}|exit={exit_code}|{err_line}"[:200]


def capture_incident(label: str, *, log_paths=None, exit_code=None, port=None,
                     health_url=None, reason: str = "", extra_stderr: str = "",
                     note: str = "") -> dict:
    """Snapshot a crash/hang as an incident right before a restart.

    Best-effort and side-effect-safe: returns a summary dict; never raises.
    Dedup: the same signature inside DEDUP_WINDOW_S bumps a recurrence counter
    in state instead of appending a duplicate row.
    """
    try:
        ts = time.time()
        pid, live_code = _launchctl_status(label)
        if exit_code is None:
            exit_code = live_code

        paths = [Path(p) for p in (log_paths or [])] or _default_log_paths(label)
        streams: dict[str, str] = {}
        combined = []
        for p in paths:
            tail = _tail(p)
            if tail:
                streams[p.name] = tail
                combined.append(tail)
        if extra_stderr:
            streams["injected"] = extra_stderr[-8000:]
            combined.append(extra_stderr)
        combined_text = "\n".join(combined)

        traceback_block = _extract_traceback(combined_text)
        err_line = _last_error_line(traceback_block or combined_text)
        sig = _signature(label, exit_code, err_line)

        state = _load_state()
        sigs = state.setdefault("signatures", {})
        rec = sigs.get(sig)
        now = ts
        if rec and (now - float(rec.get("last_ts", 0))) < DEDUP_WINDOW_S:
            # Crash-loop within the window — bump the counter, no new row.
            rec["count"] = int(rec.get("count", 1)) + 1
            rec["last_ts"] = now
            _save_state(state)
            return {"signature": sig, "count": rec["count"], "written": False,
                    "recurrence": True}

        count = (int(rec.get("count", 0)) + 1) if rec else 1
        sigs[sig] = {"count": count, "first_ts": rec.get("first_ts", now) if rec else now,
                     "last_ts": now}

        incident = {
            "kind": "crash_incident",
            "ts": now,
            "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "service": label,
            "signature": sig,
            "exit_code": exit_code,
            "pid_at_capture": pid,
            "reason": reason or "restart",
            "note": note,
            "recurrence_count": count,
            "error_line": err_line,
            "traceback": traceback_block,
            "log_tails": streams,
            "ports": _port_health(port, health_url),
            "resources": _resource_context(),
            "open_fds": _proc_fd_count(pid),
            "recent_oom": _recent_oom(),
        }
        with open(INCIDENTS, "a") as f:
            f.write(json.dumps(incident, default=str) + "\n")
        _save_state(state)

        # Register with the shared health ledger so recurrence/band-aid detection
        # links these crashes to later heal attempts on the same service.
        try:
            record_heal(service=label, signature=sig, action="crash_incident_captured",
                        verified=None, detail=(err_line or reason)[:300])
        except Exception:
            pass

        return {"signature": sig, "count": count, "written": True,
                "recurrence": count > 1, "incident": incident}
    except Exception:
        return {"signature": "", "count": 0, "written": False, "error": True}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--exit-code", type=int, default=None)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--health-url", default=None)
    ap.add_argument("--stderr-file", default=None)
    ap.add_argument("--simulate", action="store_true",
                    help="inject a sample traceback instead of a real failure")
    args = ap.parse_args()
    extra = ""
    if args.stderr_file:
        extra = Path(args.stderr_file).read_text()
    elif args.simulate:
        extra = ("Traceback (most recent call last):\n"
                 '  File "agents/foo.py", line 42, in run\n'
                 "    do_thing()\n"
                 "RuntimeError: simulated crash for crash_supervisor self-test\n")
    res = capture_incident(args.label, exit_code=args.exit_code, port=args.port,
                           health_url=args.health_url, reason="cli", extra_stderr=extra)
    print(json.dumps(res, indent=2, default=str))
