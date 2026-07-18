#!/usr/bin/env python3
"""FTC Consumer Review Rule compliance probe — deterministic, $0, no LLM.

The 16 CFR Part 465 "Rule on the Use of Consumer Reviews and Testimonials" (final Aug 2024,
enforced 2025) has NO small-business exemption and carries civil penalties up to $53,088 per
violation. On Dec 22 2025 the FTC issued its first-ever warning letters under the rule. The
practices SMBs most often bake into their own website — review *gating* ("only happy customers,
leave us a review"), explicit 5-star steering, sentiment-conditioned incentives, and
non-disparagement / review-suppression clauses — are exactly what the rule prohibits.

This module fetches ONE page (the URL the owner gives us — usually a homepage or a
"leave us a review" page) and deterministically flags those prohibited patterns in the page's
own visible text. It is owner-reproducible: every finding carries the literal snippet, so the
owner can Ctrl-F their own page and see it. No network calls other than fetching that one URL;
no LLM, so it never fabricates a violation it can't point at.

HONESTY / LIMITS — what is NOT claimed:
  - We scan only the page you give us. Fake/AI-written reviews, undisclosed *insider* (employee/
    owner/family) reviews, and bought off-site reviews live off-page and are NOT auto-detectable
    here — they are surfaced as a manual-audit checklist item, never asserted as found.
  - This is general compliance information, NOT legal advice.

  python3 -m core.ftc_review_probe https://example-business.com
  python3 -m core.ftc_review_probe --html "<p>Happy customers, please leave us a 5-star review!</p>"
"""
from __future__ import annotations

import argparse
import html as _html
import json
import re
import sys
import urllib.request

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# severity weights -> deterministic A-F grade
_WEIGHT = {"high": 34, "medium": 16, "low": 6}

# review-solicitation verbs/nouns that anchor a "we are asking for a review" context
_REVIEW = (r"review|rating|rate us|rate your|testimonial|feedback|recommend us|"
           r"leave us|five[\s-]?stars?|5[\s-]?stars?|google review|yelp")

# positive-sentiment condition tokens (the gating signal)
_POSITIVE = (r"happy|satisfied|pleased|delighted|love(?:d|s)?|enjoyed|great experience|"
             r"good experience|positive experience|wonderful|amazing|thrilled|"
             r"5[\s-]?stars?|five[\s-]?stars?|excellent")

# conditional framing ("if you ...", "when you ...", "only ...")
_COND = r"\bif\b|\bwhen\b|\bonly\b|\bprovided\b|\bas long as\b|\bshould you\b"

# incentive tokens
_INCENTIVE = (r"\$\s?\d+|\d+\s?%\s?off|discount|coupon|gift\s?card|free\b|giveaway|"
              r"enter to win|raffle|reward|earn|credit\b|voucher|entry into")

_SENT_SPLIT = re.compile(r"(?<=[.!?;:\n])\s+|<br\s*/?>|•")


