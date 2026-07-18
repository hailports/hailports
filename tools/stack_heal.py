#!/usr/bin/env python3
"""stack_heal.py — deterministic self-heal actions the alert gateway runs INSTEAD of texting.

Wired in via core/never_twice.py REGISTRY (known_fix). Each subcommand is bounded, reversible,
and idempotent. Definition of a DEAD job matches demo_alert's own test: a launchd label whose
last run left PID '-' AND a nonzero last-exit — i.e. it crashed/exited bad and isn't running.

  revive-dead   re-enable + kickstart every dead com.claude-stack job (skips guards/self)
  count-dead    exit 0 iff zero dead jobs remain  (the gateway's verify step)
  restart <label>   kickstart one label + confirm it comes up
"""
from __future__ import annotations

import os
import subprocess
import sys

UID = os.getuid()
PREFIX = "com.claude-stack."
# never bounce the safety layer itself, or a heal could fight the very guard that spawned it.
SKIP = ("runaway-guard", "guard", "sentinel", "warden", "watchdog", "healer", "deadman")


def _dead_jobs() -> list[str]:
    """Labels with PID '-' and a nonzero last-exit — crashed and not currently running."""
    try:
        out = subprocess.run(["/bin/launchctl", "list"], capture_output=True,
                             text=True, timeout=15).stdout
    except Exception:
        return []
    dead = []
    for ln in out.splitlines():
        parts = ln.split("\t") if "\t" in ln else ln.split()
        if len(parts) < 3:
            continue
        pid, status, label = parts[0], parts[1], parts[2]
        if not label.startswith(PREFIX):
            continue
        if any(s in label for s in SKIP):
            continue
        if pid == "-" and status not in ("0", "-"):
            dead.append(label)
    return dead


def _revive(label: str) -> bool:
    try:
        subprocess.run(["/bin/launchctl", "enable", f"gui/{UID}/{label}"],
                       capture_output=True, timeout=10)
        r = subprocess.run(["/bin/launchctl", "kickstart", "-k", f"gui/{UID}/{label}"],
                           capture_output=True, text=True, timeout=20)
        return r.returncode == 0
    except Exception:
        return False


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "count-dead"
    if cmd == "count-dead":
        dead = _dead_jobs()
        if dead:
            print(f"{len(dead)} dead: {', '.join(dead)}")
            return 1
        print("0 dead")
        return 0
    if cmd == "revive-dead":
        dead = _dead_jobs()
        if not dead:
            print("nothing dead")
            return 0
        revived = [l for l in dead if _revive(l)]
        print(f"revived {len(revived)}/{len(dead)}: {', '.join(revived)}")
        return 0
    if cmd == "restart" and len(argv) > 1:
        label = argv[1]
        ok = _revive(label)
        print(f"restart {label}: {'ok' if ok else 'FAILED'}")
        return 0 if ok else 1
    print(f"unknown cmd: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
