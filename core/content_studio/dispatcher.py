"""Queue-driven dispatcher for the local content studio.

Routes each job to the right modality handler, gated by the presence sensor so
the grind always yields to Operator. This is intentionally light: a JSON-file queue
to start (swap for data/queue/tasks.db SQLite when the nocturnal worker lands —
see [[project_nocturnal_agent_team_buildspec]]).

A job is a dict:
    {"modality": "image|voice|video|pdf|text", ...modality-specific fields...}

Nothing here loads launchd, downloads models, or runs long renders.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from .handlers import HANDLERS

ROOT = Path(os.environ.get("CLAUDE_STACK_ROOT", Path.home() / "claude-stack"))
QUEUE_FILE = ROOT / "data" / "content_studio" / "queue.json"
RESULTS_FILE = ROOT / "data" / "content_studio" / "results.jsonl"


def _yield_to_alex() -> dict | None:
    """Return presence verdict if Operator is active (caller should pause), else None."""
    try:
        from core.presence_sensor import is_user_active

        verdict = is_user_active()
        return verdict if verdict.get("active") else None
    except Exception:
        # If we can't tell, be safe and assume present (yield).
        return {"active": True, "reasons": ["presence_sensor_unavailable"]}


def dispatch(job: dict, *, respect_presence: bool = True) -> dict:
    """Route ONE job to its handler. Honors presence unless overridden."""
    modality = str(job.get("modality") or "").strip().lower()
    handler = HANDLERS.get(modality)
    if not handler:
        return {"ok": False, "modality": modality, "note": "unknown modality"}

    if respect_presence:
        verdict = _yield_to_alex()
        if verdict:
            return {
                "ok": False,
                "modality": modality,
                "note": "yielded — Operator is active",
                "presence": verdict,
            }

    result = handler(job)
    _record(job, result)
    return result


def _record(job: dict, result: dict) -> None:
    try:
        RESULTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with RESULTS_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": datetime.now().isoformat(),
                "job": {k: job.get(k) for k in ("modality", "title", "model")},
                "result": result,
            }) + "\n")
    except Exception:
        pass


def _load_queue() -> list[dict]:
    if QUEUE_FILE.exists():
        try:
            return json.loads(QUEUE_FILE.read_text())
        except Exception:
            return []
    return []


def run_job(job: dict, *, respect_presence: bool = True) -> dict:
    """Convenience single-shot entrypoint (e.g. for tests / manual use)."""
    return dispatch(job, respect_presence=respect_presence)


def drain(limit: int = 0, *, respect_presence: bool = True) -> list[dict]:
    """Overnight loop body: pull jobs from the JSON queue and run them, pausing
    if Operator becomes active. Returns the results processed this pass."""
    jobs = _load_queue()
    if limit:
        jobs = jobs[:limit]
    out: list[dict] = []
    remaining: list[dict] = []
    for i, job in enumerate(jobs):
        if respect_presence and _yield_to_alex():
            remaining = jobs[i:]
            break
        out.append(dispatch(job, respect_presence=False))
    # persist whatever we didn't get to
    try:
        QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
        QUEUE_FILE.write_text(json.dumps(remaining, indent=2))
    except Exception:
        pass
    return out


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "caps":
        from .capabilities import capabilities

        print(json.dumps(capabilities(), indent=2))
    else:
        print(json.dumps(drain(limit=int(os.environ.get("STUDIO_LIMIT", "0"))), indent=2))
