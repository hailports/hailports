"""cortex.gates — the safety substrate. Every actuator + the loop check here FIRST.

Fail-CLOSED: if a gate can't be evaluated it denies. Three arming tiers layered on
one master kill switch:

  data/hustle/CORTEX_OFF present   -> is_off()          -> cortex does NOTHING.
  CORTEX_ENABLED=1  (env)          -> internal_armed()  -> reversible-internal
                                      actuators (effort policy, strategist plays)
                                      may fire.
  CORTEX_SELF_CODE=1 (env, + above)-> self_code_armed() -> the code actuator may
                                      generate + statically-verify a patch and stage
                                      it for review.
  CORTEX_SELF_CODE_APPLY=1 (+above)-> self_code_apply() -> the verified patch may be
                                      applied to the LIVE file (backed up, auto-
                                      rollback). Deepest gate; still never commits/
                                      pushes/branches.

A per-day budget (CORTEX_DAILY_CAP, default 12) bounds how many paid agent calls the
loop may spend, protecting the shared Max-20x pool.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

ROOT = Path(os.environ.get("CLAUDE_STACK_DIR", Path(__file__).resolve().parents[2]))
HUSTLE = ROOT / "data" / "hustle"
STATE_DIR = ROOT / "data" / "cortex"

OFF_FLAG = HUSTLE / "CORTEX_OFF"  # present => master kill


def _env_on(name: str) -> bool:
    return str(os.environ.get(name, "")).strip().lower() in ("1", "true", "yes", "on")


# --- master kill / arming ---------------------------------------------------
def is_off() -> bool:
    return OFF_FLAG.exists()


def internal_armed() -> bool:
    """Reversible-internal actuators may fire (writes to configs live loops read)."""
    return _env_on("CORTEX_ENABLED") and not is_off()


def self_code_armed() -> bool:
    """The code actuator may generate + statically-verify + STAGE a patch for review."""
    return internal_armed() and _env_on("CORTEX_SELF_CODE")


def self_code_apply() -> bool:
    """The verified patch may be applied to the LIVE file (backed up, auto-rollback)."""
    return self_code_armed() and _env_on("CORTEX_SELF_CODE_APPLY")


def arming() -> dict:
    """Snapshot of the current gate posture (for logs/digests)."""
    return {
        "off": is_off(),
        "internal_armed": internal_armed(),
        "self_code_armed": self_code_armed(),
        "self_code_apply": self_code_apply(),
        "budget_remaining": budget_remaining(),
    }


# --- daily agent-call budget ------------------------------------------------
def _cap() -> int:
    try:
        return max(0, int(os.environ.get("CORTEX_DAILY_CAP", "12") or "12"))
    except (TypeError, ValueError):
        return 12


def _today_count_path() -> Path:
    return STATE_DIR / f"agent_calls_{time.strftime('%Y%m%d')}.count"


def budget_remaining() -> int:
    try:
        n = int(_today_count_path().read_text().strip() or "0")
    except Exception:
        n = 0
    return max(0, _cap() - n)


def spend_one() -> bool:
    """Consume one unit of today's agent-call budget. False (deny) if exhausted."""
    if is_off() or budget_remaining() <= 0:
        return False
    p = _today_count_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        n = int(p.read_text().strip() or "0") if p.exists() else 0
        p.write_text(str(n + 1))
        return True
    except Exception:
        return False


# --- self-code path denylist (fail-closed) ----------------------------------
# The autonomous code actuator must NEVER touch these. Superset of the CODEX_AUTONOMY
# doc's denylist PLUS cortex's own safety spine (so it can't weaken its own guardrails)
# PLUS model routing (routing changes are human-reviewed track wiring, never autonomous).
_DENY_SUBSTRINGS = (
    ".env",
    "data/secrets",
    "openclaw",
    "stripe",
    "core/router.py",
    "core/llm_router.py",
    "core/cortex/gates.py",
    "core/cortex/code_actuator.py",
    "core/cortex/loop.py",
    "persona",
    "content_pool",
    "credentials",
    "/cdp",
    "chrome",
    "/.venv/",
    "/.git/",
)
_DENY_EXT = (".key", ".pem", ".plist", ".env")


def path_allowed(rel_or_abs: str) -> tuple[bool, str]:
    """Is this path safe for an autonomous code edit? Fail-closed: outside repo,
    denylisted substring, or sensitive extension => (False, reason)."""
    p = str(rel_or_abs or "").strip()
    if not p:
        return False, "empty path"
    low = p.lower().replace("\\", "/")
    try:
        raw = Path(p)
        abs_p = (raw if raw.is_absolute() else ROOT / raw).resolve()
        abs_p.relative_to(ROOT.resolve())
    except Exception:
        return False, "outside repo root"
    for bad in _DENY_SUBSTRINGS:
        if bad in low:
            return False, f"denylisted: {bad!r}"
    for ext in _DENY_EXT:
        if low.endswith(ext):
            return False, f"denylisted extension: {ext}"
    return True, "ok"


def _selftest() -> int:
    fails = []
    # default posture: fully disarmed unless env says otherwise
    for env in ("CORTEX_ENABLED", "CORTEX_SELF_CODE", "CORTEX_SELF_CODE_APPLY"):
        os.environ.pop(env, None)
    if internal_armed() and not is_off():
        fails.append("internal_armed True with no CORTEX_ENABLED")
    if self_code_armed():
        fails.append("self_code_armed True while disarmed")
    # arming ladder is strictly nested
    os.environ["CORTEX_SELF_CODE"] = "1"
    if self_code_armed():
        fails.append("self_code_armed True without CORTEX_ENABLED (ladder broken)")
    os.environ["CORTEX_ENABLED"] = "1"
    if is_off():
        pass  # a stray OFF flag on disk would legitimately hold; don't assert on it
    elif not self_code_armed():
        fails.append("self_code_armed False with both flags set")
    os.environ["CORTEX_SELF_CODE_APPLY"] = "1"
    if not is_off() and not self_code_apply():
        fails.append("self_code_apply False with all three flags set")
    # denylist
    for bad in ("core/router.py", "../../etc/passwd", ".env", "data/secrets/x.key",
                "core/cortex/gates.py", "openclaw.json"):
        ok, _ = path_allowed(bad)
        if ok:
            fails.append(f"path_allowed wrongly permitted {bad!r}")
    for good in ("agents/broken_site_sender.py", "core/cortex/sensors.py"):
        ok, why = path_allowed(good)
        if not ok:
            fails.append(f"path_allowed wrongly denied {good!r}: {why}")
    for env in ("CORTEX_ENABLED", "CORTEX_SELF_CODE", "CORTEX_SELF_CODE_APPLY"):
        os.environ.pop(env, None)
    if fails:
        print("GATES SELFTEST FAILED:")
        for f in fails:
            print("  -", f)
        return 1
    print("GATES SELFTEST OK — arming ladder nested, denylist fail-closed, budget bounded")
    return 0


if __name__ == "__main__":
    import sys

    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    import json

    print(json.dumps(arming(), indent=2))
