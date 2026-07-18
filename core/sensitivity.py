"""Sensitivity / tone gate — deterministic lexicon for bad-news, HR, conflict, and security triggers.

Shared by every work-lane reply drafter (inbox + zoom). The failure this prevents: a chipper, warm
auto-reply to bad news — "anytime!!" to "we're getting laid off", a breezy ack to "prod's down" or a
harassment complaint. When the INBOUND is sensitive, the reply must be held for Operator (needs_alex),
never auto-teed / auto-sent. A human writes the response to bad news.

    check(text) -> {"sensitive": bool, "kind": str, "terms": [str]}

`kind` is the strongest matched category (bad_news > hr > conflict > security > outage), or "clean".
`terms` are the concrete matched phrases (for logging / the review-queue note). Deterministic, $0,
read-only. Conservative: any hit flips sensitive True.
"""
from __future__ import annotations

import re

# category -> compiled pattern. Ordered by severity for `kind` selection.
_LEXICON: list[tuple[str, re.Pattern[str]]] = [
    ("hr", re.compile(
        r"\b(lay ?offs?|laid off|fired|terminat(?:e|ed|ion)|resign(?:ed|ation|ing)?|"
        r"\bquit\b|\bpip\b|performance (?:plan|improvement)|write[- ]?up|"
        r"harassment|discriminat\w*|grievance|hostile work)\b", re.I)),
    ("conflict", re.compile(
        r"\b(complaint|complain(?:ing|ed)?|escalat\w+|furious|angry|upset|"
        r"not happy|unhappy|frustrat\w+|disappoint\w+|unacceptable|"
        r"this is (?:a )?(?:mess|disaster))\b", re.I)),
    ("legal", re.compile(
        r"\b(lawsuit|legal|sue(?:d|ing)?|litigation|"
        r"attorney|liabilit\w+|subpoena|nda|non[- ]?disclosure)\b", re.I)),
    ("security", re.compile(
        r"\b(breach|breached|data leak|leaked|compromis\w+|exfiltrat\w+|"
        r"ransomware|phish\w*|confidential|pii\b)\b", re.I)),
    ("outage", re.compile(
        r"\b(prod(?:uction)? (?:is )?down|outage|down for everyone|"
        r"everything(?:'s| is) broken|sev[- ]?1|p1 incident|hard down|"
        r"can'?t (?:log ?in|access) (?:anything|everything))\b", re.I)),
]


def check(text: str) -> dict:
    t = str(text or "")
    hits: list[tuple[str, str]] = []
    for kind, pat in _LEXICON:
        for m in pat.finditer(t):
            hits.append((kind, m.group(0).strip().lower()))
    if not hits:
        return {"sensitive": False, "kind": "clean", "terms": []}
    # kind = the first (highest-severity) category that matched
    order = [k for k, _ in _LEXICON]
    kind = min({k for k, _ in hits}, key=order.index)
    terms = sorted({term for _, term in hits})
    return {"sensitive": True, "kind": kind, "terms": terms}


def _selftest() -> int:
    bad = [
        "hey we might be getting laid off next week, heads up",
        "I'm filing a harassment complaint about the standup",
        "prod is down and dealers can't log in",
        "legal wants to talk about the Tavant contract",
        "there's been a data breach, this is confidential",
        "I'm furious, this is unacceptable",
    ]
    clean = [
        "hey any update on SF-4821? need it before the sandbox push",
        "thanks for putting this together!!",
        "can you review the flow when you get a sec?",
        "works for me, send a time",
    ]
    for b in bad:
        r = check(b)
        assert r["sensitive"], f"missed sensitive: {b!r}"
        print(f"  sensitive [{r['kind']:9}] {r['terms']}  <- {b[:48]}")
    for c in clean:
        r = check(c)
        assert not r["sensitive"], f"false positive: {c!r} -> {r}"
    print("sensitivity selftest ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
