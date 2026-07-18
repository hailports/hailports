"""Content-integrity gate — blocks the most dangerous draft class: a CONFIDENT FALSE claim to a colleague.

The grounding guard (`core.draft_grounding_guard`) catches ONE narrow fabrication: a NUMBER put in the
recipient's mouth (the "870" class). It misses the far more common — and more damaging — self-lies a
7B (or a hurried human) tacks onto a work reply:

  - "I've reviewed the invoices and they look good"   (the review never happened)
  - "I went into prod to make the label fixes"          (never touched prod)
  - an invented MDF technical answer stated as fact     (a spec that isn't in any source)
  - "I'll take the Tavant task" for a task nobody assigned him  (a self-invented commitment)

These are worse than a bad number: they assert Operator DID work he didn't, ANSWER a question he can't,
or ACCEPT a task he never took. Shipped to a colleague they're a credibility hit that's hard to walk
back. This gate is the deterministic, $0, read-only backstop that forces any such draft to needs_alex.

Three flag classes, all checked against the SAME grounding context the drafters already assemble
(inbound thread text + retrieved work-brain/RAG facts + any handler / blast_radius sandbox result):

  1. SELF-COMPLETION / ACTION CLAIMS — a PAST/COMPLETED first-person action ("i've reviewed", "i
     deployed", "went into prod", "it's live", "already handled"). Allowed ONLY when the sources carry
     completion evidence (a validated/passing/deployed/merged result). Ungrounded -> FLAG. Active or
     future work ("im fixing it", "i'll review", "lemme check & get back to you") also FLAGS: do the
     work first, then report the verified result instead of sending a promise.

  2. INVENTED FACTUAL ANSWERS — a definitive technical answer / spec / value / verdict ("they look
     good", "that'll work", "it maps to the co-op field", "the value is 3%") whose specifics aren't in
     the sources. FLAG.

  3. SELF-ASSIGNED TASKS — a commitment to OWN/TAKE a task whose distinctive subject the inbound never
     established. "i'll take the Tavant migration" when Tavant is nowhere in the thread = self-invented.
     ("i can pick that up" for a task the inbound DID raise stays clean — pronoun object, grounded.)

Conservative by design: when a claim's grounding is ambiguous, FLAG (needs_alex) rather than pass.

    check(draft_text, sources) -> {"clean": bool, "violations": [ {category, phrase, why} ], "kind": str}

`sources` = the same list the grounding guard takes (inbound body first, then RAG facts / handler
results). Empty violations => clean. A caller treats `clean is False` exactly like a grounding fail:
force needs_alex, never auto-tee / auto-send.

EVIDENCE IS PER-CLAIM AND NEVER SELF-CORROBORATING (finding 6 fix): categories 1 (self-completion) and
2a (verdicts) require actual CORROBORATION, not mere word presence — and a question can never
corroborate its own answer. A colleague asking "confirmed you deployed this?" contains the literal
word "confirmed"; if that word is credited as completion evidence, a fabricated "yep i deployed it"
sails through ungrounded. So for these two categories `sources` should be split into inbound vs.
evidence and only NON-inbound evidence lines are eligible to corroborate, AND a line only counts if it
shares a token with the specific claim's sentence (no global blob credit — an unrelated evidence line
elsewhere in a big RAG dump can't ground an unrelated claim just because a marker word appears in it).

Two ways to mark the inbound so it's excluded (backward compatible — a legacy flat list of plain
strings still works exactly as before, since there's no way to tell inbound from evidence in it):
  1. `sources = (inbound, evidence_sources)` — a 2-item TUPLE (never a list — a legacy flat LIST
     that happens to be `[str, list]` is NOT reinterpreted) whose SECOND item is itself a
     list/tuple. `inbound` may be a single string or a list/tuple of strings.
  2. Per-item tags — any item may be `{"text": .., "inbound": bool}` or `(text, is_inbound_bool)`.
Categories 2b (answer specifics) and 3 (self-assignment) are unaffected: their whole point is that the
INBOUND legitimately grounds a claim ("the inbound raised it") — those keep scanning inbound+evidence.
"""

from __future__ import annotations

import re

