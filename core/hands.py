#!/usr/bin/env python3
"""Hands — unified execution layer for the Mac Mini.

One interface to control everything: browsers, macOS apps, emails, APIs, files.
Every action is logged, reversible where possible, and draft-first for outbound comms.

Architecture:
  hands.browse(task)     → browser-use agent controls Chromium
  hands.app(app, action) → AppleScript controls any macOS app
  hands.click(element)   → Accessibility API clicks any UI element
  hands.email(to, subj, body) → SMTP draft queue
  hands.api(method, url, ...) → HTTP calls to any REST API
  hands.file(action, path)    → File system operations
  hands.shell(cmd)            → Run shell commands
  hands.schedule(action, at)  → Queue an action for later execution

All outbound actions (email, form submit, message send) go through the draft queue.
All actions are logged to data/hands_log.jsonl for audit trail.
"""

from core.constants import LOCAL_MODEL
import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.ui_automation_guard import get_ui_automation_guard

log = logging.getLogger("hands")

BASE = Path(__file__).resolve().parent.parent
LOG_FILE = BASE / "data" / "hands_log.jsonl"
DRAFT_QUEUE = BASE / "data" / "draft_queue.json"

# Outbound actions that require draft-queue approval
DRAFT_REQUIRED = {
    "email_send", "form_submit", "message_send", "post_publish",
    "proposal_submit", "comment_post", "gig_publish",
}

ACTION_CONFIRM_REQUIRED = DRAFT_REQUIRED | {
    "browser_click",
    "browser_keys",
    "ui_click",
}

SAFE_REVENUE_REVIEW_STATUS = "pending_operator_review"
SAFE_REVENUE_REVIEW_KIND = "SAFE_REVENUE_REVIEW"
SAFE_REVENUE_BLOCKED_ACTIONS = [
    "apply",
    "comment_post",
    "connect",
    "email_send",
    "follow",
    "form_submit",
    "message_send",
    "post_publish",
    "proposal_submit",
    "vote",
]

def _discover_browser_use_bin() -> str:
    candidates = [
        shutil.which("browser-use"),
        str(BASE / ".venv" / "bin" / "browser-use"),
        str(Path.home() / ".local" / "browser-use-venv" / "bin" / "browser-use"),
        "/opt/homebrew/bin/browser-use",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    return str(BASE / ".venv" / "bin" / "browser-use")


def _discover_fd_bin() -> str:
    candidates = [
        shutil.which("fd"),
        shutil.which("fdfind"),
        "/opt/homebrew/bin/fd",
        "/opt/homebrew/bin/fdfind",
        "/usr/local/bin/fd",
        "/usr/local/bin/fdfind",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    return ""


BROWSER_USE_BIN = _discover_browser_use_bin()
SHORTCUTS_BIN = shutil.which("shortcuts") or "/usr/bin/shortcuts"
OPEN_BIN = shutil.which("open") or "/usr/bin/open"
MDFIND_BIN = shutil.which("mdfind") or "/usr/bin/mdfind"
FD_BIN = _discover_fd_bin()
_CLOUD_BASE = Path.home() / "Library" / "CloudStorage"
_MDFIND_TIMEOUT_S = 5
_FD_TIMEOUT_S = 12

_BROWSER_RISKY_LABEL = re.compile(
    r"\b(send|submit|post|publish|apply|confirm|approve|buy|purchase|place\s+order|"
    r"delete|remove|merge|fire\s+off)\b",
    re.I,
)
_BROWSER_CONFIRM_KEYS = {"enter", "return", "cmd+enter", "command+enter", "ctrl+enter", "control+enter"}
_BROWSER_STATE_CACHE: dict[str, dict[str, Any]] = {}
_BROWSER_ELEMENT_RE = re.compile(r"^\*?\[(\d+)\]<([^>]+)>\s*(.*)$")


def _log_action(action_type: str, params: dict, result: Any, duration: float):
    """Append every action to the audit log."""
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "action": action_type,
        "params": {k: str(v)[:200] for k, v in params.items()},
        "result": str(result)[:500],
        "duration_s": round(duration, 2),
    }
    try:
        with open(LOG_FILE, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception:
        pass


def _queue_draft(action_type: str, params: dict, preview: str) -> str:
    """Queue an outbound action for human approval."""
    queue = []
    try:
        if DRAFT_QUEUE.exists():
            queue = json.loads(DRAFT_QUEUE.read_text())
    except Exception:
        queue = []

    draft_id = f"draft_{int(time.time() * 1000)}"
    queue.append({
        "id": draft_id,
        "action": action_type,
        "params": params,
        "preview": preview[:500],
        "queued_at": datetime.now(timezone.utc).isoformat(),
        "status": "pending_confirm",
    })
    DRAFT_QUEUE.write_text(json.dumps(queue, indent=2, default=str))
    return draft_id


def _escape_applescript(value: str) -> str:
    return str(value or "").replace("\\", "\\\\").replace('"', '\\"')


def _load_queue() -> list[dict[str, Any]]:
    try:
        if DRAFT_QUEUE.exists():
            payload = json.loads(DRAFT_QUEUE.read_text())
            if isinstance(payload, list):
                return payload
    except Exception:
        pass
    return []


def _save_queue(queue: list[dict[str, Any]]) -> None:
    DRAFT_QUEUE.parent.mkdir(parents=True, exist_ok=True)
    DRAFT_QUEUE.write_text(json.dumps(queue, indent=2, default=str))


def _queue_action(action_type: str, params: dict, preview: str) -> dict[str, Any]:
    draft_id = _queue_draft(action_type, params, preview)
    return {
        "status": "queued_for_confirmation",
        "action_id": draft_id,
        "action": action_type,
        "preview": preview[:500],
    }


def _stable_review_id(action_type: str, source_key: str) -> str:
    raw = f"{action_type}:{source_key}".encode("utf-8", errors="ignore")
    return f"hands_review_{hashlib.sha1(raw).hexdigest()[:16]}"


def _save_queue_full(queue: list[dict[str, Any]]) -> None:
    """Persist the review queue without trimming existing operator work."""
    DRAFT_QUEUE.parent.mkdir(parents=True, exist_ok=True)
    DRAFT_QUEUE.write_text(json.dumps(queue, indent=2, default=str))


def _load_json_payload(path: Path, default: Any = None) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text())
    except Exception:
        pass
    return [] if default is None else default


def _coerce_json_rows(payload: Any, preferred_key: str = "results") -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        rows = payload.get(preferred_key)
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
        for value in payload.values():
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
    return []


def _matches_terms(row: dict[str, Any], query: str) -> bool:
    terms = _match_terms(query)
    if not terms:
        return True
    haystack = json.dumps(row, default=str).lower()
    return all(term in haystack for term in terms)


def _queue_review_action(
    action_type: str,
    params: dict[str, Any],
    preview: str,
    *,
    source_item_id: str = "",
    priority: str = "normal",
    channel: str = "",
) -> dict[str, Any]:
    """Queue safe revenue review work that cannot be auto-submitted."""
    if action_type in ACTION_CONFIRM_REQUIRED:
        raise ValueError(f"safe revenue review cannot use executable action: {action_type}")

    safe_params = dict(params or {})
    source_key = str(
        source_item_id
        or safe_params.get("source_item_id")
        or safe_params.get("source_key")
        or safe_params.get("url")
        or safe_params.get("job_url")
        or safe_params.get("query")
        or preview
    ).strip()
    if not source_key:
        source_key = f"{action_type}:{int(time.time() * 1000)}"

    action_id = _stable_review_id(action_type, source_key)
    queue = _load_queue()
    for existing in queue:
        if str(existing.get("id") or "") == action_id:
            return {
                "status": "already_queued",
                "action_id": action_id,
                "action": action_type,
                "source_item_id": source_key,
            }
        if (
            existing.get("source_agent") == "hands"
            and existing.get("action") == action_type
            and str(existing.get("source_item_id") or "") == source_key
        ):
            return {
                "status": "already_queued",
                "action_id": str(existing.get("id") or action_id),
                "action": action_type,
                "source_item_id": source_key,
            }

    item = {
        "id": action_id,
        "action": action_type,
        "params": safe_params,
        "preview": str(preview or "")[:1200],
        "queued_at": datetime.now(timezone.utc).isoformat(),
        "status": SAFE_REVENUE_REVIEW_STATUS,
        "source_agent": "hands",
        "source_kind": SAFE_REVENUE_REVIEW_KIND,
        "source_item_id": source_key,
        "priority": priority,
        "channel": channel or safe_params.get("channel", ""),
        "approval_required": True,
        "requires_manual_review": True,
        "no_autosubmit": True,
        "blocked_actions": SAFE_REVENUE_BLOCKED_ACTIONS,
    }
    queue.append(item)
    _save_queue_full(queue)
    _log_action(action_type, {"source_item_id": source_key, "channel": item["channel"]}, {"queued": True}, 0)
    return {
        "status": "queued_for_operator_review",
        "action_id": action_id,
        "action": action_type,
        "source_item_id": source_key,
    }


def _queue_index(action_id: str, queue: list[dict[str, Any]]) -> int:
    for idx, row in enumerate(queue):
        if str(row.get("id") or "") == str(action_id or ""):
            return idx
    return -1


def _browser_installed() -> bool:
    return bool(BROWSER_USE_BIN and Path(BROWSER_USE_BIN).exists())


def _shortcuts_installed() -> bool:
    return bool(SHORTCUTS_BIN and Path(SHORTCUTS_BIN).exists())


def _normalize_session(session: str | None) -> str:
    raw = re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(session or "default").strip()).strip("-")
    return raw or "default"


