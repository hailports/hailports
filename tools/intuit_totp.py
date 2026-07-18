#!/usr/bin/env python3
"""Local TOTP code generator for the Intuit (QuickBooks Time / TSheets) account 2FA — the timeclock's
answer to the same problem work_totp.py solves for Microsoft: the headless OWA-style session on
:18823 periodically logs out and re-auth needs an MFA code. Rather than a phone, the mini mints the
same 6-digit codes locally from the base32 seed Intuit gives under "add an authenticator app".

Setup (one time):
  1. Intuit account -> Sign in & security -> Two-step verification -> add "Authenticator app"
     -> "Can't scan the QR code?" / "Enter code manually" -> copy the SECRET KEY (base32).
  2. python3 tools/intuit_totp.py setup "THE SECRET KEY"
  3. python3 tools/intuit_totp.py            # prints the current 6-digit code
  4. type that code into Intuit's confirm box to register + verify.
After that timeclock_reauth.js calls current_code() during a headless re-auth.

Seed + creds live ONLY in the login Keychain (account claude-stack), encrypted at rest — same store as
the MS creds (core/secret_store). NEVER logged/printed except the live code. This puts a 2FA factor on
the box; guard the mini accordingly.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.work_totp import totp, _normalize  # pure RFC-6238 generator, already test-vector verified

_SECRET_NAME = "intuit_totp_secret"


def _load_secret() -> str:
    try:
        from core.secret_store import get_secret
        s = get_secret(_SECRET_NAME)
        if s:
            return s.strip()
    except Exception:
        pass
    import os
    return (os.environ.get("INTUIT_TOTP_SECRET", "") or "").strip()


def current_code() -> dict:
    """{ok, code, seconds_left} for the re-auth flow. ok=False if no seed stored yet."""
    secret = _load_secret()
    if not secret:
        return {"ok": False, "code": "", "seconds_left": 0,
                "reason": "no Intuit TOTP seed stored (run: intuit_totp.py setup <secret>)"}
    try:
        return {"ok": True, "code": totp(secret), "seconds_left": 30 - int(time.time()) % 30}
    except Exception as e:
        return {"ok": False, "code": "", "seconds_left": 0, "reason": f"bad seed: {e}"}


def _store(secret: str) -> str:
    import base64
    s = _normalize(secret)
    if len(s) < 16:
        raise ValueError("that doesn't look like a TOTP secret (too short)")
    base64.b32decode(s + "=" * ((-len(s)) % 8))  # validate real base32
    from core.secret_store import set_secret
    if not set_secret(_SECRET_NAME, s):
        raise RuntimeError("keychain write failed")
    return s


def _cli() -> int:
    a = sys.argv[1:]
    if a and a[0] == "setup":
        if len(a) < 2:
            print("usage: intuit_totp.py setup <secret key from Intuit>")
            return 2
        try:
            s = _store(" ".join(a[1:]))
        except Exception as e:
            print(f"refused: {e}")
            return 1
        c = current_code()
        print(f"stored ({len(s)} chars, keychain). current code: {c['code']}  ({c['seconds_left']}s left)")
        print("-> type that code into Intuit's confirm box to verify + register.")
        return 0
    c = current_code()
    if not c["ok"]:
        print(c.get("reason", "no code"))
        return 1
    print(f"{c['code']}  ({c['seconds_left']}s left)")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
