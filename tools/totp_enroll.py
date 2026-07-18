#!/usr/bin/env python3
"""totp_enroll.py — the ONE thing Operator does per account, ONCE: drop its 2FA seed.

    tools/totp_enroll.py <platform> <account>          # prompt for the base32 seed (no echo)
    tools/totp_enroll.py <platform> <account> --seed … # non-interactive (secure arg)
    tools/totp_enroll.py --list                        # show enrolled platform/accounts (names only)
    tools/totp_enroll.py --delete <platform> <account> # remove a seed

After a seed is enrolled, session-death re-auth for that account is FULLY hands-off: the stack
mints the current 6-digit code from the seed and clears 2FA itself (headless, no window, no text).

WHERE TO FIND THE base32 SEED (do this once per account, in the account's own 2FA settings):
  * tiktok  (hailportshq) : Settings > Security & permissions > 2-step verification >
                            Authenticator app > "Set up" > "Enter key manually" / "Can't scan"
  * reddit  (hailportss)  : Settings > Account > Two-factor authentication > enable via
                            authenticator app > "Enter this code / can't scan" (the base32 string)
  * x       (persona1)    : Settings > Security and account access > Security >
                            Two-factor authentication > Authentication app > "Can't scan the QR code?"
  * youtube (redacted) : Google Account > Security > 2-Step Verification > Authenticator >
                            "Can't scan it?" (the setup key). (Note: YouTube auth is an OAuth
                            refresh-token flow, so a seed only helps if you ever re-consent by password.)

Copy the base32 string (looks like  JBSWY3DPEHPK3PXP , spaces are fine) into the prompt below.

Platform / account naming must match what reauth_social.py looks up (tools/reauth_social.py TOTP_ACCOUNTS):
  tiktok/hailportshq   reddit/hailportss   x/persona1   youtube/redacted
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

BASE = Path(os.environ.get("CLAUDE_STACK_DIR") or (Path.home() / "claude-stack"))
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from core import totp_store


def _enroll(platform: str, account: str, seed: str | None) -> int:
    if not seed:
        seed = getpass.getpass(f"paste the base32 TOTP seed for {platform}/{account} (input hidden): ")
    seed = (seed or "").strip()
    if not seed:
        print("no seed given — aborted (nothing stored).")
        return 2
    res = totp_store.set_seed(platform, account, seed)
    if not res.get("ok"):
        print(f"FAILED: {res.get('error')}")
        return 1
    # prove it works WITHOUT printing the secret
    code = totp_store.current_code(platform, account)
    print(f"enrolled {platform}/{account} -> keychain service '{res['service']}'.")
    print(f"live code sanity: {'6-digit code minted OK' if (code and code.isdigit()) else 'WARN: could not mint a code'}")
    print("that's it — this account now self-clears 2FA on re-auth, zero touch.")
    return 0


def _list() -> int:
    rows = totp_store.list_enrolled(verify=True)
    if not rows:
        print("no TOTP seeds enrolled yet.")
        print("enroll one:  tools/totp_enroll.py <platform> <account>")
        return 0
    print("enrolled TOTP seeds (names only — secrets stay in the keychain):\n")
    for r in rows:
        mark = "live" if r.get("live") else "STALE (seed missing from keychain)"
        print(f"  {r['platform']}/{r['account']}  [{mark}]")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description="Enroll a per-account base32 TOTP seed into the macOS keychain (one-time).")
    p.add_argument("platform", nargs="?", help="tiktok | reddit | x | youtube")
    p.add_argument("account", nargs="?", help="the account handle, e.g. hailportss")
    p.add_argument("--seed", help="base32 seed (secure arg; omit to be prompted with no echo)")
    p.add_argument("--list", action="store_true", help="list enrolled platform/accounts (names only)")
    p.add_argument("--delete", nargs=2, metavar=("PLATFORM", "ACCOUNT"), help="remove a seed")
    a = p.parse_args()

    if a.list:
        return _list()
    if a.delete:
        res = totp_store.delete_seed(a.delete[0], a.delete[1])
        print(f"deleted {a.delete[0]}/{a.delete[1]}" if res.get("ok")
              else f"delete failed: {res.get('error')}")
        return 0 if res.get("ok") else 1
    if not a.platform or not a.account:
        p.print_help()
        return 2
    return _enroll(a.platform, a.account, a.seed)


if __name__ == "__main__":
    raise SystemExit(main())
