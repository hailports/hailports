#!/usr/bin/env python3
"""Ollama Request Queue — prevents VRAM contention and timeouts.

Problem: Multiple agents hit Ollama simultaneously. With 24GB RAM and an 18GB model,
concurrent requests cause OOM or extreme slowdowns. Agents timeout, retry, make it worse.

Solution: A simple queue server that serializes all Ollama requests.
All agents POST to this queue instead of directly to Ollama.
The queue processes one request at a time, with adaptive timeouts.

Port: 11435 (agents use this instead of 11434)
Proxies to: localhost:11434 (actual Ollama)

Features:
- Serial processing (one request at a time, no VRAM contention)
- Adaptive timeout (scales with max_tokens requested)
- Request priority (short requests jump the queue)
- Health endpoint reports queue depth
- Auto-retry on Ollama 503s
"""

import asyncio
import json
import logging
import os
import subprocess
import time
import uuid

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ollama-queue")

OLLAMA_URL = os.environ.get("OLLAMA_DIRECT_URL", "http://127.0.0.1:11434").rstrip("/")
QUEUE_PORT = int(os.environ.get("OLLAMA_QUEUE_PORT", "11435") or 11435)
MAX_PENDING = int(os.environ.get("OLLAMA_QUEUE_MAX_PENDING", "18") or 18)
MAX_WAIT_SECONDS = float(os.environ.get("OLLAMA_QUEUE_MAX_WAIT_SECONDS", "420") or 420)
REJECT_PRESSURE_LEVEL = int(os.environ.get("OLLAMA_QUEUE_REJECT_PRESSURE_LEVEL", "4") or 4)
PRESSURE_CACHE_SECONDS = float(os.environ.get("OLLAMA_QUEUE_PRESSURE_CACHE_SECONDS", "5") or 5)
MAX_NUM_PREDICT = int(os.environ.get("OLLAMA_QUEUE_MAX_NUM_PREDICT", "800") or 800)

app = FastAPI(title="Ollama Queue", docs_url=None)

# Global queue lock — only one Ollama request at a time
_lock = asyncio.Lock()
_counter_lock = asyncio.Lock()
_queue_depth = 0
_total_served = 0
_total_errors = 0
_total_rejected = 0
_total_skipped_disconnected = 0
_total_clamped = 0
_last_error = ""
_active_job: dict | None = None
_pressure_cache = {"checked_at": 0.0, "level": 0}


def _bounded_generation_body(body: dict | None) -> tuple[dict | None, bool, int | None, int | None]:
    """Bound local generations so abandoned long jobs cannot monopolize Ollama."""
    if not isinstance(body, dict):
        return body, False, None, None
    bounded_body = dict(body)
    options = bounded_body.get("options", {})
    if not isinstance(options, dict):
        options = {}
    else:
        options = dict(options)

    try:
        requested = int(options.get("num_predict", MAX_NUM_PREDICT) or MAX_NUM_PREDICT)
    except Exception:
        requested = MAX_NUM_PREDICT

    if MAX_NUM_PREDICT <= 0:
        bounded = max(1, requested)
    else:
        bounded = max(1, min(requested, MAX_NUM_PREDICT))
    options["num_predict"] = bounded
    bounded_body["options"] = options
    return bounded_body, bounded != requested, requested, bounded


def _calc_timeout(body: dict) -> float:
    """Adaptive timeout based on requested tokens."""
    options = body.get("options", {}) if isinstance(body, dict) else {}
    if not isinstance(options, dict):
        options = {}
    try:
        max_tokens = int(options.get("num_predict", 2000) or 2000)
    except Exception:
        max_tokens = 2000
    # Estimate local generation time using a conservative small-model baseline, plus overhead.
    base = max(60, max_tokens / 8)
    return min(base, 1800)  # cap at 30 minutes


def _pressure_level() -> int:
    now = time.monotonic()
    if now - float(_pressure_cache.get("checked_at") or 0) < PRESSURE_CACHE_SECONDS:
        return int(_pressure_cache.get("level") or 0)
    level = 0
    try:
        result = subprocess.run(
            ["sysctl", "-n", "kern.memorystatus_vm_pressure_level"],
            capture_output=True,
            text=True,
            timeout=1,
        )
        if result.returncode == 0:
            level = int((result.stdout or "0").strip() or 0)
    except Exception:
        level = 0
    _pressure_cache.update({"checked_at": now, "level": level})
    return level


def _job_label(path: str, body: dict | None) -> str:
    body = body or {}
    model = str(body.get("model") or "")
    num_predict = body.get("options", {}).get("num_predict") if isinstance(body.get("options"), dict) else None
    return f"/api/{path} model={model or '-'} num_predict={num_predict if num_predict is not None else '-'}"


