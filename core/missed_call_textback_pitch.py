#!/usr/bin/env python3
"""Missed-call text-back — a vertical-wide OFFER, not a passive detector.

Whether a business actually misses calls is NOT observable from outside: no public signal
(SERP, listing, or the site's own HTML) exposes a missed or abandoned call. So this is
deliberately a PITCH module, not a web_failure_probe-style opportunity detector — faking a
detector here would mean claiming something we can't see.

It exposes one canonical offer + owner-facing angle so the outreach layer talks about the play
consistently, plus a $0/passive *fit* scorer that ranks how well a public page suits the offer
(phone present, phone-primary funnel, urgent high-ticket service vertical). Fit is targeting,
not detection: the angle is a universal-truth offer ("when a call comes in you can't grab..."),
never a claim about that specific business's call logs.

Same dict shape as core.web_failure_probe._opportunity_signals ({"snag","severity","angle"}) so
a fit result drops straight into the outreach layer — but it is consumed as its own play, NOT
injected into probe()['opportunities'] (those must be page-verifiable; a missed call is not).

  python3 -m core.missed_call_textback_pitch          # print the offer + sample pitches
"""
from __future__ import annotations

import json
import re
import sys

SNAG = "missed_call_textback"

# The single canonical offer. Plain owner-facing language, no AI tells, no operator name.
OFFER = {
    "snag": SNAG,
    "name": "Missed-Call Text-Back",
    "one_liner": ("When a call comes in that you can't pick up, it auto-texts the caller back "
                  "within seconds so the lead doesn't just dial the next company."),
    "delivery": ("Runs off the business's existing number via a forwarding/number layer "
                 "(Twilio or Telnyx, ~$1-2/mo + pennies per text) wired to a free-tier "
                 "serverless webhook (Cloudflare Worker) that fires a canned SMS the moment a "
                 "call goes unanswered, logs the lead, and routes the reply to the owner's "
                 "phone. Templated texts = no LLM, deterministic, near-zero marginal cost. "
                 "Sold as a flat monthly retainer."),
    # Hardest-hit verticals: phone-primary funnel + high ticket-per-lead + a caller who will not
    # leave a voicemail and instead dials the next listing immediately.
    "verticals_hardest": [
        "plumbing / HVAC / electrical / roofing / garage-door / appliance repair "
        "(techs are hands-busy on a job; emergency callers won't wait)",
        "auto repair & body shops (bay is loud/busy, caller shopping several shops)",
        "dental / med-spa / salon / barbershop (front desk slammed; heavy after-hours demand)",
        "personal-injury & criminal-defense law (caller phones the next firm in seconds)",
        "real-estate agents & brokers (a missed buyer call is a missed showing)",
    ],
    # Lower-fit on purpose, so outreach doesn't waste it where the math is weak.
    "verticals_weak": [
        "restaurants / retail (low ticket-per-call, walk-in driven)",
        "anything booking-first online (the funnel isn't the phone)",
    ],
    "severity": "high",  # demand level: the most-asked-for SMB automation
    "angle": ("When you're on a job or it's after hours and a call comes in you can't grab, that "
              "caller doesn't leave a voicemail — they just dial the next place on the list. This "
              "catches every missed call and texts the person back within seconds, so the lead "
              "stays yours instead of walking. It runs off your existing number and takes about a "
              "day to set up."),
}

# Per-vertical angle nuance. Same offer, sharper opening for the trade. Plain, no AI tells.
_TAILORED = {
    "home_services": ("When your crew's on a job the phone goes unanswered — and a burst-pipe or "
                      "no-heat caller is dialing the next company before you'd ever hear the "
                      "voicemail. This texts every missed caller back in seconds so the job stays "
                      "yours. Runs off your existing number, set up in about a day."),
    "auto": ("When the bay's busy the phone rings out, and someone needing a repair just calls the "
             "next shop. This auto-texts anyone you miss within seconds so they book with you "
             "instead. Works off your current number, about a day to set up."),
    "appointments": ("When the front desk is slammed or it's after hours, missed calls turn into "
                     "booked appointments at the place down the street. This texts every missed "
                     "caller back right away so they book with you. Uses your existing number, "
                     "ready in about a day."),
    "legal": ("Someone with a case who calls and doesn't get through phones the next firm "
              "immediately — they don't leave a message. This texts every missed caller back in "
              "seconds so the consult stays with you. Runs off your existing line, set up in about "
              "a day."),
    "real_estate": ("A buyer who calls about a listing and gets voicemail just calls the next "
                    "agent. This texts every missed caller back within seconds so the showing stays "
                    "yours. Works with your current number, about a day to set up."),
}

