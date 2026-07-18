#!/usr/bin/env python3
"""Message tailoring — weave a prospect PROFILE into a base outreach template, deterministically.

Pairs with core/prospect_enrichment.py. enrich() builds the fact-grounded profile (~90%
deterministic scraping + ONE local-Ollama read); tailor() here is 100% DETERMINISTIC string
assembly ($0, no LLM) that folds those verified facts into the proof-first copy so each message
reads like a neighbor noticed, not a template.

What it weaves in (only verified public-business facts already on the profile):
  - owner_name      -> personal greeting ("Hi Dave,")
  - business_name + city + the vertical NORM ("why it matters now") -> one tailored credibility line
  - review theme    -> a true reputation nod, only if reviews actually surfaced one
  - founded_year / family_owned / years_in_business -> longevity nod, only if verified
The site's specific FAILURE already lives in the base body (the prospect's outreach_angle), so
tailor() reinforces it with the norm angle rather than restating it.

HARD fail-soft contract: tailor(profile, base) with an empty / ICP-blocked / thin profile returns
the base template BYTE-FOR-BYTE unchanged. It preserves the {brand} placeholder, any proof/claim
URLs, and the 80-char subject cap. No fabricated facts: every inserted token comes from the
profile; if a fact isn't present, that clause is simply omitted.

  tailor(profile, base_template) -> personalized copy
    base_template may be (subject, body) -> returns (subject, body)
                      or  a body string  -> returns a body string

  python3 -m core.message_tailor      # self-test: prints a base vs. tailored sample

This module NEVER sends. Wiring into agents/broken_site_outreach_prep._render_copy and the
intent-reply path is delivered as WIRING_SPEC below (a patch SPEC), intentionally NOT applied.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_NORMS_FALLBACK_PATHS = (
    ROOT / "data" / "hustle" / "vertical_norms.json",
    ROOT / "data" / "vertical_norms.json",
)

# Insertion anchors in the base _render_copy bodies (proof-first branch, then fallback branch,
# then the common sign-off line). The tailored credibility paragraph goes right before whichever
# appears first; if none is found we skip the insertion (still fail-soft).
_BODY_ANCHORS = (
    "Rather than just flag it",
    "If it's useful, just reply",
    "If it's already handled",
)

_NAME_STOP = {"The", "Our", "We", "This", "Welcome", "About", "Home", "Contact", "Us", "Your",
              "Family", "Team", "Owner", "Founder", "Hi", "Hello", "Free", "Call", "Service"}


# --------------------------------------------------------------------------- norms access
_NORMS_CACHE: dict | None = None


def _norms_for(vertical: str) -> dict:
    """Resolve a vertical to its norms row. Single source of truth is
    core.prospect_enrichment.norms_for (reads data/hustle/vertical_norms.json); falls back to a
    direct read of the same table if the import isn't available. Returns {} on total failure."""
    try:
        from core.prospect_enrichment import norms_for as _nf
        return _nf(vertical or "") or {}
    except Exception:
        pass
    global _NORMS_CACHE
    if _NORMS_CACHE is None:
        _NORMS_CACHE = {}
        for p in _NORMS_FALLBACK_PATHS:
            try:
                if p.exists():
                    _NORMS_CACHE = json.loads(p.read_text(encoding="utf-8"))
                    break
            except Exception:
                continue
    v = (vertical or "").lower()
    for entry in (_NORMS_CACHE.get("verticals") or []):
        if any(syn in v for syn in entry.get("synonyms", [])):
            return entry
    return _NORMS_CACHE.get("_default", {})


# --------------------------------------------------------------------------- helpers
def _owner_first(owner_name: str) -> str:
    if not owner_name:
        return ""
    tok = owner_name.strip().split()
    if not tok:
        return ""
    first = tok[0].strip(" .,'")
    if len(first) < 2 or not first[0].isupper() or not first.isalpha() or first in _NAME_STOP:
        return ""
    return first


def _city_short(city: str) -> str:
    """'Sioux Falls, SD' / 'Sioux Falls SD' -> 'Sioux Falls' for natural in-line use."""
    if not city:
        return ""
    c = city.split(",")[0].strip()
    c = re.sub(r"\s+[A-Z]{2}$", "", c).strip()
    return c or city.strip()