# ── shared vocab ──────────────────────────────────────────────────────────────────────────────────
_STOP = frozenset(
    "the a an and or but so then this that these those there here it its it's is are was were be been "
    "being am i'm im i've ive i'll ill you your yours we our us they them their he she his her "
    "for from with without into onto over under about above below after before now soon today "
    "tomorrow yesterday just already yet still also too very really quite pretty much many some any "
    "all both each few more most other such only own same than more will would can could should shall "
    "may might must have has had do does did done get got getting go going gone want wanna gonna lemme "
    "gimme tryna hey hi hello thanks thank thx please sir man brotha amigo friend right good great "
    "sure yep yeah okay ok cool word roger sprint follow report back drop ping look looking take taking "
    "pull grab grabbing dig unblock next update status".split())

_NUM = re.compile(r'(?<![\w.\-])~?\d[\d,]*(?:\.\d+)?%?')
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_/&+-]{3,}")


def _norm_num(n: str) -> str:
    return n.replace(',', '').rstrip('%').rstrip('.').lstrip('~')


def _nums(text: str) -> set:
    return {_norm_num(m.group(0)) for m in _NUM.finditer(text or '')}


def _tokens(text: str) -> set:
    out = set()
    for w in _WORD.findall(text or ''):
        w = w.lower()
        if w not in _STOP:
            out.add(w)
        # split snake_case / slashed identifiers so JSON evidence ("sandbox_result") shares topical
        # tokens with prose claims ("the sandbox validated green")
        for p in re.split(r'[_/]', w):
            if len(p) >= 4 and p not in _STOP:
                out.add(p)
    return out


# ── sources: split inbound (never self-corroborating) from evidence (finding 6) ─────────────────────
def _is_tagged(item) -> bool:
    return isinstance(item, dict) or (
        isinstance(item, (list, tuple)) and len(item) == 2 and isinstance(item[1], bool))


def _untag(item) -> tuple:
    if isinstance(item, dict):
        return str(item.get("text") or item.get("body") or ""), bool(item.get("inbound"))
    if isinstance(item, (list, tuple)) and len(item) == 2 and isinstance(item[1], bool):
        return str(item[0] or ""), bool(item[1])
    return str(item or ""), False


def _to_lines(x) -> list:
    items = x if isinstance(x, (list, tuple)) else [x]
    lines: list = []
    for it in items:
        text, _ = _untag(it) if _is_tagged(it) else (str(it or ""), False)
        lines.extend(ln for ln in re.split(r'[\r\n]+', text) if ln.strip())
    return lines


def _split_sources(sources) -> tuple:
    """(inbound_text, evidence_lines). See module docstring for the two tagging shapes.

    Legacy flat list of plain strings -> inbound_text="" and every item becomes an evidence
    line (can't tell inbound from evidence without a tag, so behavior for un-migrated callers
    is unchanged: nothing here silently regresses them).

    The (inbound, evidence) form must be a TUPLE: a legacy caller passing a flat LIST that
    happens to be exactly [str, list] is NOT silently reinterpreted as the tuple form."""
    if sources is None:
        return "", []
    if (isinstance(sources, tuple) and len(sources) == 2
            and isinstance(sources[1], (list, tuple)) and not isinstance(sources[0], dict)):
        inbound_raw, evidence_raw = sources
        inbound_items = inbound_raw if isinstance(inbound_raw, (list, tuple)) else [inbound_raw]
        return "\n".join(str(t or "") for t in inbound_items), _to_lines(evidence_raw)
    if isinstance(sources, (list, tuple)) and any(_is_tagged(it) for it in sources):
        inbound_bits, evidence_bits = [], []
        for it in sources:
            text, is_inbound = _untag(it)
            (inbound_bits if is_inbound else evidence_bits).append(text)
        return "\n".join(inbound_bits), _to_lines(evidence_bits)
    return "", _to_lines(sources)


# the marker words themselves (union of _COMPLETION_EVIDENCE + _VERDICT_OK vocab). A draft that
# merely PARROTS a marker word ("deployed & validated") must not self-corroborate off it — the
# topical link has to come from a NON-marker token.
_MARKER_TOKENS = frozenset(
    "validated validating passing passed green success successful succeeded deployed merged merge "
    "completed complete verified reconciled reconciles reconcile confirmed committed live went "
    "errors error checks checked matches matched match correct accurate sandbox_result".split())


