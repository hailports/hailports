"""Setup wizard operations for local credentials, packaging, and wipe flows."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from core import auth_maintenance
from core import credentials_ops
from scripts import package_for_sale
from scripts import snapshot_machine_capabilities

BASE_DIR = Path(__file__).resolve().parent.parent
HOME_DIR = Path.home()
ENV_PATH = BASE_DIR / ".env"
ENV_EXAMPLE_PATH = BASE_DIR / ".env.example"
USERS_PATH = BASE_DIR / "config" / "users.toml"
USERS_TEMPLATE_PATH = BASE_DIR / "config" / "users.template.toml"
VENV_PATH = BASE_DIR / ".venv"
TOKEN_DIR = BASE_DIR / "data" / "tokens"
CERT_DIR = BASE_DIR / "data" / "certs"
RUNTIME_DIR = BASE_DIR / "data" / "runtime"
LOG_DIR = BASE_DIR / "data" / "logs"
COLLAB_DIR = BASE_DIR / "data" / "collab_bus"
SNAPSHOT_PATH = BASE_DIR / "data" / "capability_snapshots" / "machine_capabilities.json"
DIST_DIR = BASE_DIR / "dist"
MANIFEST_PATH = BASE_DIR / "distribution_manifest.json"
INVENTORY_PATH = BASE_DIR / "data" / "sale_ready_packaging_inventory.json"
LAUNCH_AGENTS_DIR = HOME_DIR / "Library" / "LaunchAgents"

SHAREABLE_EXTRA_PATTERNS = [
    "data/capability_snapshots/",
]
FULL_WIPE_EXTRA_PATTERNS = [
    ".venv/",
    "dist/",
]
SHAREABLE_SKIP_PATTERNS = {
    ".venv/",
    "dist/",
}


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def _safe_rel(path: Path) -> str:
    try:
        return path.relative_to(BASE_DIR).as_posix()
    except Exception:
        return str(path)


def _human_size(size: int) -> str:
    value = float(size)
    units = ["B", "KB", "MB", "GB"]
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
        value /= 1024
    return f"{size}B"


def _iter_repo_paths() -> list[Path]:
    paths: list[Path] = []
    for child in BASE_DIR.iterdir():
        paths.append(child)
        if child.is_dir():
            paths.extend(child.rglob("*"))
    return paths


def _collapse_paths(paths: list[Path]) -> list[Path]:
    selected: list[Path] = []
    selected_set = set()
    for path in sorted(set(paths), key=lambda p: len(p.parts)):
        if path == BASE_DIR:
            continue
        if not str(path.resolve()).startswith(str(BASE_DIR.resolve())):
            continue
        if any(parent in selected_set for parent in path.parents):
            continue
        selected.append(path)
        selected_set.add(path)
    return sorted(selected, key=lambda p: _safe_rel(p))


def _collect_distribution_patterns(scope: str) -> list[str]:
    patterns = set()
    if MANIFEST_PATH.exists():
        patterns.update(((_load_json(MANIFEST_PATH).get("exclude_from_distribution") or [])))
    if INVENTORY_PATH.exists():
        patterns.update(((_load_json(INVENTORY_PATH).get("files_to_exclude_from_distribution") or [])))
    patterns.update(SHAREABLE_EXTRA_PATTERNS)
    if scope == "shareable":
        patterns.difference_update(SHAREABLE_SKIP_PATTERNS)
    if scope == "full":
        patterns.update(FULL_WIPE_EXTRA_PATTERNS)
    return sorted(str(p).strip() for p in patterns if str(p).strip())


def _match_paths(patterns: list[str], scope: str) -> list[Path]:
    hits: list[Path] = []
    for path in _iter_repo_paths():
        if path == BASE_DIR / ".git" or (BASE_DIR / ".git") in path.parents:
            continue
        if scope == "shareable":
            if path == VENV_PATH or VENV_PATH in path.parents:
                continue
            if path == DIST_DIR or DIST_DIR in path.parents:
                continue
        rel = _safe_rel(path)
        if package_for_sale._should_exclude(rel, path.is_dir(), patterns):
            hits.append(path)
    for pattern in patterns:
        token = str(pattern).rstrip("/")
        if "/" in token and "*" not in token and "?" not in token:
            direct = BASE_DIR / token
            if direct.exists():
                hits.append(direct)
    return _collapse_paths(hits)


def _path_category(path: Path) -> str:
    rel = _safe_rel(path)
    if rel in {".env", "config/users.toml"} or rel.startswith("data/tokens") or rel.startswith("data/certs"):
        return "credentials"
    if rel.startswith("data/"):
        return "runtime"
    if rel.startswith(".venv") or rel.startswith("dist/"):
        return "tooling"
    if "__pycache__" in rel or rel.endswith(".pyc") or rel == ".DS_Store":
        return "cache"
    if rel.startswith("scripts/_tmp_"):
        return "temporary"
    return "other"


def _launch_agent_paths() -> list[Path]:
    if not LAUNCH_AGENTS_DIR.exists():
        return []
    return sorted(LAUNCH_AGENTS_DIR.glob("com.claude-stack*.plist"))


def _launch_agent_rows() -> list[dict[str, Any]]:
    return [{"name": path.name, "path": str(path)} for path in _launch_agent_paths()]


def wipe_preview(scope: str = "shareable") -> dict[str, Any]:
    scope = (scope or "shareable").strip().lower()
    if scope not in {"shareable", "full"}:
        raise ValueError(f"Unsupported wipe scope: {scope}")

    patterns = _collect_distribution_patterns(scope)
    matched = _match_paths(patterns, scope)
    categories: dict[str, int] = {}
    rows: list[dict[str, Any]] = []
    for path in matched:
        category = _path_category(path)
        categories[category] = categories.get(category, 0) + 1
        rows.append(
            {
                "path": _safe_rel(path),
                "kind": "dir" if path.is_dir() else "file",
                "category": category,
            }
        )

    return {
        "scope": scope,
        "patterns": patterns,
        "existing_count": len(rows),
        "categories": categories,
        "items": rows,
        "sample": rows[:40],
        "launch_agents": _launch_agent_rows() if scope == "full" else [],
    }


def _remove_path(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)


def _best_effort_bootout_launch_agent(path: Path) -> None:
    service = path.stem
    uid = os.getuid()
    commands = [
        ["launchctl", "bootout", f"gui/{uid}/{service}"],
        ["launchctl", "bootout", f"gui/{uid}", str(path)],
        ["launchctl", "unload", str(path)],
    ]
    for cmd in commands:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        except FileNotFoundError:
            return
        if result.returncode == 0:
            return


def apply_wipe(scope: str = "shareable") -> dict[str, Any]:
    preview = wipe_preview(scope)
    removed: list[str] = []
    errors: list[dict[str, str]] = []

    if preview["scope"] == "full":
        for row in preview["launch_agents"]:
            path = Path(row["path"])
            try:
                _best_effort_bootout_launch_agent(path)
                path.unlink(missing_ok=True)
                removed.append(str(path))
            except Exception as exc:
                errors.append({"path": str(path), "error": str(exc)})

    matched_paths = [BASE_DIR / row["path"] for row in preview["items"]]
    for path in sorted(matched_paths, key=lambda p: len(_safe_rel(p).split("/")), reverse=True):
        try:
            _remove_path(path)
            removed.append(_safe_rel(path))
        except Exception as exc:
            errors.append({"path": _safe_rel(path), "error": str(exc)})

    return {
        "ok": not errors,
        "scope": preview["scope"],
        "removed_count": len(removed),
        "removed": removed[:120],
        "errors": errors[:40],
    }


def _load_capability_snapshot() -> dict[str, Any]:
    if not SNAPSHOT_PATH.exists():
        return {"available": False, "path": str(SNAPSHOT_PATH)}
    try:
        data = _load_json(SNAPSHOT_PATH)
    except Exception as exc:
        return {"available": True, "path": str(SNAPSHOT_PATH), "error": str(exc)}
    return {
        "available": True,
        "path": str(SNAPSHOT_PATH),
        "captured_at": data.get("captured_at"),
        "machine": data.get("machine") or {},
        "summary": data.get("summary") or {},
    }


def refresh_capability_snapshot() -> dict[str, Any]:
    snapshot = snapshot_machine_capabilities.capture_snapshot()
    return {
        "ok": True,
        "path": str(SNAPSHOT_PATH),
        "captured_at": snapshot.get("captured_at"),
        "summary": snapshot.get("summary") or {},
    }


def build_share_package(bundle_name: str | None = None) -> dict[str, Any]:
    return package_for_sale.build_package(bundle_name=bundle_name or None)


def _dir_count(path: Path, glob_pattern: str = "*") -> int:
    if not path.exists() or not path.is_dir():
        return 0
    return sum(1 for _ in path.glob(glob_pattern))


def _bootstrap_rows() -> list[dict[str, Any]]:
    launch_agents = _launch_agent_rows()
    rows = [
        {
            "key": "env",
            "label": ".env",
            "ok": ENV_PATH.exists(),
            "detail": "present" if ENV_PATH.exists() else "copy from .env.example",
            "path": str(ENV_PATH),
        },
        {
            "key": "users",
            "label": "config/users.toml",
            "ok": USERS_PATH.exists(),
            "detail": "present" if USERS_PATH.exists() else "copy from config/users.template.toml",
            "path": str(USERS_PATH),
        },
        {
            "key": "venv",
            "label": ".venv",
            "ok": VENV_PATH.exists(),
            "detail": "ready" if VENV_PATH.exists() else "run deploy/install.sh",
            "path": str(VENV_PATH),
        },
        {
            "key": "tokens",
            "label": "Stored tokens",
            "ok": _dir_count(TOKEN_DIR, "*.json") > 0,
            "detail": f"{_dir_count(TOKEN_DIR, '*.json')} token file(s)",
            "path": str(TOKEN_DIR),
        },
        {
            "key": "certs",
            "label": "Local certs",
            "ok": _dir_count(CERT_DIR) > 0,
            "detail": f"{_dir_count(CERT_DIR)} file(s)",
            "path": str(CERT_DIR),
        },
        {
            "key": "launch_agents",
            "label": "LaunchAgents",
            "ok": bool(launch_agents),
            "detail": f"{len(launch_agents)} installed",
            "path": str(LAUNCH_AGENTS_DIR),
        },
        {
            "key": "snapshot",
            "label": "Capability snapshot",
            "ok": SNAPSHOT_PATH.exists(),
            "detail": "present" if SNAPSHOT_PATH.exists() else "refresh machine snapshot",
            "path": str(SNAPSHOT_PATH),
        },
    ]
    return rows


def _dist_rows() -> list[dict[str, Any]]:
    bundles: list[dict[str, Any]] = []
    if DIST_DIR.exists():
        for path in sorted(DIST_DIR.glob("*.tar.gz"), key=lambda p: p.stat().st_mtime, reverse=True)[:10]:
            bundles.append(
                {
                    "name": path.name,
                    "size": _human_size(path.stat().st_size),
                    "path": str(path),
                    "mtime": path.stat().st_mtime,
                }
            )
    patterns = _collect_distribution_patterns("shareable")
    return [
        {
            "key": "manifest",
            "label": "distribution_manifest.json",
            "ok": MANIFEST_PATH.exists(),
            "detail": "present" if MANIFEST_PATH.exists() else "missing",
            "path": str(MANIFEST_PATH),
        },
        {
            "key": "inventory",
            "label": "sale_ready_packaging_inventory.json",
            "ok": INVENTORY_PATH.exists(),
            "detail": "present" if INVENTORY_PATH.exists() else "missing",
            "path": str(INVENTORY_PATH),
        },
        {
            "key": "snapshot_exclusion",
            "label": "Snapshot excluded from distribution",
            "ok": "data/capability_snapshots/" in patterns,
            "detail": "excluded" if "data/capability_snapshots/" in patterns else "not excluded",
            "path": str(SNAPSHOT_PATH),
        },
    ], bundles


async def status_all() -> dict[str, Any]:
    credentials = await credentials_ops.status_all()
    auth_state = auth_maintenance.status_snapshot()
    dist_checks, bundles = _dist_rows()
    share_preview = wipe_preview("shareable")
    full_preview = wipe_preview("full")
    return {
        "as_of": time.time(),
        "bootstrap": _bootstrap_rows(),
        "credentials": credentials,
        "auth_maintenance": auth_state,
        "capability_snapshot": _load_capability_snapshot(),
        "packaging": {
            "checks": dist_checks,
            "bundles": bundles,
        },
        "wipe": {
            "shareable": {
                "existing_count": share_preview["existing_count"],
                "categories": share_preview["categories"],
                "sample": share_preview["sample"],
            },
            "full": {
                "existing_count": full_preview["existing_count"],
                "categories": full_preview["categories"],
                "sample": full_preview["sample"],
                "launch_agents": full_preview["launch_agents"],
            },
        },
        "paths": {
            "repo_root": str(BASE_DIR),
            "env": str(ENV_PATH),
            "users": str(USERS_PATH),
            "snapshot": str(SNAPSHOT_PATH),
            "dist_dir": str(DIST_DIR),
        },
    }


def status_all_sync() -> dict[str, Any]:
    return asyncio.run(status_all())