def _credibility_line(profile: dict, norms: dict) -> str:
    """One tailored line: a VERIFIED business nod + the vertical's 'why it matters now', built
    only from facts present on the profile. Empty string if there's nothing true to say."""
    bn = (profile.get("business_name") or "").strip()
    if not bn:
        return ""
    city = _city_short(profile.get("city") or "")
    vertical = (profile.get("vertical") or "").strip() or "local"
    themes = profile.get("review_themes") or []
    theme = themes[0] if themes else ""
    founded = profile.get("founded_year")
    years = profile.get("years_in_business")
    family = bool(profile.get("family_owned"))

    # --- nod: a true fact about THIS business (first available wins) ---
    if theme and city:
        nod = f"Folks around {city} already rate {bn} for being {theme}, so the website should make that just as obvious."
    elif theme:
        nod = f"People clearly rate {bn} for being {theme}, so the website should make that just as obvious."
    elif founded and city:
        nod = f"You've built {bn} into a name around {city} since {founded} — the site should carry that same weight."
    elif founded:
        nod = f"You've been running {bn} since {founded}, and the site should carry that same weight."
    elif years:
        nod = f"After {years}+ years, {bn} has earned its reputation — the site should reflect it."
    elif family and city:
        nod = f"For a family-run shop like {bn} in {city}, the website is the first handshake before anyone calls."
    elif family:
        nod = f"For a family-run shop like {bn}, the website is the first handshake before anyone calls."
    elif city:
        nod = f"For a {vertical} business in {city}, most people look you up online before they ever call."
    else:
        nod = f"For a {vertical} business, most people look you up online before they ever call."

    # --- the vertical NORM 'why it matters now' (generic industry truth, never a business claim) ---
    cust = (norms.get("customer_default") or "").strip()
    proof = (norms.get("proof_that_lands") or "").strip()
    why = ""
    if cust and proof:
        why = f" And {cust} look you up online before they call — {proof} is what turns that visit into a booked job."
    elif proof:
        why = f" {proof[0].upper() + proof[1:]} is usually what turns a visitor into a booked job."
    elif cust:
        why = f" And {cust} tend to check the website before they call."

    return (nod + why).strip()


def _tailor_body(body: str, profile: dict, norms: dict) -> str:
    if not body:
        return body
    out = body

    # 1) personal greeting
    first = _owner_first(profile.get("owner_name") or "")
    if first and "Hi there," in out:
        out = out.replace("Hi there,", f"Hi {first},", 1)

    # 2) one tailored credibility + 'why now' paragraph, inserted before the first anchor.
    line = _credibility_line(profile, norms)
    if line and line not in out:
        for anchor in _BODY_ANCHORS:
            idx = out.find(anchor)
            if idx != -1:
                out = out[:idx] + line + "\n\n" + out[idx:]
                break
    return out


def _tailor_subject(subject: str, profile: dict) -> str:
    if not subject:
        return subject
    first = _owner_first(profile.get("owner_name") or "")
    if not first:
        return subject
    if first.lower() in subject.lower():
        return subject
    cand = f"{first}, {subject}"
    return cand if len(cand) <= 80 else subject


# --------------------------------------------------------------------------- public API
def tailor(profile: dict, base_template):
    """Weave a prospect profile into a base outreach template. Deterministic, $0, fail-soft.

    profile       : the dict from core.prospect_enrichment.enrich()/build_profile().
    base_template : (subject, body) tuple/list  -> returns (subject, body)
                    or a body string            -> returns a body string

    An empty / ICP-blocked / nameless profile returns base_template UNCHANGED."""
    is_pair = isinstance(base_template, (tuple, list)) and len(base_template) == 2
    if is_pair:
        subject, body = base_template[0], base_template[1]
    else:
        subject, body = "", str(base_template)

    if (not isinstance(profile, dict) or not profile
            or profile.get("icp_blocked") or not profile.get("business_name")):
        return base_template

    try:
        norms = profile.get("norms") or _norms_for(profile.get("vertical") or "")
        new_body = _tailor_body(body, profile, norms)
        new_subject = _tailor_subject(subject, profile)
    except Exception:
        return base_template  # never let tailoring break the caller

    if is_pair:
        return (new_subject, new_body)
    return new_body


