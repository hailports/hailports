"""
MCP Server Resilience Layer
=============================
Drop-in self-healing for all local MCP servers.

Features:
  - Auto-retry with smart back-off
  - App-not-running detection → auto-launch
  - Stale window / zombie cleanup (AppleScript servers)
  - Structured failure logging with pattern detection
  - Auto-rotating session token refresh (HTTP API servers)
  - Circuit breaker: stops hammering a dead service

Usage (AppleScript servers — Outlook, Mail, Zoom):
    from resilience import resilient_applescript, heal
    result = resilient_applescript(script, app_name="Microsoft Outlook")

Usage (HTTP API servers — SFDC, Monday):
    from resilience import resilient_http
    result = resilient_http(client, method, url, **kwargs)

Usage (decorator on any tool):
    @heal(app="Microsoft Outlook")
    def my_tool(...):
        ...
"""

import functools
import json
import logging
import os
import subprocess
import tempfile
import time
from collections import defaultdict
from datetime import datetime, timedelta

logger = logging.getLogger("mcp-resilience")

# ---------------------------------------------------------------------------
# Failure journal — structured log for pattern detection
# ---------------------------------------------------------------------------

_failure_log: list = []
_failure_counts: dict[str, int] = defaultdict(int)
_FAILURE_LOG_MAX = 200

# Circuit breaker state per app/service
_circuit: dict[str, dict] = {}
_CIRCUIT_THRESHOLD = 5        # consecutive failures before opening
_CIRCUIT_COOLDOWN = 60        # seconds before retrying after circuit opens


def _record_failure(source: str, error: str, context: str = ""):
    """Log a failure for pattern analysis."""
    entry = {
        "ts": datetime.now().isoformat(),
        "source": source,
        "error": error[:500],
        "context": context,
    }
    _failure_log.append(entry)
    if len(_failure_log) > _FAILURE_LOG_MAX:
        _failure_log.pop(0)
    _failure_counts[source] += 1
    logger.warning(f"[resilience] {source}: {error[:200]}")


def _record_success(source: str):
    """Reset circuit breaker on success."""
    if source in _circuit:
        _circuit[source] = {"failures": 0, "open_until": None}


def _check_circuit(source: str) -> str:
    """Return error string if circuit is open, None if OK to proceed."""
    state = _circuit.get(source, {"failures": 0, "open_until": None})
    if state.get("open_until") and datetime.now() < state["open_until"]:
        return f"Circuit breaker open for {source} — too many consecutive failures. Retrying after cooldown."
    return None


def _trip_circuit(source: str):
    state = _circuit.setdefault(source, {"failures": 0, "open_until": None})
    state["failures"] = state.get("failures", 0) + 1
    if state["failures"] >= _CIRCUIT_THRESHOLD:
        state["open_until"] = datetime.now() + timedelta(seconds=_CIRCUIT_COOLDOWN)
        logger.error(f"[resilience] Circuit OPEN for {source} — {_CIRCUIT_THRESHOLD} consecutive failures")


# ---------------------------------------------------------------------------
# AppleScript helpers
# ---------------------------------------------------------------------------

_KNOWN_APPS = {
    "Microsoft Outlook": "com.microsoft.Outlook",
    "Mail": "com.apple.mail",
    "Zoom": "us.zoom.xos",
    "zoom.us": "us.zoom.xos",
}


