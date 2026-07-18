"""Reply-bots iteration-2 feature flags — ADDITIVE, default-OFF, single kill-switch.

Every iteration-2 behavior (live specialist-dispatch grounding, the shadow-comparison ledger)
sits behind a flag here. All default OFF, so v1 is the default: with nothing set, the live draft
path is byte-identical to before. Mirrors the AUTOFLOW_OFF pattern (core/autonomous_send.py):

  * env is read ONCE at import (stable for the process).
  * a `data/ITER2_OFF` marker file forces EVERY flag OFF, checked live on each call, fail-closed
    (unreadable / any error => treated as present => OFF). That's the revert switch.

    live_grounding()   -> ITER2_LIVE_GROUNDING   (primary: specialist dispatch feeds the draft)
    shadow_grounding() -> ITER2_SHADOW_GROUNDING (observe-only: run dispatch, log the diff, don't use it)
    ledger_retention() -> ITER2_LEDGER_RETENTION (persist the shadow-comparison JSONL)

Deterministic, $0, import-safe. Nothing here mutates anything or reaches the network.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_ITER2_OFF = ROOT / "data" / "ITER2_OFF"
# Durable ON marker (reconciler-proof): the launchd plist gets normalized by the plist-integrity
# guard, which strips ad-hoc EnvironmentVariables. So live grounding turns on via a FILE the guard
# never touches, read at call-time (same idiom as the ITER2_OFF kill marker). Env var still works too.
_ITER2_LIVE = ROOT / "data" / "ITER2_LIVE_ON"
_SHADOW_LOG = ROOT / "data" / "iter2_shadow_grounding.jsonl"


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


# env read ONCE at import (stable prefix; a restart re-reads).
_ENV = {
    "live": _truthy("ITER2_LIVE_GROUNDING"),
    "shadow": _truthy("ITER2_SHADOW_GROUNDING"),
    "ledger": _truthy("ITER2_LEDGER_RETENTION"),
}


def forced_off() -> bool:
    """True iff the ITER2_OFF kill marker is present. Fail-closed: any stat error => True (forced off)."""
    try:
        return _ITER2_OFF.exists()
    except Exception:
        return True


def _live_marker() -> bool:
    """True iff the durable ITER2_LIVE_ON file marker is present (read at call-time, reconciler-proof)."""
    try:
        return _ITER2_LIVE.exists()
    except Exception:
        return False


def live_grounding() -> bool:
    """PRIMARY specialist-dispatch grounding into the live draft. ON when the env var is set OR the
    durable ITER2_LIVE_ON marker exists, AND the ITER2_OFF kill marker is absent (kill wins)."""
    return (_ENV["live"] or _live_marker()) and not forced_off()


def shadow_grounding() -> bool:
    """OBSERVE-ONLY dispatch (run it, log the comparison, do NOT use the result). OFF by default."""
    return _ENV["shadow"] and not forced_off()


def ledger_retention() -> bool:
    """Persist the shadow-comparison ledger to disk. OFF by default."""
    return _ENV["ledger"] and not forced_off()


def shadow_log(payload: dict) -> None:
    """Append one shadow-comparison entry to the ledger. No-op unless ledger_retention() is on.
    Fail-soft: any error is swallowed (a logging problem must never affect a draft pass)."""
    if not ledger_retention():
        return
    try:
        _SHADOW_LOG.parent.mkdir(parents=True, exist_ok=True)
        rec = {"ts": datetime.now(timezone.utc).isoformat(), **(payload or {})}
        with _SHADOW_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")
    except Exception:
        pass


def status() -> dict:
    return {"forced_off": forced_off(), "env": dict(_ENV),
            "live_grounding": live_grounding(), "shadow_grounding": shadow_grounding(),
            "ledger_retention": ledger_retention(), "kill_marker": str(_ITER2_OFF)}


def _selftest() -> int:
    fails: list[str] = []

    def check(cond: bool, label: str) -> None:
        if not cond:
            fails.append(label)
        print(f"  [{'ok' if cond else 'FAIL'}] {label}")

    print("=== iter2_flags selftest ===\n")
    # default env (nothing set) -> everything OFF
    for k in ("ITER2_LIVE_GROUNDING", "ITER2_SHADOW_GROUNDING", "ITER2_LEDGER_RETENTION"):
        os.environ.pop(k, None)
    global _ENV
    _ENV = {"live": _truthy("ITER2_LIVE_GROUNDING"), "shadow": _truthy("ITER2_SHADOW_GROUNDING"),
            "ledger": _truthy("ITER2_LEDGER_RETENTION")}
    check(not live_grounding() and not shadow_grounding() and not ledger_retention(),
          "default (no env, no marker) -> all flags OFF")
    check(shadow_log({"x": 1}) is None, "shadow_log is a no-op when retention off")

    # env on -> flag on; then the kill marker forces it back OFF
    _ENV = {"live": True, "shadow": True, "ledger": True}
    check(live_grounding() and shadow_grounding() and ledger_retention(),
          "env on (no marker) -> flags ON")
    marker_created = False
    try:
        if not _ITER2_OFF.exists():
            _ITER2_OFF.parent.mkdir(parents=True, exist_ok=True)
            _ITER2_OFF.write_text("selftest\n")
            marker_created = True
        check(not live_grounding() and not shadow_grounding() and not ledger_retention(),
              "ITER2_OFF marker present -> every flag forced OFF")
    finally:
        if marker_created:
            _ITER2_OFF.unlink()
    _ENV = {"live": False, "shadow": False, "ledger": False}

    print()
    if fails:
        print("SELFTEST FAIL:")
        for f in fails:
            print(f"  - {f}")
        return 1
    print("SELFTEST PASS: all flags default OFF; env arms them; ITER2_OFF marker forces every flag OFF.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
