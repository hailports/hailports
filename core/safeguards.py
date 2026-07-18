"""Shared safeguards — import into any agent for memory/rate protection.

Usage:
    from core.safeguards import check_memory, check_rate_limit, safe_to_run

    if not safe_to_run("my_agent"):
        log.warning("Safeguard blocked run")
        return
"""
import json
import logging
import os
import subprocess
import time
from datetime import datetime, date
from pathlib import Path

log = logging.getLogger("safeguards")

STATE_DIR = Path.home() / "claude-stack" / "data" / "safeguards"
STATE_DIR.mkdir(parents=True, exist_ok=True)


def get_memory_pressure():
    """Return memory pressure level: 'normal', 'warn', 'critical'."""
    try:
        result = subprocess.run(
            ["sysctl", "-n", "kern.memorystatus_vm_pressure_level"],
            capture_output=True, text=True, timeout=5,
        )
        level = int(result.stdout.strip())
        # 1=normal, 2=warn, 4=critical
        if level >= 4:
            return "critical"
        elif level >= 2:
            return "warn"
        return "normal"
    except Exception:
        pass

    # Fallback: check swap usage
    try:
        result = subprocess.run(
            ["sysctl", "-n", "vm.swapusage"],
            capture_output=True, text=True, timeout=5,
        )
        line = result.stdout.strip()
        # Parse "total = 3072.00M  used = 1547.38M"
        parts = line.split()
        for i, p in enumerate(parts):
            if p == "used":
                used = float(parts[i + 2].rstrip("M"))
                if used > 2500:
                    return "critical"
                elif used > 1500:
                    return "warn"
                return "normal"
    except Exception:
        pass

    return "normal"


def get_python_process_count():
    """Count running python3 processes."""
    try:
        result = subprocess.run(
            ["pgrep", "-c", "python3"],
            capture_output=True, text=True, timeout=5,
        )
        return int(result.stdout.strip())
    except Exception:
        return 0


def check_memory(agent_name="unknown"):
    """Return True if safe to run, False if memory pressure is too high."""
    pressure = get_memory_pressure()
    if pressure == "critical":
        log.warning("[%s] BLOCKED: memory pressure CRITICAL", agent_name)
        return False
    if pressure == "warn":
        procs = get_python_process_count()
        if procs > 8:
            log.warning("[%s] BLOCKED: memory WARN + %d python processes", agent_name, procs)
            return False
    return True


def check_rate_limit(agent_name, max_runs_per_day=10, max_runs_per_hour=3):
    """Rate limiting per agent. Returns True if safe to run."""
    state_file = STATE_DIR / f"{agent_name}_rate.json"
    now = datetime.now()
    today = date.today().isoformat()
    hour_key = now.strftime("%Y-%m-%d-%H")

    state = {}
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text())
        except Exception:
            state = {}

    daily = state.get("daily", {})
    hourly = state.get("hourly", {})

    day_count = daily.get(today, 0)
    hour_count = hourly.get(hour_key, 0)

    if day_count >= max_runs_per_day:
        log.info("[%s] Daily rate limit reached (%d/%d)", agent_name, day_count, max_runs_per_day)
        return False

    if hour_count >= max_runs_per_hour:
        log.info("[%s] Hourly rate limit reached (%d/%d)", agent_name, hour_count, max_runs_per_hour)
        return False

    # Record this run
    daily[today] = day_count + 1
    hourly[hour_key] = hour_count + 1

    # Clean old entries
    daily = {k: v for k, v in daily.items() if k >= (date.today().isoformat())}
    hourly = {k: v for k, v in hourly.items() if k >= now.strftime("%Y-%m-%d")}

    state["daily"] = daily
    state["hourly"] = hourly
    state_file.write_text(json.dumps(state, indent=2))

    return True


def safe_to_run(agent_name, max_daily=10, max_hourly=3):
    """Combined check: memory + rate limit. Returns True if safe."""
    if not check_memory(agent_name):
        return False
    if not check_rate_limit(agent_name, max_daily, max_hourly):
        return False
    return True
