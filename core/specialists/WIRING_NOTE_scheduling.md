# Wiring note — `scheduling` specialist -> `core/specialist_dispatch.py`

The module `core/specialists/scheduling_availability.py` is built, self-tested green, and $0
(deterministic; no LLM on the happy path). It was NOT wired into `core/specialist_dispatch.py`
because that file was being actively edited by another agent during this build (its REGISTRY gained
`reporting_metrics` + `rebate_portal_domain` mid-session) — editing it concurrently risks a
lost-update collision. Apply these three insertions when the file is free. All are additive.

### 1) REGISTRY — add one entry (anywhere in the dict; e.g. after `"analyst"`)
```python
    "scheduling": {
        "tier": "fast",
        "agent_type": "startup-data-analyst",
        "charter": "meeting-scheduling / availability — read-only free/busy from ZOOM_CALENDAR.md, "
                   "deterministic slot-finder + invite cross-check; delivers an availability reply, "
                   "sending/booking an invite is human-gated (propose only, never auto-creates)",
    },
```

### 2) `classify_ask()` — add a branch right AFTER the SF specialists block (the `sf_access`
return) and BEFORE `# 2) analyst overlay`, so a scheduling ask isn't swallowed by the
analyst/general overlay:
```python
    # 1b) scheduling / availability — meeting free/busy asks (before the analyst overlay so an
    # "are you free / 5 mins? / can you attend / schedule X for Monday" ask isn't miscategorized).
    try:
        from core.specialists import scheduling_availability as _sched
        _sc = _sched.classify(text)
    except Exception:
        _sc = {"is_scheduling": False}
    if _sc.get("is_scheduling"):
        return {"specialist": "scheduling", "category": "scheduling",
                "why": _sc.get("why") or "a scheduling/availability ask", "router": router}
```
(`text` and `low` already exist in `classify_ask`.)

### 3) `dispatch()` fast-path — add one `elif` to the specialist chain (before the `general` else):
```python
    elif specialist == "scheduling":
        from core.specialists import scheduling_availability as _sched
        out, tier = _sched.handle(text, context), "fast"
```

### Verify after wiring
```bash
python3 -m core.specialists.scheduling_availability --selftest      # module proof (green)
python3 - <<'PY'                                                    # end-to-end through dispatch
from core import specialist_dispatch as sd
cal = ("# Zoom Calendar\n"
       "- 2026-07-14 15:30 · Salesforce - Daily Standup · 30m\n"
       "- 2026-07-14 17:00 · DPP Sync · 60m\n")
ctx = {"calendar_md": cal, "now": "2026-07-14T09:00:00-05:00"}
d = sd.dispatch({"last_message": "do you have any time this morning, maybe 30 minutes"}, ctx)
assert d["specialist"] == "scheduling" and d["ran"] and d["tier"] == "fast", d
n = d["draft_material"]["numbers"]
assert n["free_min"] == 150 and n["recompute"]["conservation_ok"] is True, n
assert d["draft_material"]["mode"] == "deliver" and d["needs_alex"] is False
print("OK ->", d["draft_material"]["summary"])
PY
```

### Rails honored
- Read-only free/busy only. No calendar write, no invite send/accept/decline anywhere.
- A "please schedule X" ask returns `mode=propose`, `needs_alex=True`, `staged_artifacts=[]`
  (never a write artifact) and asks before sending — the outward invite is Operator's to send.
- Every number recomputed a second way (conservation: `busy + free == window`); an ask that fails
  reconciliation or has no reachable calendar returns an honest handoff, never a guessed slot.
- No AI/automation trace in draft_material (voice-scrubbed via `core.work_reply_voice`).
