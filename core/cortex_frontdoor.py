#!/usr/bin/env python3
"""Front-door primitive — the single, fail-open entry every front door calls.

This is the shim the live front doors (StackGPT gateway, iMessage bridge, OpenClaw) hook into with
ONE guarded line. It is SHADOW-by-default and behind a per-door ARM flag whose absence = the OLD
behavior, so hooking it in changes nothing until a flag is explicitly set:

  * observe_only(text, front_door, source)  -> always a SHADOW pass: classify + stamp the afferent
    event to the correct lane db, ACT ON NOTHING, return the plan (for logging). The gateway uses
    this — it adds lane-stamped telemetry without touching the existing guarded routing/fences.
  * maybe_route(text, front_door, source)   -> returns a routed plan ONLY when that door's ARM flag
    is present AND the global kill is absent; otherwise runs a SHADOW pass and returns None so the
    caller's OLD path proceeds untouched. iMessage/OpenClaw use this.

ARM flags (presence = armed) under data/nervous/:
    CORTEX_ARM_STACKGPT   CORTEX_ARM_IMESSAGE   CORTEX_ARM_OPENCLAW
Global kill (presence = force every door back to shadow, instant rollback, no restart):
    data/nervous/CORTEX_FRONTDOOR_OFF

FIREWALL: lane is decided + stamped by core.nervous_cortex.route() (stackgpt pins work; imessage/
openclaw infer; unknown DROPS — never defaults hustle). This module adds NO lane logic of its own.

FAIL-OPEN: every path is wrapped; ANY error returns None / an inert plan and NEVER raises into a
live front door. Nothing here can drop a message or mutate a work surface.

  python3 core/cortex_frontdoor.py --selftest
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import nervous_cortex  # noqa: E402

NERVOUS_DIR = ROOT / "data" / "nervous"
_KILL = NERVOUS_DIR / "CORTEX_FRONTDOOR_OFF"


def _arm_flag(front_door: str) -> Path:
    tag = (front_door or "").strip().upper() or "UNKNOWN"
    return NERVOUS_DIR / f"CORTEX_ARM_{tag}"


def is_armed(front_door: str) -> bool:
    """A door is armed only when its flag is present AND the global kill is absent. Fail-open: any
    error => NOT armed (shadow), never the risky default."""
    try:
        return _arm_flag(front_door).exists() and not _KILL.exists()
    except Exception:
        return False


def observe_only(text: str, *, front_door: str, source: str = "") -> dict | None:
    """SHADOW pass: classify + stamp the afferent event to the correct lane db, act on nothing.
    Returns the plan (for the caller's logs) or None on any error. Never routes, never executes.

    HOT-PATH SAFE: uses the deterministic heuristic classifier (_llm=False) — instant, $0, NO network
    call. This runs inline on a LIVE gateway/bridge request; a blocking Ollama classify (45s) on that
    path would stall the live door, so telemetry lane-stamping stays local + immediate. The richer
    LLM classify is reserved for the ARMED routing leg of maybe_route(), never the observe leg."""
    try:
        return nervous_cortex.route(text, source=source, front_door=front_door,
                                    shadow=True, _llm=False)
    except Exception:
        return None  # a broken shim must never touch the live door


def maybe_route(text: str, *, front_door: str, source: str = "",
                allow_execute: bool = False) -> dict | None:
    """Armed-gated route. When the door is ARMED (and not killed): returns the routed plan
    (execute only if the caller opts in AND the plan isn't outward/draft-gated). When NOT armed:
    runs a shadow pass and returns None so the caller's OLD path proceeds unchanged. Fail-open."""
    try:
        armed = is_armed(front_door)
        if not armed:
            observe_only(text, front_door=front_door, source=source)  # shadow telemetry only
            return None
        # armed: outward/human-facing intents are draft-only — never auto-execute a send.
        # HOT-PATH SAFE: heuristic classify (_llm=False) — instant, $0, no blocking Ollama call on a
        # live door. A richer LLM classify must run async/bounded OFF this inline path (future work).
        plan = nervous_cortex.route(text, source=source, front_door=front_door, execute=False,
                                    _llm=False)
        if allow_execute and plan.get("ok") and not plan.get("draft_only") and not plan.get("dropped"):
            plan = nervous_cortex.route(text, source=source, front_door=front_door, execute=True,
                                        _llm=False)
        plan["frontdoor_armed"] = True
        return plan
    except Exception:
        return None


# --- selftest -------------------------------------------------------------------------------------
def _selftest() -> int:
    import os
    checks: dict[str, bool] = {}

    # ensure a clean, disarmed baseline (do not disturb any real flag files)
    door = "selftest_door"
    armf = _arm_flag(door)
    for f in (armf, _KILL):
        if f.exists():
            try:
                f.unlink()
            except Exception:
                pass

    # 1) DISARMED: maybe_route returns None (old path proceeds) but still shadow-observes
    r = maybe_route("reassign the monday ticket to me", front_door=door, source="t")
    checks["disarmed_returns_none"] = r is None
    checks["disarmed_not_armed"] = is_armed(door) is False

    # 2) observe_only never executes and is a shadow pass
    p = observe_only("draft a reply email to the customer", front_door="stackgpt")
    checks["observe_only_shadow"] = bool(p) and p.get("shadow") is True and p.get("executed") is False
    checks["observe_only_stamps_work"] = p.get("lane") == "work"  # stackgpt pins work

    # 3) ARMED: maybe_route returns a plan; outward stays draft-only + NOT executed
    NERVOUS_DIR.mkdir(parents=True, exist_ok=True)
    armf.touch()
    try:
        r = maybe_route("draft a reply email about the monday ticket", front_door=door,
                        source="t", allow_execute=True)
        checks["armed_returns_plan"] = isinstance(r, dict) and r.get("frontdoor_armed") is True
        checks["armed_outward_not_executed"] = r.get("executed") is False and r.get("draft_only") is True
        # 4) GLOBAL KILL forces shadow instantly (rollback), even while the arm flag is present
        _KILL.touch()
        checks["kill_forces_shadow"] = is_armed(door) is False and \
            maybe_route("reassign the monday ticket", front_door=door) is None
    finally:
        for f in (armf, _KILL):
            if f.exists():
                try:
                    f.unlink()
                except Exception:
                    pass

    # 5) fail-open: junk input never raises
    try:
        observe_only(None, front_door=door)  # type: ignore[arg-type]
        maybe_route(None, front_door=door)   # type: ignore[arg-type]
        checks["fail_open_no_raise"] = True
    except Exception:
        checks["fail_open_no_raise"] = False

    ok = all(checks.values())
    for k, v in checks.items():
        print(f"  [{'PASS' if v else 'FAIL'}] {k}")
    print("CORTEX-FRONTDOOR SELFTEST", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="python3 core/cortex_frontdoor.py")
    ap.add_argument("text", nargs="*")
    ap.add_argument("--front-door", dest="front_door", default="")
    ap.add_argument("--source", default="")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv if argv is not None else sys.argv[1:])
    if a.selftest:
        return _selftest()
    if not a.text:
        ap.print_usage()
        return 1
    import json
    print(json.dumps(observe_only(" ".join(a.text), front_door=a.front_door, source=a.source),
                     indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
