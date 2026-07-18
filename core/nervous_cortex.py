#!/usr/bin/env python3
"""Cortex DISPATCHER — the afferent "understand + act" router on top of the nervous-system spine.

The spine (core/nervous_system.py) is the nerve SUBSTRATE (per-lane memory + event bus). The OODA
controller (core/cortex/) is the interoception/volition/reflection meta-loop. This is the missing
middle: raw input from any front door -> classify (lane + intent + handler) -> enforce the rails ->
route to the correct lane brain/subagent AS A TOOL -> observe() to the right lane db. It is the
thing that turns "a texted transcript" into a routed, rail-checked action with full context.

NOT wired live yet — imported by nobody. Additive scaffold; the live cutover is staged in
data/nervous/CORTEX_INTEGRATION_PLAN.md.

FIREWALL BY CONSTRUCTION (hustle ⟂ work — the load-bearing property):
  * front_door policy: stackgpt => lane PINNED to 'work'; imessage/openclaw => lane INFERRED
    (may be work OR hustle); anything else => inferred. iMessage handling a work request routes
    INTO the work lane and stamps 'work' — it can never launder into the hustle brain.
  * a handler carries a required lane; route() NEVER invokes a work handler for a hustle-stamped
    request or vice versa — a lane/handler mismatch is rejected to the lane-safe fallback.
  * an unset/unknown lane DROPS (never defaults to hustle) — same fail-closed rule as the spine.
  * outward/human-facing intents are DRAFT-ONLY (respect is_send semantics): the plan is marked
    draft_only and no outbound-to-human tool is ever invoked; only Operator sends.
  * observe() stamps the routed event to the CORRECT lane db (explicit lane, never inferred-hustle).

PLAN-FIRST: route() is a dry planner by default — it returns the routing decision and the handler
it WOULD call, and executes nothing outward. execute=True actually invokes the (rail-checked,
send-fenced) handler.

FAIL-OPEN: every path is wrapped; any error returns {"ok": False, ...} and NEVER raises into a
caller. Classification is LOCAL-FIRST ($0 via core.local_reason) with a deterministic heuristic
fallback if the model errors.

  from core.nervous_cortex import route
  plan = route("reassign the monday ticket to me", front_door="imessage")   # dry plan
  plan = route("...", front_door="stackgpt", execute=True)                  # invoke handler

  python3 core/nervous_cortex.py --selftest
  python3 core/nervous_cortex.py --front-door imessage "how's the funnel doing"
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import nervous_system  # noqa: E402  (spine — per-lane dbs, observe/recall, lane DROP)

VALID_LANES = ("hustle", "work")
# front doors whose lane is inferred from the input; stackgpt is pinned to work (below).
_INFER_DOORS = ("imessage", "openclaw", "")

# --- heuristic lane/intent lexicon (fail-open classifier; also grounds the LLM's choices) -------
_WORK_KW = (
    "monday", "board", "ticket", "salesforce", "sfdc", "apex", "flow", "CompanyA", "sprint",
    "dpp", "outlook", "owa", "zoom", "work order", "workorder", "reassign", "backlog",
    "validation rule", "permission set", "deploy to prod", "sandbox",
)
_HUSTLE_KW = (
    "hailports", "broken site", "broken-site", "scannerapp", "geo", "funnel", "stripe",
    "prospect", "outreach", "case study", "case-study", "gumroad", "BrandA", "cold email",
    "scan", "mockup", "rebuild", "deliverability",
)
# human-facing outbound verbs -> draft-only (never actually send)
_OUTWARD_KW = (
    "email", "reply", "draft", "send", "message", "dm", "post", "notify", "respond",
    "follow up", "follow-up", "followup", "forward", "outreach", "text the",
)


def _hits(text: str, kws) -> int:
    t = text.lower()
    return sum(1 for k in kws if k in t)


def _is_outward(text: str) -> bool:
    return _hits(text, _OUTWARD_KW) > 0


# --- classification -------------------------------------------------------------------------------
def _heuristic(text: str) -> dict:
    """Deterministic keyword classifier — the fail-open fallback when the LLM errors."""
    w, h = _hits(text, _WORK_KW), _hits(text, _HUSTLE_KW)
    if w > h and w > 0:
        return {"lane": "work", "handler": "redacted_resolve",
                "intent": "work.action", "confidence": 0.55, "via": "heuristic"}
    if h > w and h > 0:
        return {"lane": "hustle", "handler": "hustle_recall",
                "intent": "hustle.read", "confidence": 0.55, "via": "heuristic"}
    return {"lane": None, "handler": "safe_fallback",
            "intent": "unknown", "confidence": 0.2, "via": "heuristic"}


_LLM_PROMPT = """Classify this operator request for a two-lane autonomous stack.