def _corroborated(claim_sentence: str, evidence_lines: list, marker_re) -> bool:
    """True iff a NON-inbound evidence line matches `marker_re` AND genuinely overlaps the claim's
    own sentence -- per-claim, per-line, never a global blob credit. "Genuinely" = 2+ shared
    tokens of which at least one is a NON-marker token: a parroted marker word can't
    self-corroborate, and a single org-common token ("salesforce") can't ground an unrelated
    claim off unrelated real evidence."""
    claim_tok = _tokens(claim_sentence)
    if not claim_tok:
        return False
    for line in evidence_lines:
        if not (line and marker_re.search(line)):
            continue
        overlap = _tokens(line) & claim_tok
        if len(overlap) >= 2 and (overlap - _MARKER_TOKENS):
            return True
    return False


# ── 1. SELF-COMPLETION / ACTION CLAIMS ──────────────────────────────────────────────────────────────
# PERFECT / PAST first-person action. Perfect ("i've reviewed") + simple past ("i reviewed", "i went")
# are inherently COMPLETED; future forms are handled separately below. Negation ("i haven't reviewed",
# "i've not reviewed") also can't match
# (a "not"/"n't" sits between the pronoun and the participle).
_ACT = (r"reviewed|checked|looked(?:\s+(?:at|over|into|through))?|tested|verified|validated|completed|"
        r"finished|fixed|deployed|pushed|uploaded|updated|configured|merged|implemented|installed|"
        r"resolved|handled|migrated|rebuilt|refactored|patched|wired")
_DID = re.compile(
    r"\b(?:i'?ve|i have|ive)\s+(?:just\s+|already\s+|gone\s+(?:ahead\s+)?(?:and\s+)?)?(?:" + _ACT + r")\b"
    r"|\bi\s+(?:just\s+|already\s+)?(?:" + _ACT + r"|went|ran|made|created|added|removed|changed)\b",
    re.I)
# state assertions of completion
_STATE = re.compile(
    r"\b(?:it'?s|that'?s|this is|it is|everything'?s|everything is|the\s+\w+'?s)\s+(?:all\s+)?"
    r"(?:done|deployed|live|staged|shipped|handled|fixed|complete|completed|ready|configured|merged|"
    r"pushed|updated|uploaded|resolved|taken care of)\b"
    r"|\balready\s+(?:handled|done|deployed|fixed|taken care of|pushed|merged|updated|configured|"
    r"reviewed|checked|shipped|resolved|live)\b"
    r"|\b(?:i'?m|im)\s+(?:already\s+)?working on (?:it|that|this)\b",
    re.I)
# Work-in-progress claims still assert that Operator is actively doing work. They must not reach a
# colleague before that work finishes and produces proof.
_ACTIVE_WORK = re.compile(
    r"\b(?:i\s+am|i'?m|im)\s+(?:currently\s+|now\s+|already\s+)?"
    r"(?:working\s+on|on\s+(?:it|that|this)|taking\s+(?:a\s+)?look|looking\s+into|digging\s+into|"
    r"going\s+through|checking|reviewing|researching|investigating|confirming|following\s+up|pulling|"
    r"testing|validating|handling|correcting|changing|updating|fixing|configuring|assigning|granting|"
    r"deploying|implementing|resetting|remapping|mapping|patching|editing|modifying)\b",
    re.I)
# Work the bot knows how to perform must land before the recipient sees a reply. Future-work promises
# are held internally; a completed, verified result can then be drafted in past tense with evidence.
_FUTURE_ACTION = (
    r"(?:go\s+ahead\s+and\s+)?(?:check|take\s+(?:a\s+)?look|look\s+(?:into|over)|dig\s+into|review|"
    r"research|investigate|confirm|follow\s+up|fix|correct|change|update|configure|assign|grant|deploy|"
    r"implement|reset|remap|map|patch|edit|modify|send|pull|test|validate|handle|take\s+care\s+of|get|"
    r"drop|work|circle\s+back|reach\s+out|let\s+you\s+know|keep\s+you\s+posted|run\s+this\s+down|"
    r"sort\s+(?:it|that|this)\s+out|make\s+(?:the\s+|a\s+|some\s+)?(?:update|change|fix|edit)s?)\b"
)
_FUTURE_WORK = re.compile(
    r"\b(?:i'?ll|i\s+will|i\s+can|i'?d|i'?m\s+going\s+to|im\s+going\s+to|going\s+to|gonna|"
    r"i\s+(?:still\s+)?need\s+to|lemme)\s+" + _FUTURE_ACTION,
    re.I)
