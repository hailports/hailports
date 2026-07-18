#!/usr/bin/env python3
"""Tailscale Proxy — exposes key Mini services to the Tailscale network only.

Listens on 0.0.0.0:8400 and proxies requests to localhost services based on path:
  /llm/*     → localhost:11434 (Ollama)
  /mcp/*     → localhost:8050  (MCP unified server)
  /api/*     → localhost:8200  (LLM API)
  /health    → health check of all services

Only accepts connections from Tailscale IPs (100.x.x.x).
Adds bearer token auth for extra security.

The CompanyA MacBook hits 10.0.0.1:8400 and gets access to the Mini's brain.
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
import uvicorn
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tailscale-proxy")

app = FastAPI(title="Tailscale Proxy", docs_url=None, redoc_url=None)

PROXY_PORT = int(os.environ.get("TAILSCALE_PROXY_PORT", "8400"))
PROXY_TOKEN = os.environ.get("TAILSCALE_PROXY_TOKEN", "")

# Load token from file if not in env
TOKEN_FILE = Path.home() / ".tailscale-proxy-token"
if not PROXY_TOKEN and TOKEN_FILE.exists():
    PROXY_TOKEN = TOKEN_FILE.read_text().strip()
if not PROXY_TOKEN:
    # Generate one on first run
    import secrets
    PROXY_TOKEN = secrets.token_hex(32)
    TOKEN_FILE.write_text(PROXY_TOKEN)
    log.info("Generated proxy token: %s", TOKEN_FILE)

# Service routes
ROUTES = {
    "/llm": "http://127.0.0.1:11434",
    "/mcp": "http://127.0.0.1:8050",
    "/api": "http://127.0.0.1:8200",
}

# Tailscale IP range
TAILSCALE_PREFIX = "100."


def _is_tailscale(ip: str) -> bool:
    return ip.startswith(TAILSCALE_PREFIX) or ip == "127.0.0.1"


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    client_ip = request.client.host if request.client else "unknown"

    # Only allow Tailscale IPs
    if not _is_tailscale(client_ip):
        log.warning("Rejected non-Tailscale connection from %s", client_ip)
        return JSONResponse({"error": "Tailscale only"}, status_code=403)

    # Health endpoint — no auth needed
    if request.url.path == "/health":
        return await call_next(request)

    # Check bearer token
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer ") or auth[7:] != PROXY_TOKEN:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    return await call_next(request)


@app.get("/health")
async def health():
    """Health check — also shows status of all backend services."""
    checks = {}
    async with httpx.AsyncClient() as client:
        for name, url in [("ollama", "http://127.0.0.1:11434/api/tags"),
                          ("mcp", "http://127.0.0.1:8050/health"),
                          ("llm_api", "http://127.0.0.1:8200/"),
                          ("webui", "http://127.0.0.1:8100/")]:
            try:
                r = await client.get(url, timeout=3)
                checks[name] = {"ok": r.status_code == 200, "status": r.status_code}
            except Exception:
                checks[name] = {"ok": False, "status": "unreachable"}
    return {"status": "ok", "services": checks}


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy(path: str, request: Request):
    """Proxy requests to the appropriate backend service."""
    # Find matching route
    target_base = None
    strip_prefix = ""
    for prefix, backend in ROUTES.items():
        if path.startswith(prefix.lstrip("/")):
            target_base = backend
            strip_prefix = prefix.lstrip("/")
            break

    if not target_base:
        return JSONResponse({"error": "unknown route", "available": list(ROUTES.keys())}, status_code=404)

    # Build target URL
    remaining_path = path[len(strip_prefix):]
    if not remaining_path.startswith("/"):
        remaining_path = "/" + remaining_path
    target_url = target_base + remaining_path

    # Forward query params
    if request.url.query:
        target_url += "?" + request.url.query

    # Forward the request
    body = await request.body()
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in ("host", "authorization", "content-length")}

    async with httpx.AsyncClient(timeout=60) as client:
        try:
            resp = await client.request(
                request.method, target_url,
                headers=headers, content=body,
            )
            return JSONResponse(
                content=resp.json() if "json" in resp.headers.get("content-type", "") else {"raw": resp.text[:5000]},
                status_code=resp.status_code,
            )
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=502)


if __name__ == "__main__":
    log.info("Tailscale proxy starting on port %d", PROXY_PORT)
    log.info("Token file: %s", TOKEN_FILE)
    log.info("Routes: %s", ROUTES)
    uvicorn.run(app, host="0.0.0.0", port=PROXY_PORT, log_level="warning")
