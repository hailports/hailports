#!/usr/bin/env python3
"""preview_offer.py — generalize the broken-site "mockup -> claim" model.

The broken-site rescue motion is: run a real deterministic probe on the prospect's
own asset -> hand them the genuinely-free fix -> show them the FINISHED deliverable
(their site already rebuilt) -> a single LIVE-Stripe claim link unlocks the labor /
go-live / monitoring. This module lifts that shape off "broken-site" so every
proof-first lane (broken-site, email-auth, exposure, domain-expiry, intent-leads)
gets the same honest structure from one place.

Doctrine baked in (white-hat, anon, honest):
  * FREE  = the real answer (full disclosure THAT a problem exists, independently
            verifiable by the owner) + the genuinely-free fix handed over.
  * PAID  = done-for-you labor, the full recurring feed, go-live, or monitoring.
  * Every probe result is real deterministic output — NO fabrication. We reuse the
    existing engines (web_failure_probe, exposure_scan, email_auth_probe, RDAP).
  * The claim link is always the ONE live Stripe account, minted at click time via
    stripe_checkout (one-time) or trial_checkout (subscription trial). Never an
    f3a0 / buy.stripe.com link.

This module is import-cheap: every probe / Stripe / generator dependency is imported
lazily inside the function that needs it, so importing preview_offer never drags in
network libs and it compiles with deps absent.

  from core.preview_offer import build_preview, risk_reversal
  offer = build_preview("broken_site", "joescafe.com")
  link  = offer.claim_link(offer.claims[0], email="user@example.com")
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict


# ─────────────────────────── data model ───────────────────────────

@dataclass
class ClaimOption:
    """One way to pay — always a LIVE-Stripe path on the single account."""
    label: str            # button text, e.g. "Get the rebuild — $197"
    tier: str             # key in stripe_checkout.TIERS / env price id
    mode: str             # "payment" (one-time) | "trial" (subscription trial)
    price_hint: str       # human price, e.g. "$197 one-time" / "$29/mo · 14-day free"
    trial_days: int = 0   # only for mode="trial"
    sells: str = ""       # what the money actually buys (labor / feed / go-live)


@dataclass
class PreviewOffer:
    """A finished, see-it-before-you-pay deliverable + the claim that unlocks it."""
    lane: str
    target: str
    severity: str
    broken: bool
    headline: str
    proof: list[str] = field(default_factory=list)        # FREE — full disclosure
    free_fixes: list[str] = field(default_factory=list)   # FREE — handed over
    deliverable_kind: str = ""                            # rebuild_mockup / dmarc_fix_sheet / ...
    deliverable_url: str = ""                             # served path (e.g. /mockups/<d>.html)
    deliverable_html: str = ""                            # inline finished artifact
    gate: str = ""                                       # what PAYING unlocks
    claims: list[ClaimOption] = field(default_factory=list)
    risk_reversal: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["claims"] = [asdict(c) for c in self.claims]
        d["live_ready"] = [c.tier for c in self.claims if _price_configured(c.tier)]
        return d

    def claim_link(
        self,
        option: ClaimOption,
        *,
        email: str = "",
        success_url: str = "",
        cancel_url: str = "",
        metadata: dict | None = None,
    ) -> dict:
        """Mint the LIVE Stripe claim link for an option (one-time or trial).

        Returns the stripe_checkout/trial_checkout result dict ({checkout_url,...} or
        {error,...}). On a configuration miss it adds `fallback_path` so a route can
        degrade to the existing /api/checkout flow instead of dead-ending. Logs a
        checkout_started funnel event on success. Never raises."""
        meta = {"lane": self.lane, "target": self.target, "severity": self.severity}
        if metadata:
            meta.update(metadata)
        # The rescue/fix flow keys delivery off scan_id == the domain (see app.py
        # _rescue_fix_plan_domain), so carry the target through as scan_id for parity.
        meta.setdefault("scan_id", self.target)
        try:
            if option.mode == "trial":
                from products.self_serve.trial_checkout import create_trial_session
                res = create_trial_session(
                    customer_email=email, tier=option.tier, trial_days=option.trial_days,
                    success_url=success_url, cancel_url=cancel_url, metadata=meta)
            else:
                from products.self_serve.stripe_checkout import create_checkout_session
                res = create_checkout_session(
                    customer_email=email, tier=option.tier,
                    success_url=success_url, cancel_url=cancel_url, metadata=meta)
        except Exception as e:
            res = {"error": str(e)}
        if res.get("checkout_url"):
            _log("checkout_started", email=email, lane=self.lane, product=option.tier,
                 detail=f"{self.target} · {option.mode}")
        else:
            res.setdefault("fallback_path", f"/go/{option.tier}")
        return res


# ─────────────────────────── helpers ───────────────────────────

def _safe(domain: str) -> str:
    return re.sub(r"[^a-z0-9._-]", "", (domain or "").lower()) or "site"


def _price_configured(tier: str) -> bool:
    try:
        from products.self_serve.stripe_checkout import resolve_price
        return bool(resolve_price(tier))
    except Exception:
        return False


def _log(stage: str, **kw) -> None:
    try:
        from core.funnel_tracker import log_event
        log_event(stage, **kw)
    except Exception:
        pass


def _esc(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


# ─────────────────────────── risk-reversal snippet (reusable) ───────────────────────────

def risk_reversal(kind: str = "one_time", *, anchor: str = "", as_html: bool = True) -> dict:
    """Honest risk-reversal copy block. Reusable across every paid CTA.

    kind="one_time"     -> plain money-back, anchored to the real, already-shown defect.
    kind="subscription" -> real self-serve cancel + "we only bill if there's something to do".

    `anchor` is the concrete finding the guarantee is pinned to (e.g. "the SSL error
    you can see now") so the promise is credible, not a vibe. Returns {html, text}.
    Copy is kept FTC-clean: no banned earnings/scam phrasing, no income claims."""
    a = (anchor or "the exact problem the free check already showed you").strip().rstrip(".")
    if kind == "subscription":
        text = (
            f"Cancel any time, yourself, in two clicks — no contract, no calls, no "
            f"cancel fee. We email you before the first charge, and we only keep "
            f"billing while there's something real to watch ({a}). Stop whenever it "
            f"stops being worth it; you keep everything we already sent."
        )
        head = "Cancel any time — no lock-in"
    else:
        # See-it-first IS the risk reversal — no outcome/"if it works" promise (a tough,
        # disputable bar). They preview the finished work, then decide; the only guarantee
        # is satisfaction with OUR work (which we control), time-boxed.
        text = (
            f"You see the finished version before you pay a cent — pay only if you want it "
            f"live on your own domain. One charge, no contract, no upsell calls. And if "
            f"you're not happy with the work within 14 days, full refund, no argument."
        )
        head = "See it first — then decide"
    if not as_html:
        return {"text": text, "head": head}
    html = (
        f'<div class="guarantee" style="border:1px solid #1f2937;border-radius:10px;'
        f'padding:12px 14px;margin:14px 0;background:#0b0f14">'
        f'<b style="color:#34d399">{_esc(head)}</b>'
        f'<div style="color:#9ca3af;font-size:14px;margin-top:4px">{_esc(text)}</div></div>'
    )
    return {"text": text, "head": head, "html": html}


# ─────────────────────────── deliverable renderers ───────────────────────────

def _sheet(title: str, rows: list[str]) -> str:
    body = "".join(f'<div style="margin:6px 0">{_esc(r)}</div>' for r in rows)
    return (
        f'<div class="deliverable" style="border:1px solid #1f2937;border-radius:10px;'
        f'padding:14px;background:#0b0f14"><h3 style="margin:0 0 8px">{_esc(title)}</h3>'
        f'{body}</div>'
    )


def _records_pre(fixes: list[dict]) -> str:
    lines = []
    for fx in fixes or []:
        host = fx.get("host", "")
        val = fx.get("value", "")
        typ = fx.get("type", "TXT")
        note = fx.get("note", "")
        lines.append(f"{typ:<5} {host}\n      {val}" + (f"\n      ({note})" if note else ""))
    pre = _esc("\n\n".join(lines)) or "(provider sets DKIM automatically once enabled)"
    return f'<pre style="white-space:pre-wrap;color:#e5e7eb;font-size:13px">{pre}</pre>'


# ─────────────────────────── lane builders ───────────────────────────

def _build_broken_site(target: str, prospect: dict | None, build_mockup: bool) -> PreviewOffer:
    from core.web_failure_probe import probe
    r = probe(target)
    reasons = [x for x in (r.get("reasons") or []) if x and x.lower() != "healthy"]
    free = []
    ssl_days = r.get("ssl_days_left")
    if isinstance(ssl_days, int) and ssl_days <= 21:
        free.append("SSL is free to fix: a Let's Encrypt certificate (or your host's "
                    "one-click HTTPS) renews it at $0 — most hosts auto-renew once it's on.")

    # See-it-before-you-pay: the prospect's site already rebuilt. Reuse the validated
    # generate_mockup -> /mockups/<domain>.html. Only build when explicitly asked (it
    # calls the local LLM); otherwise reference an already-built mockup if present.
    safe = _safe(target)
    url = ""
    if build_mockup:
        try:
            from core.site_generator import generate_mockup
            ctx = dict(prospect or {})
            ctx.setdefault("domain", target)
            ctx.setdefault("reasons", reasons)
            m = generate_mockup(ctx)
            if m.get("valid"):
                url = m.get("url") or ""
        except Exception:
            url = ""
    if not url:
        try:
            from core.site_generator import MOCKUP_DIR
            if (MOCKUP_DIR / f"{safe}.html").exists():
                url = f"/mockups/{safe}.html"
        except Exception:
            pass

    claims = [
        ClaimOption("Get the full rebuild — $197", "fix_plan", "payment",
                    "$197 one-time", sells="multi-page rebuild + go-live + handoff"),
        ClaimOption("Add Site Care — 14-day free, then $29/mo", "monitoring", "trial",
                    "$29/mo · 14-day free", trial_days=14,
                    sells="ongoing uptime/SSL/expiry monitoring + alerts"),
    ]
    return PreviewOffer(
        lane="broken_site", target=target, severity=r.get("severity") or "ok",
        broken=bool(r.get("broken")),
        headline=(f"We loaded {target} — {len(reasons)} thing(s) are broken right now"
                  if reasons else f"{target} looks healthy — nothing to fix"),
        proof=reasons, free_fixes=free,
        deliverable_kind="rebuild_mockup", deliverable_url=url,
        gate="the full multi-page rebuild, going live on your domain, and the handoff "
             "(plus optional always-on Site Care). The single-page preview is free.",
        claims=claims,
        risk_reversal=risk_reversal("one_time",
                                    anchor=(reasons[0] if reasons else "")),
    )


def _build_email_auth(target: str) -> PreviewOffer:
    from agents.email_auth_probe import probe
    r = probe(target)
    gaps = r.get("gaps") or []
    fixes = r.get("fix_records") or []
    # The fix records ARE the finished deliverable AND they are genuinely free — we sell
    # the done-for-you publish/verify + the ongoing MSP feed, never the bare records.
    free = ["Here are the exact DNS records to paste — yours to keep, free:"]
    sheet = _sheet(f"Email-auth fix sheet for {target}", []) if not fixes else (
        f'<div class="deliverable" style="border:1px solid #1f2937;border-radius:10px;'
        f'padding:14px;background:#0b0f14"><h3 style="margin:0 0 8px">'
        f'Email-auth fix sheet for {_esc(target)}</h3>{_records_pre(fixes)}</div>'
    )
    claims = [
        ClaimOption("Set it up for me — 14-day free, then $29/mo", "monitoring", "trial",
                    "$29/mo · 14-day free", trial_days=14,
                    sells="we publish + verify the records, then watch for drift/spoofing"),
    ]
    return PreviewOffer(
        lane="email_auth", target=target, severity=r.get("severity") or "ok",
        broken=bool(r.get("broken")),
        headline=(f"{target} is spoofable — {len(gaps)} email-auth gap(s)"
                  if gaps else f"{target} email-auth is fully set up"),
        proof=gaps, free_fixes=free,
        deliverable_kind="dmarc_fix_sheet", deliverable_html=sheet,
        gate="done-for-you publishing + verification of the records above, then the "
             "ongoing feed that warns you the moment auth drifts or someone spoofs you. "
             "The records themselves are free.",
        claims=claims,
        risk_reversal=risk_reversal("subscription",
                                    anchor=(gaps[0] if gaps else "")),
    )


def _build_exposure(target: str) -> PreviewOffer:
    from core.exposure_scan import scan
    r = scan(target)
    if r.get("error"):
        return PreviewOffer(lane="exposure", target=target, severity="ok", broken=False,
                            headline=f"Could not scan {target}: {r['error']}")
    findings = r.get("findings") or []
    proof = [f"{f.get('severity','').upper()}: {f.get('title','')} — {f.get('proof','')}"
             for f in findings]
    # Hand over the genuinely-free fixes (header/SPF lines etc.); sell the labor.
    free = [f.get("fix", "") for f in findings if f.get("fix")][:5]
    rows = "".join(
        f'<div style="margin:8px 0"><b>{_esc(f.get("title",""))}</b> '
        f'<span style="color:#9ca3af">({_esc(f.get("severity",""))})</span>'
        f'<div style="color:#9ca3af;font-size:13px">{_esc(f.get("proof",""))}</div>'
        f'<div style="color:#34d399;font-size:13px">Fix: {_esc(f.get("fix",""))}</div></div>'
        for f in findings)
    sheet = _sheet(f"Fix plan for {target}", []) if not findings else (
        f'<div class="deliverable" style="border:1px solid #1f2937;border-radius:10px;'
        f'padding:14px;background:#0b0f14"><h3 style="margin:0 0 8px">'
        f'Fix plan for {_esc(target)} — {len(findings)} item(s)</h3>{rows}</div>'
    )
    claims = [
        ClaimOption("Have it fixed for me — $197", "fix_plan", "payment", "$197 one-time",
                    sells="we apply every fix + confirm the exposure is closed"),
        ClaimOption("Watch it for me — 14-day free, then $29/mo", "monitoring", "trial",
                    "$29/mo · 14-day free", trial_days=14,
                    sells="ongoing re-scan + alert when a new exposure appears"),
    ]
    return PreviewOffer(
        lane="exposure", target=target, severity=r.get("severity") or "ok",
        broken=bool(findings),
        headline=(f"{target} — {len(findings)} public exposure(s) anyone can see"
                  if findings else f"{target} — clean posture, nothing exposed"),
        proof=proof, free_fixes=free,
        deliverable_kind="fix_plan", deliverable_html=sheet,
        gate="us applying the fixes for you and confirming each exposure is closed, plus "
             "optional ongoing monitoring. Every finding and its fix is shown free.",
        claims=claims,
        risk_reversal=risk_reversal("one_time", anchor=(proof[0] if proof else "")),
    )


def _build_domain_expiry(target: str) -> PreviewOffer:
    from datetime import datetime, timezone
    from agents.domain_expiry_monitor import _fetch_rdap, _expiry_date, _severity
    rdap = _fetch_rdap(target)
    exp = _expiry_date(rdap) if rdap else None
    if not exp:
        return PreviewOffer(lane="domain_expiry", target=target, severity="ok", broken=False,
                            headline=f"Could not read a public expiry date for {target}")
    days = (exp - datetime.now(timezone.utc)).days
    when = exp.strftime("%Y-%m-%d")
    sev = _severity(days)
    if days < 0:
        proof = [f"{target} EXPIRED {abs(days)} day(s) ago (on {when}) — it can be claimed "
                 f"by anyone once it drops; your site and email go dark."]
    else:
        proof = [f"{target} expires in {days} day(s), on {when}. If it lapses, the site and "
                 f"email stop working and the name can be taken."]
    free = [f"Renew it yourself, free of us: log in to the registrar where you bought "
            f"{target} and renew before {when}. (Run `whois {target}` to see your "
            f"registrar if you've forgotten it.)"]
    sheet = _sheet(f"Renewal countdown — {target}",
                   [f"Expiry date: {when}",
                    f"Days left: {days}",
                    "Action: renew at your registrar before that date."])
    claims = [
        ClaimOption("Never miss it — 14-day free, then $29/mo", "monitoring", "trial",
                    "$29/mo · 14-day free", trial_days=14,
                    sells="we watch expiry + uptime + SSL and warn you well ahead"),
    ]
    return PreviewOffer(
        lane="domain_expiry", target=target, severity=sev, broken=days <= 21,
        headline=proof[0], proof=proof, free_fixes=free,
        deliverable_kind="renewal_notice", deliverable_html=sheet,
        gate="hands-off monitoring that warns you before expiry/SSL/downtime ever bites — "
             "renewing it yourself stays free.",
        claims=claims,
        risk_reversal=risk_reversal("subscription", anchor=proof[0]),
    )


def _build_intent_leads(target: str) -> PreviewOffer:
    """Live-Sample lane: the finished deliverable is real scored leads (served by
    app.py /sample). The claim is the recurring feed with a real free first cycle."""
    claims = [
        ClaimOption("First weekly list free, then $99/mo", "intent_lead_finder", "trial",
                    "$99/mo · first list free", trial_days=7,
                    sells="a fresh scored buyer list every week"),
    ]
    return PreviewOffer(
        lane="intent_leads", target=target or "your niche", severity="info", broken=False,
        headline="See real people asking for what you sell — 5 live leads, free",
        proof=["Two real leads are shown free on the sample page; the email unlock opens all five."],
        free_fixes=[],
        deliverable_kind="intent_sample", deliverable_url="/sample",
        gate="the full recurring feed — a fresh scored list every week. The sample is free.",
        claims=claims,
        risk_reversal=risk_reversal("subscription",
                                    anchor="the leads you can already see on the sample"),
    )


_BUILDERS = {
    "broken_site": _build_broken_site,
    "email_auth": _build_email_auth,
    "exposure": _build_exposure,
    "domain_expiry": _build_domain_expiry,
    "intent_leads": _build_intent_leads,
}

LANES = tuple(_BUILDERS)


def build_preview(lane: str, target: str, *, prospect: dict | None = None,
                  build_mockup: bool = False) -> PreviewOffer:
    """Build the see-it-before-you-pay offer for one lane + target.

    Runs the real deterministic probe for the lane and assembles the free disclosure,
    the genuinely-free fix, the finished-deliverable preview, and the LIVE-Stripe claim
    options. `build_mockup=True` (broken_site only) renders the per-prospect rebuild via
    the local generator; default reuses an already-built mockup if present. No network
    Stripe call happens here — links are minted on demand via PreviewOffer.claim_link."""
    fn = _BUILDERS.get(lane)
    if not fn:
        raise ValueError(f"unknown lane {lane!r}; known: {', '.join(LANES)}")
    if lane == "broken_site":
        offer = fn(target, prospect, build_mockup)
    else:
        offer = fn(target)
    _log("other:preview_built", lane=lane, detail=f"{target} sev={offer.severity}")
    return offer


def main(argv=None) -> int:
    import sys, json
    argv = argv if argv is not None else sys.argv[1:]
    if len(argv) < 2:
        print(f"usage: python3 -m core.preview_offer <lane> <target>\n  lanes: {', '.join(LANES)}")
        return 1
    offer = build_preview(argv[0], argv[1])
    print(json.dumps(offer.to_dict(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