def _run_command(args: list[str], *, timeout: int = 60, cwd: str | Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(cwd) if cwd else None,
    )


def _parse_browser_json(stdout: str) -> dict[str, Any]:
    lines = [line.strip() for line in str(stdout or "").splitlines() if line.strip()]
    for line in reversed(lines):
        try:
            payload = json.loads(line)
        except Exception:
            continue
        if isinstance(payload, dict):
            return payload
    raise RuntimeError("browser-use returned non-JSON output")


def _browser_cli(command: list[str], *, session: str = "default", timeout: int = 90) -> dict[str, Any]:
    if not _browser_installed():
        raise RuntimeError(f"browser-use not installed at {BROWSER_USE_BIN}")
    args = [BROWSER_USE_BIN, "--json", "--session", _normalize_session(session), *command]
    result = _run_command(args, timeout=timeout)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(detail or f"browser-use exited with code {result.returncode}")
    payload = _parse_browser_json(result.stdout)
    if payload.get("success") is False:
        raise RuntimeError(str(payload.get("error") or payload))
    return payload


def _parse_browser_state_text(raw_text: str) -> dict[str, Any]:
    title = ""
    url = ""
    viewport = ""
    elements: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    for line in str(raw_text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.lower().startswith("viewport:"):
            viewport = stripped.split(":", 1)[1].strip()
            continue
        if stripped.lower().startswith("url:"):
            url = stripped.split(":", 1)[1].strip()
            continue
        match = _BROWSER_ELEMENT_RE.match(stripped)
        if match:
            current = {
                "index": int(match.group(1)),
                "role": match.group(2).strip(),
                "label": match.group(3).strip(),
                "active": stripped.startswith("*["),
            }
            elements.append(current)
            continue
        if stripped.lower().startswith("page:") or stripped.lower().startswith("scroll:"):
            continue
        if current and line.startswith((" ", "\t")):
            extra = stripped
            if extra:
                label = str(current.get("label") or "")
                current["label"] = f"{label} {extra}".strip()
            continue
        if not title and not stripped.startswith(("*[", "[")):
            title = stripped

    return {
        "title": title,
        "url": url,
        "viewport": viewport,
        "elements": elements,
        "raw_text": raw_text,
    }


def _cache_browser_state(session: str, state: dict[str, Any]) -> None:
    _BROWSER_STATE_CACHE[_normalize_session(session)] = dict(state)


def _cached_browser_state(session: str) -> dict[str, Any]:
    return dict(_BROWSER_STATE_CACHE.get(_normalize_session(session), {}))


def _is_risky_browser_target(label: str) -> bool:
    return bool(_BROWSER_RISKY_LABEL.search(str(label or "")))


def _lookup_browser_label(session: str, index: int) -> str:
    state = _cached_browser_state(session)
    elements = state.get("elements") or []
    for item in elements:
        if int(item.get("index", -1)) == int(index):
            return str(item.get("label") or "")
    try:
        state = browser_state(session=session)
    except Exception:
        return ""
    for item in state.get("elements") or []:
        if int(item.get("index", -1)) == int(index):
            return str(item.get("label") or "")
    return ""


def _resolve_existing_path(path_str: str) -> Path:
    candidate = Path(path_str).expanduser()
    resolved = candidate.resolve() if candidate.exists() else candidate.absolute()
    if not resolved.exists():
        raise FileNotFoundError(f"path not found: {path_str}")
    return resolved


def _discover_onedrive_roots(account_hint: str = "") -> list[Path]:
    candidates: list[Path] = []
    if _CLOUD_BASE.exists():
        for entry in sorted(_CLOUD_BASE.iterdir(), key=lambda p: p.name.lower()):
            if entry.is_dir() and entry.name.startswith("OneDrive"):
                candidates.append(entry)
    for entry in sorted(Path.home().glob("OneDrive*"), key=lambda p: p.name.lower()):
        if entry.is_dir():
            candidates.append(entry)

    seen: set[str] = set()
    roots: list[Path] = []
    for entry in candidates:
        key = str(entry.resolve())
        if key not in seen:
            roots.append(entry.resolve())
            seen.add(key)

    hint = str(account_hint or "").strip().lower()
    if hint:
        roots.sort(key=lambda p: (hint not in p.name.lower(), len(str(p)), p.name.lower()))
    return roots


def _coerce_search_roots(roots: list[str] | None, account_hint: str = "") -> list[Path]:
    if roots:
        return [_resolve_existing_path(root) for root in roots]
    discovered = _discover_onedrive_roots(account_hint=account_hint)
    if discovered:
        return discovered
    if _CLOUD_BASE.exists():
        return [_CLOUD_BASE.resolve()]
    return [Path.home().resolve()]


def _match_terms(query: str) -> list[str]:
    return [token for token in re.split(r"\s+", str(query or "").strip().lower()) if token]


def _path_match_score(path: Path, query: str) -> tuple[int, int, int, int]:
    query_lower = str(query or "").strip().lower()
    terms = _match_terms(query_lower)
    name = path.name.lower()
    full = str(path).lower()
    exact = 1 if query_lower and (query_lower == name or query_lower == path.stem.lower()) else 0
    token_hits = sum(1 for token in terms if token in name)
    full_hits = sum(1 for token in terms if token in full)
    return (exact, token_hits, full_hits, -len(full))


def _serialize_local_path(path: Path, *, query: str = "") -> dict[str, Any]:
    stat = path.stat()
    return {
        "path": str(path),
        "name": path.name,
        "parent": str(path.parent),
        "kind": "folder" if path.is_dir() else (path.suffix.lower().lstrip(".") or "file"),
        "is_dir": path.is_dir(),
        "size_kb": round(stat.st_size / 1024, 1) if path.is_file() else None,
        "modified_ts": stat.st_mtime,
        "match_score": list(_path_match_score(path, query)) if query else [],
    }


def _mdfind_search(paths: list[Path], query: str, limit: int) -> list[Path]:
    results: list[Path] = []
    seen: set[str] = set()
    patterns = [str(query).strip()]
    for token in _match_terms(query):
        if token not in patterns:
            patterns.append(token)

    for root in paths:
        for pattern in patterns:
            if not pattern:
                continue
            cmd = [MDFIND_BIN, "-onlyin", str(root), "-name", pattern]
            try:
                result = _run_command(cmd, timeout=_MDFIND_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                log.warning("mdfind timed out for %s in %s", pattern, root)
                break
            if result.returncode != 0:
                continue
            for line in (result.stdout or "").splitlines():
                line = line.strip()
                if not line:
                    continue
                candidate = Path(line)
                try:
                    resolved = candidate.resolve()
                except Exception:
                    continue
                if not resolved.exists():
                    continue
                key = str(resolved)
                if key in seen:
                    continue
                seen.add(key)
                results.append(resolved)
                if len(results) >= limit:
                    return results
    return results


def _walk_search(paths: list[Path], query: str, limit: int) -> list[Path]:
    results: list[Path] = []
    seen: set[str] = set()
    terms = _match_terms(query)
    for root in paths:
        for current_root, dirnames, filenames in os.walk(root):
            dirnames[:] = [name for name in dirnames if not name.startswith(".")]
            names = list(dirnames) + list(filenames)
            for name in names:
                if name.startswith("."):
                    continue
                candidate = Path(current_root) / name
                haystack = str(candidate).lower()
                if terms and not all(term in haystack for term in terms):
                    continue
                key = str(candidate.resolve())
                if key in seen:
                    continue
                seen.add(key)
                results.append(candidate.resolve())
                if len(results) >= limit:
                    return results
    return results


def _fd_search(paths: list[Path], query: str, limit: int) -> list[Path]:
    if not FD_BIN:
        return []
    terms = _match_terms(query)
    pattern = ".*".join(re.escape(term) for term in terms) if terms else re.escape(str(query).strip())
    results: list[Path] = []
    seen: set[str] = set()
    for root in paths:
        cmd = [
            FD_BIN,
            "--hidden",
            "--follow",
            "--full-path",
            "--max-results",
            str(limit),
            pattern,
            str(root),
        ]
        try:
            result = _run_command(cmd, timeout=_FD_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            log.warning("fd timed out for %s in %s", query, root)
            continue
        if result.returncode not in {0, 1}:
            continue
        for line in (result.stdout or "").splitlines():
            line = line.strip()
            if not line:
                continue
            candidate = Path(line)
            try:
                resolved = candidate.resolve()
            except Exception:
                continue
            if not resolved.exists():
                continue
            key = str(resolved)
            if key in seen:
                continue
            seen.add(key)
            results.append(resolved)
            if len(results) >= limit:
                return results
    return results


def _filter_and_rank_paths(
    results: list[Path],
    *,
    query: str,
    include_files: bool,
    include_dirs: bool,
    limit: int,
) -> list[Path]:
    filtered: list[Path] = []
    for path in results:
        if path.is_dir() and not include_dirs:
            continue
        if path.is_file() and not include_files:
            continue
        filtered.append(path)
    filtered.sort(key=lambda p: _path_match_score(p, query), reverse=True)
    return filtered[:limit]


# ── Browser Hands ────────────────────────────────────────────────────────────

async def browse(task: str, url: str = None, execute: bool = False) -> dict:
    """Control a real Chrome browser to perform any web task.

    Args:
        task: Natural language description of what to do
        url: Optional starting URL
        execute: If False (default), stops before final submit/post
    """
    start = time.time()
    try:
        from browser_use import Agent, ChatAnthropic
        from dotenv import load_dotenv
        load_dotenv(BASE / ".env")

        llm = ChatAnthropic(
            model="claude-sonnet-4-20250514",
            api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        )

        if not execute:
            task += "\n\nIMPORTANT: Prepare everything but DO NOT click the final submit/publish/post/send button."

        agent = Agent(task=task, llm=llm, use_vision=False)
        result = await agent.run()

        extracted = []
        for item in result.all_results:
            if item.extracted_content and len(item.extracted_content) > 20:
                extracted.append(item.extracted_content)

        output = {
            "success": True,
            "steps": len(result.all_results),
            "extracted": extracted,
            "executed": execute,
        }
        _log_action("browse", {"task": task[:200], "url": url or ""}, output, time.time() - start)
        return output

    except Exception as e:
        output = {"success": False, "error": str(e)}
        _log_action("browse", {"task": task[:200]}, output, time.time() - start)
        return output


# ── macOS App Hands ──────────────────────────────────────────────────────────

def app(script: str, app_name: str = None) -> dict:
    """Run AppleScript to control any macOS application.

    Args:
        script: AppleScript code to execute
        app_name: Optional app name for logging
    """
    start = time.time()
    try:
        r = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=30,
        )
        output = {
            "success": r.returncode == 0,
            "stdout": r.stdout.strip()[:500],
            "stderr": r.stderr.strip()[:200] if r.returncode != 0 else "",
        }
        _log_action("app", {"app": app_name or "osascript", "script": script[:200]}, output, time.time() - start)
        return output
    except Exception as e:
        output = {"success": False, "error": str(e)}
        _log_action("app", {"app": app_name or "osascript"}, output, time.time() - start)
        return output


def click(app_name: str, element_description: str) -> dict:
    """Click a UI element using Accessibility API via AppleScript.

    Args:
        app_name: Name of the application (e.g., "Safari", "Mail")
        element_description: Description of what to click
    """
    script = f'''
    tell application "System Events"
        tell process "{app_name}"
            set frontmost to true
            -- Click described element
            click button "{element_description}" of window 1
        end tell
    end tell'''
    return app(script, app_name)


def dismiss_dialog(app_name: str = None) -> dict:
    """Dismiss any modal dialog in the frontmost app (or specified app)."""
    if app_name:
        script = f'''
        tell application "System Events"
            tell process "{app_name}"
                if exists sheet 1 of window 1 then
                    click button "Cancel" of sheet 1 of window 1
                    return "dismissed sheet"
                else if exists button "Cancel" of window 1 then
                    click button "Cancel" of window 1
                    return "dismissed dialog"
                else if exists button "OK" of window 1 then
                    click button "OK" of window 1
                    return "dismissed OK"
                end if
            end tell
        end tell'''
    else:
        script = '''
        tell application "System Events"
            set fp to first process whose frontmost is true
            tell fp
                if exists button "Cancel" of window 1 then
                    click button "Cancel" of window 1
                    return "dismissed"
                else if exists button "OK" of window 1 then
                    click button "OK" of window 1
                    return "dismissed OK"
                end if
            end tell
        end tell'''
    return app(script, app_name or "System Events")


# ── Email Hands ──────────────────────────────────────────────────────────────

def email(to: str, subject: str, body_html: str, attachments: list[str] = None) -> dict:
    """Queue an email for sending. Always goes to draft queue first."""
    preview = f"To: {to}\nSubject: {subject}\n\n{body_html[:200]}"
    draft_id = _queue_draft("email_send", {
        "to": to,
        "subject": subject,
        "body_html": body_html,
        "attachments": attachments or [],
    }, preview)
    _log_action("email_draft", {"to": to, "subject": subject}, {"draft_id": draft_id}, 0)
    return {"draft_id": draft_id, "status": "queued", "to": to, "subject": subject}


def email_send_now(to: str, subject: str, body_html: str, attachments: list[str] = None) -> dict:
    """Actually send an email immediately. Only called after draft approval."""
    from products.sf_health_scanner.engagement_flow import _send_email
    start = time.time()
    ok = _send_email(to, subject, body_html, attachments)
    result = {"sent": ok, "to": to, "subject": subject}
    _log_action("email_send", {"to": to, "subject": subject}, result, time.time() - start)
    return result


# ── API Hands ────────────────────────────────────────────────────────────────

async def api(method: str, url: str, headers: dict = None, body: dict = None, timeout: int = 30) -> dict:
    """Make an HTTP API call to any endpoint."""
    import httpx
    start = time.time()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.request(method, url, headers=headers, json=body)
            output = {
                "status": r.status_code,
                "body": r.text[:1000],
                "headers": dict(r.headers),
            }
            _log_action("api", {"method": method, "url": url}, {"status": r.status_code}, time.time() - start)
            return output
    except Exception as e:
        output = {"error": str(e)}
        _log_action("api", {"method": method, "url": url}, output, time.time() - start)
        return output


# ── File Hands ───────────────────────────────────────────────────────────────

def file_read(path: str) -> str:
    """Read a file and return its contents."""
    return Path(path).read_text(errors="replace")


def file_write(path: str, content: str) -> dict:
    """Write content to a file."""
    start = time.time()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(content)
    _log_action("file_write", {"path": path, "size": len(content)}, {"ok": True}, time.time() - start)
    return {"ok": True, "path": path, "size": len(content)}


# ── Shell Hands ──────────────────────────────────────────────────────────────

def shell(cmd: str, timeout: int = 30) -> dict:
    """Run a shell command and return output."""
    start = time.time()
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        output = {
            "returncode": r.returncode,
            "stdout": r.stdout.strip()[:2000],
            "stderr": r.stderr.strip()[:500],
        }
        _log_action("shell", {"cmd": cmd[:200]}, {"rc": r.returncode}, time.time() - start)
        return output
    except Exception as e:
        output = {"error": str(e)}
        _log_action("shell", {"cmd": cmd[:200]}, output, time.time() - start)
        return output


# ── LLM Hands (local, $0) ───────────────────────────────────────────────────

async def think(prompt: str, max_tokens: int = 500) -> str:
    """Use local Ollama to reason, compose, or analyze. $0 cost."""
    import httpx
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                "http://127.0.0.1:11434/api/generate",
                json={"model": LOCAL_MODEL, "prompt": prompt, "stream": False,
                      "options": {"num_predict": max_tokens, "temperature": 0.3}},
                timeout=60,
            )
            if r.status_code == 200:
                return r.json().get("response", "").strip()
    except Exception as e:
        log.warning("Ollama think failed: %s", e)
    return ""


# ── Schedule Hands ───────────────────────────────────────────────────────────

def schedule(action_type: str, params: dict, delay_hours: float) -> dict:
    """Schedule an action to execute after a delay."""
    from products.sf_health_scanner.engagement_flow import schedule_email
    scheduled_at = datetime.now(timezone.utc).isoformat()
    _log_action("schedule", {"action": action_type, "delay_h": delay_hours}, {"scheduled": scheduled_at}, 0)
    return {"scheduled": True, "action": action_type, "delay_hours": delay_hours}


# ── Composite Actions ────────────────────────────────────────────────────────

async def research_and_draft(topic: str, recipient: str, context: str = "") -> dict:
    """Research a topic via web search, then draft an email using local LLM."""
    # Step 1: Research
    search_result = await browse(
        f"Search Google for '{topic}' and extract the top 5 results with titles and summaries. Do not click any links.",
    )

    # Step 2: Compose with local LLM
    research_context = "\n".join(search_result.get("extracted", []))[:1000]
    draft = await think(
        f"Write a professional email to {recipient} about: {topic}\n\n"
        f"Research context:\n{research_context}\n\n"
        f"Additional context: {context}\n\n"
        f"Rules: Professional consulting tone. Never mention AI or automation. "
        f"Position as 'our team' at docsapp. Keep under 200 words. Sign as 'The docsapp Team'."
    )

    # Step 3: Queue as draft
    result = email(recipient, f"Re: {topic}", f"<p>{draft}</p>")
    result["research"] = research_context[:300]
    result["draft_body"] = draft
    return result


async def upwork_proposal(job_url: str, custom_note: str = "") -> dict:
    """Research an Upwork job and draft a tailored proposal."""
    # Step 1: Read the job posting
    job_info = await browse(
        f"Go to {job_url} and extract: job title, description, budget, required skills, and client details. Do not apply.",
        url=job_url,
    )

    # Step 2: Compose proposal with local LLM
    job_context = "\n".join(job_info.get("extracted", []))[:1500]
    proposal = await think(
        f"Write an Upwork proposal for this job:\n\n{job_context}\n\n"
        f"Additional note: {custom_note}\n\n"
        f"Rules:\n"
        f"- Open with a specific observation about THEIR project (not a generic intro)\n"
        f"- Reference relevant experience (10+ years Salesforce, 1000+ users supported)\n"
        f"- Mention a free diagnostic assessment as a trust-builder\n"
        f"- Never mention AI, automation, or tools\n"
        f"- Professional but warm tone\n"
        f"- Under 200 words\n"
        f"- End with a clear next step"
    )

    # Step 3: Queue as safe review only. Submission stays manual.
    queued = _queue_review_action("hands_upwork_proposal_review", {
        "channel": "marketplace_proposals",
        "platform": "upwork",
        "job_url": job_url,
        "proposal": proposal,
        "job_context": job_context[:500],
        "manual_next_step": "Review proposal quality and submit manually in Upwork if approved.",
    }, f"Upwork proposal review only:\n\n{proposal}", source_item_id=job_url, priority="high", channel="marketplace_proposals")

    return {
        "draft_id": queued["action_id"],
        "job_url": job_url,
        "proposal": proposal,
        "job_context": job_context[:300],
        "status": queued["status"],
    }


def upwork_search(query: str = "Salesforce admin help", limit: int = 10, source: str = "hands") -> dict[str, Any]:
    """Queue safe Upwork opportunity review work without applying or submitting."""
    max_items = max(1, min(int(limit or 10), 50))
    rows = _coerce_json_rows(_load_json_payload(BASE / "products" / "hunter" / "daily_results.json"), "results")
    candidates: list[dict[str, Any]] = []
    for row in rows:
        text = json.dumps(row, default=str).lower()
        if "upwork" not in text and "upwork.com" not in text:
            continue
        if _matches_terms(row, query):
            candidates.append(row)

    queued: list[dict[str, Any]] = []
    if candidates:
        for idx, row in enumerate(candidates[:max_items], start=1):
            url = str(row.get("url") or row.get("job_url") or row.get("link") or "").strip()
            title = str(row.get("title") or row.get("job_title") or row.get("name") or f"Upwork result {idx}").strip()
            source_key = url or f"upwork:{query}:{title}"
            preview = (
                "Upwork opportunity review only\n"
                f"Title: {title}\n"
                f"URL: {url or 'unknown'}\n"
                "Rules: research fit, draft notes, do not apply, submit, message, or click final actions."
            )
            queued.append(
                _queue_review_action(
                    "hands_upwork_opportunity_review",
                    {
                        "channel": "marketplace_proposals",
                        "platform": "upwork",
                        "query": query,
                        "title": title,
                        "url": url,
                        "source": source,
                        "raw_result": row,
                        "manual_next_step": "Open the job manually, verify fit, and approve any proposal outside automation.",
                    },
                    preview,
                    source_item_id=source_key,
                    priority="high",
                    channel="marketplace_proposals",
                )
            )
    else:
        source_key = f"upwork_search:{query}:{max_items}"
        preview = (
            "Manual Upwork search review\n"
            f"Query: {query}\n"
            f"Target reviews: {max_items}\n"
            "Rules: discover opportunities, capture fit notes, do not apply, submit, message, or click final actions."
        )
        queued.append(
            _queue_review_action(
                "hands_upwork_search_review",
                {
                    "channel": "marketplace_proposals",
                    "platform": "upwork",
                    "query": query,
                    "limit": max_items,
                    "source": source,
                    "manual_next_step": "Run the marketplace search manually and add promising jobs for operator review.",
                },
                preview,
                source_item_id=source_key,
                priority="high",
                channel="marketplace_proposals",
            )
        )

    return {
        "success": True,
        "workflow": "upwork_search",
        "query": query,
        "candidate_count": len(candidates),
        "queued_count": sum(1 for item in queued if item.get("status") == "queued_for_operator_review"),
        "already_queued_count": sum(1 for item in queued if item.get("status") == "already_queued"),
        "items": queued,
    }


def reddit_engage(topic: str = "Salesforce", limit: int = 5) -> dict[str, Any]:
    """Queue Reddit engagement reviews without posting comments."""
    max_items = max(1, min(int(limit or 5), 25))
    rows = _coerce_json_rows(_load_json_payload(BASE / "products" / "outreach" / "data" / "reddit_comment_drafts.json"))
    candidates = [row for row in rows if _matches_terms(row, topic)]

    queued: list[dict[str, Any]] = []
    if candidates:
        for idx, row in enumerate(candidates[:max_items], start=1):
            url = str(row.get("url") or "").strip()
            title = str(row.get("post_title") or row.get("title") or f"Reddit discussion {idx}").strip()
            draft_comment = str(row.get("draft_comment") or "").strip()
            source_key = url or f"reddit:{topic}:{title}"
            preview = (
                "Reddit engagement review only\n"
                f"Post: {title}\n"
                f"URL: {url or 'unknown'}\n"
                f"Draft: {draft_comment[:500]}\n"
                "Rules: review for fit and tone, do not post, vote, follow, DM, or automate engagement."
            )
            queued.append(
                _queue_review_action(
                    "hands_reddit_engagement_review",
                    {
                        "channel": "social_reddit",
                        "platform": "reddit",
                        "topic": topic,
                        "post_title": title,
                        "url": url,
                        "draft_comment": draft_comment,
                        "manual_next_step": "Review the draft and post manually only if it is genuinely helpful.",
                    },
                    preview,
                    source_item_id=source_key,
                    priority="normal",
                    channel="social_reddit",
                )
            )
    else:
        source_key = f"reddit_engage:{topic}:{max_items}"
        preview = (
            "Manual Reddit engagement research\n"
            f"Topic: {topic}\n"
            f"Target reviews: {max_items}\n"
            "Rules: find relevant discussions and draft helpful comments; do not post or automate engagement."
        )
        queued.append(
            _queue_review_action(
                "hands_reddit_research_review",
                {
                    "channel": "social_reddit",
                    "platform": "reddit",
                    "topic": topic,
                    "limit": max_items,
                    "manual_next_step": "Find relevant discussions manually and add comment drafts for operator review.",
                },
                preview,
                source_item_id=source_key,
                priority="normal",
                channel="social_reddit",
            )
        )

    return {
        "success": True,
        "workflow": "reddit_engage",
        "topic": topic,
        "candidate_count": len(candidates),
        "queued_count": sum(1 for item in queued if item.get("status") == "queued_for_operator_review"),
        "already_queued_count": sum(1 for item in queued if item.get("status") == "already_queued"),
        "items": queued,
    }


def monitor_fiverr(limit: int = 10) -> dict[str, Any]:
    """Queue Fiverr listing and inbox monitor reviews without logging in or messaging."""
    max_items = max(1, min(int(limit or 10), 25))
    move_payload = _load_json_payload(BASE / "data" / "hustle" / "next_money_move.json", {})
    channels = move_payload.get("channels") if isinstance(move_payload, dict) else []
    fiverr_state = {}
    if isinstance(channels, list):
        for channel in channels:
            if isinstance(channel, dict) and channel.get("channel") == "fiverr":
                fiverr_state = channel
                break

    gigs_doc = BASE / "products" / "freelance" / "fiverr_gigs.md"
    gig_titles: list[str] = []
    try:
        text = gigs_doc.read_text(errors="replace")
        for line in text.splitlines():
            match = re.match(r"^##\s+Gig\s+\d+:\s+(.+)$", line.strip())
            if match:
                gig_titles.append(match.group(1).strip())
    except Exception:
        pass

    tasks = gig_titles[:max_items] or ["Fiverr listing and inbox monitor"]
    queued: list[dict[str, Any]] = []
    for idx, title in enumerate(tasks, start=1):
        source_key = f"fiverr_monitor:{title}"
        preview = (
            "Fiverr monitor review only\n"
            f"Listing: {title}\n"
            f"Known live gigs: {fiverr_state.get('gigs_live', 'unknown')}\n"
            f"Seller status: {fiverr_state.get('seller_status', 'unknown')}\n"
            "Rules: inspect listing/inbox health manually; do not log in, message, submit, publish, or alter gigs by automation."
        )
        queued.append(
            _queue_review_action(
                "hands_fiverr_monitor_review",
                {
                    "channel": "marketplace_fiverr",
                    "platform": "fiverr",
                    "title": title,
                    "known_state": fiverr_state,
                    "manual_next_step": "Manually inspect listing health, buyer requests, and inbox state before taking action.",
                },
                preview,
                source_item_id=source_key,
                priority="normal",
                channel="marketplace_fiverr",
            )
        )

    return {
        "success": True,
        "workflow": "monitor_fiverr",
        "queued_count": sum(1 for item in queued if item.get("status") == "queued_for_operator_review"),
        "already_queued_count": sum(1 for item in queued if item.get("status") == "already_queued"),
        "items": queued,
    }


def status(limit: int = 10) -> dict[str, Any]:
    queue = _load_queue()
    pending = [row for row in queue if str(row.get("status") or "pending_confirm") in {"pending_confirm", SAFE_REVENUE_REVIEW_STATUS}]
    recent = queue[-max(0, int(limit)):] if limit else []
    guard = get_ui_automation_guard(force=True)
    return {
        "browser_use_available": _browser_installed(),
        "shortcuts_available": _shortcuts_installed(),
        "ui_guard": guard,
        "pending_actions": len(pending),
        "recent_actions": recent,
    }


def list_pending_actions(limit: int = 25) -> dict[str, Any]:
    queue = _load_queue()
    pending = [row for row in queue if str(row.get("status") or "pending_confirm") in {"pending_confirm", SAFE_REVENUE_REVIEW_STATUS}]
    return {
        "count": len(pending),
        "items": pending[: max(0, int(limit))],
    }


def search_path(
    query: str,
    *,
    roots: list[str] | None = None,
    account_hint: str = "",
    limit: int = 25,
    include_files: bool = True,
    include_dirs: bool = True,
) -> dict[str, Any]:
    if not str(query or "").strip():
        raise ValueError("query is required")
    start = time.time()
    search_roots = _coerce_search_roots(roots, account_hint=account_hint)
    max_results = max(1, min(int(limit or 25), 100))
    candidate_limit = max(max_results, min(max_results * 8, 500))

    results = _mdfind_search(search_roots, str(query), candidate_limit)
    search_mode = "spotlight"
    if not results:
        results = _fd_search(search_roots, str(query), candidate_limit)
        search_mode = "fd"
    if not results:
        results = _walk_search(search_roots, str(query), candidate_limit)
        search_mode = "walk"

    ranked = _filter_and_rank_paths(
        results,
        query=str(query),
        include_files=bool(include_files),
        include_dirs=bool(include_dirs),
        limit=max_results,
    )
    payload = {
        "success": True,
        "query": str(query),
        "account_hint": str(account_hint or ""),
        "search_mode": search_mode,
        "roots": [str(root) for root in search_roots],
        "count": len(ranked),
        "results": [_serialize_local_path(path, query=str(query)) for path in ranked],
    }
    _log_action("search_path", {"query": query, "account_hint": account_hint}, {"count": len(ranked)}, time.time() - start)
    return payload


def find_in_onedrive(
    query: str,
    *,
    account_hint: str = "",
    limit: int = 25,
    reveal_best_match: bool = False,
    open_best_match: bool = False,
) -> dict[str, Any]:
    roots = _discover_onedrive_roots(account_hint=account_hint)
    payload = search_path(
        query,
        roots=[str(root) for root in roots] if roots else None,
        account_hint=account_hint,
        limit=limit,
        include_files=True,
        include_dirs=True,
    )
    results = payload.get("results") or []
    if results and (reveal_best_match or open_best_match):
        best_path = str(results[0]["path"])
        payload["action"] = (
            open_path(best_path)
            if open_best_match
            else reveal_in_finder(best_path)
        )
    return payload


def list_folder(path: str, *, limit: int = 200, include_hidden: bool = False) -> dict[str, Any]:
    target = _resolve_existing_path(path)
    if not target.is_dir():
        raise NotADirectoryError(f"not a directory: {path}")
    start = time.time()
    entries: list[dict[str, Any]] = []
    for entry in sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
        if not include_hidden and entry.name.startswith("."):
            continue
        try:
            entries.append(_serialize_local_path(entry))
        except OSError:
            continue
        if len(entries) >= max(1, min(int(limit or 200), 500)):
            break
    payload = {
        "success": True,
        "path": str(target),
        "count": len(entries),
        "items": entries,
    }
    _log_action("list_folder", {"path": path}, {"count": len(entries)}, time.time() - start)
    return payload


def reveal_in_finder(path: str) -> dict[str, Any]:
    target = _resolve_existing_path(path)
    start = time.time()
    result = _run_command([OPEN_BIN, "-R", str(target)], timeout=20)
    payload = {
        "success": result.returncode == 0,
        "path": str(target),
        "stderr": (result.stderr or "").strip()[:300],
    }
    _log_action("reveal_in_finder", {"path": path}, payload, time.time() - start)
    return payload


def open_path(path: str) -> dict[str, Any]:
    target = _resolve_existing_path(path)
    start = time.time()
    result = _run_command([OPEN_BIN, str(target)], timeout=20)
    payload = {
        "success": result.returncode == 0,
        "path": str(target),
        "stderr": (result.stderr or "").strip()[:300],
    }
    _log_action("open_path", {"path": path}, payload, time.time() - start)
    return payload


def browser_open(url: str, *, session: str = "default") -> dict[str, Any]:
    if not str(url or "").strip():
        raise ValueError("url is required")
    start = time.time()
    payload = _browser_cli(["open", str(url).strip()], session=session, timeout=90)
    result = {
        "success": True,
        "session": _normalize_session(session),
        "url": str(((payload.get("data") or {}).get("url") or url)).strip(),
    }
    _cache_browser_state(session, {"url": result["url"], "session": result["session"]})
    _log_action("browser_open", {"session": session, "url": url}, result, time.time() - start)
    return result


def browser_state(*, session: str = "default") -> dict[str, Any]:
    start = time.time()
    payload = _browser_cli(["state"], session=session, timeout=30)
    data = payload.get("data") or {}
    parsed = _parse_browser_state_text(str(data.get("_raw_text") or ""))
    parsed["session"] = _normalize_session(session)
    parsed["success"] = True
    if not parsed.get("url"):
        parsed["url"] = str((_cached_browser_state(session).get("url") or "")).strip()
    _cache_browser_state(session, parsed)
    _log_action("browser_state", {"session": session}, {
        "session": parsed["session"],
        "title": parsed.get("title", ""),
        "url": parsed.get("url", ""),
        "elements": len(parsed.get("elements") or []),
    }, time.time() - start)
    return parsed


def _execute_browser_click(*, session: str, index: int | None = None, x: int | None = None, y: int | None = None) -> dict[str, Any]:
    start = time.time()
    args = ["click"]
    if index is not None:
        args.append(str(int(index)))
    elif x is not None and y is not None:
        args.extend([str(int(x)), str(int(y))])
    else:
        raise ValueError("index or x/y is required")
    payload = _browser_cli(args, session=session, timeout=45)
    label = _lookup_browser_label(session, int(index)) if index is not None else ""
    result = {
        "success": True,
        "session": _normalize_session(session),
        "index": index,
        "x": x,
        "y": y,
        "label": label,
        "data": payload.get("data"),
    }
    _log_action("browser_click", {"session": session, "index": index, "x": x, "y": y, "label": label}, result, time.time() - start)
    return result


def browser_click(
    *,
    session: str = "default",
    index: int | None = None,
    x: int | None = None,
    y: int | None = None,
    explicit_approval: bool = False,
) -> dict[str, Any]:
    if index is None and not explicit_approval:
        preview = f"Coordinate browser click queued for confirmation: ({x}, {y})"
        return _queue_action(
            "browser_click",
            {"session": _normalize_session(session), "index": index, "x": x, "y": y, "label": ""},
            preview,
        )
    label = _lookup_browser_label(session, int(index)) if index is not None else ""
    if label and _is_risky_browser_target(label) and not explicit_approval:
        preview = f"Browser click queued for confirmation: [{index}] {label}".strip()
        return _queue_action(
            "browser_click",
            {"session": _normalize_session(session), "index": index, "x": x, "y": y, "label": label},
            preview,
        )
    return _execute_browser_click(session=session, index=index, x=x, y=y)


def browser_input(*, index: int, text: str, session: str = "default") -> dict[str, Any]:
    start = time.time()
    payload = _browser_cli(["input", str(int(index)), str(text)], session=session, timeout=45)
    result = {
        "success": True,
        "session": _normalize_session(session),
        "index": int(index),
        "typed_chars": len(str(text)),
        "data": payload.get("data"),
    }
    _log_action("browser_input", {"session": session, "index": index}, result, time.time() - start)
    return result


def browser_keys(*, keys: str, session: str = "default", explicit_approval: bool = False) -> dict[str, Any]:
    normalized = str(keys or "").strip().lower().replace(" ", "")
    if normalized in _BROWSER_CONFIRM_KEYS and not explicit_approval:
        preview = f"Browser keypress queued for confirmation: {keys}"
        return _queue_action(
            "browser_keys",
            {"session": _normalize_session(session), "keys": str(keys)},
            preview,
        )
    start = time.time()
    payload = _browser_cli(["keys", str(keys)], session=session, timeout=30)
    result = {
        "success": True,
        "session": _normalize_session(session),
        "keys": str(keys),
        "data": payload.get("data"),
    }
    _log_action("browser_keys", {"session": session, "keys": keys}, result, time.time() - start)
    return result


def browser_wait_for_text(*, text: str, session: str = "default", timeout_s: int = 30) -> dict[str, Any]:
    start = time.time()
    payload = _browser_cli(["wait", "text", str(text)], session=session, timeout=max(5, int(timeout_s)))
    result = {
        "success": True,
        "session": _normalize_session(session),
        "text": str(text),
        "data": payload.get("data"),
    }
    _log_action("browser_wait_text", {"session": session, "text": text}, result, time.time() - start)
    return result


def browser_screenshot(*, session: str = "default", full: bool = False, path: str = "") -> dict[str, Any]:
    start = time.time()
    target = Path(path).expanduser() if path else (BASE / "data" / "browser_shots" / f"{_normalize_session(session)}_{int(time.time())}.png")
    target.parent.mkdir(parents=True, exist_ok=True)
    args = ["screenshot"]
    if full:
        args.append("--full")
    args.append(str(target))
    payload = _browser_cli(args, session=session, timeout=60)
    result = {
        "success": True,
        "session": _normalize_session(session),
        "path": str(target),
        "data": payload.get("data"),
    }
    _log_action("browser_screenshot", {"session": session, "path": str(target)}, result, time.time() - start)
    return result


def browser_extract(*, query: str, session: str = "default") -> dict[str, Any]:
    start = time.time()
    payload = _browser_cli(["extract", str(query)], session=session, timeout=90)
    result = {
        "success": True,
        "session": _normalize_session(session),
        "query": str(query),
        "data": payload.get("data"),
    }
    _log_action("browser_extract", {"session": session, "query": query[:160]}, result, time.time() - start)
    return result


def activate_app(app_name: str) -> dict[str, Any]:
    if not str(app_name or "").strip():
        raise ValueError("app_name is required")
    start = time.time()
    normalized = str(app_name).strip().lower()
    if normalized in {"safari", "apple safari"} and not os.environ.get("HANDS_ALLOW_SAFARI_ACTIVATE", "").strip():
        output = {
            "success": False,
            "app_name": str(app_name),
            "stderr": "Safari activation suppressed to avoid focus stealing. Set HANDS_ALLOW_SAFARI_ACTIVATE=1 to override.",
        }
        _log_action("activate_app", {"app_name": app_name, "suppressed": True}, output, time.time() - start)
        return output
    result = _run_command([OPEN_BIN, "-a", str(app_name)], timeout=15)
    output = {
        "success": result.returncode == 0,
        "app_name": str(app_name),
        "stderr": (result.stderr or "").strip()[:200],
    }
    _log_action("activate_app", {"app_name": app_name}, output, time.time() - start)
    return output


def click_ui(*, app_name: str, button_label: str, explicit_approval: bool = False) -> dict[str, Any]:
    if _is_risky_browser_target(button_label) and not explicit_approval:
        preview = f'UI click queued for confirmation: {app_name} → "{button_label}"'
        return _queue_action(
            "ui_click",
            {"app_name": str(app_name), "button_label": str(button_label)},
            preview,
        )

    safe_app = _escape_applescript(app_name)
    safe_button = _escape_applescript(button_label)
    script = f'''
    tell application "System Events"
        tell process "{safe_app}"
            set frontmost to true
            click button "{safe_button}" of window 1
        end tell
    end tell'''
    return app(script, app_name)


def list_shortcuts() -> dict[str, Any]:
    if not _shortcuts_installed():
        return {"success": False, "error": f"shortcuts not installed at {SHORTCUTS_BIN}"}
    result = _run_command([SHORTCUTS_BIN, "list"], timeout=30)
    if result.returncode != 0:
        return {"success": False, "error": (result.stderr or result.stdout or "").strip()}
    items = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
    return {"success": True, "count": len(items), "items": items}


def run_shortcut(*, shortcut_name: str, input_path: str = "", output_path: str = "", output_type: str = "") -> dict[str, Any]:
    if not _shortcuts_installed():
        return {"success": False, "error": f"shortcuts not installed at {SHORTCUTS_BIN}"}
    args = [SHORTCUTS_BIN, "run", str(shortcut_name)]
    if input_path:
        args.extend(["--input-path", str(Path(input_path).expanduser())])
    if output_path:
        args.extend(["--output-path", str(Path(output_path).expanduser())])
    if output_type:
        args.extend(["--output-type", str(output_type)])
    start = time.time()
    result = _run_command(args, timeout=120)
    output = {
        "success": result.returncode == 0,
        "shortcut_name": str(shortcut_name),
        "stdout": (result.stdout or "").strip()[:2000],
        "stderr": (result.stderr or "").strip()[:300],
    }
    _log_action("run_shortcut", {"shortcut_name": shortcut_name}, output, time.time() - start)
    return output


def confirm_action(action_id: str, *, explicit_approval: bool = False) -> dict[str, Any]:
    if not explicit_approval:
        return {
            "success": False,
            "error": "explicit approval required to confirm a queued action",
        }
    queue = _load_queue()
    idx = _queue_index(action_id, queue)
    if idx < 0:
        return {"success": False, "error": f"queued action not found: {action_id}"}

    item = dict(queue[idx])
    if str(item.get("status") or "") != "pending_confirm":
        return {"success": False, "error": f"queued action is not pending: {action_id}"}

    action = str(item.get("action") or "")
    params = item.get("params") or {}
    try:
        if action == "browser_click":
            result = _execute_browser_click(
                session=str(params.get("session") or "default"),
                index=params.get("index"),
                x=params.get("x"),
                y=params.get("y"),
            )
        elif action == "browser_keys":
            result = browser_keys(
                session=str(params.get("session") or "default"),
                keys=str(params.get("keys") or ""),
                explicit_approval=True,
            )
        elif action == "ui_click":
            result = click_ui(
                app_name=str(params.get("app_name") or ""),
                button_label=str(params.get("button_label") or ""),
                explicit_approval=True,
            )
        elif action == "email_send":
            result = email_send_now(
                str(params.get("to") or ""),
                str(params.get("subject") or ""),
                str(params.get("body_html") or ""),
                params.get("attachments") or [],
            )
        else:
            return {"success": False, "error": f"unsupported queued action: {action}"}
    except Exception as exc:
        item["status"] = "failed"
        item["error"] = str(exc)
        item["updated_at"] = datetime.now(timezone.utc).isoformat()
        queue[idx] = item
        _save_queue(queue)
        return {"success": False, "action_id": action_id, "error": str(exc)}

    item["status"] = "executed"
    item["updated_at"] = datetime.now(timezone.utc).isoformat()
    item["result"] = result
    queue[idx] = item
    _save_queue(queue)
    return {"success": True, "action_id": action_id, "result": result}
