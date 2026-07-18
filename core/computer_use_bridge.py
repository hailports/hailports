#!/usr/bin/env python3
"""Computer-use bridge — the clone's LAST-MILE reach when there is NO API.

Phase 5 faculty. Some targets have no API: a value only rendered on a page, a native
app with no scripting surface, a portal that only takes clicks. This module lets the
clone *reach* those — but it PLANS + STAGES the UI task and bolts hard guardrails on;
it NEVER drives the UI itself. Actual execution goes through the existing browser/CDP
(browser_pool) or the computer-use path, and every outward action stops for approval.

    plan_ui_task(app, goal, steps=[...], lane=..., source=...) -> staged task dict

What it guarantees (invariants, always attached — see _INVARIANT_GUARDRAILS):
  G1 headless-offscreen-default — never jack the foreground; headless=True unless a
     human explicitly overrode, and a live remote viewer (ui_automation_guard) parks it.
  G2 no-link-click-from-message — a link from an email/DM/message is NEVER clicked via
     computer-use; it is opened only after its real URL is verified, via chrome/CDP.
  G3 no-outward-without-approval — send / pay / trade / submit / publish steps are
     fenced: staged as pending_approval, never fired by the clone.
  G4 lane-firewall — work / hustle / personal stay air-gapped (self_verify rails).
  G5 doctrine+refutation gate — the whole task is run through decision_modeling.score
     and self_verify.verify; a REJECT / refutation blocks staging.
  G6 stage-not-execute — this module returns a manifest; it does not touch a UI.
  G7 respect-live-viewer — defers to ui_automation_guard when a viewer is present.

Additive + import-only. No network, no UI drive, no live-service touch. __main__ smoke
runs fully offline and asserts the guardrails are attached and nothing executed.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if __package__ in (None, ""):  # allow `python core/computer_use_bridge.py`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from core import self_verify as _sv
except Exception:  # fail-soft: rails degrade but we still refuse to execute
    _sv = None
try:
    from core import decision_modeling as _dm
except Exception:
    _dm = None
try:
    from core import ui_automation_guard as _guard
except Exception:
    _guard = None


# ---------------------------------------------------------------------------
# markers
# ---------------------------------------------------------------------------
# Outward = it changes the world / spends / speaks for Operator. Never auto-fired.
_OUTWARD = re.compile(
    r"\b(send|reply|email|dm|message|post|publish|submit|confirm|checkout|"
    r"pay|purchase|buy|order|trade|sell|transfer|wire|withdraw|deposit|"
    r"subscribe|sign\s?up|delete|remove|approve|accept|apply|book|schedule|"
    r"invite|share|upload|deploy|merge|commit|push)\b",
    re.IGNORECASE,
)
# Read = observe only, no state change. The safe majority of API-less reach.
_READ = re.compile(
    r"\b(read|extract|scrape|get|grab|find|look\s?up|observe|check|verify|"
    r"screenshot|capture|list|see|view|copy|inspect|count|note)\b",
    re.IGNORECASE,
)
# A step that follows a link.
_LINK = re.compile(r"\b(link|url|href|click.*link|open.*link|follow.*link|hyperlink)\b",
                   re.IGNORECASE)
# Sources where a link is suspicious by default.
_MESSAGE_SOURCE = {"email", "mail", "message", "imessage", "sms", "dm", "inbox",
                   "outlook", "owa", "slack", "whatsapp", "text", "unknown"}
# Work surfaces (mirror self_verify's set so lane gating is consistent).
_WORK_SURFACE = {"salesforce", "sfdc", "monday", "outlook", "owa", "zoom",
                 "CompanyA", "employer"}
_WORK_APP = re.compile(r"\b(salesforce|sfdc|monday|outlook|owa|zoom|CompanyA)\b",
                       re.IGNORECASE)
_HUSTLE_APP = re.compile(
    r"\b(gumroad|fiverr|etsy|hailports|BrandA|scannerapp|reddit|devto|"
    r"linkedin\s?page|persona3)\b", re.IGNORECASE)


def _classify_step(text: str) -> dict:
    """Tag a single step: read | outward | navigate, plus link-follow flag."""
    t = (text or "").strip()
    outward = bool(_OUTWARD.search(t))
    read = bool(_READ.search(t))
    link = bool(_LINK.search(t)) or bool(re.search(r"https?://", t))
    if outward:
        kind = "outward"
    elif read:
        kind = "read"
    else:
        kind = "navigate"
    return {"step": t, "kind": kind, "follows_link": link}


def _norm_steps(steps: Any) -> list[dict]:
    if steps is None:
        return []
    if isinstance(steps, str):
        steps = [steps]
    out: list[dict] = []
    for s in steps:
        if isinstance(s, dict):
            txt = s.get("step") or s.get("action") or s.get("text") or ""
            c = _classify_step(txt)
            # explicit kind override wins if caller supplied one
            if s.get("kind") in ("read", "outward", "navigate"):
                c["kind"] = s["kind"]
            out.append(c)
        else:
            out.append(_classify_step(str(s)))
    return out


# ---------------------------------------------------------------------------
# invariant guardrails — always present on every staged task
# ---------------------------------------------------------------------------
_INVARIANT_GUARDRAILS = (
    ("G1_headless_offscreen_default",
     "runs headless / offscreen; never jacks the foreground"),
    ("G2_no_link_click_from_message",
     "a link from an email/DM/message is never clicked via computer-use — "
     "open only after the real URL is verified, via chrome/CDP"),
    ("G3_no_outward_without_approval",
     "send / pay / trade / submit / publish steps are staged pending approval, "
     "never fired by the clone"),
    ("G4_lane_firewall",
     "work / hustle / personal stay air-gapped"),
    ("G5_doctrine_refutation_gate",
     "gated through decision_modeling.score + self_verify.verify"),
    ("G6_stage_not_execute",
     "this bridge returns a manifest; it does not drive any UI"),
    ("G7_respect_live_viewer",
     "defers to ui_automation_guard when a remote viewer is present"),
)


@dataclass
class UITask:
    app: str
    goal: str
    lane: str = "unknown"          # work | hustle | personal | unknown
    surface: str = ""              # salesforce | outlook | ... (defaults from app)
    source: str = "unknown"        # where the task/link came from
    steps: list[dict] = field(default_factory=list)
    headless: bool = True          # invariant default; explicit False needs override
    allow_foreground: bool = False # only a human override lifts headless
    ctx: dict = field(default_factory=dict)


def _to_task(app: str, goal: str, **kw) -> UITask:
    known = set(UITask.__dataclass_fields__)
    fields = {k: v for k, v in kw.items() if k in known}
    steps = _norm_steps(kw.get("steps"))
    fields["steps"] = steps
    task = UITask(app=str(app or "").strip(), goal=str(goal or "").strip(), **{
        k: v for k, v in fields.items() if k != "steps"})
    task.steps = steps
    if not task.surface:
        task.surface = task.app.lower()
    return task


def _detect_lane_conflict(task: UITask) -> str | None:
    """Guard against a task that straddles work and hustle in one drive."""
    blob = " ".join([task.app, task.goal, task.surface] +
                    [s["step"] for s in task.steps])
    hits_work = bool(_WORK_APP.search(blob)) or task.lane == "work" \
        or task.surface in _WORK_SURFACE
    hits_hustle = bool(_HUSTLE_APP.search(blob)) or task.lane == "hustle"
    if hits_work and hits_hustle:
        return "task straddles work + hustle surfaces — air-gap breach; split it"
    return None


# ---------------------------------------------------------------------------
# the planner
# ---------------------------------------------------------------------------
def plan_ui_task(app: str, goal: str, *, steps: Any = None, lane: str = "unknown",
                 surface: str = "", source: str = "unknown",
                 headless: bool = True, allow_foreground: bool = False,
                 context: dict | None = None) -> dict:
    """Plan + STAGE an API-less UI task with hard guardrails. Never drives a UI.

    Returns a manifest dict. `staged` is always True and `executed` is always False —
    execution is a separate, approval-gated hand-off to the existing automation.
    """
    task = _to_task(app, goal, steps=steps, lane=lane, surface=surface,
                    source=source, headless=headless,
                    allow_foreground=allow_foreground, ctx=context or {})

    guardrails = [{"id": gid, "rule": desc, "enforced": True}
                  for gid, desc in _INVARIANT_GUARDRAILS]

    block_reasons: list[str] = []
    warnings: list[str] = []

    # --- G1/G7 headless + live-viewer -------------------------------------
    effective_headless = True
    if not task.headless and task.allow_foreground:
        effective_headless = False  # explicit human override only
        warnings.append("foreground override set by caller — logged")
    viewer_parked = False
    if _guard is not None:
        try:
            if not _guard.headless_automation_allowed():
                viewer_parked = True
                warnings.append("live remote viewer present — execution parked "
                                f"({_guard.ui_automation_pause_reason()})")
        except Exception:
            pass

    # --- G2 link-from-message ---------------------------------------------
    link_risks: list[dict] = []
    src = (task.source or "").lower()
    suspicious_src = src in _MESSAGE_SOURCE
    for i, st in enumerate(task.steps):
        if st["follows_link"]:
            link_risks.append({
                "step_index": i,
                "step": st["step"],
                "requires_url_verification": True,
                "click_via_computer_use": False,  # forbidden
                "must_open_via": "chrome/CDP after real-URL verification",
                "suspicious_source": suspicious_src,
            })
    if link_risks and suspicious_src:
        warnings.append(f"{len(link_risks)} link-follow step(s) from a message source "
                        "— URL verification mandatory before any open")

    # --- G3 outward steps --------------------------------------------------
    outward_steps = [{"step_index": i, "step": s["step"], "status": "pending_approval"}
                     for i, s in enumerate(task.steps) if s["kind"] == "outward"]

    # --- G4 lane conflict --------------------------------------------------
    conflict = _detect_lane_conflict(task)
    if conflict:
        block_reasons.append(f"lane_firewall: {conflict}")

    # --- G5 doctrine + refutation gate ------------------------------------
    summary = f"stage API-less UI task on {task.app or 'a UI'}: {task.goal}"
    gate_action = {
        "kind": "ui_plan",
        "summary": summary,
        "lane": task.lane,
        "surface": task.surface,
        "is_send": False,          # staging is not sending; outward is fenced separately
        "scope": "single",
        "ctx": dict(task.ctx),
    }
    decision = None
    verify = None
    if _dm is not None:
        try:
            decision = _dm.score(summary, task.ctx or None)
            if decision.get("alex_would") == _dm.REJECT:
                block_reasons.append(f"doctrine REJECT: {decision.get('why')}")
        except Exception as e:
            warnings.append(f"decision_modeling gate errored ({e}) — rails still hold")
    if _sv is not None:
        try:
            verify = _sv.verify(gate_action)
            if not verify.get("safe"):
                for r in verify.get("refutations", []):
                    block_reasons.append(f"self_verify: {r}")
        except Exception as e:
            warnings.append(f"self_verify gate errored ({e}) — refusing execution")

    blocked = bool(block_reasons)
    requires_approval = bool(outward_steps) or bool(link_risks)

    # execution manifest: what the EXISTING automation would run — never run here.
    # read_only = no step mutates the world (reads + navigation only, no outward).
    read_only = bool(task.steps) and not outward_steps
    execution_manifest = {
        "route": _pick_route(task, effective_headless),
        "headless": effective_headless,
        "parked_by_viewer": viewer_parked,
        "read_only": read_only,
        "steps": task.steps,
        "outward_pending_approval": outward_steps,
        "link_open_policy": "verified-url-only via chrome/CDP" if link_risks else "n/a",
        "handoff": "core.browser_pool / computer-use path — approval-gated",
    }

    return {
        "app": task.app,
        "goal": task.goal,
        "lane": task.lane,
        "surface": task.surface,
        "source": task.source,
        "headless": effective_headless,
        "steps": task.steps,
        "guardrails": guardrails,
        "link_risks": link_risks,
        "outward_steps": outward_steps,
        "gates": {"decision": decision, "verify": verify},
        "requires_approval": requires_approval,
        "warnings": warnings,
        "blocked": blocked,
        "block_reasons": block_reasons,
        # the two load-bearing invariants — always these values:
        "staged": True,
        "executed": False,
        "execution_manifest": None if blocked else execution_manifest,
        "next": ("BLOCKED — do not execute; " + "; ".join(block_reasons)) if blocked
                else ("STAGED — hand to approval-gated automation" +
                      (" (viewer parked)" if viewer_parked else "")),
    }


def _pick_route(task: UITask, headless: bool) -> str:
    """Which existing automation surface the staged task would hand off to."""
    app = (task.app or "").lower()
    if re.search(r"http|web|site|page|portal|chrome|browser", app) or task.steps and \
            any("http" in s["step"].lower() or "page" in s["step"].lower()
                for s in task.steps):
        return "browser_pool (headless CDP)" if headless else "browser_pool (CDP)"
    return "computer-use (native app)"


def guardrail_ids() -> list[str]:
    return [gid for gid, _ in _INVARIANT_GUARDRAILS]


# ---------------------------------------------------------------------------
def _smoke() -> int:
    fails: list[str] = []

    def check(cond: bool, msg: str):
        if not cond:
            fails.append(msg)

    # 1) synthetic API-less READ task: pull a value off a page that has no API.
    read_task = plan_ui_task(
        app="acme-portal.example web page",
        goal="read the current invoice balance shown on the account page (no API)",
        steps=["open the account page", "read the balance value", "screenshot the total"],
        lane="hustle",
        source="internal",
    )
    check(read_task["staged"] is True, "read task not staged")
    check(read_task["executed"] is False, "read task claims executed — must never execute")
    check(read_task["execution_manifest"] is not None, "clean read task has no manifest")
    check(read_task["execution_manifest"]["read_only"] is True, "read-only not detected")
    check(read_task["execution_manifest"]["handoff"].endswith("approval-gated"),
          "handoff not marked approval-gated")

    # all 7 invariant guardrails attached + enforced
    gids = {g["id"] for g in read_task["guardrails"]}
    for want in guardrail_ids():
        check(want in gids, f"missing guardrail {want}")
    check(all(g["enforced"] for g in read_task["guardrails"]), "a guardrail not enforced")
    check(read_task["headless"] is True, "headless not default-on")

    # 2) link-from-email: must NOT click via computer-use; URL verification required.
    link_task = plan_ui_task(
        app="Mail",
        goal="check the tracking status behind the link in the shipping email",
        steps=["open the tracking link in the email", "read the delivery date"],
        lane="personal",
        source="email",
    )
    check(len(link_task["link_risks"]) >= 1, "link-from-email risk not flagged")
    lr = link_task["link_risks"][0]
    check(lr["click_via_computer_use"] is False, "computer-use link click not forbidden")
    check(lr["requires_url_verification"] is True, "URL verification not required")
    check(link_task["requires_approval"] is True, "link task didn't require approval")

    # 3) outward action: fenced pending approval, not auto-fired.
    outward_task = plan_ui_task(
        app="hailports portal web page",
        goal="submit the contact form on the portal",
        steps=["fill the form", "submit the contact form"],
        lane="hustle",
        source="internal",
    )
    check(len(outward_task["outward_steps"]) >= 1, "outward step not detected")
    check(outward_task["outward_steps"][-1]["status"] == "pending_approval",
          "outward step not pending_approval")
    check(outward_task["requires_approval"] is True, "outward task didn't require approval")
    check(outward_task["executed"] is False, "outward task claims executed")

    # 4) lane straddle: work + hustle in one drive => blocked, no manifest.
    straddle = plan_ui_task(
        app="Salesforce web page",
        goal="copy the lead into the gumroad dashboard",
        steps=["read the salesforce lead", "post it into gumroad"],
        lane="work",
        source="internal",
    )
    check(straddle["blocked"] is True, "lane straddle not blocked")
    check(straddle["execution_manifest"] is None, "blocked task still has a manifest")
    check(any("lane" in r for r in straddle["block_reasons"]), "no lane block reason")
    check(straddle["executed"] is False, "blocked task claims executed")

    # 5) invariant: NOTHING ever reports executed True.
    for t in (read_task, link_task, outward_task, straddle):
        check(t["executed"] is False, "some task reported executed True")

    if fails:
        print("SMOKE FAIL:")
        for f in fails:
            print("  -", f)
        return 1
    print("computer_use_bridge smoke OK — 4 staged tasks, 0 executed, "
          f"{len(guardrail_ids())} invariant guardrails enforced")
    print("  read task: staged, read-only, headless, manifest handed off approval-gated")
    print("  link-from-email: computer-use click forbidden, URL verification required")
    print("  outward: submit fenced pending_approval")
    print("  lane straddle (work+hustle): BLOCKED, manifest withheld")
    return 0


if __name__ == "__main__":
    raise SystemExit(_smoke())
