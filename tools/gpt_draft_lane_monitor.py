#!/usr/bin/env python3
"""Last-resort health monitor for the GPT draft-arbitrage lane (core.gpt_draft_lane).

Doctrine (Operator 2026-07-15): the stack must EXHAUST every free self-heal before it ever alerts a human.
So this monitor is SILENT by default. It escalates ONLY at true last resort — when BOTH:
  1) the GPT lane is degraded (codex genuinely failing, not merely the daily cap — cap = healthy), AND
  2) the local fallback (Ollama) is ALSO down,
i.e. drafting is fully dead with no free path left to self-heal. Anything short of that self-heals
(GPT down -> local; local slow -> free cloud; all down but recoverable -> next cycle) and stays quiet.

Run on a short interval (e.g. launchd every ~15m). Prints a one-line status; only calls alert_gateway
on the defcon condition.
"""
from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import gpt_draft_lane  # noqa: E402


def _ollama_up() -> bool:
    try:
        urllib.request.urlopen("http://localhost:11434/api/tags", timeout=4)
        return True
    except Exception:
        return False


def main() -> int:
    h = gpt_draft_lane.health()
    gpt_down = bool(h.get("degraded"))
    local_up = _ollama_up()
    # SELF-HEAL states (stay silent): lane ok, or capped (expected), or gpt down BUT local still carries.
    if not gpt_down or local_up:
        status = "ok" if not gpt_down else "gpt-degraded-but-local-carries (self-healed)"
        print(f"gpt_draft_lane monitor: {status} | health={h} local_ollama={'up' if local_up else 'down'}")
        return 0
    # LAST RESORT: gpt lane degraded AND local ollama down -> drafting has no free path left.
    msg = (f"drafting lane FULLY DOWN: GPT arbitrage degraded ({h}) AND local Ollama unreachable — "
           f"no free self-heal path left. drafts are not generating.")
    print("DEFCON:", msg)
    try:
        from core import alert_gateway
        alert_gateway.page_critical(
            source="gpt_draft_lane_monitor",
            subject="drafting lane fully down (last resort)",
            body=msg,
            issue_key="gpt-draft-lane-fully-down",
        )
    except Exception as e:
        print(f"(alert_gateway unavailable: {e})")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
