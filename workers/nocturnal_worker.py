#!/usr/bin/env python3
"""Nocturnal worker — grind the task queue on local 30B during off-peak hours.

Runs 1:00 AM — 6:00 AM daily. Checks presence sensor: if Operator is active, SKIP.
Otherwise pulls ONE task from the queue, processes it via local Ollama, logs result.

Task queue format (JSONL at data/queue/nocturnal_tasks.jsonl):
  {"id": "uuid", "role": "content|seo|strategy|...", "priority": 1, "payload": {...}, "status": "pending"}

Output: data/queue/nocturnal_results.jsonl (one per completed task)
  {"task_id": "uuid", "role": "...", "status": "done", "result": "...", "elapsed_s": 42.1, "ts": "2026-05-30T..."}
"""

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

# Add claude-stack to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.presence_sensor import is_user_active
from core.llm_router import try_local_then_api

QUEUE_FILE = Path.home() / "claude-stack" / "data" / "queue" / "nocturnal_tasks.jsonl"
RESULTS_FILE = Path.home() / "claude-stack" / "data" / "queue" / "nocturnal_results.jsonl"
LOG_FILE = Path.home() / "claude-stack" / "logs.internal" / "nocturnal_worker.log"
WORK_DIR = Path.home() / "claude-stack" / "data" / "queue"

WORK_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)


def log(msg: str):
    """Write to log file + stdout."""
    ts = datetime.utcnow().isoformat() + "Z"
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def check_presence() -> bool:
    """Return True if Operator is active (we should SKIP)."""
    try:
        result = is_user_active()
        active = result.get("active", False)
        verdict = result.get("verdict", "")
        if active:
            log(f"PRESENCE ACTIVE: {verdict} — skipping. Reasons: {result.get('reasons', [])}")
            return True
        else:
            log(f"PRESENCE CLEAR: {verdict}")
            return False
    except Exception as e:
        log(f"WARNING: presence_sensor failed: {e}. Proceeding cautiously.")
        return False


def pull_task() -> dict | None:
    """Read the first pending task from the queue. Return None if empty."""
    if not QUEUE_FILE.exists():
        return None
    try:
        with open(QUEUE_FILE, "r") as f:
            for line in f:
                if not line.strip():
                    continue
                task = json.loads(line)
                if task.get("status") == "pending":
                    return task
    except Exception as e:
        log(f"ERROR reading queue: {e}")
    return None


def mark_task_done(task_id: str):
    """Update task status to 'done' in the queue file (simple line removal)."""
    if not QUEUE_FILE.exists():
        return
    try:
        lines = QUEUE_FILE.read_text().splitlines()
        new_lines = []
        for line in lines:
            if line.strip():
                task = json.loads(line)
                if task.get("id") == task_id:
                    task["status"] = "done"
                new_lines.append(json.dumps(task))
        QUEUE_FILE.write_text("\n".join(new_lines) + ("\n" if new_lines else ""))
    except Exception as e:
        log(f"ERROR marking task done: {e}")


async def process_task(task: dict) -> str:
    """Run the task through local Ollama → free pool → API chain. Return the result string."""
    role = task.get("role", "unknown")
    payload = task.get("payload", {})
    prompt = payload.get("prompt", "")

    if not prompt:
        return "[ERROR] no prompt in task payload"

    try:
        async def api_fn(): return None  # No paid API for nocturnal batch work
        response, source = await try_local_then_api(
            prompt=prompt,
            api_fn=api_fn,
            displaced_tier="sonnet",
            source=f"nocturnal_worker:{role}",
            local_model="quality",
            local_timeout=600.0,
            max_tokens=2000,
        )
        return response.strip() if response else "[EMPTY RESPONSE]"
    except Exception as e:
        return f"[ERROR] {type(e).__name__}: {str(e)[:200]}"


def write_result(task: dict, result: str, elapsed_s: float):
    """Append completed task result to results file."""
    try:
        output = {
            "task_id": task.get("id"),
            "role": task.get("role"),
            "status": "done",
            "result": result[:500],  # Truncate for log
            "elapsed_s": round(elapsed_s, 1),
            "ts": datetime.utcnow().isoformat() + "Z",
        }
        with open(RESULTS_FILE, "a") as f:
            f.write(json.dumps(output) + "\n")
        log(f"RESULT written: task_id={task.get('id')} role={task.get('role')} elapsed={elapsed_s:.1f}s")
    except Exception as e:
        log(f"ERROR writing result: {e}")


async def main_async():
    """Main loop: check presence, pull one task, process, log result."""
    log("nocturnal_worker started")

    if check_presence():
        log("EXITING: Operator is active. Local model reserved for his use.")
        return

    task = pull_task()
    if not task:
        log("Queue is empty. Nothing to do.")
        return

    task_id = task.get("id", "unknown")
    log(f"Processing task: id={task_id} role={task.get('role')} priority={task.get('priority')}")

    start = time.time()
    result = await process_task(task)
    elapsed = time.time() - start

    write_result(task, result, elapsed)
    mark_task_done(task_id)

    log(f"Task complete: id={task_id} elapsed={elapsed:.1f}s")


def main():
    """Sync wrapper for main_async."""
    import asyncio
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
