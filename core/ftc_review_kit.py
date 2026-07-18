#!/usr/bin/env python3
"""FTC Consumer Review Rule Compliance Kit — the $39 deliverable (deterministic, $0, no LLM).

Pairs with core/ftc_review_probe.py: the probe flags prohibited patterns for free; this kit is
what a flagged business buys to FIX them. For a business it writes, under
products_internal/ftc_kits/<slug>/:

  - compliant_review_asks.md   sentiment-neutral review-request templates (email / SMS / in-person /
                               QR card) that go to ALL customers — the compliant replacement for gating
  - disclosure_language.md     ready-to-paste material-connection + insider (employee/owner/family)
                               + incentive disclosures
  - audit_checklist.md         the full 16 CFR Part 465 self-audit (on-page + off-page)
  - remediation_list.md        what to remove/rewrite now (gating copy, 5-star steering,
                               non-disparagement clauses) + a non-disparagement removal note
  - preview.html               watermarked proof-first preview surface (free scan upsell)
  - manifest.json

Two modes off ONE generator (mirrors core/geo_fix_kit):
  watermark=True  -> free proof preview: real templates, visibly stamped, the rest locked.
  watermark=False -> paid full kit: clean, un-watermarked, ready to use.

Everything is general compliance information, explicitly NOT legal advice.

  python3 -m core.ftc_review_kit "Joe's Plumbing" --domain joesplumbing.com
  python3 -m core.ftc_review_kit "Joe's Plumbing" --full     # un-watermarked
"""
from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(os.environ.get("CLAUDE_STACK_DIR", Path(__file__).resolve().parents[1]))
KITS_DIR = ROOT / "products_internal" / "ftc_kits"
WATERMARK = "PREVIEW — unlock the full editable kit for $39 · "
RULE = "FTC 16 CFR Part 465 — Rule on the Use of Consumer Reviews and Testimonials"
NOT_LEGAL = ("This kit is general compliance information for educational purposes only and is NOT "
             "legal advice. Penalties and requirements are set by the FTC; consult a licensed "
             "attorney before relying on it.")


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(s or "").lower()).strip("-")


def kit_dir_for(business: str, *, base: Path = KITS_DIR) -> Path:
    return base / _slug(business)


def build_review_asks(business: str) -> str:
    return f"""# Compliant Review-Request Templates — {business}

> Why these are compliant: every template is sent to **all** customers regardless of how the
> job went, asks for an **honest** review (never "5 stars"), and offers **no** reward tied to a
> positive review. That removes the "only happy customers" gating the {RULE} prohibits.

## 1. Email (after any completed job)
**Subject:** How did we do?

Hi {{first_name}},

Thanks for choosing {business}. We'd genuinely value your honest feedback — good or bad — so we
can keep improving and so other customers know what to expect.

If you have a minute, please share an honest review here: {{review_link}}

Whatever your experience was, we want to hear it.

— The {business} team

## 2. SMS
Hi {{first_name}}, thanks for choosing {business}. We'd appreciate an honest review of how it
went — good or bad: {{review_link}}. Thank you!

## 3. In-person / phone script
"We're always trying to improve. If you have a moment, we'd appreciate an honest review of your
experience — the good and the bad both help us. I can text you the link."

## 4. QR card / receipt line
**Tell us how we really did — honest reviews only.** Scan to leave your feedback: {{review_link}}

---
### Do / Don't
- DO ask every customer; DON'T pre-screen for happy ones.
- DO say "honest review"; DON'T say "5-star", "if you loved it", or "if you're happy".
- DON'T offer a discount/gift/entry **for a positive** review (see disclosure_language.md for the
  narrow, disclosed way incentives are allowed).

*{NOT_LEGAL}*
"""


def build_disclosures(business: str) -> str:
    return f"""# Disclosure Language — {business}

Clear, conspicuous disclosures the {RULE} requires for connected or incentivized reviews.
Paste verbatim; keep them near the review itself, not buried in a footer.

## Insider reviews (employee / owner / family / agent)
Anyone connected to {business} who posts a review MUST disclose it. Use one of:
- "I work at {business}." / "I'm the owner of {business}." / "{business} is my family's business."
- Platform tag: add the relationship in the first line of the review text.

## Material-connection disclosure (gifts, free product, affiliates)
- "{business} gave me this product/service for free in exchange for my honest review."
- "I received [item] from {business}; this is my honest opinion."

## Incentivized reviews — the compliant pattern
Incentives are allowed ONLY when (a) NOT conditioned on the review being positive and (b) the
connection is disclosed. Compliant wording:
- "Leave an honest review — positive or negative — and you'll be entered to win {{prize}}.
  Your entry does not depend on what you say."
- On the review itself: "I was entered into a giveaway for leaving a review."

## What NOT to do (these are violations)
- "Leave a 5-star review and get $X off."  ❌ (conditioned on positivity)
- Offering a reward for a review with no disclosure.  ❌

*{NOT_LEGAL}*
"""