def _strip_html(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>", "\n", html)
    html = re.sub(r"(?i)</(p|div|li|h[1-6]|tr)>", "\n", html)
    txt = re.sub(r"<[^>]+>", " ", html)
    txt = _html.unescape(txt)
    return re.sub(r"[ \t\f\v]+", " ", txt)


def _windows(text: str) -> list[str]:
    """Short sentence-ish windows so co-occurrence means 'in the same breath', not page-wide."""
    out = []
    for raw in _SENT_SPLIT.split(text):
        s = raw.strip()
        if 3 <= len(s) <= 320:
            out.append(s)
    return out


def _snip(s: str, n: int = 160) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _has(pattern: str, s: str) -> bool:
    return re.search(pattern, s, re.I) is not None


def scan_text(text: str) -> list[dict]:
    """Deterministic findings from already-extracted visible text. Pure; no I/O."""
    findings: list[dict] = []
    seen: set[str] = set()

    def add(code, severity, title, why, fix, evidence):
        key = (code, _snip(evidence, 80))
        if key in seen:
            return
        seen.add(key)
        findings.append({"code": code, "severity": severity, "title": title,
                         "why": why, "fix": fix, "evidence": _snip(evidence)})

    for w in _windows(text):
        has_review = _has(_REVIEW, w)

        # 1) review GATING — sentiment condition + review ask in the same sentence
        if has_review and _has(_POSITIVE, w) and re.search(_COND, w, re.I):
            add("review_gating", "high",
                "Review gating (soliciting only happy customers)",
                "The Rule prohibits steering only satisfied customers to post reviews; "
                "conditioning a review request on a positive experience is exactly the "
                "'only happy customers' pattern the FTC has called out.",
                "Replace with a neutral ask sent to ALL customers regardless of how the job went.",
                w)
            continue

        # 2) explicit 5-star steering ("give us a 5-star review", "rate us 5 stars")
        if re.search(r"(give|leave|rate|tap|click|select|drop|post)\b[^.?!]{0,40}"
                     r"(a\s+)?(5|five)[\s-]?stars?", w, re.I):
            add("star_steering", "high",
                "Explicit 5-star / positive-rating steering",
                "Asking specifically for 5 stars (rather than an honest rating) steers the "
                "sentiment of reviews, which the Rule treats as manipulating the review pool.",
                "Ask for an honest review or rating — never specify the star count.",
                w)
            continue

        # 3) incentive tied to leaving a review (must be unconditioned on sentiment + disclosed)
        if has_review and _has(_INCENTIVE, w):
            sev = "high" if _has(_POSITIVE, w) else "medium"
            add("incentivized_review", sev,
                "Incentive offered for a review",
                "Incentives are allowed ONLY if not conditioned on the review being positive AND "
                "the material connection is clearly disclosed. An incentive tied to a positive "
                "review, or offered without a disclosure, violates the Rule."
                if sev == "high" else
                "Review incentives are allowed only when not conditioned on a positive review and "
                "when the material connection is clearly and conspicuously disclosed — verify both.",
                "Drop any positivity condition and add a clear 'we offered an incentive' disclosure, "
                "or remove the incentive.",
                w)
            continue

        # 4) non-disparagement / review-suppression clauses
        if (_has(r"non[\s-]?disparag", w)
                or re.search(r"(agree|promise|consent)\b[^.?!]{0,50}not to\b[^.?!]{0,40}"
                             r"(post|leave|write|publish|share)[^.?!]{0,30}"
                             r"(negative|bad|disparag|review)", w, re.I)
                or re.search(r"(we\s+(?:will|can|may|reserve the right to)\s+)?"
                             r"(remove|delete|take[\s-]?down|hide|suppress)\b[^.?!]{0,30}"
                             r"(negative|bad|unfavorable|1[\s-]?star|critical)\b[^.?!]{0,15}review", w, re.I)):
            add("review_suppression", "high",
                "Review suppression / non-disparagement language",
                "The Rule bans contract terms or practices that bar or penalize honest negative "
                "reviews (non-disparagement clauses, threats, or promises to remove negative reviews).",
                "Remove the clause entirely; honest negative reviews are protected.",
                w)
            continue

    return findings


def grade_from_findings(findings: list[dict]) -> tuple[str, int]:
    score = 100 - sum(_WEIGHT.get(f["severity"], 0) for f in findings)
    score = max(0, min(100, score))
    if not findings:
        grade = "A"
    elif score >= 80:
        grade = "B"
    elif score >= 60:
        grade = "C"
    elif score >= 40:
        grade = "D"
    else:
        grade = "F"
    return grade, score


def _fetch(url: str, timeout: float = 10.0) -> str:
    u = url if str(url).startswith("http") else "https://" + str(url).strip().lstrip("/")
    req = urllib.request.Request(u, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read(2_000_000)
    return raw.decode("utf-8", "ignore")


# Always-on manual-audit items (off-page practices we honestly cannot see from one page).
MANUAL_CHECKLIST = [
    "Insider reviews: any review by an employee, owner, family member, or agent must clearly "
    "disclose that connection — these live off-page and aren't auto-detectable.",
    "Fake / AI-written / purchased reviews on Google, Yelp, or Facebook are prohibited and "
    "off-page — confirm none exist for your business.",
    "If you run or control a 'review' site or widget, it must not pose as independent.",
]


def scan(url: str | None = None, html: str | None = None) -> dict:
    """Scan a page (by URL or raw HTML). Never raises; returns a full result dict."""
    fetched = False
    fetch_error = ""
    if html is None:
        try:
            html = _fetch(url or "")
            fetched = True
        except Exception as e:  # noqa: BLE001
            html = ""
            fetch_error = f"{type(e).__name__}: {str(e)[:80]}"
    text = _strip_html(html or "")
    findings = scan_text(text)
    grade, score = grade_from_findings(findings)
    return {
        "url": url or "",
        "fetched": fetched,
        "fetch_error": fetch_error,
        "scanned_chars": len(text),
        "grade": grade,
        "score": score,
        "findings": findings,
        "findings_count": len(findings),
        "manual_checklist": MANUAL_CHECKLIST,
        "rule": "FTC 16 CFR Part 465 — Rule on Consumer Reviews and Testimonials",
        "disclaimer": ("General compliance information only — NOT legal advice. This scan reads "
                       "only the page provided and does not evaluate off-page reviews."),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("url", nargs="?", default="")
    ap.add_argument("--html", default=None, help="scan raw HTML instead of fetching a URL")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if not args.url and args.html is None:
        ap.print_help()
        return 1
    res = scan(url=args.url or None, html=args.html)
    if args.json:
        print(json.dumps(res, indent=2))
        return 0
    print(f"FTC Consumer Review Rule scan — {res['url'] or '(raw html)'}")
    if res["fetch_error"]:
        print(f"  ! fetch failed: {res['fetch_error']}")
    print(f"  GRADE: {res['grade']}   score {res['score']}/100   "
          f"({res['findings_count']} prohibited pattern(s), {res['scanned_chars']} chars scanned)")
    for f in res["findings"]:
        print(f"  [{f['severity']:>6}] {f['title']}")
        print(f"           evidence: “{f['evidence']}”")
    if not res["findings"]:
        print("  No prohibited review patterns found on the scanned page.")
    print(f"  {res['disclaimer']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