# Trade keywords -> vertical group, for passive page-fit tagging (best-effort, never raises).
_VERTICAL_KW = [
    ("home_services", ("plumb", "hvac", "heating", "cooling", "air conditioning", "furnace",
                       "electric", "electrician", "roof", "garage door", "appliance repair",
                       "drain", "septic", "water heater", "handyman", "pest control",
                       "landscap", "lawn care", "tree service", "fencing", "remodel")),
    ("auto", ("auto repair", "body shop", "collision", "mechanic", "tire", "transmission",
              "oil change", "brake", "automotive")),
    ("appointments", ("dentist", "dental", "orthodont", "med spa", "medspa", "salon", "barber",
                      "spa", "massage", "chiropract", "veterinar", "clinic", "aesthetic",
                      "lash", "nail", "tattoo", "physical therapy")),
    ("legal", ("law firm", "attorney", "lawyer", "personal injury", "criminal defense",
               "law office", "legal")),
    ("real_estate", ("real estate", "realtor", "realty", "broker", "homes for sale", "listings")),
]

# Channels that mean the phone is NOT the only funnel -> weaker fit. Mirrors web_failure_probe.
_OTHER_CHANNEL_KW = ("calendly", "acuity", "/booking", "book now", "book online",
                     "book an appointment", "schedule an appointment", "setmore", "vagaro",
                     "squareup.com/appointments", "intercom", "tawk.to", "livechat", "drift.com")

_PHONE_RE = re.compile(r"(?:\+?1[\s.\-]?)?\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}")


def pitch(vertical: str | None = None) -> dict:
    """The canonical offer as an outreach-ready {"snag","severity","angle"} dict.

    `vertical` (one of the _TAILORED keys) sharpens the opening; unknown/None -> generic angle.
    Pure function, never raises."""
    angle = _TAILORED.get(vertical or "", OFFER["angle"])
    return {"snag": SNAG, "severity": OFFER["severity"], "angle": angle}


def _public_phone(low: str) -> str | None:
    """First public phone number surfaced on the page (tel: link preferred). For personalization
    only — its presence is also a weak proxy that the phone is a real intake channel."""
    m = re.search(r'tel:\+?([0-9\-\.\(\)\s]{7,})', low)
    if m:
        digits = re.sub(r"\D", "", m.group(1))
        if 10 <= len(digits) <= 11:
            return m.group(1).strip()
    m = _PHONE_RE.search(low)
    return m.group(0).strip() if m else None


def _vertical_of(low: str) -> str | None:
    for group, kws in _VERTICAL_KW:
        if any(k in low for k in kws):
            return group
    return None


def fit(page: str) -> dict | None:
    """PASSIVE pitch-fit score for one fetched public page. NOT a detection of missed calls —
    it scores how well the OFFER fits, from page-visible signals only:
      - a public phone (the offer needs a number to text back from / personalize),
      - phone-primary funnel (no online-booking/chat = the phone IS the intake),
      - an urgent high-ticket service vertical.
    Returns the outreach-ready {"snag","severity","angle"} dict plus fit metadata, or None when
    there's no public phone (nothing to target on). Never raises."""
    if not page:
        return None
    try:
        low = page.lower()
    except Exception:
        return None

    phone = _public_phone(low)
    if not phone:
        return None  # no observable hook; don't pitch blind

    vertical = _vertical_of(low)
    phone_primary = not any(k in low for k in _OTHER_CHANNEL_KW)

    signals = ["public phone on page"]
    if phone_primary:
        signals.append("phone-primary (no online booking/chat)")
    if vertical:
        signals.append(f"vertical:{vertical}")

    # high only when the math is strong: urgent service vertical AND phone is the funnel.
    if vertical and phone_primary:
        severity = "high"
    elif vertical or phone_primary:
        severity = "medium"
    else:
        severity = "low"

    out = pitch(vertical)
    out["severity"] = severity
    out["fit_signals"] = signals
    out["vertical"] = vertical
    out["public_phone"] = phone
    return out


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    print("OFFER:")
    print(json.dumps(OFFER, indent=2))
    print("\nGENERIC PITCH:")
    print(json.dumps(pitch(), indent=2))
    print("\nVERTICAL PITCH (home_services):")
    print(json.dumps(pitch("home_services"), indent=2))
    sample = ('<html><head><title>Joe\'s Plumbing & Drain — Omaha</title></head><body>'
              '<h1>24/7 Emergency Plumbing</h1><a href="tel:XPHONEX">XPHONEX</a>'
              '<p>Call us for water heater and drain service.</p></body></html>')
    print("\nPASSIVE FIT on a realistic page:")
    print(json.dumps(fit(sample), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
