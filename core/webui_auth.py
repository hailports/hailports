"""Password-backed WebUI credential store.

Uses a local PBKDF2 hash file so the WebUI can move away from plaintext
WEBUI_PASSWORD checks while preserving the existing env-based bootstrap path.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path


_CREDS_PATH = Path(__file__).resolve().parent.parent / "data" / "runtime" / "webui_credentials.json"
_ITERATIONS = 260_000
# Login username -> internal user_id. Operator2 has her OWN distinct id ("Operator2"),
# never the generic "partner"/"spouse" lane.
_DEFAULT_USERS = {
    "Operator": "Operator",
    "dfkdmoney": "Operator",
    "Operator2": "Operator2",
    "nicoledemo": "Operator2",
}


def _hash_password(password: str, salt: str | None = None) -> dict:
    salt_bytes = base64.b64decode(salt.encode("ascii")) if salt else secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", str(password or "").encode("utf-8"), salt_bytes, _ITERATIONS)
    return {
        "algorithm": "pbkdf2_sha256",
        "iterations": _ITERATIONS,
        "salt": base64.b64encode(salt_bytes).decode("ascii"),
        "hash": base64.b64encode(digest).decode("ascii"),
    }


def _verify_password(password: str, entry: dict) -> bool:
    if not isinstance(entry, dict) or entry.get("algorithm") != "pbkdf2_sha256":
        return False
    try:
        iterations = int(entry.get("iterations") or _ITERATIONS)
        salt_bytes = base64.b64decode(str(entry.get("salt") or "").encode("ascii"))
        expected = base64.b64decode(str(entry.get("hash") or "").encode("ascii"))
        actual = hashlib.pbkdf2_hmac("sha256", str(password or "").encode("utf-8"), salt_bytes, iterations)
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def _load_creds() -> dict:
    try:
        if _CREDS_PATH.exists():
            payload = json.loads(_CREDS_PATH.read_text())
            if isinstance(payload, dict):
                return payload
    except Exception:
        pass
    return {}


def _save_creds(payload: dict) -> None:
    _CREDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CREDS_PATH.write_text(json.dumps(payload, indent=2, sort_keys=True))


def seed_from_env(force: bool = False) -> dict:
    """Seed hashed credentials from WEBUI_PASSWORD/WEBUI_CRED_* when needed."""
    existing = _load_creds()
    if existing and not force:
        return existing
    payload = {} if force else dict(existing)
    shared_password = str(os.environ.get("WEBUI_PASSWORD") or "").strip()
    if shared_password:
        for username, user_id in _DEFAULT_USERS.items():
            payload.setdefault(username, {"user_id": user_id, **_hash_password(shared_password)})
    for env_key, env_value in os.environ.items():
        if not env_key.startswith("WEBUI_CRED_") or "," not in env_value:
            continue
        username = env_key[len("WEBUI_CRED_"):].strip().lower()
        password, user_id = env_value.split(",", 1)
        if username and password.strip() and user_id.strip():
            payload[username] = {"user_id": user_id.strip(), **_hash_password(password.strip())}
    if payload:
        _save_creds(payload)
    return payload


def check_credentials(username: str, password: str) -> str:
    username = str(username or "").strip().lower()
    if not username or not password:
        return ""
    creds = _load_creds() or seed_from_env()
    entry = creds.get(username)
    if not entry or not _verify_password(password, entry):
        return ""
    return str(entry.get("user_id") or "")


def change_password(username: str, new_password: str) -> bool:
    username = str(username or "").strip().lower()
    if not username or not new_password:
        return False
    creds = _load_creds() or seed_from_env()
    entry = dict(creds.get(username) or {})
    if not entry:
        return False
    entry.update(_hash_password(new_password))
    creds[username] = entry
    _save_creds(creds)
    return True
