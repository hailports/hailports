"""OpenAI API key discovery for stack routes.

The stack should not duplicate API keys when OpenClaw already owns the auth
profile. This module checks process env, the stack .env, then OpenClaw's local
auth profile store.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


STACK_DIR = Path(__file__).resolve().parents[1]


def _env_file_value(name: str, env_path: Path) -> str:
    try:
        for line in env_path.read_text(errors="ignore").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or not stripped.startswith(f"{name}="):
                continue
            return stripped.split("=", 1)[1].strip().strip("'\"")
    except Exception:
        return ""
    return ""


def _openclaw_auth_paths() -> list[Path]:
    paths: list[Path] = []
    explicit = os.environ.get("OPENCLAW_AUTH_PROFILES")
    if explicit:
        paths.append(Path(explicit).expanduser())

    agent_dir = os.environ.get("OPENCLAW_AGENT_DIR") or os.environ.get("PI_CODING_AGENT_DIR")
    if agent_dir:
        paths.append(Path(agent_dir).expanduser() / "auth-profiles.json")

    home = Path.home()
    paths.append(home / ".openclaw" / "agents" / "main" / "agent" / "auth-profiles.json")
    return paths


def _key_from_openclaw_profile(path: Path) -> str:
    try:
        payload = json.loads(path.read_text(errors="ignore"))
    except Exception:
        return ""
    profiles = payload.get("profiles") if isinstance(payload, dict) else None
    if not isinstance(profiles, dict):
        return ""

    for profile_id in ("openai:default", "openai-codex:default"):
        profile = profiles.get(profile_id)
        if isinstance(profile, dict):
            value = str(profile.get("key") or profile.get("apiKey") or profile.get("token") or "").strip()
            if value:
                return value

    for profile in profiles.values():
        if not isinstance(profile, dict) or profile.get("provider") != "openai":
            continue
        value = str(profile.get("key") or profile.get("apiKey") or profile.get("token") or "").strip()
        if value:
            return value
    return ""


def load_openai_api_key() -> str:
    value = os.environ.get("OPENAI_API_KEY", "").strip()
    if value:
        return value

    value = _env_file_value("OPENAI_API_KEY", STACK_DIR / ".env")
    if value:
        return value

    for path in _openclaw_auth_paths():
        value = _key_from_openclaw_profile(path)
        if value:
            return value
    return ""