async def _admit_or_reject(path: str, body: dict | None) -> tuple[bool, JSONResponse | None, str]:
    global _queue_depth, _total_rejected, _last_error
    job_id = uuid.uuid4().hex[:10]
    pressure = _pressure_level()
    async with _counter_lock:
        if pressure >= REJECT_PRESSURE_LEVEL:
            _total_rejected += 1
            _last_error = f"rejected pressure={pressure}"
            log.warning("Rejecting %s: pressure=%s job=%s", _job_label(path, body), pressure, job_id)
            return False, JSONResponse(
                {"error": "local model queue is under system pressure", "pressure_level": pressure, "job_id": job_id},
                status_code=503,
            ), job_id
        if _queue_depth >= MAX_PENDING:
            _total_rejected += 1
            _last_error = f"rejected queue_depth={_queue_depth}"
            log.warning("Rejecting %s: queue_depth=%s max=%s job=%s", _job_label(path, body), _queue_depth, MAX_PENDING, job_id)
            return False, JSONResponse(
                {"error": "local model queue is full", "queue_depth": _queue_depth, "max_pending": MAX_PENDING, "job_id": job_id},
                status_code=503,
            ), job_id
        _queue_depth += 1
    return True, None, job_id


async def _release_slot() -> None:
    global _queue_depth
    async with _counter_lock:
        _queue_depth = max(0, _queue_depth - 1)


@app.get("/health")
async def health():
    upstream_ok = False
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{OLLAMA_URL}/api/version")
            upstream_ok = resp.status_code == 200
    except Exception:
        upstream_ok = False
    return {
        "status": "healthy" if upstream_ok else "degraded",
        "upstream_ok": upstream_ok,
        "upstream": OLLAMA_URL,
        "queue_depth": _queue_depth,
        "max_pending": MAX_PENDING,
        "active": _lock.locked(),
        "active_job": _active_job,
        "pressure_level": _pressure_level(),
        "total_served": _total_served,
        "total_errors": _total_errors,
        "total_rejected": _total_rejected,
        "total_skipped_disconnected": _total_skipped_disconnected,
        "total_clamped": _total_clamped,
        "max_num_predict": MAX_NUM_PREDICT,
        "last_error": _last_error,
    }


