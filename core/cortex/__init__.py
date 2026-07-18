"""cortex — the stack's self-improvement meta-loop.

The stack has ~258 launchd jobs firing single-shot agents, several genuine
closed loops (the copy/volume bandit, seo_evolver, outcome_heal) and a graveyard
of parked self-improvement machinery — but nothing ABOVE them that reads real
outcomes and reallocates the whole machine. cortex is that missing controller.

ONE closed loop, self-paced:

    SENSE     unified machine-state (revenue scoreboard + strategist brief +
              chronic failures + funnel)                          -> cortex.sensors
    DIAGNOSE  a dynamic-workflow fan-out (via scripts/agent_run.sh -> Claude Code
              Workflow) returns a grounded change-set; fail-soft to a free-LLM
              then a deterministic diagnosis                       -> cortex.diagnose
    DECIDE    classify each change: reversible-internal | reversible-code |
              irreversible-outward                                 -> cortex.loop
    ACT       internal via the existing core.strategist_actuators; code via the
              gated, git-isolated cortex.code_actuator; outward staged to
              core.approval_ledger (never auto-sent)               -> cortex.loop
    VERIFY    re-measure next cycle, roll back regressions         -> core.heal_verify
    LEARN     record change -> outcome                             -> core.strategist_memory

Safety: everything is gated in cortex.gates. Master kill = data/hustle/CORTEX_OFF.
Reversible-internal actuation is armed by CORTEX_ENABLED=1; autonomous code edits
by CORTEX_SELF_CODE=1 (and only ever on an isolated path, test+rail-gated, with a
recorded undo). Default posture is a pure dry-run that mutates nothing.
"""
from __future__ import annotations

__all__ = ["gates", "sensors", "diagnose", "code_actuator", "loop", "harness", "portfolio"]

VERSION = "0.1.0"