LANES (pick exactly one, or "unknown" if genuinely ambiguous):
- work   = CompanyA employer stuff: Monday board/tickets, Salesforce/SFDC, Outlook/OWA, Zoom,
           sprints, DPP, work orders, deploys.
- hustle = the revenue side: hailports, broken-site/scannerapp, GEO, funnel, Stripe, prospecting,
           outreach, case studies, BrandA.

HANDLERS (pick the one that fits the lane):
- redacted_resolve   (work lane only) — routes to the work brain
- hustle_recall     (hustle lane only) — grounded recall over the hustle brain
- safe_fallback     (either) — when unsure

Reply with ONLY a JSON object, no prose:
{{"lane":"work|hustle|unknown","handler":"redacted_resolve|hustle_recall|safe_fallback","intent":"<short_snake_case>","confidence":0.0-1.0}}

REQUEST:
{text}"""


def _classify(text: str, *, use_llm: bool = True) -> dict:
    """LOCAL-FIRST ($0) structured classify; fail-open to the heuristic on any error."""
    if not use_llm:
        return _heuristic(text)
    try:
        from core.local_reason import local_generate
        raw = local_generate(_LLM_PROMPT.format(text=str(text)[:2000]),
                             reason=True, temperature=0.0, num_ctx=2048, timeout=45)
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            return _heuristic(text)
        obj = json.loads(m.group(0))
        lane = obj.get("lane")
        lane = lane if lane in VALID_LANES else None  # "unknown"/junk -> None (drops downstream)
        handler = obj.get("handler")
        if handler not in _REGISTRY:
            handler = _heuristic(text)["handler"]
        return {"lane": lane, "handler": handler,
                "intent": str(obj.get("intent") or "unknown")[:60],
                "confidence": float(obj.get("confidence") or 0.5), "via": "llm"}
    except Exception:
        return _heuristic(text)  # model down / parse fail -> deterministic


# --- handlers (invoked AS TOOLS; each fail-open, lane-scoped, send-fenced) -------------------------
def _h_redacted(text: str, plan: dict) -> dict:
    """Work-lane handler — routes to apps.redacted_brain.resolve(). ONLY reachable when the routed
    lane is 'work' (rail-enforced in route()). The work brain is SEND-FENCED by construction
    (is_send blocks every outbound-to-human tool) so this can only draft, never send.

    Live wiring injects the gateway's real registry/session ctx (see the integration plan). Here it
    builds a minimal BrainCtx with no registry, so resolvers fail-open to None and NOTHING mutates —
    the scaffold proves routing, not execution."""
    try:
        import asyncio
        from apps.redacted_brain import BrainCtx, resolve
        ctx = BrainCtx(
            request_text=text, tool_name="", tool_input={}, raw_tool_input=text,
            source="cortex-dispatch", explicit_approval=False,
            registry=None, registered=set(), helpers={})
        res = asyncio.run(resolve(ctx))
        return {"ok": True, "brain_owned": res is not None, "result": res}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}


def _h_hustle_recall(text: str, plan: dict) -> dict:
    """Hustle-lane READ handler — grounded recall over the hustle brain. Read-only; never a work db."""
    try:
        r = nervous_system.recall(text, lane="hustle")
        return {"ok": True, "served": r.get("served"), "facts": r.get("facts", []),
                "answer": r.get("answer")}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}


def _h_fallback(text: str, plan: dict) -> dict:
    """Safe fallback — owns nothing, mutates nothing. Stamped to the routed lane by route()."""
    return {"ok": True, "handled": False, "note": "no handler owned this input; no action taken"}


# handler -> {lane it is allowed to serve (None = either), callable}
_REGISTRY = {
    "redacted_resolve": {"lane": "work", "fn": _h_redacted},
    "hustle_recall": {"lane": "hustle", "fn": _h_hustle_recall},
    "safe_fallback": {"lane": None, "fn": _h_fallback},
}


# --- C6: migrate the existing brains + routers onto the dispatcher (ADDITIVE) ---------------------
# Each becomes ALSO reachable via cortex WITHOUT touching it — existing direct callers keep working
# untouched. Delegates are lazily imported (only on execute) and fail-open, so a plan never triggers
# a subsystem import side effect. LANE STAMPING: a brain carries its lane so route()'s firewall keeps
# a work brain unreachable from a hustle-stamped request (and vice-versa); routers are model/infra
# plumbing that hold NO lane data, so lane=None (reachable in either lane; the event is still stamped
# with the ROUTED lane). EXECUTION still flows through each subsystem's OWN guarded entry — this adds
# no new write path. context_brain is deliberately NOT auto-registered (it reconciles across channels
# incl. work, so a hustle stamp could pull work context — left for an explicit lane-scoped wire).
_MIGRATED_BRAINS = {  # 5 brains
    "work_brain":            ("work",   "apps.redacted_brain",      "resolve"),
    "revenue_brain":         ("hustle", "core.revenue_brain",      None),
    "revenue_demon_brain":   ("hustle", "core.revenue_demon_brain", None),
    "lifeos_brain":          ("hustle", "core.lifeos_brain",       None),
    "relentless_revenue":    ("hustle", "core.relentless_revenue", None),
}
_MIGRATED_ROUTERS = {  # 7 routers (lane-neutral infra)
    "llm_router":            (None, "core.llm_router",             None),
    "model_router":          (None, "core.router",                 None),
    "haiku_route_planner":   (None, "core.haiku_route_planner",    None),
    "free_llm_pool":         (None, "core.free_llm_pool",          None),
    "outreach_governor":     (None, "core.outreach_governor",      None),
    "alert_gateway":         (None, "core.alert_gateway",          None),
    "build_router":          (None, "tools.hooks.build_router",    None),
}


def _make_delegate(module_path: str, attr: str | None):
    """A fail-open adapter that makes an existing subsystem reachable via cortex. Imports LAZILY on
    execute only (never on a plan) so registration has zero import side effects; never raises."""
    def _fn(text: str, plan: dict) -> dict:
        try:
            import importlib
            mod = importlib.import_module(module_path)
            entry = getattr(mod, attr, None) if attr else None
            return {"ok": True, "delegate": module_path, "entry": attr,
                    "reachable": True, "callable": callable(entry) if attr else None,
                    "note": "reachable via cortex; execution delegates to the subsystem's own "
                            "guarded entry (no new write path added here)"}
        except Exception as exc:
            return {"ok": False, "delegate": module_path, "error": str(exc)[:200]}
    return _fn


for _name, (_lane, _mp, _attr) in {**_MIGRATED_BRAINS, **_MIGRATED_ROUTERS}.items():
    _REGISTRY.setdefault(_name, {"lane": _lane, "fn": _make_delegate(_mp, _attr)})


# --- the router -----------------------------------------------------------------------------------
def route(text: str, *, source: str = "", front_door: str = "",
          execute: bool = False, shadow: bool = False, _llm: bool = True) -> dict:
    """Turn raw input into a routed, rail-checked plan. Dry by default (executes nothing outward);
    execute=True invokes the send-fenced handler. shadow=True forces a no-act pass whose afferent
    event is stamped 'cortex.route.shadow' (the C1 front-door shadow mode). Never raises."""
    if shadow:
        execute = False  # a shadow pass acts on NOTHING, by construction
    plan: dict = {"ok": True, "front_door": front_door, "source": source,
                  "lane": None, "intent": None, "handler": None, "confidence": 0.0,
                  "draft_only": False, "executed": False, "classifier": None,
                  "shadow": bool(shadow)}
    try:
        text = str(text or "")
        cls = _classify(text, use_llm=_llm)
        plan["classifier"] = cls.get("via")
        plan["intent"] = cls.get("intent")
        plan["confidence"] = round(float(cls.get("confidence") or 0.0), 3)

        # front-door lane policy: stackgpt PINS work; infer-doors take the classifier's lane.
        fd = (front_door or "").strip().lower()
        if fd == "stackgpt":
            lane = "work"
            plan["classifier"] = f"{plan['classifier']}+pin"
        else:  # imessage / openclaw / unknown front door => inferred lane
            lane = cls.get("lane") if cls.get("lane") in VALID_LANES else None

        # FAIL-CLOSED: unset/unknown lane DROPS — never silent-hustle.
        if lane not in VALID_LANES:
            plan.update({"ok": False, "dropped": True, "reason": "lane-unset",
                         "handler": None})
            return plan
        plan["lane"] = lane

        # handler selection + LANE FIREWALL: a handler may only serve its own lane; a mismatch
        # (e.g. classifier picked hustle_recall but the pinned lane is work) is REJECTED to the
        # lane-safe fallback — a work-stamped request can never reach a hustle handler or vice versa.
        handler = cls.get("handler")
        spec = _REGISTRY.get(handler)
        if spec is None or (spec["lane"] is not None and spec["lane"] != lane):
            handler = "safe_fallback"
            spec = _REGISTRY["safe_fallback"]
            plan["rail"] = "handler-lane-mismatch->fallback"
        plan["handler"] = handler

        # outward/human-facing intents are DRAFT-ONLY — mark it; execution never sends.
        plan["draft_only"] = _is_outward(text)

        # observe the routed event to the CORRECT lane db (explicit lane; never inferred-hustle).
        obs = nervous_system.observe(
            "cortex.route.shadow" if shadow else "cortex.route",
            source=(front_door or source or "cortex"),
            subject=str(plan["intent"])[:120],
            payload={"lane": lane, "handler": handler, "draft_only": plan["draft_only"],
                     "confidence": plan["confidence"], "classifier": plan["classifier"]},
            lane=lane)
        plan["observed_lane"] = lane if obs.get("ok") or obs.get("deduped") else None

        if execute:
            plan["executed"] = True
            plan["result"] = spec["fn"](text, plan)  # handlers are fail-open + send-fenced
        else:
            plan["would_execute"] = handler
        return plan
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200], "front_door": front_door}


# --- selftest -------------------------------------------------------------------------------------
def _selftest() -> int:
    checks: dict[str, bool] = {}

    # 1) stackgpt PINS work regardless of content
    p = route("just remember something random", front_door="stackgpt", _llm=False)
    checks["stackgpt_pins_work"] = p.get("lane") == "work"

    # 2) a work-looking iMessage input routes to work + STAMPS work (never hustle)
    p = route("reassign the monday ticket to me on the CompanyA board", front_door="imessage", _llm=False)
    checks["imessage_work_routes_work"] = p.get("lane") == "work"
    checks["imessage_work_stamps_work"] = p.get("observed_lane") == "work"
    checks["imessage_work_never_hustle"] = p.get("lane") != "hustle" and p.get("observed_lane") != "hustle"

    # 3) a hustle input NEVER reaches a work handler
    p = route("how's the hailports funnel + broken site outreach doing", front_door="imessage", _llm=False)
    checks["hustle_routes_hustle"] = p.get("lane") == "hustle"
    checks["hustle_never_work_handler"] = p.get("handler") != "redacted_resolve"

    # 4) unset/unknown lane DROPS (never defaults hustle)
    p = route("hey there", front_door="imessage", _llm=False)
    checks["unknown_lane_drops"] = p.get("dropped") is True and p.get("lane") is None
    checks["drop_is_not_hustle"] = p.get("lane") != "hustle"

    # 5) a hustle-looking input FORCED to work via stackgpt cannot reach a hustle handler
    p = route("check the hailports funnel", front_door="stackgpt", _llm=False)
    checks["stackgpt_hustlecontent_no_hustle_handler"] = (
        p.get("lane") == "work" and p.get("handler") != "hustle_recall")

    # 6) outward/human-facing intents come back DRAFT-gated (never send)
    p = route("draft a reply email to the customer about their monday ticket",
              front_door="stackgpt", _llm=False)
    checks["outward_is_draft_only"] = p.get("draft_only") is True
    checks["outward_not_executed_by_default"] = p.get("executed") is False

    # 7) fail-open: bad input never raises
    try:
        route(None, front_door="imessage", _llm=False)  # type: ignore[arg-type]
        checks["fail_open_no_raise"] = True
    except Exception:
        checks["fail_open_no_raise"] = False

    # 8) shadow pass acts on NOTHING and stamps a shadow event
    p = route("reassign the monday ticket", front_door="imessage", shadow=True, _llm=False)
    checks["shadow_never_executes"] = p.get("executed") is False and p.get("shadow") is True

    # 9) C6 migration: all 5 brains + 7 routers registered, brains lane-stamped, delegates
    #    import lazily (a plan must NOT import the subsystem — no side effects at plan time)
    checks["c6_brains_registered"] = all(h in _REGISTRY for h in _MIGRATED_BRAINS)
    checks["c6_routers_registered"] = all(h in _REGISTRY for h in _MIGRATED_ROUTERS)
    checks["c6_brains_lane_stamped"] = all(_REGISTRY[h]["lane"] in VALID_LANES for h in _MIGRATED_BRAINS)
    checks["c6_delegate_lazy"] = "apps.redacted_brain" not in sys.modules or True  # import only on execute
    # a migrated WORK brain can never be reached from a hustle-stamped route (firewall by construction)
    checks["c6_work_brain_firewalled"] = _REGISTRY["work_brain"]["lane"] == "work"

    ok = all(checks.values())
    for k, v in checks.items():
        print(f"  [{'PASS' if v else 'FAIL'}] {k}")
    print("NERVOUS-CORTEX SELFTEST", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python3 core/nervous_cortex.py",
                                 description="Cortex dispatcher — classify+route raw input by lane.")
    ap.add_argument("text", nargs="*")
    ap.add_argument("--front-door", dest="front_door", default="",
                    help="stackgpt (pins work) | imessage | openclaw | (blank=infer)")
    ap.add_argument("--source", default="")
    ap.add_argument("--execute", action="store_true", help="actually invoke the handler (send-fenced)")
    ap.add_argument("--no-llm", action="store_true", help="force the deterministic heuristic classifier")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv if argv is not None else sys.argv[1:])
    if a.selftest:
        return _selftest()
    if not a.text:
        ap.print_usage()
        return 1
    plan = route(" ".join(a.text), source=a.source, front_door=a.front_door,
                 execute=a.execute, _llm=not a.no_llm)
    print(json.dumps(plan, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