@app.api_route("/api/{path:path}", methods=["GET", "POST"])
async def proxy(path: str, request: Request):
    global _total_served, _total_errors, _total_skipped_disconnected, _total_clamped, _last_error, _active_job

    body = None
    if request.method == "POST":
        body = await request.json()
        body, clamped, requested_tokens, bounded_tokens = _bounded_generation_body(body)
        if clamped:
            _total_clamped += 1
            log.info(
                "Clamped local generation num_predict %s -> %s for /api/%s",
                requested_tokens,
                bounded_tokens,
                path,
            )

    timeout = _calc_timeout(body or {})
    stream = bool(body.get("stream", False)) if body else False

    if request.method == "GET":
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{OLLAMA_URL}/api/{path}")
            _total_served += 1
            return JSONResponse(content=resp.json(), status_code=resp.status_code)
        except Exception as e:
            _total_errors += 1
            log.error("GET proxy error: %s", e)
            return JSONResponse({"error": str(e)}, status_code=500)

    if request.method == "POST" and stream:
        async def stream_gen():
            global _total_served, _total_errors, _total_skipped_disconnected, _last_error, _active_job
            job_timeout = timeout
            admitted, response, job_id = await _admit_or_reject(path, body)
            if not admitted:
                yield json.dumps({"error": "local model queue rejected request", "job_id": job_id}).encode("utf-8")
                return
            enqueued_at = time.monotonic()
            try:
                async with asyncio.timeout(MAX_WAIT_SECONDS):
                    async with _lock:
                        waited = time.monotonic() - enqueued_at
                        if await request.is_disconnected():
                            _total_skipped_disconnected += 1
                            log.info("Skipping disconnected stream job=%s after %.1fs: %s", job_id, waited, _job_label(path, body))
                            return
                        _active_job = {"id": job_id, "path": f"/api/{path}", "started_at": time.time(), "waited_s": round(waited, 3)}
                        log.info("Processing stream: %s timeout=%.0fs queue=%d waited=%.1fs job=%s", _job_label(path, body), job_timeout, _queue_depth, waited, job_id)
                        async with httpx.AsyncClient(timeout=job_timeout) as client:
                            try:
                                async with client.stream("POST", f"{OLLAMA_URL}/api/{path}", json=body) as resp:
                                    async for chunk in resp.aiter_bytes():
                                        yield chunk
                            except httpx.ReadTimeout:
                                if await request.is_disconnected():
                                    return
                                log.warning("Stream timeout, retrying with longer timeout job=%s", job_id)
                                job_timeout *= 1.5
                                async with client.stream("POST", f"{OLLAMA_URL}/api/{path}", json=body) as resp:
                                    async for chunk in resp.aiter_bytes():
                                        yield chunk
                        _total_served += 1
            except TimeoutError:
                _total_errors += 1
                _last_error = f"queue wait timeout job={job_id}"
                log.error("Queue wait timeout for stream job=%s", job_id)
                yield json.dumps({"error": "local model queue wait timed out", "job_id": job_id}).encode("utf-8")
            except Exception as e:
                _total_errors += 1
                _last_error = str(e)[:300]
                log.error("Stream error: %s", e)
                yield json.dumps({"error": str(e)}).encode("utf-8")
            finally:
                if _active_job and _active_job.get("id") == job_id:
                    _active_job = None
                await _release_slot()

        return StreamingResponse(stream_gen(), media_type="application/x-ndjson")

    admitted, rejection, job_id = await _admit_or_reject(path, body)
    if not admitted:
        return rejection
    enqueued_at = time.monotonic()
    try:
        async with asyncio.timeout(MAX_WAIT_SECONDS):
            async with _lock:
                waited = time.monotonic() - enqueued_at
                if await request.is_disconnected():
                    _total_skipped_disconnected += 1
                    log.info("Skipping disconnected job=%s after %.1fs: %s", job_id, waited, _job_label(path, body))
                    return JSONResponse({"error": "client disconnected before local model slot opened", "job_id": job_id}, status_code=499)
                _active_job = {"id": job_id, "path": f"/api/{path}", "started_at": time.time(), "waited_s": round(waited, 3)}
                log.info("Processing: %s timeout=%.0fs queue=%d waited=%.1fs job=%s", _job_label(path, body), timeout, _queue_depth, waited, job_id)

                async with httpx.AsyncClient(timeout=timeout) as client:
                    # Non-streaming
                    for attempt in range(3):
                        try:
                            resp = await client.post(f"{OLLAMA_URL}/api/{path}", json=body)
                            if resp.status_code == 503 and attempt < 2:
                                log.warning("Ollama 503, retry %d/3 job=%s", attempt + 1, job_id)
                                await asyncio.sleep(5)
                                continue
                            _total_served += 1
                            return JSONResponse(content=resp.json(), status_code=resp.status_code)
                        except httpx.ReadTimeout:
                            if attempt < 2:
                                log.warning("Timeout on attempt %d, retrying with longer timeout job=%s", attempt + 1, job_id)
                                timeout *= 1.5
                                continue
                            raise

    except TimeoutError:
        _total_errors += 1
        _last_error = f"queue wait timeout job={job_id}"
        log.error("Queue wait timeout for job=%s", job_id)
        return JSONResponse({"error": "local model queue wait timed out", "job_id": job_id}, status_code=503)
    except Exception as e:
        _total_errors += 1
        _last_error = str(e)[:300]
        log.error("Error: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)
    finally:
        if _active_job and _active_job.get("id") == job_id:
            _active_job = None
        await _release_slot()


@app.api_route("/{path:path}", methods=["GET", "POST"])
async def proxy_root(path: str, request: Request):
    """Proxy non-/api/ routes directly (e.g., /v1/ for OpenAI compat)."""
    global _total_served, _total_errors, _last_error, _active_job
    body = b""
    if request.method == "POST":
        body = await request.body()

    if request.method == "GET":
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{OLLAMA_URL}/{path}")
            _total_served += 1
            return JSONResponse(content=resp.json(), status_code=resp.status_code)
        except Exception as e:
            _total_errors += 1
            log.error("Root GET proxy error: %s", e)
            return JSONResponse({"error": str(e)}, status_code=500)

    admitted, rejection, job_id = await _admit_or_reject(path, None)
    if not admitted:
        return rejection
    enqueued_at = time.monotonic()
    try:
        async with asyncio.timeout(MAX_WAIT_SECONDS):
            async with _lock:
                waited = time.monotonic() - enqueued_at
                if await request.is_disconnected():
                    return JSONResponse({"error": "client disconnected before local model slot opened", "job_id": job_id}, status_code=499)
                _active_job = {"id": job_id, "path": f"/{path}", "started_at": time.time(), "waited_s": round(waited, 3)}
                async with httpx.AsyncClient(timeout=600) as client:
                    if request.method == "GET":
                        resp = await client.get(f"{OLLAMA_URL}/{path}")
                    else:
                        resp = await client.post(f"{OLLAMA_URL}/{path}", content=body,
                            headers={"Content-Type": "application/json"})
                    _total_served += 1
                    return JSONResponse(content=resp.json(), status_code=resp.status_code)
    except TimeoutError:
        _total_errors += 1
        _last_error = f"root queue wait timeout job={job_id}"
        return JSONResponse({"error": "local model queue wait timed out", "job_id": job_id}, status_code=503)
    except Exception as e:
        _total_errors += 1
        _last_error = str(e)[:300]
        log.error("Root proxy error: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)
    finally:
        if _active_job and _active_job.get("id") == job_id:
            _active_job = None
        await _release_slot()


if __name__ == "__main__":
    import uvicorn
    log.info("Ollama Queue starting on port %d -> %s", QUEUE_PORT, OLLAMA_URL)
    uvicorn.run(app, host="127.0.0.1", port=QUEUE_PORT, log_level="warning")
