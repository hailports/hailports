"""Grounding / hallucination guard for work-lane reply drafts.

The 870 failure: a draft attributed "your ~870 estimate" to Ravi, who never said it -- an invented
anchor put in the recipient's mouth. This guard catches that class BEFORE a draft is staged, on
EVERY reply, deterministically.

Two checks, both against a GROUNDING CONTEXT (the inbound thread body + whatever real data the draft
was built from -- SF query results, attached-file facts):

  1. ATTRIBUTION guard (high precision): any "you mentioned / your <X> estimate / you asked for ..."
     claim must have its object actually present in what the recipient wrote. A number or noun
     attributed to them that isn't in their message = a fabricated anchor. THIS is the 870 catch.

  2. NUMBER guard (advisory): any number in the reply that appears in NONE of the sources is flagged
     as possibly ungrounded. Real derived figures pass by including the data source in `sources`;
     an invented figure (870) is in no source and surfaces.

check() returns {grounded, attributions, numbers}. The pipeline treats an attribution violation as
BLOCKING (route to Operator, never silent-stage) and number violations as review-flags.
"""

from __future__ import annotations
import re

# a number token: 2,474 / 870 / 19.4% / 3  (not part of an identifier like SF-3413 or a word)
_NUM = re.compile(r'(?<![\w.\-])~?\d[\d,]*(?:\.\d+)?%?')
# the recipient is being credited with having said / wanted / estimated something
_ATTR_VERB = re.compile(
    r'\b(mentioned|said|told\s+me|asked(?:\s+for)?|wanted|requested|noted|flagged|'
    r'estimate[ds]?|estimating|expect(?:ed|ing|ation)?|indicated|suggested|proposed|quoted|'
    r'reported|claimed|figure[ds]?|number\s+you|per\s+your)\b',
    re.I)
_YOU = re.compile(r"\b(your|you|you'?re|u)\b", re.I)


def _norm(n: str) -> str:
    return n.replace(',', '').rstrip('%').rstrip('.').lstrip('~')


def _nums(text: str) -> set:
    return {_norm(m.group(0)) for m in _NUM.finditer(text or '')}


def _tokens(text: str) -> set:
    return {w.lower() for w in re.findall(r"[A-Za-z][A-Za-z']{3,}", text or '')}


def check(reply_text: str, sources) -> dict:
    """reply_text = the NEW prose the reply adds (above the sig/quote). sources = list of ground-truth
    strings (inbound body first). Returns violations; empty lists => grounded."""
    reply = reply_text or ''
    src = " \n ".join(str(s or '') for s in (sources or []))
    src_nums, src_tok = _nums(src), _tokens(src)

    # 1. attributions: for each recipient-credit verb (estimate/mentioned/asked/...), scan a window
    #    spanning ~45 chars BEFORE (the number can precede the noun: "your ~870 estimate") to ~70 after.
    #    If a "you/your" is in that window AND it carries a number absent from every source, the draft is
    #    crediting the recipient with a figure they never gave -- the 870 class. BLOCKING.
    attributions = []
    for m in _ATTR_VERB.finditer(reply):
        window = reply[max(0, m.start() - 45): m.end() + 70]
        if not _YOU.search(window):
            continue
        claim_nums = [w for w in (_norm(x.group(0)) for x in _NUM.finditer(window)) if w and w not in src_nums]
        if claim_nums:
            attributions.append({
                "phrase": window.strip()[:100],
                "ungrounded": claim_nums,
            })

    # 2. loose number guard: numbers in the reply grounded in NO source (years/2024-2027 exempt --
    #    routinely legit period references; single digits <=3 exempt as trivial counts).
    numbers = []
    for m in _NUM.finditer(reply):
        raw, n = m.group(0), _norm(m.group(0))
        if n in src_nums:
            continue
        if re.fullmatch(r'20\d\d', n):  # a plausible year
            continue
        if n.isdigit() and int(n) <= 3:  # "3 files" etc -- trivial count, not a claim
            continue
        numbers.append(raw)

    return {
        "grounded": not attributions,          # attribution = the hard fail
        "attributions": attributions,          # BLOCKING (fabricated anchor)
        "numbers": sorted(set(numbers)),       # advisory review-flag
    }


