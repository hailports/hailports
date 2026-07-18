#!/usr/bin/env python3
"""totp_store.py — keychain-only lifecycle for per-account 2FA (TOTP) seeds.

The stack owns the secret lifecycle: Operator drops a base32 seed ONCE (tools/totp_enroll.py),
it lands in the macOS login keychain, and nothing ever writes it to disk/git again. This
module is the single read/write choke point both reauth_social.py and totp_enroll.py use.

Service-name convention (keychain generic-password):  claude-stack-totp-<platform>-<account>
  account attribute (-a)                             :  <account>          (the handle)

A "seed" is the base32 string from an authenticator "manual entry / can't scan" screen.
current_code() turns it into the live 6-digit code via pyotp — that's the zero-touch 2FA path.

A NAMES-ONLY index (data/runtime/totp_enrolled.json) records which <platform>/<account> pairs
are enrolled so --list can report them WITHOUT ever touching the secret. The seed itself lives
only in the keychain.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
INDEX = BASE_DIR / "data" / "runtime" / "totp_enrolled.json"

_KIND = "claude-stack TOTP seed"
_COMMENT = "faceless-lane 2FA auto-reauth seed (base32)"


def service_name(platform: str, account: str) -> str:
    return f"claude-stack-totp-{platform}-{account}"


def normalize_seed(seed: str) -> str:
    """Strip the spaces/dashes authenticator setup screens add, upcase to canonical base32."""
    return "".join((seed or "").split()).replace("-", "").upper()


def _seed_is_valid(seed: str) -> bool:
    """A seed is valid iff pyotp can mint a 6-digit code from it (catches bad base32)."""
    try:
        import pyotp

        code = pyotp.TOTP(seed).now()
        return bool(code) and code.isdigit() and len(code) == 6
    except Exception:
        return False


# ───────────────────────────────────────── keychain read/write (security CLI)
def get_seed(platform: str, account: str) -> str | None:
    """Read the base32 seed from the keychain, or None if not enrolled."""
    r = subprocess.run(
        ["security", "find-generic-password", "-s", service_name(platform, account),
         "-a", account, "-w"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return None
    seed = (r.stdout or "").strip()
    return seed or None


def set_seed(platform: str, account: str, seed: str) -> dict:
    """Store (or update, -U) a validated base32 seed in the keychain. NEVER writes it to disk.

    -A allows the stack's own processes to read it without a per-access GUI prompt (required
    for headless auto-reauth); the secret still never leaves the keychain.
    """
    seed = normalize_seed(seed)
    if not _seed_is_valid(seed):
        return {"ok": False, "error": "invalid base32 TOTP seed (pyotp could not mint a code)"}
    r = subprocess.run(
        ["security", "add-generic-password",
         "-s", service_name(platform, account), "-a", account,
         "-w", seed, "-U", "-A", "-D", _KIND, "-j", _COMMENT],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return {"ok": False, "error": (r.stderr or "security add-generic-password failed").strip()}
    _index_add(platform, account)
    return {"ok": True, "service": service_name(platform, account), "account": account}


def delete_seed(platform: str, account: str) -> dict:
    r = subprocess.run(
        ["security", "delete-generic-password", "-s", service_name(platform, account), "-a", account],
        capture_output=True, text=True,
    )
    _index_remove(platform, account)
    ok = r.returncode == 0
    return {"ok": ok, "error": None if ok else (r.stderr or "not found").strip()}


def current_code(platform: str, account: str) -> str | None:
    """The live 6-digit TOTP code for this account, or None if no seed is enrolled."""
    seed = get_seed(platform, account)
    if not seed:
        return None
    try:
        import pyotp

        return pyotp.TOTP(seed).now()
    except Exception:
        return None


def has_seed(platform: str, account: str) -> bool:
    return get_seed(platform, account) is not None


# ───────────────────────────────────────── names-only enrollment index
def _load_index() -> list[dict]:
    try:
        d = json.loads(INDEX.read_text())
        return d if isinstance(d, list) else []
    except Exception:
        return []


def _save_index(rows: list[dict]) -> None:
    INDEX.parent.mkdir(parents=True, exist_ok=True)
    INDEX.write_text(json.dumps(rows, indent=2, sort_keys=True))


def _index_add(platform: str, account: str) -> None:
    rows = _load_index()
    if not any(r.get("platform") == platform and r.get("account") == account for r in rows):
        rows.append({"platform": platform, "account": account})
        _save_index(sorted(rows, key=lambda r: (r["platform"], r["account"])))


def _index_remove(platform: str, account: str) -> None:
    rows = [r for r in _load_index()
            if not (r.get("platform") == platform and r.get("account") == account)]
    _save_index(rows)


def list_enrolled(verify: bool = True) -> list[dict]:
    """Enrolled <platform>/<account> pairs (names only, NEVER the secret).

    verify=True cross-checks each index row against the keychain and marks `live` — so a row
    whose seed was deleted out-of-band still surfaces, flagged as stale."""
    rows = _load_index()
    if not verify:
        return rows
    return [{**r, "live": has_seed(r["platform"], r["account"])} for r in rows]