# A subjectless clause is Operator's implied promise ("will send status"). A named subject is factual
# behavior and must remain clean ("Salesforce will send a reset email"). Anchor the bare form to a
# sentence/clause start instead of matching every `will` in the draft.
_BARE_FUTURE_WORK = re.compile(
    r"(?:^|(?<=[.!?;\n])|(?<=--))\s*"
    r"(?:(?:yep|yeah|ok(?:ay)?|sure|thanks|got\s+it|sounds\s+good)\s*[,!:-]?\s*)?"
    r"will\s+" + _FUTURE_ACTION,
    re.I | re.M)
# explicit prod-touch (the "went into prod" class) + "made the ... fixes"
_PROD = re.compile(r"\b(?:went|got|hopped|jumped|logged)\s+(?:in|into)\s+prod(?:uction)?\b"
                   r"|\b(?:in|into)\s+prod(?:uction)?\s+(?:and|to|&)\b", re.I)
_MADE_FIX = re.compile(r"\bi\s+made\s+(?:the\s+|a\s+|some\s+)?[\w\s/&-]{0,28}?"
                       r"(?:fix|fixes|change|changes|edit|edits|tweak|tweaks|correction|corrections)\b", re.I)

# a completed claim is grounded ONLY if the sources carry RESULT-flavored completion evidence — not the
# mere presence of the request verb (an inbound "can you update X" contains "update" but proves nothing).
_COMPLETION_EVIDENCE = re.compile(
    r"\b(validated|passing|passed|green|success(?:ful)?|deployed|merged|completed|reconciled|"
    r"reconcile[ds]?|no\s+errors|errors[:=]\s*0|checks?\s+out|all\s+match(?:ed)?|"
    r"confirmed|committed|is\s+live|went\s+live)\b|✅|"
    r"[\"'](?:applied|write_verified|mutation_verified|update_verified)[\"']\s*:\s*(?:true|True)", re.I)


# ── 2. INVENTED FACTUAL ANSWERS ──────────────────────────────────────────────────────────────────────
# (a) VERDICTS — a positive correctness judgment Operator can only make if he actually checked (and that
#     check must be grounded). Flagged unless the sources corroborate the verdict.
_VERDICT = re.compile(
    r"\b(?:they|it|that|this|those|these|everything|the\s+\w+)\s+(?:all\s+)?(?:look|looks|seem|seems|"
    r"are|is)\s+(?:all\s+)?(?:good|fine|correct|right|ok|okay|solid|clean|accurate)\b"
    r"|\bthat(?:'?ll| will)\s+work\b|\bthat works\b|\bit works\b|\bthese work\b"
    r"|\bno\s+(?:issues|problems|errors|concerns)\b|\ball\s+(?:good|set|correct|clear)\b"
    r"|\bchecks?\s+out\b|\bthat'?s\s+(?:correct|right|fine|good)\b|\blooks?\s+good\b|\blook\s+good\b"
    r"|\beverything\s+(?:checks out|looks good|works|matches)\b",
    re.I)
_VERDICT_OK = re.compile(
    r"\b(validated|passing|passed|verified|reconciled|matches|matched|confirmed|no\s+errors|correct|"
    r"accurate|checks?\s+out|all\s+match(?:ed)?)\b|✅", re.I)

# (b) ANSWER SPECIFICS — a definitive answer that introduces a spec/value/mapping. If the sentence
#     carries specifics (a number or a distinctive token) that appear in NO source, it's invented.
_ANSWER_CUE = re.compile(
    r"\b(?:it\s+maps\s+to|maps\s+to|the\s+mapping\s+is|the\s+answer\s+is|the\s+value\s+is|"
    r"you'?ll\s+want\s+to|you\s+(?:need|have)\s+to|you\s+should|set\s+(?:it|that|the\s+\w+)\s+to|"
    r"the(?:\s+\w+){1,3}\s+(?:is|are|should\s+be)|it'?s\s+set\s+to|configured\s+to|"
    r"the\s+field\s+(?:is|maps)|use\s+the\s+\w+\s+field|the\s+correct\s+(?:value|setting|field)\s+is)\b",
    re.I)


