#!/usr/bin/env python3
"""Autonomous-send master switch — the single owner control that replaces the human
staging/approval step for value-first EMAIL outreach, WITHOUT weakening any deterministic
safety gate (anon_scrub, enterprise_block, outreach_governor, brand_dedup, content_quality,
canspam postal, email validation all still fire in every sender that calls this).

Reads data/hustle/AUTONOMOUS_SEND.json:
    {"armed": true, "live": false, "exclude_lanes": ["BrandA"], "first_wave_daily_cap": 5, ...}

Contract (fail-CLOSED everywhere — any error / missing file / kill marker => do not send):
  armed_live(lane)              -> bool. True iff armed AND live AND lane NOT excluded AND no
                                   kill marker (AUTOFLOW_OFF). This is the autonomous "--live"
                                   authority: a sender may self-arm ONLY when this is True.
  first_wave_cap(lane, cap)     -> int. min(cap, first_wave_daily_cap) while armed_live — a
                                   CEILING only; it NEVER raises the caller's cap. Clamps a
                                   leftover high per-brand cap (e.g. the old 40) to the
                                   conservative first wave.
  vetoes(lane)                  -> bool. True iff the switch FILE EXISTS but this lane is not
                                   armed_live (live:false / excluded / kill marker). A present-
                                   but-not-live switch HALTS the send even if the caller passed
                                   an explicit --live, so `live:false` and `touch AUTOFLOW_OFF`
                                   are true kill switches. If the file is ABSENT there is no
                                   veto (senders then fall back to their own env gates, which
                                   default OFF), so deleting the file can never *enable* a blast.

Override the switch path with env AUTONOMOUS_SEND_FILE (used for dry-run harnesses so a scratch
file can be exercised without touching the live one). Kill:
  - set live:false in the json, or
  - touch data/hustle/AUTOFLOW_OFF, or
  - rm the json (falls back to the default-OFF env gates).

  python3 -m core.autonomous_send --selftest
  python3 -m core.autonomous_send --status broken_site
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_SWITCH = ROOT / "data" / "hustle" / "AUTONOMOUS_SEND.json"
_AUTOFLOW_OFF = ROOT / "data" / "hustle" / "AUTOFLOW_OFF"


def _switch_path() -> Path:
    override = os.environ.get("AUTONOMOUS_SEND_FILE", "").strip()
    return Path(override) if override else _DEFAULT_SWITCH


def switch_present() -> bool:
    """True iff the master-switch file exists (regardless of its state)."""
    try:
        return _switch_path().exists()
    except Exception:
        return False


def _load() -> dict | None:
    """Parse the switch file. None on absent/unreadable/malformed (fail-closed for callers)."""
    p = _switch_path()
    try:
        if not p.exists():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _kill_marker() -> bool:
    """Global hard-off (halts every autoflow loop). Fail-closed: unreadable => treat as ON (killed)."""
    try:
        return _AUTOFLOW_OFF.exists()
    except Exception:
        return True


def armed_live(lane: str) -> bool:
    """The autonomous send authority for `lane`. Fail-CLOSED: any error => False.

    True iff: no kill marker AND armed AND live AND lane not in exclude_lanes.
    """
    if _kill_marker():
        return False
    data = _load()
    if not data:
        return False
    try:
        if not (data.get("armed") is True and data.get("live") is True):
            return False
        excluded = {str(x).strip().lower() for x in (data.get("exclude_lanes") or [])}
        if (lane or "").strip().lower() in excluded:
            return False
        return True
    except Exception:
        return False


def vetoes(lane: str) -> bool:
    """True iff the switch file EXISTS but this lane is NOT armed_live — the master switch is
    present and is telling us to HALT this lane. A caller that would otherwise send (even on an
    explicit --live) MUST NOT send when this is True. Absent file => no veto (returns False)."""
    if not switch_present():
        return False
    return not armed_live(lane)


def first_wave_daily_cap() -> int:
    """The configured conservative first-wave ceiling (default 5). Never negative."""
    data = _load() or {}
    try:
        v = int(data.get("first_wave_daily_cap", 5))
        return max(0, v)
    except Exception:
        return 5


def first_wave_cap(lane: str, requested_cap: int) -> int:
    """CEILING today's per-run cap at the first-wave cap while armed_live. Only ever LOWERS the
    caller's cap (deliverability safety) — never raises it. When not armed_live, returns the
    caller's cap unchanged."""
    try:
        rc = int(requested_cap)
    except Exception:
        return 0
    if not armed_live(lane):
        return rc
    return max(0, min(rc, first_wave_daily_cap()))


def status(lane: str) -> dict:
    """Human-readable snapshot for logs / dry-runs."""
    return {
        "lane": lane,
        "switch_present": switch_present(),
        "kill_marker": _kill_marker(),
        "armed_live": armed_live(lane),
        "vetoes": vetoes(lane),
        "first_wave_daily_cap": first_wave_daily_cap(),
    }


def _selftest() -> int:
    import tempfile
    fails: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "AUTONOMOUS_SEND.json"
        os.environ["AUTONOMOUS_SEND_FILE"] = str(f)

        # 1. absent file => fail-closed, no veto
        if armed_live("broken_site"):
            fails.append("absent file armed_live should be False")
        if vetoes("broken_site"):
            fails.append("absent file should not veto")
        if first_wave_cap("broken_site", 40) != 40:
            fails.append("absent file must not clamp caller cap")

        # 2. armed but not live => halt + veto
        f.write_text(json.dumps({"armed": True, "live": False, "exclude_lanes": ["BrandA"],
                                 "first_wave_daily_cap": 5}))
        if armed_live("broken_site"):
            fails.append("armed+not-live must be False")
        if not vetoes("broken_site"):
            fails.append("armed+not-live present switch must veto")

        # 3. armed + live => send + clamp
        f.write_text(json.dumps({"armed": True, "live": True, "exclude_lanes": ["BrandA"],
                                 "first_wave_daily_cap": 5}))
        if not armed_live("broken_site"):
            fails.append("armed+live must be True")
        if vetoes("broken_site"):
            fails.append("armed+live must not veto")
        if first_wave_cap("broken_site", 40) != 5:
            fails.append("armed+live must clamp 40 -> 5")
        if first_wave_cap("broken_site", 3) != 3:
            fails.append("clamp must never raise (3 stays 3)")

        # 4. excluded lane (BrandA) never sends even when live
        if armed_live("BrandA"):
            fails.append("excluded lane BrandA must never be armed_live")
        if not vetoes("BrandA"):
            fails.append("excluded lane BrandA must veto")

    os.environ.pop("AUTONOMOUS_SEND_FILE", None)
    if fails:
        print("SELFTEST FAIL:")
        for x in fails:
            print("  -", x)
        return 1
    print("autonomous_send selftest OK")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv[1:]:
        raise SystemExit(_selftest())
    if "--status" in sys.argv[1:]:
        i = sys.argv.index("--status")
        lane = sys.argv[i + 1] if i + 1 < len(sys.argv) else "broken_site"
        print(json.dumps(status(lane), indent=2))
        raise SystemExit(0)
    print(json.dumps(status("broken_site"), indent=2))
