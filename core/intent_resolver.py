"""Deterministic-first robust intent matcher for the CompanyA work gateway.

WHY THIS EXISTS
---------------
apps/chatgpt_redacted_action.py routes a natural-language work request through a stack of
hand-authored regex alternations (the briefing classifier ~L3614, the calendar guard ~L3436,
the sprint-board guard ~L3628, plus the `_TOOL_ALIASES` squashed-string dict at L5379). Every one
of those is EXACT/substring match against a fixed phrase list. A reworded-but-equivalent question
that shares the MEANING but not the literal trigger phrase misses every branch and drops to
`terminal_fallback` (fuzzy work-context junk or a "needs_detail" clarify) — OR gambles on a cold 3s
local-model call. Proven misses today: "what do i have going on", "give me the lay of the land",
"am i behind on anything", "whats keeping the team busy", "show me today's tickets".

WHAT THIS DOES
--------------
A pure, deterministic (no premium AI) resolver the gateway can call as ONE line right before
`terminal_fallback`. Pipeline: unicode/dash fold -> multiword phrase-synonym collapse -> tokenize ->
light lemma -> synonym expansion to CONCEPT tokens -> weighted concept scoring against a small
intent catalog + a difflib fuzzy phrase bonus. Returns the best intent, a confidence, the concrete
READ tool to run, and a human-readable `why`. Every mapped tool is a READ — the resolver can never
select a write/send (there is a hard assert on that), so wiring it in front of terminal_fallback
adds robustness without touching any fence.

An optional cheap-model escalation hook fires ONLY on true ambiguity (top two intents within a
small margin); by default it is off and the resolver is 100% deterministic.

Repo idiom: `from core.intent_resolver import resolve`. Run this file directly for the smoke test.
"""
from __future__ import annotations

import difflib
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

if __name__ == "__main__" and __package__ is None:  # allow direct execution
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ── normalization ───────────────────────────────────────────────────────────────────────────────
_DASHES = "‐‑‒–—―−­"
_DASH_RE = re.compile("[" + _DASHES + "]")

# Every mapped `tool` MUST be a read. This guards against a catalog edit accidentally wiring a
# mutating tool into a path that sits in front of the send/write fences.
_WRITE_MARKER = re.compile(
    r"update|create|delete|remove|assign|reassign|bulk|send|reply|forward|deploy|approve|reject|"
    r"comment|move|transfer|password|reset|onboard|deactivate|apply_", re.I)