# ── PRESENTATION GUARD: no AI/marketing traces, no raw links (Operator 2026-07-13) ──────────────────────
_AI_TRACE = re.compile(
    r"\bas requested\b|(?:please )?let me know if you (?:need|require|have|'?d like)|"
    r"anything else (?:we|i) can (?:do|assist|help)|we can do to assist|"
    r"(?:happy|glad|here) to (?:assist|help)\b|this (?:data|report|information) should help|at your service|"
    r"i hope this helps|do not hesitate|feel free to|as an ai|thank you for reaching out|"
    r"let me know if you (?:have any questions|need (?:further|any)|require)|"
    r"(?:if you )?need(?:ing)? (?:further|any(?: further)?) (?:assistance|help|details|information)|"
    r"for any (?:further )?(?:questions|assistance)|please (?:review|find|see) (?:and|the )|"
    r"we are here to (?:help|assist)|should you (?:have|require)\b", re.I)


def _strip_anchors(html: str) -> str:
    s = re.sub(r'<a\b[^>]*>.*?</a>', '', html or '', flags=re.I | re.S)   # whole hyperlinks (ok)
    s = re.sub(r'href\s*=\s*"[^"]*"', '', s, flags=re.I)                    # any stray href attr
    s = re.sub(r"href\s*=\s*'[^']*'", '', s, flags=re.I)
    return s


def scrub_presentation(html: str) -> str:
    """Deterministically REMOVE AI/marketing-trace filler and hyperlink any raw URL -- so a good draft
    that merely tacked on 'please let me know if you need anything further' is SAVED (that sentence
    dropped) rather than thrown away. AI traces are almost always standalone trailing <p>/<li> filler."""
    s = html or ""
    def _drop_block(m):
        return "" if _AI_TRACE.search(m.group(0)) else m.group(0)
    s = re.sub(r'<p\b[^>]*>.*?</p>', _drop_block, s, flags=re.I | re.S)
    s = re.sub(r'<li\b[^>]*>.*?</li>', _drop_block, s, flags=re.I | re.S)
    # any raw URL left in visible text -> wrap as a hyperlink (never a bare link)
    def _wrap(m):
        url = m.group(1)
        return f'<a href="{url}">{url}</a>'
    s = re.sub(r'(?<!["\'=])(https?://[^\s<>"\']+)', _wrap, s)
    return re.sub(r'\n{3,}', '\n\n', s).strip()


def presentation_check(html: str) -> dict:
    """Deterministic BLOCK-worthy presentation faults: AI/marketing tells + raw (non-hyperlinked) URLs.
    Operator: bots NEVER paste raw links (only formatted <a> hyperlinks) and NEVER read as an AI/marketing list."""
    text = html or ""
    ai = sorted({m.group(0).lower() for m in _AI_TRACE.finditer(text)})
    raw_links = bool(re.search(r'https?://\S', _strip_anchors(text)))  # a URL that isn't inside an <a>
    return {"clean": not ai and not raw_links, "ai_traces": ai, "raw_links": raw_links}


if __name__ == "__main__":
    inbound = ("Could you export case data for 2025 and 2026 YTD? Our goal is the top 20-30 recurring "
               "problems. Thanks, Ravi")
    data = "narrow=879 broad=2474 filled=480 blank=1994 transcripts=110"
    bad = "quick note on your ~870 estimate -- narrow is 879 cases, 861 in 2025."
    good = "you asked for the 2025 + 2026 export -- broad is 2,474 cases, narrow 879."
    print("BAD :", check(bad, [inbound, data]))
    print("GOOD:", check(good, [inbound, data]))
    assert check(bad, [inbound, data])["attributions"], "870 not caught"
    assert check(good, [inbound, data])["grounded"], "grounded draft wrongly flagged"
    print("selftest ok")
