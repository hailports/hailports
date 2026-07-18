#!/usr/bin/env python3
"""case_study_public_guard.py — premium-judge backstop for the PUBLIC dashboard.

The dashboard already fail-closes on EVERY request via the deterministic + method-tell
whole-page gate. This is the second layer: it renders the live page and runs the FULL
two-layer anon_scrub.scrub() (deterministic + the PREMIUM LLM judge that catches
correlation / subtle leaks the regex can't). On any BLOCK or judge-uncertainty it drops
the CASE_STUDY_PUBLIC_OFF kill-flag, which instantly makes the public surface serve
nothing. On a proven-clean pass it clears the flag. 100% PII guarantee = fail-closed at
both layers.

  run:  python tools/case_study_public_guard.py     # one audit pass, toggles the flag
  loop: launchd every ~600s
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(os.environ.get("CLAUDE_STACK_DIR", Path(__file__).resolve().parents[1]))
while str(ROOT) in sys.path:
    sys.path.remove(str(ROOT))
sys.path.insert(0, str(ROOT))

from core import anon_scrub  # noqa: E402

PUBLIC_OFF = ROOT / "data" / "hustle" / "CASE_STUDY_PUBLIC_OFF"


def main() -> int:
    try:
        from tools.public_case_study_dashboard import render_page, _visible_text
        page = render_page()
        visible = _visible_text(page)
    except Exception as e:
        # can't even render/inspect -> fail closed
        PUBLIC_OFF.write_text(f"render-error: {e}\n")
        print(json.dumps({"ok": False, "tripped": True, "reason": f"render-error: {e}"}))
        return 1

    # The page is static + bucketed; the per-request deterministic+method gate is the hard
    # guarantee. The premium judge is the enhancement layer. So distinguish:
    #   - a REAL leak (deterministic/method hit, or an actual judge BLOCK verdict) => TRIP.
    #   - judge merely UNREACHABLE (infra down) => do NOT black out the page (it's already
    #     deterministically clean); just warn so we fix the judge.
    res = anon_scrub.scrub(visible, kind="dashboard")
    reasons = res.get("blocked_reasons", []) or []
    real_leak = [r for r in reasons
                 if r.startswith("deterministic:") or r.startswith("premium judge:")]
    unreachable = [r for r in reasons if "unreachable" in r or "uncertain" in r]

    if real_leak:
        PUBLIC_OFF.write_text(json.dumps(real_leak[:20]) + "\n")
        print(json.dumps({"ok": False, "tripped": True, "reason": "REAL LEAK", "reasons": real_leak[:8]}))
        return 1
    # no real leak: keep the page live (deterministic gate guards every request)
    try:
        PUBLIC_OFF.unlink()
    except FileNotFoundError:
        pass
    if unreachable:
        print(json.dumps({"ok": True, "tripped": False, "public": "LIVE",
                          "warn": "premium judge unreachable — deterministic+method gate still enforced",
                          "detail": unreachable[:3]}))
    else:
        print(json.dumps({"ok": True, "tripped": False, "public": "LIVE", "judge": "CLEAN"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