# --------------------------------------------------------------------------- WIRING SPEC
WIRING_SPEC = r"""
================================================================================
WIRING SPEC — fold enrich() + tailor() into the two outreach surfaces.
SPEC ONLY. Do NOT apply here: agents/broken_site_outreach_prep.py and the intent
engine are owned by another build. Both patches are ADDITIVE and FAIL-SOFT — an
empty/thin profile yields the base copy byte-for-byte, and canspam.render() is
untouched. $0: profiles cache 14d; the only model call is local-Ollama (paid path
hard-disabled), and contact email/phone reuse discover_contact's cache.
================================================================================

--- PATCH A: agents/broken_site_outreach_prep.py :: _render_copy(prospect) -----

STEP A1 — imports (top of file, after `from agents.core import canspam`):

    try:
        from core.prospect_enrichment import enrich_record   # record-aware enrich (passes
                                                              # vertical/city/name/outreach_angle)
        from core.message_tailor import tailor
    except Exception:                       # enrichment must never break prep
        enrich_record = None
        tailor = None

STEP A2 — inside _render_copy(prospect), replace BOTH
              `return subject, body`
          (the proof-first branch AND the fallback branch) with:

        if enrich_record and tailor:
            try:
                prof = enrich_record(prospect).get("profile", {})   # cached 14d, $0 + 1 local read
                subject, body = tailor(prof, (subject, body))
            except Exception:
                pass                          # fail-soft: keep base copy
        return subject, body

Notes:
  * enrich_record(prospect) feeds build_profile the full queue row, so norms key off
    the real vertical and city/owner come through. (For a bare domain with no record,
    use enrich(domain) instead.)
  * tailor() keeps the {brand} placeholder, the proof_url, CLAIM_URL/OFFER_URL, and the
    80-char subject cap intact; it only adds a greeting, ONE credibility+why-now line,
    and (when present) a true review-theme nod.
  * To stage deterministically (no LLM): the synth layer self-falls-back; or call
    enrich(prospect["domain"], ...) paths with use_llm=False via build_profile.

--- PATCH B: the intent-reply drafting path --------------------------------------
    (e.g. agents/intent_response_engine.py / agents/intent_reply_poster.py — wherever
     a reply body is composed for a scored intent lead that carries a domain)

STEP B1 — import (top of the drafting module):

    try:
        from core.prospect_enrichment import enrich
        from core.message_tailor import tailor
    except Exception:
        enrich = None
        tailor = None

STEP B2 — after the base reply (subject?, body) is assembled:

    dom = lead.get("domain") or lead.get("website") or lead.get("site")
    if enrich and tailor and dom:
        try:
            prof = enrich(dom, failure=(lead.get("pain") or lead.get("angle") or ""))
            _subj, body = tailor(prof, ("", body))   # intent replies are body-only
        except Exception:
            pass                                      # fail-soft: keep the base reply
    # Leads with no domain skip enrichment entirely; the base reply stands.

  * Same fail-soft contract: prof == {} => base reply unchanged.
  * No send path is added or implied — this only shapes the DRAFT the existing
    operator-/identity-gated path already handles.
================================================================================
"""


# --------------------------------------------------------------------------- self-test CLI
def _demo() -> int:
    profile = {
        "business_name": "A1 Pumping & Excavating",
        "owner_name": "Dave Halverson",
        "vertical": "septic service",
        "city": "Sioux Falls, SD",
        "review_themes": ["honest", "responsive"],
        "founded_year": 2003,
        "family_owned": True,
        "failure": "Your site's security certificate has expired, so browsers warn visitors away.",
        "norms": _norms_for("septic service"),
    }
    base_subject = "I rebuilt A1 Pumping & Excavating's website — preview inside"
    base_body = (
        "Hi there,\n\n"
        "I ran a quick health check on A1 Pumping & Excavating's site (a1pumpingsd.com) and found this:\n\n"
        "Your site's security certificate has expired, so browsers are warning visitors away with a "
        "'Not Secure' screen.\n\n"
        "Rather than just flag it, I went ahead and built a modern, working version so you can see exactly "
        "what it'd look like — no charge to look:\nhttps://scannerapp.dev/mockups/a1pumpingsd.com.html\n\n"
        "If you want it live on your domain, you can claim it here: https://scannerapp.dev/site-scan\n\n"
        "If it's already handled, ignore this and sorry for the noise.\n\n"
        "— {brand}"
    )
    s2, b2 = tailor(profile, (base_subject, base_body))
    print("=" * 72 + "\nBASE SUBJECT:", base_subject)
    print("BASE BODY:\n" + base_body)
    print("=" * 72 + "\nTAILORED SUBJECT:", s2)
    print("TAILORED BODY:\n" + b2)
    print("=" * 72)
    # fail-soft proof
    assert tailor({}, (base_subject, base_body)) == (base_subject, base_body), "empty profile must be no-op"
    assert tailor({"icp_blocked": "lawfirm"}, base_body) == base_body, "icp-blocked must be no-op"
    assert "{brand}" in b2, "brand placeholder must survive"
    print("fail-soft + placeholder checks: OK")
    return 0


if __name__ == "__main__":
    sys.exit(_demo())