def build_audit_checklist(business: str) -> str:
    return f"""# {RULE}
# Self-Audit Checklist — {business}

## On-page (your own website & forms)
- [ ] No "only happy customers" / "if you loved it" / "if you're satisfied" gating copy anywhere.
- [ ] No request that specifies a star count ("leave us 5 stars").
- [ ] No incentive tied to a *positive* review; any incentive is unconditioned **and** disclosed.
- [ ] No non-disparagement clause in any contract, intake form, or terms.
- [ ] No promise/threat to remove, hide, or penalize negative reviews.
- [ ] Any embedded testimonials are real, current, and not materially misleading.

## Off-page (review platforms & operations)
- [ ] No fake, AI-generated, or purchased reviews on Google / Yelp / Facebook / BBB.
- [ ] Every insider review (employee, owner, family, contractor) discloses the connection.
- [ ] You don't suppress/hide negative reviews on portals you control.
- [ ] Any "review" site or widget you control is not presented as independent.
- [ ] Social proof counts (followers/likes) aren't inflated with bots/fake indicators.

## Process
- [ ] Review-request goes to ALL customers, not a filtered "happy" subset.
- [ ] Staff trained: ask for *honest* reviews; never coach the rating.
- [ ] A written record of your review policy exists (this checklist, dated).

Audited by: ____________________   Date: __________

*{NOT_LEGAL}*
"""


def build_remediation(business: str, findings: list[dict] | None) -> str:
    lines = [f"# Remediation List — {business}", "",
             f"Fix order for {RULE}. Start with anything your free scan flagged.", ""]
    if findings:
        lines.append("## Flagged on your scanned page")
        for f in findings:
            lines.append(f"- **[{f.get('severity', '').upper()}] {f.get('title', '')}** — "
                         f"{f.get('fix', '')}")
            ev = f.get("evidence")
            if ev:
                lines.append(f"  - found: “{ev}”")
        lines.append("")
    lines += [
        "## Standard remediations",
        "1. **Gating copy** — replace 'happy/satisfied customers, leave a review' with the neutral "
        "ask in compliant_review_asks.md. Ask everyone, honestly.",
        "2. **5-star steering** — delete any specific star count from requests, cards, autotexts.",
        "3. **Conditioned incentives** — remove the positivity condition; add the disclosure in "
        "disclosure_language.md, or drop the incentive.",
        "4. **Non-disparagement clauses** — strike them from contracts/terms/intake forms.",
        "5. **Insider reviews** — add the connection disclosure to any employee/owner/family review.",
        "",
        "## Non-disparagement removal note (for existing contracts)",
        "> The non-disparagement / no-negative-review clause in our agreement is removed effective "
        "immediately and will not be enforced. Customers are free to post honest reviews of their "
        "experience.",
        "",
        f"*{NOT_LEGAL}*",
    ]
    return "\n".join(lines) + "\n"


# ----------------------------------------------------------------- preview surface