def _fold(text: str) -> str:
    s = (text or "").lower()
    s = _DASH_RE.sub("-", s)
    s = s.replace("'", "").replace("’", "")  # what's -> whats, so phrase tables need no apostrophes
    s = re.sub(r"[^a-z0-9\- ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


# Multiword phrase -> concept tokens. Matched as substrings on the folded text, longest first so a
# specific phrase ("up to speed") wins over its fragments. This is where reworded equivalents get
# their meaning back before single-token scoring.
_PHRASE_CONCEPTS: dict[str, tuple[str, ...]] = {
    "what did i miss": ("BRIEFING",), "did i miss": ("BRIEFING",), "fill me in": ("BRIEFING",),
    "catch me up": ("BRIEFING",), "catch up": ("BRIEFING",), "caught up": ("BRIEFING",),
    "up to speed": ("BRIEFING",), "up to date": ("BRIEFING",), "bring me current": ("BRIEFING",),
    "get me current": ("BRIEFING",), "lay of the land": ("BRIEFING", "STATE"),
    "where do things stand": ("BRIEFING", "STATE"), "where things stand": ("BRIEFING", "STATE"),
    "where do i stand": ("BRIEFING", "STATE"), "state of things": ("BRIEFING", "STATE"),
    "where do we stand": ("BRIEFING", "STATE"), "the lowdown": ("BRIEFING",),
    "going on": ("ACTIVITY",), "goin on": ("ACTIVITY",), "whats up": ("ACTIVITY",),
    "on my plate": ("PLATE",), "on my docket": ("PLATE",), "on my radar": ("PLATE",),
    "on fire": ("PRESSING",), "needs my attention": ("PRESSING",), "need my attention": ("PRESSING",),
    "needs attention": ("PRESSING",), "most pressing": ("PRESSING",), "dropping the ball": ("PRESSING",),
    "drop the ball": ("PRESSING",), "falling behind": ("PRESSING",), "behind on": ("PRESSING",),
    "keeping busy": ("ACTIVITY",), "the team": ("TEAM",), "team working": ("TEAM",),
    "sprint board": ("BOARD",), "the board": ("BOARD",), "in progress": ("INPROGRESS",),
    "dev ready": ("PREP",), "ready for dev": ("PREP",), "handoff ready": ("PREP",),
    "ready to hand": ("PREP",), "the plan": ("PLAN",), "execution order": ("PLAN",),
    "what should we do first": ("PLAN",), "what do we do first": ("PLAN",), "do first": ("PLAN",),
    "new email": ("INBOX", "NEW"), "my inbox": ("INBOX",), "any mail": ("INBOX",),
    "rest of my day": ("CALENDAR", "TIMEWORD"), "my day": ("CALENDAR",), "the rest of the week": ("CALENDAR",),
    "day look": ("CALENDAR",), "day ahead": ("CALENDAR", "TIMEWORD"),
}

# Single-token (post-lemma) -> concept.
_WORD_CONCEPTS: dict[str, str] = {
    "brief": "BRIEFING", "briefing": "BRIEFING", "recap": "BRIEFING", "rundown": "BRIEFING",
    "summary": "BRIEFING", "overview": "BRIEFING", "catchup": "BRIEFING", "sitrep": "BRIEFING",
    "digest": "BRIEFING", "situation": "BRIEFING", "standup": "BRIEFING", "update": "BRIEFING",
    "priority": "PRESSING", "pressing": "PRESSING", "urgent": "PRESSING", "fire": "PRESSING",
    "attention": "PRESSING", "slipping": "PRESSING", "behind": "PRESSING", "blocked": "PRESSING",
    "blocker": "PRESSING",
    "happening": "ACTIVITY", "going": "ACTIVITY",
    "plate": "PLATE", "workload": "PLATE", "docket": "PLATE", "radar": "PLATE",
    "open": "OPEN", "outstanding": "OPEN", "pending": "OPEN", "todo": "OPEN", "task": "OPEN",
    "backlog": "OPEN", "unfinished": "OPEN", "owe": "OPEN",
    "calendar": "CALENDAR", "schedule": "CALENDAR", "agenda": "CALENDAR", "meeting": "CALENDAR",
    "appointment": "CALENDAR",
    "today": "TIMEWORD", "tomorrow": "TIMEWORD", "tonight": "TIMEWORD", "morning": "TIMEWORD",
    "afternoon": "TIMEWORD", "evening": "TIMEWORD",
    "free": "FREEBUSY", "busy": "FREEBUSY", "booked": "FREEBUSY",
    "board": "BOARD", "sprint": "BOARD",
    "team": "TEAM", "everyone": "TEAM", "dev": "TEAM", "developer": "TEAM",
    "progress": "INPROGRESS", "wip": "INPROGRESS",
    "ticket": "TICKET",
    "inbox": "INBOX", "email": "INBOX", "mail": "INBOX", "unread": "INBOX", "message": "INBOX",
    "new": "NEW",
    "prep": "PREP", "ready": "PREP", "handoff": "PREP", "devready": "PREP",
    "plan": "PLAN", "prioritize": "PLAN", "reprioritize": "PLAN", "sequence": "PLAN",
    "status": "STATUS", "latest": "STATUS",
    "owner": "PEOPLE", "own": "PEOPLE", "assignee": "PEOPLE", "assigned": "PEOPLE",
    "handling": "PEOPLE", "responsible": "PEOPLE", "whose": "PEOPLE",
}

_WEEKDAY_RE = re.compile(r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b")

# irregular / collision-prone plurals handled explicitly; everything else gets a light -s/-es strip
_LEMMA: dict[str, str] = {
    "priorities": "priority", "tickets": "ticket", "meetings": "meeting", "emails": "email",
    "messages": "message", "tasks": "task", "todos": "todo", "blockers": "blocker",
    "appointments": "appointment", "developers": "developer", "devs": "dev", "owns": "own",
    "owners": "owner", "assignees": "assignee",
}


def _lemma(tok: str) -> str:
    if tok in _LEMMA:
        return _LEMMA[tok]
    if len(tok) > 4 and tok.endswith("es") and tok[:-2] in _WORD_CONCEPTS:
        return tok[:-2]
    if len(tok) > 3 and tok.endswith("s") and not tok.endswith("ss") and tok[:-1] in _WORD_CONCEPTS:
        return tok[:-1]
    return tok


def concepts(text: str) -> set[str]:
    """Fold -> phrase-collapse -> tokenize -> lemma -> synonym-expand into a set of CONCEPT tokens."""
    folded = _fold(text)
    found: set[str] = set()
    residue = folded
    for phrase, cons in sorted(_PHRASE_CONCEPTS.items(), key=lambda kv: -len(kv[0])):
        if phrase in residue:
            found.update(cons)
            residue = residue.replace(phrase, " ")
    for raw in residue.split():
        c = _WORD_CONCEPTS.get(_lemma(raw))
        if c:
            found.add(c)
    if _WEEKDAY_RE.search(folded):
        found.update(("CALENDAR", "WEEKDAY"))
    return found


# ── intent catalog ────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Intent:
    name: str
    tool: Optional[str]          # concrete READ tool to run, or None if the gateway must bind object context
    signals: dict[str, float]    # concept -> weight
    phrases: tuple[str, ...]     # representative phrasings for the difflib fuzzy bonus
    note: str = ""


_INTENTS: tuple[Intent, ...] = (
    Intent("briefing", "getChiefOfStaffBriefing",
           {"BRIEFING": 3.0, "PRESSING": 2.6, "ACTIVITY": 2.6, "STATE": 2.2, "PLATE": 1.4, "OPEN": 1.2},
           ("what did i miss this week", "catch me up on everything", "whats going on at work",
            "bring me up to speed", "where do things stand", "what needs my attention",
            "give me the rundown", "whats on fire", "what am i behind on", "the lay of the land"),
           "broad cross-system catch-up / priorities"),
    # tool is the no-arg agenda read by DEFAULT; resolve() UPGRADES it to outlook_events_for_date
    # WITH a date_text whenever the request names a concrete non-today day (see _calendar_binding).
    # Never emits outlook_events_for_date with {} (that KeyErrors in the registry).
    Intent("calendar", "outlook_today_agenda",
           {"CALENDAR": 3.0, "FREEBUSY": 2.6, "WEEKDAY": 2.0, "TIMEWORD": 1.3},
           ("whats on my calendar today", "hows my day looking", "what meetings do i have",
            "am i free this afternoon", "whats my schedule tomorrow", "rest of my day"),
           "day / schedule / meetings"),
    Intent("sprint_board", "monday_get_salesforce_sprint_matrix",
           {"BOARD": 3.0, "TEAM": 2.5, "INPROGRESS": 1.8},
           ("show me the sprint board", "whats on the board", "whats the team working on",
            "whats in progress this sprint", "the current sprint board", "whats keeping the team busy"),
           "live Monday sprint board state"),
    Intent("open_items", "getChiefOfStaffBriefing",
           {"OPEN": 3.0, "PLATE": 3.0, "TICKET": 2.5, "PRESSING": 1.0},
           ("everything on my plate", "all my open tickets", "what do i have outstanding",
            "my open action items", "whats still open"),
           "cross-system open items sweep (folds into the chief-of-staff read)"),
    Intent("inbox", "outlook_get_inbox",
           {"INBOX": 3.0, "NEW": 1.6},
           ("any new email", "check my inbox", "whats in my inbox", "unread messages",
            "did i get any email", "any new mail in my inbox"),
           "inbox / unread mail"),
    # label-only (tool=None): `ticket_prep` is NOT in registry.get_all_definitions() — it's a
    # dedicated special-case branch in the gateway (chatgpt_redacted_action ~L7655) that runs UPSTREAM
    # of this resolver. Recognizing the intent keeps a reworded ask labeled correctly, but tool=None
    # means .claimed is False so the front-of-terminal_fallback wiring never tries to registry-execute
    # a non-registered name (which would dead-end). The gateway's own branch already handled it.
    Intent("ticket_prep", None,
           {"PREP": 3.0, "PLAN": 2.5, "TICKET": 1.2},
           ("make this ticket dev ready", "prep the ticket for handoff", "whats the plan",
            "what should we do first", "the execution order", "get this ready for dev"),
           "dev-ready ticket package / the wave plan (gateway special-case, not a registry tool)"),
    # label-only: needs an object/topic the gateway's own classifier already binds. Recognized so a
    # reworded ask returns the right LABEL (tool=None) instead of dead-ending.
    Intent("ticket_status", None,
           {"STATUS": 3.0, "TICKET": 1.5},
           ("whats the status of that ticket", "latest on the ticket", "is the ticket blocked"),
           "status of a specific/topic ticket"),
    Intent("people", None,
           {"PEOPLE": 3.0, "TICKET": 1.0},
           ("who owns this", "whos assigned to that", "who is handling it", "whose ticket is this"),
           "who owns / is assigned"),
)

_INTENT_BY_NAME = {i.name: i for i in _INTENTS}

# Concrete non-today date phrases the calendar tool can parse (mirrors the gateway's own calendar
# guard regex ~L6883). "today"/bare requests are deliberately EXCLUDED so they fall to the no-arg
# agenda tool instead of a date_text lookup.
_CAL_DATE_RE = re.compile(
    r"\b(day after tomorrow|tomorrow|yesterday|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|\d{4}-\d{2}-\d{2})\b")


def _calendar_binding(folded: str) -> tuple[str, dict]:
    """Pick a RUNNABLE (never-error) calendar tool + args from a folded request.

    - A concrete non-today day  -> outlook_events_for_date{"date_text": <phrase>}  (required arg satisfied)
    - bare 'my day' / today / free-busy with no named day -> outlook_today_agenda{}  (no required args)

    This is the fix for the broken wiring: the resolver used to map calendar -> outlook_events_for_date
    and the wiring called it with {}, so the registry KeyError'd on the required date_text and the
    gateway surfaced 'Error executing...' as a successful answer."""
    m = _CAL_DATE_RE.search(folded)
    if m:
        return "outlook_events_for_date", {"date_text": m.group(1)}
    return "outlook_today_agenda", {}


def _bind(it: Intent, folded: str) -> tuple[Optional[str], dict]:
    """Resolve an intent to (tool, tool_input). Only calendar needs a computed argument; every other
    mapped tool is a no-required-arg READ that the registry runs with {}."""
    if it.name == "calendar":
        return _calendar_binding(folded)
    return it.tool, {}

# fail-closed: no mapped tool may look like a write/send
for _i in _INTENTS:
    assert _i.tool is None or not _WRITE_MARKER.search(_i.tool), f"intent {_i.name} maps to non-read tool {_i.tool}"


# ── scoring ───────────────────────────────────────────────────────────────────────────────────
_FUZZY_FLOOR = 0.60     # difflib ratio below this contributes nothing
_FUZZY_WEIGHT = 2.0     # a strong phrase match is worth roughly one strong concept
_SCORE_SATURATE = 4.5   # score -> confidence saturation constant
_CLAIM_THRESHOLD = 0.50  # below this the resolver declines (gateway keeps its own fallbacks)
_AMBIGUOUS_GAP = 0.12    # top-vs-runnerup confidence gap under which we flag ambiguity


@dataclass
class Resolution:
    intent: Optional[str]
    tool: Optional[str]
    confidence: float
    why: str
    method: str
    tool_input: dict = field(default_factory=dict)  # concrete args for `tool` (e.g. calendar date_text)
    matched: list[str] = field(default_factory=list)
    ambiguous: bool = False
    alternatives: list[tuple[str, float]] = field(default_factory=list)

    @property
    def claimed(self) -> bool:
        """True when the resolver is confident enough for the gateway to act on it."""
        return self.intent is not None and self.tool is not None and self.confidence >= _CLAIM_THRESHOLD


def _fuzzy_bonus(folded: str, phrases: tuple[str, ...]) -> tuple[float, str]:
    best, who = 0.0, ""
    for p in phrases:
        r = difflib.SequenceMatcher(None, folded, p).ratio()
        if r > best:
            best, who = r, p
    if best >= _FUZZY_FLOOR:
        return (best - _FUZZY_FLOOR) / (1 - _FUZZY_FLOOR) * _FUZZY_WEIGHT, who
    return 0.0, ""


def _score(text: str) -> list[tuple[Intent, float, list[str], str]]:
    cons = concepts(text)
    folded = _fold(text)
    out = []
    for it in _INTENTS:
        matched = [c for c in it.signals if c in cons]
        cscore = sum(it.signals[c] for c in matched)
        fscore, fphrase = _fuzzy_bonus(folded, it.phrases)
        total = cscore + fscore
        if total <= 0:
            continue
        method = "concept" if cscore >= fscore else "fuzzy"
        if cscore and fscore:
            method = "concept+fuzzy"
        note = ", ".join(matched) + (f"; ~\"{fphrase}\"" if fphrase else "")
        out.append((it, total, matched, f"[{method}] {note}"))
    out.sort(key=lambda r: -r[1])
    return out


def resolve(text: str, escalate: Optional[Callable[[str, list[str]], Optional[str]]] = None) -> Resolution:
    """Best intent for a work request. Deterministic by default. Returns a `Resolution` dataclass.

    escalate: optional cheap-model hook `(text, candidate_intent_names) -> chosen_name|None`, called
    ONLY when the top two intents are within `_AMBIGUOUS_GAP` AND it is actually callable. Never
    invoked for a clear win, so the common path costs zero model tokens. A returned name that isn't a
    candidate is ignored.

    Note on core.openclaw_thread_context._resolve_query: that caller probes this module with
    `resolve(query, prior_turns)` expecting a query-REWRITE string. This resolver returns a Resolution
    (not a str/dict), so the caller's isinstance-guarded extraction falls through to its identity
    fallback — the intended defensive behavior. The `callable(escalate)` guard below ensures passing a
    non-callable second positional (the history list) can never raise.
    """
    ranked = _score(text)
    if not ranked:
        return Resolution(None, None, 0.0, "no known work concepts matched", "none")

    def conf(raw: float) -> float:
        return round(min(1.0, raw / _SCORE_SATURATE), 3)

    folded = _fold(text)
    top_it, top_raw, top_matched, top_why = ranked[0]
    top_conf = conf(top_raw)
    alts = [(it.name, conf(raw)) for it, raw, _, _ in ranked[1:4]]
    second_conf = alts[0][1] if alts else 0.0
    ambiguous = bool(alts) and (top_conf - second_conf) < _AMBIGUOUS_GAP

    if ambiguous and callable(escalate):
        try:
            cands = [top_it.name] + [n for n, _ in alts if (top_conf - dict(alts).get(n, 0)) < _AMBIGUOUS_GAP]
            picked = escalate(text, cands)
        except Exception:
            picked = None
        if picked in _INTENT_BY_NAME:
            it = _INTENT_BY_NAME[picked]
            tool, tool_input = _bind(it, folded)
            return Resolution(it.name, tool, max(top_conf, _CLAIM_THRESHOLD),
                              f"{it.note} (cheap-model tiebreak among {cands})", "escalated",
                              tool_input=tool_input, matched=top_matched, ambiguous=True, alternatives=alts)

    tool, tool_input = _bind(top_it, folded)
    return Resolution(top_it.name, tool, top_conf,
                      f"{top_it.note} :: {top_why}", top_why.split("]")[0].strip("["),
                      tool_input=tool_input, matched=top_matched, ambiguous=ambiguous, alternatives=alts)


def best_tool(text: str) -> Optional[str]:
    """Convenience: the concrete READ tool to run, or None if not confidently resolvable/executable."""
    r = resolve(text)
    return r.tool if r.claimed else None


# ── WIRING (do NOT edit the live gateway runtime; stage this, no daemon restart) ────────────────
# In apps/chatgpt_redacted_action.py, inside run_tool, add ONE guarded call just BEFORE the
# "GUARANTEED TERMINAL CATCH-ALL" block (~L8331) so a reworded read is answered instead of dumped:
#
#     from core.intent_resolver import resolve as _intent_resolve          # top-of-file import
#     _ir = _intent_resolve(body.request_text or "")
#     if _ir.claimed and _ir.tool in registered:                          # read-only, fenced upstream
#         _d = _json_or_text(await registry.execute(_ir.tool, _ir.tool_input, source="chatgpt_work:intent_resolver"))
#         return _brain.ok(_ir.tool, _d if isinstance(_d, str) else json.dumps(_d, default=str))
#
# It sits AFTER every legacy router (so it only catches what they missed) and BEFORE terminal_fallback
# (so misses still degrade gracefully). `.claimed` gates on confidence AND tool!=None; every mapped
# tool is a READ (asserted at import), so this can never reach a write/send fence. Pass
# `_ir.tool_input` (NOT a hardcoded {}) — the calendar intent puts the required date_text there, so a
# `outlook_events_for_date` call is never emitted argument-less (which would KeyError in the registry).
# Label-only intents (ticket_prep/ticket_status/people) have tool=None -> .claimed False -> skipped
# here, leaving them to the gateway's own upstream special-case branches.

if __name__ == "__main__":
    # Smoke test — runs via .venv/bin/python, touches NO live service (no gateway call, no DB read).
    # Two honest checks:
    #   (A) intent MAPPING consistency: each reworded variant collapses to the one expected intent.
    #       This is the only metric we can prove here without a live gateway, so it's the only one
    #       reported — no fabricated "N rescued vs the old regex" number.
    #   (B) calendar RUNNABILITY: every calendar variant emits a tool call that cannot error — either
    #       the no-arg agenda tool, or outlook_events_for_date with a date_text the REAL parser accepts.
    from tools.outlook_calendar_sqlite import _parse_calendar_date_text  # the actual tool-side parser

    # (variant, expected intent). Each group = rewordings of ONE real question.
    cases = [
        ("what's on my plate today", "open_items"),
        ("show me today's tickets", "open_items"),
        ("what do I have going on", "briefing"),
        ("give me the lay of the land", "briefing"),
        ("am i behind on anything", "briefing"),
        ("bring me current on everything", "briefing"),
        ("catch me up", "briefing"),
        ("what did i miss this week", "briefing"),
        ("how's my day looking", "calendar"),
        ("what meetings do i have tomorrow", "calendar"),
        ("am i free friday afternoon", "calendar"),
        ("whats keeping the team busy", "sprint_board"),
        ("what's the team working on right now", "sprint_board"),
        ("show me the sprint board", "sprint_board"),
        ("any new email in my inbox", "inbox"),
        ("did i get any mail", "inbox"),
        ("make this ticket dev ready", "ticket_prep"),
        ("what should we do first", "ticket_prep"),
    ]

    print("=" * 92)
    print("INTENT RESOLVER — (A) mapping consistency on reworded work questions")
    print("=" * 92)
    print(f"{'':3}{'request':<38}{'INTENT':<14}{'TOOL':<26}{'conf':>5}")
    print("-" * 92)
    correct = 0
    for text, expect in cases:
        r = resolve(text)
        ok = (r.intent == expect)          # MAPPING metric — intent, not .claimed (label-only tools are None)
        correct += ok
        tool_disp = (r.tool or "(label-only)") + (f" {r.tool_input}" if r.tool_input else "")
        print(f"{'OK ' if ok else 'XX '}{text:<38.38}{(r.intent or '-'):<14}{tool_disp:<26.26}{r.confidence:>5.2f}")
    print("-" * 92)
    print(f"mapping consistency: {correct}/{len(cases)}")

    # (B) Calendar wiring proof — the bug this change fixes. Every variant must yield a NON-ERROR call.
    print("\n" + "=" * 92)
    print("(B) calendar RUNNABILITY — no variant may emit outlook_events_for_date with a bad/empty date")
    print("=" * 92)
    cal_variants = [
        "how's my day looking", "what's on my calendar today",
        "rest of my day", "am i free this afternoon", "whats my schedule",     # -> no-arg agenda
        "what meetings do i have tomorrow", "am i free friday afternoon",
        "what's on my calendar monday", "any meetings on 2026-07-15",          # -> events_for_date{date_text}
    ]
    cal_ok = 0
    for v in cal_variants:
        r = resolve(v)
        if r.intent != "calendar":
            runnable, detail = False, f"NOT classified calendar (got {r.intent!r}) — bad fixture"
        elif r.tool == "outlook_today_agenda" and r.tool_input == {}:
            runnable, detail = True, "no-arg agenda"
        elif r.tool == "outlook_events_for_date":
            dt = r.tool_input.get("date_text", "")
            parsed = _parse_calendar_date_text(dt) if dt else None
            runnable = bool(dt) and parsed is not None      # REAL parser must accept it (would-error check)
            detail = f"date_text={dt!r} -> {parsed}"
        else:
            runnable, detail = False, f"unexpected tool {r.tool!r}"
        cal_ok += runnable
        print(f"{'OK ' if runnable else 'XX '}{v:<40.40}{(r.tool or '-'):<26}{detail}")
    print("-" * 92)
    print(f"runnable calendar calls: {cal_ok}/{len(cal_variants)}")

    # ambiguity + escalation-hook demo (deterministic default stays off)
    _AMBQ = "what's the status of my open tickets"  # STATUS vs OPEN both saturate -> true tie
    amb = resolve(_AMBQ)
    print(f"\nambiguity demo  -> {_AMBQ!r}: intent={amb.intent} conf={amb.confidence} "
          f"ambiguous={amb.ambiguous} alts={amb.alternatives}")

    def _fake_cheap_model(_t, cands):
        return "open_items" if "open_items" in cands else None
    esc = resolve(_AMBQ, escalate=_fake_cheap_model)
    print(f"with escalate   -> intent={esc.intent} conf={esc.confidence} method={esc.method}")

    # non-callable escalate (the openclaw_thread_context probe passes a history LIST here) must not raise
    _probe = resolve(_AMBQ, escalate=[("You", "hi")])
    print(f"non-callable escalate probe -> intent={_probe.intent} (no exception)  ->  OK")

    # write-safety invariant restated at runtime
    for it in _INTENTS:
        assert it.tool is None or not _WRITE_MARKER.search(it.tool)
    print("write-safety: every mapped tool is a READ  ->  OK")

    assert correct == len(cases), f"mapping regressed: {correct}/{len(cases)}"
    assert cal_ok == len(cal_variants), f"calendar wiring emits an errorable call: {cal_ok}/{len(cal_variants)}"
    print("\nSMOKE TEST PASSED")
