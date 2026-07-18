#!/usr/bin/env python3
"""Presence gate: is Operator actively using the Mac right now?

Two-sided throttle for the whole stack:
  ACTIVE (he's at the keyboard)  -> foreground automations must back off
  IDLE   (away/screen locked)    -> full-send: scheme, build, hunt money

Primary signal = macOS HID input-idle time. Screen-lock is a bonus signal.

CLI:
    user_active.py            # prints state; exit 0 if ACTIVE, 1 if IDLE
    user_active.py --idle-secs  # just print idle seconds

Library:
    from core.user_active import is_active, idle_seconds, should_foreground
    if is_active(): defer()           # he's here — don't grab the screen
    if should_foreground(): go_loud() # he's away — headed/foreground ok
"""
import os
import subprocess
import sys

ACTIVE_IDLE_THRESHOLD_S = int(os.environ.get("ACTIVE_IDLE_THRESHOLD_S", "300"))


def idle_seconds():
    """Seconds since the last local keyboard/mouse input. Large => away."""
    try:
        out = subprocess.run(
            ["ioreg", "-c", "IOHIDSystem"],
            capture_output=True, text=True, timeout=8,
        ).stdout
    except Exception:
        return 0.0
    for line in out.splitlines():
        if "HIDIdleTime" in line:
            try:
                ns = int(line.split("=")[-1].strip())
                return ns / 1_000_000_000.0
            except Exception:
                return 0.0
    return 0.0


def screen_locked():
    try:
        from Quartz import CGSessionCopyCurrentDictionary  # type: ignore
        d = CGSessionCopyCurrentDictionary()
        return bool(d and d.get("CGSSessionScreenIsLocked", 0))
    except Exception:
        return False


def is_active(threshold_s=None):
    """True when Operator is at the machine: recent input AND screen unlocked."""
    t = ACTIVE_IDLE_THRESHOLD_S if threshold_s is None else threshold_s
    if screen_locked():
        return False
    return idle_seconds() < t


def should_foreground(threshold_s=None):
    """Inverse: safe to do headed/foreground/mouse/keyboard work."""
    return not is_active(threshold_s)


def main():
    if "--idle-secs" in sys.argv:
        print(int(idle_seconds()))
        return 0
    active = is_active()
    print(
        f"{'ACTIVE' if active else 'IDLE'} "
        f"(idle={int(idle_seconds())}s, threshold={ACTIVE_IDLE_THRESHOLD_S}s, "
        f"locked={screen_locked()})"
    )
    return 0 if active else 1


if __name__ == "__main__":
    sys.exit(main())