# ── 3. SELF-ASSIGNED TASKS ────────────────────────────────────────────────────────────────────────────
# A commitment to OWN/TAKE a task. Clean when its object is a pronoun (refers to the inbound) OR its
# distinctive object tokens are in the sources (the inbound raised it). FLAG only when Operator commits to
# a DISTINCTIVE, ungrounded task — a self-invented assignment.
_COMMIT = re.compile(
    r"\b(?:i'?ll|i\s+will|i\s+can|i'?d|ill)\s+(?:go\s+ahead\s+and\s+)?"
    r"(?:take|own|handle|cover|lead|drive|run|grab|pick\s+up|take\s+care\s+of|take\s+on)\b"
    r"|\bi'?ve\s+(?:got|taken|picked\s+up)\b|\b(?:i'?m|im)\s+(?:taking|owning|on)\b"
    r"|\bcount\s+me\s+in\b|\bput\s+me\s+down\b|\bassign\s+(?:it|that|this|me)\b",
    re.I)


# ── sentence helpers ──────────────────────────────────────────────────────────────────────────────
def _sentence_at(text: str, pos: int) -> str:
    start = max((text.rfind(c, 0, pos) for c in ".!?\n;"), default=-1) + 1
    ends = [text.find(c, pos) for c in ".!?\n;"]
    ends = [e for e in ends if e != -1]
    end = min(ends) if ends else len(text)
    return text[start:end].strip()


def check(draft_text: str, sources) -> dict:
    """Deterministic content-integrity verdict. See module docstring.

    Returns {clean, violations, kind}. Any violation -> clean=False -> caller forces needs_alex.
    Conservative: ambiguous grounding flags rather than passes."""
    reply = draft_text or ""
    # inbound vs. evidence (finding 6): categories 1/2a require real, non-inbound corroboration —
    # a question can never corroborate its own answer. 2b/3 legitimately scan inbound+evidence (the
    # inbound raising a topic IS valid grounding for those), so they use the combined blob below.
    inbound_text, evidence_lines = _split_sources(sources)
    src = (inbound_text + "\n" + "\n".join(evidence_lines)).strip()
    src_tok, src_num = _tokens(src), _nums(src)

    violations: list[dict] = []

    def _add(cat: str, phrase: str, why: str) -> None:
        phrase = re.sub(r"\s+", " ", (phrase or "")).strip()[:120]
        violations.append({"category": cat, "phrase": phrase, "why": why})

    # 1. self-completion / action claims — flagged unless a NON-inbound evidence line, sharing a
    #    token with THIS claim, actually carries completion evidence (per-claim, never global-blob).
    for pat in (_DID, _STATE, _PROD, _MADE_FIX):
        for m in pat.finditer(reply):
            sent = _sentence_at(reply, m.start())
            if not _corroborated(sent, evidence_lines, _COMPLETION_EVIDENCE):
                _add("self_completion", sent,
                     "completed/asserted action with no completion evidence in non-inbound sources")

    # An active mutation is intentionally never cleared by read-only `verified` findings. The reply
    # waits until the mutation is complete, then reports the verified past-tense result instead.
    for m in _ACTIVE_WORK.finditer(reply):
        _add("self_action", _sentence_at(reply, m.start()),
             "claims work is in progress before a completed, verified mutation exists")
    for pat in (_FUTURE_WORK, _BARE_FUTURE_WORK):
        for m in pat.finditer(reply):
            _add("self_action", _sentence_at(reply, m.start()),
                 "promises future work instead of reporting a completed, verified result")

    # 2a. verdicts — flagged unless a NON-inbound evidence line corroborates THIS verdict
    for m in _VERDICT.finditer(reply):
        sent = _sentence_at(reply, m.start())
        if not _corroborated(sent, evidence_lines, _VERDICT_OK):
            _add("invented_answer", sent,
                 "correctness verdict not corroborated by non-inbound sources")

    # 2b. answer specifics — a definitive answer that introduces ungrounded numbers/specifics
    for m in _ANSWER_CUE.finditer(reply):
        sent = _sentence_at(reply, m.start())
        ungrounded_specs = sorted(
            ({n for n in _nums(sent) if n not in src_num}
             | {t for t in _tokens(sent) if t not in src_tok}))
        # keep only genuinely distinctive specifics (a number, or a token with a digit / >=5 chars) —
        # short generic words aren't a "spec".
        distinctive = [s for s in ungrounded_specs
                       if any(ch.isdigit() for ch in s) or len(s) >= 5]
        if distinctive:
            _add("invented_answer", sent,
                 f"definitive answer states specifics absent from sources: {distinctive[:5]}")

    # 3. self-assigned tasks — a commitment whose distinctive object the inbound never established
    for m in _COMMIT.finditer(reply):
        tail = reply[m.end(): m.end() + 90]
        # pronoun object ("i can pick that up") -> refers to the inbound -> grounded, skip
        if re.match(r"\s*(?:it|that|this|those|these|them)\b", tail, re.I):
            continue
        obj_tokens = _tokens(tail)
        if not obj_tokens:
            continue  # no concrete object named -> nothing to self-invent
        ungrounded = sorted(t for t in obj_tokens if t not in src_tok)
        if ungrounded and len(ungrounded) == len(obj_tokens):
            # EVERY distinctive object token is absent from sources -> a self-invented task
            _add("self_assignment", _sentence_at(reply, m.start()),
                 f"commits to a task the inbound never established: {ungrounded[:5]}")

    kind = violations[0]["category"] if violations else "clean"
    return {"clean": not violations, "violations": violations, "kind": kind}