_PREVIEW_CSS = (
    "body{font-family:-apple-system,Segoe UI,Arial,sans-serif;max-width:720px;margin:0 auto;"
    "padding:26px 18px;line-height:1.55;color:#15171a;background:#fafbfc}"
    ".grade{font-size:3.2rem;font-weight:800;line-height:1}"
    ".f{color:#c62828}.d{color:#ef6c00}.c{color:#f9a825}.b{color:#2e7d32}.a{color:#1b5e20}"
    "pre{background:#0d1117;color:#c9d1d9;padding:14px;border-radius:10px;overflow:auto;"
    "font-size:12.5px;position:relative;white-space:pre-wrap}"
    ".wm{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;"
    "color:rgba(255,255,255,.16);font-weight:800;font-size:20px;transform:rotate(-18deg);pointer-events:none}"
    ".card{background:#fff;border:1px solid #e6e8eb;border-radius:14px;padding:16px 18px;margin:16px 0}"
    ".cta{display:block;text-align:center;background:#111;color:#fff;padding:13px;border-radius:11px;"
    "text-decoration:none;font-weight:700;margin:18px 0}"
    ".disc{background:#fffbe6;border:1px solid #f0d060;border-radius:8px;padding:8px 14px;font-size:13px;margin:14px 0}"
)


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_preview_html(business: str, scan: dict | None = None, *, checkout_url: str = "#") -> str:
    grade = (scan or {}).get("grade", "F")
    findings = (scan or {}).get("findings", []) or []
    n = len(findings)
    if scan and n:
        verdict = (f"<strong>{business}</strong>'s page shows <strong>{n} pattern(s)</strong> the "
                   f"FTC Consumer Review Rule prohibits. Compliance grade: <strong>{grade}</strong>.")
        items = "".join(f"<li><b>{_esc(f.get('title',''))}</b> — found: "
                        f"“{_esc(f.get('evidence',''))}”</li>" for f in findings[:4])
        flagged = f"<div class=card><h3>What was flagged on your page</h3><ul>{items}</ul></div>"
    else:
        verdict = (f"This kit brings <strong>{business}</strong> into line with the FTC Consumer "
                   f"Review Rule before an enforcement letter does.")
        flagged = ""
    asks_preview = build_review_asks(business)
    return f"""<!DOCTYPE html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>{business} — FTC Review Rule Compliance Kit</title>
<style>{_PREVIEW_CSS}</style></head><body>
<p style="color:#777;font-size:13px">FTC CONSUMER REVIEW RULE · COMPLIANCE CHECK</p>
<div class="grade {grade.lower()}">{grade}</div>
<p>{verdict}</p>
{flagged}
<div class="card"><h3>Your fix is already written (preview)</h3>
<p>Here are the compliant, honest review-request templates that replace gating — watermarked until you unlock:</p>
<pre><span class="wm">PREVIEW · $39 UNLOCKS</span>{_esc(asks_preview[:1100])}…</pre>
<p style="font-size:13px;color:#777">The full kit also includes ready-to-paste insider/incentive
disclosures, the complete on-page + off-page audit checklist, and a remediation list (incl. a
non-disparagement removal note) — all editable and un-watermarked.</p></div>
<a class="cta" href="{checkout_url}">Get {business} compliant — $39 Compliance Kit</a>
<p style="text-align:center;font-size:13px;color:#777;margin-top:6px">One-time. Instant delivery.
30-day money-back guarantee.</p>
<div class="disc">{NOT_LEGAL}</div>
</body></html>"""


# ----------------------------------------------------------------- top-level generate

def generate_kit(business: str, *, domain: str = "", scan: dict | None = None,
                 watermark: bool = True, checkout_url: str = "#", base: Path = KITS_DIR) -> dict:
    out = kit_dir_for(business, base=base)
    out.mkdir(parents=True, exist_ok=True)
    findings = (scan or {}).get("findings") if scan else None

    docs = {
        "compliant_review_asks.md": build_review_asks(business),
        "disclosure_language.md": build_disclosures(business),
        "audit_checklist.md": build_audit_checklist(business),
        "remediation_list.md": build_remediation(business, findings),
    }
    written = []
    if watermark:
        # preview: ship the first doc whole, lock the rest behind a teaser
        first = "compliant_review_asks.md"
        for name, body in docs.items():
            if name == first:
                (out / name).write_text(f"<!-- {WATERMARK}{business} -->\n" + body, encoding="utf-8")
            else:
                (out / name).write_text(
                    f"# [LOCKED] {name}\n\n{WATERMARK}\n\n"
                    f"This personalized FTC-compliance document for {business} is included in the "
                    f"$39 Compliance Kit. Unlock all 4 editable docs.\n", encoding="utf-8")
            written.append(name)
    else:
        for name, body in docs.items():
            (out / name).write_text(body, encoding="utf-8")
            written.append(name)

    files = list(written)
    preview = build_preview_html(business, scan, checkout_url=checkout_url)
    (out / "preview.html").write_text(preview, encoding="utf-8")
    files.append("preview.html")

    manifest = {
        "business": business, "domain": domain, "rule": RULE,
        "watermark": watermark,
        "grade": (scan or {}).get("grade"),
        "findings_count": len(findings or []),
        "files": files,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "kit_type": "preview" if watermark else "full_paid",
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    manifest["path"] = str(out)
    return manifest


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("business")
    ap.add_argument("--domain", default="")
    ap.add_argument("--full", action="store_true", help="un-watermarked paid kit (default = preview)")
    ap.add_argument("--checkout", default="#")
    args = ap.parse_args(argv)

    scan = None
    if args.domain:
        try:
            from core.ftc_review_probe import scan as ftc_scan
            scan = ftc_scan(url=args.domain)
        except Exception as e:  # noqa: BLE001
            print(f"(scan unavailable, building kit without grade context: {e})")

    m = generate_kit(args.business, domain=args.domain, scan=scan,
                     watermark=not args.full, checkout_url=args.checkout)
    print(f"{'FULL' if args.full else 'PREVIEW'} kit -> {m['path']}")
    for f in m["files"]:
        print(f"  - {f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