def _escape_applescript(s: str) -> str:
    """Escape a string for safe interpolation into AppleScript."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _is_app_running(app_name: str) -> bool:
    """Check if a macOS app is running."""
    try:
        result = subprocess.run(
            ["pgrep", "-xq", app_name],
            capture_output=True, timeout=5
        )
        if result.returncode == 0:
            return True
        # fallback: AppleScript check
        safe_name = _escape_applescript(app_name)
        check = subprocess.run(
            ["osascript", "-e", f'tell application "System Events" to (name of processes) contains "{safe_name}"'],
            capture_output=True, text=True, timeout=10
        )
        return "true" in check.stdout.lower()
    except Exception:
        return True  # assume running if we can't check


def _launch_app(app_name: str) -> bool:
    """Launch an app and wait for it to be ready."""
    logger.info(f"[resilience] Launching {app_name}...")
    try:
        subprocess.run(["open", "-a", app_name], timeout=10)
        # wait up to 15s for app to be responsive
        safe_name = _escape_applescript(app_name)
        for _ in range(15):
            time.sleep(1)
            check = subprocess.run(
                ["osascript", "-e", f'tell application "{safe_name}" to name'],
                capture_output=True, text=True, timeout=5
            )
            if check.returncode == 0:
                logger.info(f"[resilience] {app_name} is ready")
                return True
        return False
    except Exception as e:
        logger.error(f"[resilience] Failed to launch {app_name}: {e}")
        return False


def _close_orphan_windows(app_name: str):
    """Close any orphaned draft/compose windows in Outlook or Mail."""
    if "Outlook" in app_name:
        script = '''
tell application "Microsoft Outlook"
    set wins to windows
    set closed to 0
    repeat with w in wins
        try
            set wName to name of w
            -- Close compose windows: Re:/Fw:/Fwd: replies, untitled drafts, and new compose windows
            if wName starts with "Re:" or wName starts with "Fw:" or wName starts with "Fwd:" or wName is "" or wName is "Untitled" or wName is "(No Subject)" then
                close w saving no
                set closed to closed + 1
            end if
        end try
    end repeat
    return closed as text
end tell
'''
    elif "Mail" in app_name:
        script = '''
tell application "Mail"
    set wins to windows
    set closed to 0
    repeat with w in wins
        try
            if name of w contains "New Message" or name of w contains "Re:" then
                close w saving no
                set closed to closed + 1
            end if
        end try
    end repeat
    return closed as text
end tell
'''
    else:
        return

    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=15
        )
        closed = result.stdout.strip()
        if closed and closed != "0":
            logger.info(f"[resilience] Closed {closed} orphaned window(s) in {app_name}")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Error classifiers — detect what went wrong and how to fix it
# ---------------------------------------------------------------------------

_RECOVERABLE_PATTERNS = {
    # Specific error codes FIRST (before generic "execution error" catches them)
    "(-600)":               "app_not_running",     # procNotFound
    "(-609)":               "app_not_running",     # connectionInvalid
    "(-1728)":              "object_not_found",    # errAENoSuchObject
    "(-1712)":              "timeout",             # errAETimeout
    # Then specific phrases
    "not running":          "app_not_running",
    "application isn\u2019t running": "app_not_running",
    "application isn't running": "app_not_running",
    "connection is invalid": "app_not_running",
    "not allowed assistive": "accessibility_denied",
    "not allowed to send":  "accessibility_denied",
    "timed out":            "timeout",
    "timeout":              "timeout",
    # Generic AppleScript error last (broadest match)
    "execution error":      "applescript_error",
}


def _classify_error(error: str) -> str:
    """Classify an error into a recovery strategy."""
    lower = error.lower()
    for pattern, category in _RECOVERABLE_PATTERNS.items():
        if pattern.lower() in lower:
            return category
    return "unknown"


# ---------------------------------------------------------------------------
# Core: resilient AppleScript execution
# ---------------------------------------------------------------------------

def resilient_applescript(
    script: str,
    app_name: str = "",
    use_file: bool = False,
    timeout: int = 60,
    max_retries: int = 2,
) -> str:
    """
    Execute AppleScript with self-healing.

    Automatically handles:
      - App not running → launch it, retry
      - Orphaned windows → close them before retry
      - Timeouts → retry with extended timeout
      - Object not found → clean error message
    """
    source = app_name or "applescript"

    # Circuit breaker check
    cb = _check_circuit(source)
    if cb:
        return json.dumps({"error": cb})

    for attempt in range(max_retries + 1):
        raw = _exec_applescript(script, use_file=use_file, timeout=timeout)

        # Check for success
        try:
            parsed = json.loads(raw)
            if "error" not in parsed:
                _record_success(source)
                return raw
            error_msg = parsed["error"]
        except (json.JSONDecodeError, TypeError):
            # Non-JSON output = success (raw AppleScript output)
            _record_success(source)
            return raw

        # We have an error — classify and attempt recovery
        category = _classify_error(error_msg)
        _record_failure(source, error_msg, context=category)

        if attempt >= max_retries:
            _trip_circuit(source)
            break

        logger.info(f"[resilience] {source} attempt {attempt+1} failed ({category}), recovering...")

        if category == "app_not_running" and app_name:
            _launch_app(app_name)
            time.sleep(2)

        elif category == "timeout":
            timeout = int(timeout * 1.5)
            _close_orphan_windows(app_name)

        elif category == "applescript_error":
            _close_orphan_windows(app_name)
            time.sleep(1)

        elif category == "object_not_found":
            # No point retrying — the message/event doesn't exist
            break

        elif category == "accessibility_denied":
            # Can't self-heal this — needs user to grant permissions
            break

        else:
            time.sleep(1)

    return raw


def _exec_applescript(script: str, use_file: bool = False, timeout: int = 60) -> str:
    """Raw AppleScript execution (file or inline)."""
    try:
        if use_file:
            tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.scpt', delete=False)
            tmp.write(script)
            tmp.close()
            try:
                result = subprocess.run(
                    ["osascript", tmp.name],
                    capture_output=True, text=True, timeout=timeout
                )
            finally:
                os.unlink(tmp.name)
        else:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=timeout
            )

        if result.returncode != 0:
            return json.dumps({"error": result.stderr.strip()})
        return result.stdout.strip()

    except subprocess.TimeoutExpired:
        return json.dumps({"error": f"AppleScript timed out ({timeout}s)"})
    except Exception as e:
        return json.dumps({"error": "Operation failed"})


# ---------------------------------------------------------------------------
# Core: resilient HTTP execution (SFDC, Monday, etc.)
# ---------------------------------------------------------------------------

def resilient_http(
    client,  # httpx.Client or similar
    method: str,
    url: str,
    max_retries: int = 2,
    reauth_fn=None,
    **kwargs,
) -> "httpx.Response":
    """
    HTTP request with self-healing.

    Automatically handles:
      - 401 → call reauth_fn() and retry
      - 429 → back off and retry
      - 500/502/503 → retry with back-off
      - Connection errors → retry
    """
    source = url.split("/")[2] if "/" in url else "http"

    cb = _check_circuit(source)
    if cb:
        raise ConnectionError(cb)

    last_error = None
    for attempt in range(max_retries + 1):
        try:
            resp = getattr(client, method.lower())(url, **kwargs)

            if resp.status_code < 400:
                _record_success(source)
                return resp

            if resp.status_code == 401 and reauth_fn and attempt < max_retries:
                logger.info(f"[resilience] 401 from {source}, re-authenticating...")
                _record_failure(source, "401 Unauthorized", "reauth")
                reauth_fn()
                continue

            if resp.status_code == 429:
                wait = min(int(resp.headers.get("Retry-After", 2 ** attempt)), 30)
                logger.info(f"[resilience] 429 from {source}, waiting {wait}s...")
                _record_failure(source, "429 Rate Limited", "rate_limit")
                time.sleep(wait)
                continue

            if resp.status_code >= 500 and attempt < max_retries:
                wait = 2 ** attempt
                logger.info(f"[resilience] {resp.status_code} from {source}, retrying in {wait}s...")
                _record_failure(source, f"{resp.status_code}", "server_error")
                time.sleep(wait)
                continue

            # 4xx (not 401/429) — not retryable
            _record_success(source)  # reset circuit, it's responding
            return resp

        except Exception as e:
            last_error = e
            _record_failure(source, str(e), "connection_error")
            if attempt < max_retries:
                time.sleep(2 ** attempt)
                continue
            _trip_circuit(source)
            raise

    return resp


# ---------------------------------------------------------------------------
# Decorator: @heal — wrap any MCP tool for self-healing
# ---------------------------------------------------------------------------

def heal(app: str = "", retries: int = 2):
    """
    Decorator for MCP tool functions.
    Catches exceptions, cleans up orphaned windows, retries if possible.

    Usage:
        @mcp.tool()
        @heal(app="Microsoft Outlook")
        def my_tool(...):
            ...
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            source = app or fn.__name__

            cb = _check_circuit(source)
            if cb:
                return json.dumps({"error": cb})

            for attempt in range(retries + 1):
                try:
                    result = fn(*args, **kwargs)

                    # Check if the result itself contains an error
                    try:
                        parsed = json.loads(result) if isinstance(result, str) else result
                        if isinstance(parsed, dict) and "error" in parsed:
                            error_msg = parsed["error"]
                            if not isinstance(error_msg, str):
                                error_msg = json.dumps(error_msg) if error_msg else ""
                            category = _classify_error(error_msg)
                            _record_failure(source, error_msg, category)

                            if attempt < retries and category in ("app_not_running", "timeout", "applescript_error"):
                                logger.info(f"[resilience] @heal {fn.__name__} attempt {attempt+1} ({category}), recovering...")
                                if category == "app_not_running" and app:
                                    _launch_app(app)
                                elif category in ("timeout", "applescript_error"):
                                    _close_orphan_windows(app)
                                time.sleep(1)
                                continue

                            _trip_circuit(source)
                            return result
                    except (json.JSONDecodeError, TypeError):
                        pass

                    _record_success(source)
                    return result

                except Exception as e:
                    _record_failure(source, str(e), "exception")
                    if attempt < retries:
                        logger.info(f"[resilience] @heal {fn.__name__} exception, retrying: {e}")
                        if app:
                            _close_orphan_windows(app)
                        time.sleep(1)
                        continue
                    _trip_circuit(source)
                    return json.dumps({"error": f"Failed after {retries + 1} attempts: {str(e)}"})

            return result
        return wrapper
    return decorator


# ---------------------------------------------------------------------------
# Diagnostics tool — expose health info to Claude
# ---------------------------------------------------------------------------

def get_health_report() -> dict:
    """Return current health state for all services."""
    return {
        "failure_counts": dict(_failure_counts),
        "circuit_breakers": {
            k: {
                "failures": v["failures"],
                "open": bool(v.get("open_until") and datetime.now() < v["open_until"]),
                "open_until": v["open_until"].isoformat() if v.get("open_until") else None,
            }
            for k, v in _circuit.items()
        },
        "recent_errors": _failure_log[-10:],
    }