# ── selftest ──────────────────────────────────────────────────────────────────────────────────────
def _selftest() -> int:
    print("== content_integrity selftest ($0, deterministic, read-only) ==")

    # the 4 REAL inbox-cleanup examples -> ALL must FLAG
    flag_cases = [
        ("self_completion",
         "yep, i've reviewed the invoices and they look good, all set on my end",
         ["hey can you take a look at the march invoices when you get a sec?"]),
        ("self_completion",
         "went into prod and made the label fixes, it's live now",
         ["the dealer labels are showing wrong on the intake form, can you check?"]),
        ("invented_answer",
         "for MDF, the accrual rate is 3% and it maps to the co-op budget field",
         ["quick q -- how does MDF accrual actually get calculated on our end?"]),
        ("self_assignment",
         "i'll take the Tavant integration ticket and get it wrapped up this sprint",
         ["we still need an owner for the salesforce reporting cleanup, any takers?"]),
        ("self_action",
         "i'll review the invoices and follow up",
         ["can you take a look at the march invoices?"]),
        ("self_action",
         "lemme check on the label issue & get back to you",
         ["the dealer labels are showing wrong on the form"]),
        ("self_action",
         "good q -- lemme dig into the MDF accrual logic and i'll follow up right here",
         ["how does MDF accrual get calculated?"]),
        ("self_action",
         "hey Rich! no full update yet but SF-4821's in the current sprint -- lemme pull the "
         "specifics & i'll follow up right here",
         ["any update on SF-4821? need it before the sandbox push"]),
        ("self_action",
         "will send the verified status once I have it",
         ["can you send the verified status?"]),
    ]
    for want_kind, draft, sources in flag_cases:
        r = check(draft, sources)
        cats = {v["category"] for v in r["violations"]}
        print(f"\n  FLAG expected [{want_kind}]:")
        print(f"    draft: {draft}")
        print(f"    -> clean={r['clean']} kind={r['kind']} cats={sorted(cats)}")
        assert not r["clean"], f"MISS: should have flagged -> {draft}"
        assert want_kind in cats, f"wrong class for: {draft} (got {sorted(cats)})"

    # Grounded assignment acceptance, completed work with evidence, and named third-party behavior
    # remain clean. Future-work promises above are intentionally blocked.
    clean_cases = [
        ("yep i can pick that up",
         ["can you own the salesforce reporting cleanup?"]),
        # grounded completion: sources carry the validated sandbox result -> the completed claim passes
        ("pushed SF-4821 to the partial sandbox & it validated green, want the PR?",
         ["any update on SF-4821?", '{"sandbox_result": {"ok": true, "errors": 0}, "validated": "green"}']),
        ("Salesforce will send a reset email after the password reset",
         ["Salesforce sends a reset email after the password reset"]),
    ]
    for draft, sources in clean_cases:
        r = check(draft, sources)
        print(f"\n  CLEAN expected:")
        print(f"    draft: {draft[:70]}")
        print(f"    -> clean={r['clean']} violations={r['violations']}")
        assert r["clean"], f"FALSE POSITIVE: wrongly flagged -> {draft} :: {r['violations']}"

    # finding 6 regression: an inbound QUESTION that happens to contain a marker word must NOT
    # self-corroborate. (inbound, evidence_sources) tuple form -> ALL must still FLAG.
    finding6_flag_cases = [
        ("self_completion", "confirmed you deployed this?",
         "yep, i deployed it, all confirmed on my end"),
        ("invented_answer", "does that look correct to you?",
         "yep it all looks correct, checks out fine"),
        # per-line topic-overlap: evidence exists + has the marker, but is about a DIFFERENT claim
        # (no shared token with the sentence) -> must not ground it (no blob-wide credit).
        ("self_completion", None,
         "i deployed the new label fix, it's live now"),
    ]
    unrelated_evidence = ["the march invoices are validated and reconciled"]
    for want_kind, inbound, draft in finding6_flag_cases:
        sources = (inbound, unrelated_evidence) if inbound is not None else (None, unrelated_evidence)
        r = check(draft, sources)
        cats = {v["category"] for v in r["violations"]}
        print(f"\n  FINDING-6 FLAG expected [{want_kind}]:")
        print(f"    draft: {draft}")
        print(f"    -> clean={r['clean']} kind={r['kind']} cats={sorted(cats)}")
        assert not r["clean"], f"MISS (inbound/unrelated-evidence self-corroborated): {draft}"
        assert want_kind in cats, f"wrong class for: {draft} (got {sorted(cats)})"

    # finding 6 positive control: genuine NON-inbound, TOPICALLY-OVERLAPPING evidence still clears —
    # the fix tightens, it doesn't turn the gate into a permanent flag.
    r = check("pushed SF-4821 to the partial sandbox & it validated green, want the PR?",
              ("any update on SF-4821?",
               ['{"sandbox_result": {"ok": true, "errors": 0}, "validated": "green"}']))
    print(f"\n  FINDING-6 CLEAN expected (real non-inbound evidence, tuple form):")
    print(f"    -> clean={r['clean']} violations={r['violations']}")
    assert r["clean"], f"FALSE POSITIVE on genuine non-inbound evidence: {r['violations']}"

    # ── _corroborated hardening (marker-echo + single-common-token probes) ─────────────────────────
    # probe C: the draft PARROTS the marker word; the only shared token with the evidence line IS
    # the marker ("validated") -> must NOT self-corroborate -> FLAG.
    r = check("i deployed it & it's validated clean on my end",
              (None, ["the deploy pipeline run validated the config export"]))
    print(f"\n  MARKER-ECHO FLAG expected: clean={r['clean']} kind={r['kind']}")
    assert not r["clean"] and r["kind"] == "self_completion", \
        f"marker-echo self-corroboration must be rejected: {r}"
    # probe E: ONE org-common shared token ("salesforce") + a marker elsewhere in an UNRELATED
    # evidence line must not ground the claim -> FLAG.
    r = check("i deployed the salesforce dashboard fix",
              (None, ["salesforce weekly notes: rebate export validated & reconciled"]))
    print(f"  SINGLE-TOKEN FLAG expected: clean={r['clean']} kind={r['kind']}")
    assert not r["clean"] and r["kind"] == "self_completion", \
        f"single common-token overlap must not corroborate an unrelated claim: {r}"
    # positive control for the hardened rule: evidence sharing 2+ tokens incl. a NON-marker
    # topical token still corroborates -> CLEAN.
    r = check("i deployed the rebate export fix & the sandbox validated green",
              ("did the rebate export go out?",
               ['{"sandbox_result": {"ok": true, "component": "rebate export"}, "validated": "green"}']))
    print(f"  HARDENED-RULE CLEAN expected: clean={r['clean']} violations={r['violations']}")
    assert r["clean"], f"topically-linked real evidence must still corroborate: {r['violations']}"

    # ── _split_sources ambiguity: a legacy flat LIST that happens to be [str, list] is NOT the
    # tuple form (only a real TUPLE is) ────────────────────────────────────────────────────────────
    inb, ev = _split_sources(("the inbound question?", ["an evidence line"]))
    assert inb == "the inbound question?" and ev == ["an evidence line"], "tuple form must split"
    inb, ev = _split_sources(["the inbound question?", ["an evidence line"]])
    assert inb == "" and len(ev) >= 1, "[str, list] LIST must stay legacy-flat, never tuple-split"
    print("  _split_sources: tuple form splits; legacy [str, list] LIST stays flat (ambiguity closed)")

    print("\nselftest ok")
    return 0


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Content-integrity gate (deterministic, $0, read-only)")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--check", metavar="TEXT", help="check a draft string against empty sources")
    args = ap.parse_args()
    if args.check is not None:
        import json
        print(json.dumps(check(args.check, []), indent=2))
        return 0
    return _selftest()


if __name__ == "__main__":
    raise SystemExit(main())
