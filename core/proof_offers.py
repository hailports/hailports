#!/usr/bin/env python3
"""Proof-first TRY-ME + SAMPLE engine — one reusable module every storefront can surface.

Two public calls, both $0, anonymous, white-hat, and NEVER fabricated:

  run_try_me(brand, input)  -> a REAL deterministic result for THEIR asset (reuses the live
                               probes: web_failure_probe / exposure_scan / email_auth_probe /
                               RDAP domain-expiry) plus an honest "here's what we found, want the
                               full report / done-for-you?" upgrade. The #1 finding's proof and
                               the genuinely-free fixes (exact SPF/DMARC records, SSL/renewal
                               pointer) are handed over FREE; the full finding list + per-item
                               fixes unlock with one email (lead capture, not a paywall).

  give_sample(brand)        -> a real slice of THAT brand's deliverable (N free intent leads, or
                               3 real flagged domains + proof). Two rows shown free; email unlocks
                               the full set. Only the source URL / contact is masked on the locked
                               preview — the proof itself (their words, the score, the defect) is
                               always shown.

Gate model: FREE = the real answer + the genuinely-free fix; PAID (handled by the storefront,
not here) = done-for-you, the full recurring feed, go-live, or monitoring. Email is a $0 soft
gate logged through core.funnel_tracker. No f3a0 links, no fabricated output, no identity leak.

All heavy imports are lazy so importing this module never fails because of a sibling's deps.
"""
from __future__ import annotations

from datetime import datetime, timezone


# ── brand → lane registry ────────────────────────────────────────────────────
# Keyed by the same storefront brand keys the house-of-brands app uses. Anonymous:
# no personal name, no AI fingerprint. `try_me` = the default deterministic probe a
# brand leads with; `sample` = the default deliverable slice it shows. `offer` = the
# honest done-for-you upgrade copy (the storefront owns the actual checkout wiring).
_DEFAULT_OFFER = {
    "site": {"label": "Fix it for you", "tier": "fix_plan", "price": "$197 one-time",
             "blurb": "We repair every issue above and rebuild what's broken — one-time, no calls.",
             "care": "Site Care monitoring, $29/mo — we watch it and fix the next thing before you hear about it."},
    "exposure": {"label": "Close the gaps for you", "tier": "fix_plan", "price": "$197 one-time",
                 "blurb": "We harden every exposure above and hand back a clean posture.",
                 "care": "Monitoring, $29/mo — we re-scan and alert on the next exposure."},
    "email_auth": {"label": "Set up your email auth", "tier": "dmarc_starter", "price": "done-for-you setup",
                   "blurb": "The records below are yours to paste free. Want it set up and tightened to p=reject for you? We do it with your provider.",
                   "care": "Deliverability feed, monthly — we watch your records and flag drift."},
    "domain_expiry": {"label": "Never let it lapse", "tier": "site_care", "price": "$29/mo",
                      "blurb": "Renew at your registrar with the link below (free). Want us watching every domain so one never expires on you? Site Care.",
                      "care": "Site Care, $29/mo — we monitor expiry, SSL and uptime."},
    "leads": {"label": "Get the full weekly list", "tier": "intent_lead_finder", "price": "$99/mo (first list free)",
              "blurb": "Every Monday, the full scored list of people asking for what you sell — their words, the source, the score.",
              "care": "Start free: the first weekly list is on us, cancel any time before the trial ends."},
}

BRANDS: dict[str, dict] = {
    "builtfast":   {"name": "Built Fast",        "try_me": "site",          "sample": "leads"},
    "promptsite":  {"name": "PromptSite",        "try_me": "site",          "sample": "broken"},
    "frontcounter":{"name": "Front Counter",     "try_me": "domain_expiry", "sample": "broken"},
    "signalhq":    {"name": "BuyerSignal HQ",    "try_me": "site",          "sample": "leads"},
    "opsapp":     {"name": "opsapp",           "try_me": "email_auth",    "sample": "leads"},
    "hailport":    {"name": "Hailport Research", "try_me": "email_auth",    "sample": "dmarc"},
}

_GENERIC = {"name": "", "try_me": "site", "sample": "broken"}

_SEV_RANK = {"critical": 4, "expired": 4, "high": 3, "medium": 2, "low": 1, "info": 0, "ok": 0}


def brand_cfg(brand: str) -> dict:
    return BRANDS.get((brand or "").strip().lower(), _GENERIC)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm_domain(raw: str) -> str:
    s = (raw or "").strip().lower()
    for p in ("https://", "http://"):
        if s.startswith(p):
            s = s[len(p):]
    s = s.split("/")[0].split("?")[0].split("#")[0].strip().strip(".")
    if s.startswith("www."):
        s = s[4:]
    if "@" in s:                       # accepted an email address → use its domain
        s = s.split("@")[-1]
    return s


def _funnel(stage: str, **kw) -> None:
    try:
        from core.funnel_tracker import log_event
        log_event(stage, **kw)
    except Exception:
        pass


# ── per-lane probes → a unified finding shape ────────────────────────────────
# finding = {"severity","title","proof","fix","free": bool}.  free=True marks a
# genuinely-free, hand-it-over fix that is shown even on the locked (no-email) view.

def _findings_site(domain: str) -> tuple[list[dict], dict]:
    findings: list[dict] = []
    meta: dict = {}
    try:
        from core.exposure_scan import scan as _expo
        r = _expo(domain)
        if r.get("error"):
            meta["error"] = r["error"]
        for f in (r.get("findings") or []):
            findings.append({"severity": f.get("severity", "info"), "title": f.get("title", ""),
                             "proof": f.get("proof", ""), "fix": f.get("fix", ""), "free": False})
    except Exception:
        pass
    try:
        from core.web_failure_probe import probe as _site
        s = _site(domain)
        meta["ssl_days_left"] = s.get("ssl_days_left")
        if s and s.get("broken"):
            sev = s.get("severity") or "high"
            sev = "high" if sev == "ok" else sev
            ssl_days = s.get("ssl_days_left")
            for reason in (s.get("reasons") or []):
                text = str(reason).strip()
                if not text or text.lower() == "healthy":
                    continue
                free = False
                fix = "We can repair or rebuild it — see the proof."
                if "ssl" in text.lower() and isinstance(ssl_days, int):
                    text = (f"SSL certificate already expired ({-ssl_days} days ago)"
                            if ssl_days < 0 else f"SSL expires in {ssl_days} days")
                    # SSL renewal is a genuinely-free fix we point straight to.
                    fix = ("Renew/reissue the certificate free via Let's Encrypt (certbot) or your "
                           "host's one-click SSL — no charge.")
                    free = True
                findings.append({"severity": sev, "title": "Website problem",
                                 "proof": text, "fix": fix, "free": free})
    except Exception:
        pass
    return findings, meta


def _findings_exposure(domain: str) -> tuple[list[dict], dict]:
    try:
        from core.exposure_scan import scan as _expo
        r = _expo(domain)
        if r.get("error"):
            return [], {"error": r["error"]}
        out = [{"severity": f.get("severity", "info"), "title": f.get("title", ""),
                "proof": f.get("proof", ""), "fix": f.get("fix", ""), "free": False}
               for f in (r.get("findings") or [])]
        return out, {"score": r.get("score")}
    except Exception:
        return [], {}


def _findings_email_auth(domain: str) -> tuple[list[dict], dict]:
    try:
        from agents.email_auth_probe import probe as _ea
        r = _ea(domain)
    except Exception:
        return [], {}
    findings: list[dict] = []
    fixes = r.get("fix_records") or []
    for i, gap in enumerate(r.get("gaps") or []):
        fx = fixes[i] if i < len(fixes) else None
        # The exact record IS a genuinely-free fix we always hand over.
        if fx and fx.get("type") == "TXT":
            fix = f"Publish this TXT record on `{fx.get('host')}`:  {fx.get('value')}  ({fx.get('note','')})"
        elif fx:
            fix = f"{fx.get('value','')}  ({fx.get('note','')})"
        else:
            fix = "We provide the exact record to publish."
        findings.append({"severity": r.get("severity", "high"), "title": "Email-auth gap",
                         "proof": gap, "fix": fix, "free": bool(fx)})
    meta = {"spf": r.get("spf"), "dmarc": r.get("dmarc"), "dkim": r.get("dkim"),
            "fix_records": fixes}
    return findings, meta


def _findings_domain_expiry(domain: str) -> tuple[list[dict], dict]:
    try:
        from agents.domain_expiry_monitor import _fetch_rdap, _expiry_date, _severity
    except Exception:
        return [], {}
    rdap = _fetch_rdap(domain)
    exp = _expiry_date(rdap) if rdap else None
    if not exp:
        return [], {"error": "no RDAP expiry date available for this TLD/domain"}
    days_left = (exp - datetime.now(timezone.utc)).days
    sev = _severity(days_left)
    if days_left < 0:
        proof = f"Domain registration EXPIRED {-days_left} days ago ({exp.date()}) — it can be lost or grabbed."
    elif days_left <= 45:
        proof = f"Domain registration expires in {days_left} days ({exp.date()})."
    else:
        return [], {"days_left": days_left, "expiry": exp.date().isoformat(),
                    "clean": True}  # nothing urgent
    fix = (f"Renew it at your registrar before {exp.date()} — that's the free fix. "
           "Turn on auto-renew so it can't lapse.")
    return ([{"severity": sev, "title": "Domain expiry", "proof": proof, "fix": fix, "free": True}],
            {"days_left": days_left, "expiry": exp.date().isoformat()})


_PROBES = {
    "site": _findings_site,
    "exposure": _findings_exposure,
    "email_auth": _findings_email_auth,
    "domain_expiry": _findings_domain_expiry,
}

_VERIFY = {
    "site": "Load the domain in a browser and check the padlock — everything above is what a visitor sees.",
    "exposure": "Open the URLs / response headers yourself — every item is publicly observable right now.",
    "email_auth": "Run `dig +short TXT _dmarc.{d}` and `dig +short TXT {d}` — you'll see the same gap.",
    "domain_expiry": "Run a public WHOIS/RDAP lookup on the domain — the expiry date matches.",
}


def run_try_me(brand: str, input: str, *, kind: str = "", unlocked: bool = False,
               log: bool = True) -> dict:
    """Run ONE real deterministic probe on the prospect's own asset and return an honest,
    independently-verifiable result + a done-for-you upgrade. Never fabricated.

    unlocked=False (default): white-hat disclosure — the #1 finding's proof + every genuinely-
    free fix are shown; the rest of the finding list is reported as a count and gated behind the
    email capture. unlocked=True (after email): the full finding list + per-item fixes.
    """
    cfg = brand_cfg(brand)
    kind = (kind or cfg["try_me"]).strip().lower()
    domain = _norm_domain(input)
    result: dict = {
        "brand": brand, "kind": kind, "input": domain, "ts": _now(),
        "ok": False, "problem_found": False, "findings": [], "free_fixes": [],
        "locked_count": 0, "unlocked": bool(unlocked),
    }
    if not domain or "." not in domain:
        result["error"] = "Enter a valid domain (e.g. yourshop.com)."
        return result
    probe = _PROBES.get(kind, _findings_site)
    findings, meta = probe(domain)
    result["meta"] = meta
    if log:
        _funnel("try_run", lane=f"proof:{kind}", company=domain,
                product=brand or "", detail=f"kind={kind} findings={len(findings)}")
    if meta.get("error"):
        result["error"] = meta["error"]
        return result
    result["ok"] = True
    findings.sort(key=lambda f: -_SEV_RANK.get(f.get("severity", "info"), 0))
    if not findings:
        result["problem_found"] = False
        result["headline"] = (f"{domain} — nothing publicly broken on the {kind.replace('_',' ')} "
                              "check. Clean. So there's nothing to buy here.")
        result["verify_yourself"] = _VERIFY.get(kind, "")
        return result

    result["problem_found"] = True
    free_fixes = [{"proof": f["proof"], "fix": f["fix"]} for f in findings if f.get("free")]
    result["free_fixes"] = free_fixes  # always handed over free
    result["verify_yourself"] = _VERIFY.get(kind, "").replace("{d}", domain)
    offer = dict(_DEFAULT_OFFER.get(kind, _DEFAULT_OFFER["site"]))
    offer["contact"] = cfg.get("name") or ""
    result["upgrade"] = offer

    sevs = {}
    for f in findings:
        sevs[f["severity"]] = sevs.get(f["severity"], 0) + 1
    result["severity_breakdown"] = sevs
    result["total_findings"] = len(findings)

    if unlocked:
        result["headline"] = (f"Everything we can see on {domain} — {len(findings)} item(s), "
                              "with the exact fix beside each.")
        result["findings"] = findings
        result["locked_count"] = 0
    else:
        top = findings[0]
        result["headline"] = f"We checked {domain} — {len(findings)} item(s) need attention."
        result["disclosure"] = top  # the proof of the #1 finding, shown free (white-hat)
        result["findings"] = [top]
        result["locked_count"] = max(0, len(findings) - 1)
        result["unlock_hint"] = ("One email opens the full list — every finding and its fix — "
                                 "right here, free. We'll send you a copy too.")
    return result


# ── give_sample — a real slice of the brand's deliverable ────────────────────
def _sample_leads(n: int) -> list[dict]:
    """Top intent leads from the live multi-market feed — the exact rows the weekly list ships."""
    from pathlib import Path
    import json
    src = Path.home() / "claude-stack" / "data" / "hustle" / "multi_market_leads.json"
    try:
        rows = json.loads(src.read_text()).get("leads", [])
    except Exception:
        return []
    rows = [r for r in rows if r.get("url") and r.get("title")]
    rows.sort(key=lambda r: ((r.get("source") or "") != "web", r.get("intent_score") or 0), reverse=True)
    out, seen = [], set()
    for r in rows:
        if r["url"] in seen:
            continue
        seen.add(r["url"])
        out.append({
            "title": str(r.get("title") or "")[:200],
            "ask": str(r.get("snippet") or "")[:300],
            "score": r.get("intent_score") or 0,
            "market": r.get("market") or "",
            "source_url": r["url"],          # masked on the locked preview
        })
        if len(out) >= n:
            break
    return out


def _sample_probed_domains(lane: str, n: int) -> list[dict]:
    """Real flagged domains from the live biz list — broken-site feed or DMARC feed.
    Probes deterministically; returns only domains that ACTUALLY flag (no fabrication)."""
    from pathlib import Path
    src = Path.home() / "claude-stack" / "data" / "hustle" / "biz_domains.txt"
    try:
        domains = [d.strip().lower() for d in src.read_text().splitlines()
                   if d.strip() and not d.startswith("#")]
    except Exception:
        return []
    out: list[dict] = []
    scanned = 0
    for d in domains:
        if len(out) >= n or scanned >= 80:   # bounded — sample, not a full sweep
            break
        scanned += 1
        try:
            if lane == "dmarc":
                from agents.email_auth_probe import probe as _ea
                r = _ea(d)
                if r.get("broken"):
                    out.append({"domain": d, "defect": (r.get("gaps") or [""])[0],
                                "severity": r.get("severity", "high"),
                                "fix_records": r.get("fix_records") or [],   # given free
                                "source_url": f"dns:{d}"})
            else:  # broken-site
                from core.web_failure_probe import probe as _site
                s = _site(d)
                if s.get("broken"):
                    reasons = [x for x in (s.get("reasons") or []) if x.lower() != "healthy"]
                    out.append({"domain": d, "defect": (reasons or [""])[0],
                                "severity": s.get("severity", "high"),
                                "source_url": f"https://{d}"})
        except Exception:
            continue
    return out


def _mask(row: dict) -> dict:
    """Locked-preview row: hide the source URL / contact (convenience), keep the proof."""
    out = dict(row)
    for k in ("source_url", "contact", "contact_email", "url", "fix_records"):
        out.pop(k, None)
    out["locked"] = True
    return out


def give_sample(brand: str, *, email: str = "", lane: str = "", n: int = 5,
                log: bool = True) -> dict:
    """Return a REAL slice of the brand's deliverable. With a valid email the full N rows
    unlock (and an email_capture event is logged); without one, 2 proof-bearing rows are shown
    with only the source URL / contact masked. Never fabricated — empty if the feed is empty."""
    cfg = brand_cfg(brand)
    lane = (lane or cfg["sample"]).strip().lower()
    if lane == "leads":
        rows = _sample_leads(n)
        deliverable = "the weekly Intent Lead Finder list"
    else:
        rows = _sample_probed_domains("dmarc" if lane == "dmarc" else "broken", n)
        deliverable = ("the DMARC-gap feed" if lane == "dmarc" else "the broken-site feed")

    valid = isinstance(email, str) and "@" in email and "." in email.split("@")[-1]
    result = {"brand": brand, "lane": lane, "deliverable": deliverable, "ts": _now(),
              "total": len(rows)}

    if log:
        _funnel("try_run", lane=f"sample:{lane}", email=email if valid else "",
                product=brand or "", detail=f"rows={len(rows)} unlocked={valid}")

    if valid:
        if log:
            _funnel("email_capture", email=email, lane=f"sample:{lane}",
                    product=brand or "", detail=f"unlocked {len(rows)} from {deliverable}")
        result.update({"unlocked": True, "rows": rows})
        return result

    preview = [_mask(r) for r in rows[:2]]
    result.update({"unlocked": False, "preview": preview,
                   "locked_count": max(0, len(rows) - len(preview)),
                   "gate": "email",
                   "gate_hint": f"Enter your email to unlock all {len(rows)} rows of {deliverable}, free."})
    return result


# ── Referral loop (give-to-get) ──────────────────────────────────────────────
# Distinct from the affiliate CASH loop: here the SHARER and the REFEREE both unlock
# value (a bonus premium scan), and we reward the SHARE/SIGNUP only — NEVER a review
# (FTC-safe). White-hat for every brand. Anonymity-safe: the sharer is identified by a
# one-way hash code, never an email/name; the referee is stored hashed too. $0, no LLM,
# deterministic. State = an append-only JSONL ledger replayed on read (same pattern as
# core.funnel_tracker), so nothing to migrate and a crash can't half-write state.
import hashlib as _hashlib
import json as _json
import os as _os
from pathlib import Path as _Path

_REF_ROOT = _Path(_os.environ.get("CLAUDE_STACK_DIR", _Path(__file__).resolve().parents[1]))
_REF_LEDGER = _REF_ROOT / "data" / "hustle" / "referral_ledger.jsonl"
_REF_SALT = "scannerapp-referral-v1"        # rotates the code space; not a secret
SHARER_REWARD = 1                            # bonus premium scans the sharer earns per real signup
REFEREE_WELCOME = 1                          # bonus the referee unlocks for arriving via a friend


def _ref_norm_email(email: str) -> str:
    e = (email or "").strip().lower()
    return e if ("@" in e and "." in e.split("@")[-1]) else ""


def referral_code(email: str) -> str:
    """Deterministic, idempotent, anonymity-safe sharer code derived from the email.
    Same email → same code forever; the email is NOT recoverable from the code."""
    e = _ref_norm_email(email)
    if not e:
        return ""
    return "r" + _hashlib.sha256((_REF_SALT + e).encode()).hexdigest()[:10]


def _ref_email_hash(email: str) -> str:
    e = _ref_norm_email(email)
    return _hashlib.sha256((_REF_SALT + "ref|" + e).encode()).hexdigest()[:16] if e else ""


def _ref_clean_code(code: str) -> str:
    c = (code or "").strip().lower()
    import re as _re
    return c if _re.fullmatch(r"r[0-9a-f]{10}", c) else ""


def referral_link(email_or_code: str, *, base_url: str = "https://scannerapp.dev",
                  path: str = "/site-scan") -> str:
    """Shareable link that carries the sharer's ref code. Accepts an email or a code."""
    code = _ref_clean_code(email_or_code) or referral_code(email_or_code)
    if not code:
        return ""
    sep = "&" if "?" in path else "?"
    u = "utm_source=referral&utm_medium=share&utm_campaign=give_to_get"
    return f"{base_url.rstrip('/')}{path}{sep}ref={code}&{u}"


def _ref_append(row: dict) -> None:
    try:
        _REF_LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with open(_REF_LEDGER, "a", encoding="utf-8") as f:
            f.write(_json.dumps(row, sort_keys=True) + "\n")
    except Exception:
        pass


def _ref_replay() -> dict:
    """Rebuild per-code state from the ledger. Returns {code: {signups:set, earned, redeemed}}."""
    from collections import defaultdict
    state: dict = defaultdict(lambda: {"signups": set(), "earned": 0, "redeemed": 0})
    if not _REF_LEDGER.exists():
        return state
    for line in _REF_LEDGER.read_text(errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            r = _json.loads(line)
        except Exception:
            continue
        if not isinstance(r, dict):
            continue
        code = r.get("code", "")
        ev = r.get("event")
        if not code:
            continue
        if ev == "signup":
            rh = r.get("referee_hash", "")
            if rh:                       # de-dup: a given referee counts once per code
                state[code]["signups"].add(rh)
        elif ev == "redeem":
            state[code]["redeemed"] += int(r.get("n", 1) or 1)
    for code, st in state.items():
        st["earned"] = len(st["signups"]) * SHARER_REWARD
    return state


def referral_status(email_or_code: str) -> dict:
    """Sharer dashboard: how many friends signed up and how many bonus scans are available."""
    code = _ref_clean_code(email_or_code) or referral_code(email_or_code)
    if not code:
        return {"ok": False, "error": "Enter a valid email."}
    st = _ref_replay().get(code, {"signups": set(), "earned": 0, "redeemed": 0})
    signups = len(st["signups"])
    earned = signups * SHARER_REWARD
    available = max(0, earned - st["redeemed"])
    return {"ok": True, "code": code, "signups": signups,
            "earned_credits": earned, "redeemed": st["redeemed"], "available_credits": available,
            "link": referral_link(code)}


def record_referral_signup(ref_code: str, referee_email: str, *, brand: str = "",
                           source: str = "", log: bool = True) -> dict:
    """A referee signed up via a friend's link. Credits BOTH (give-to-get) and returns the
    grants. De-duped per (code, referee); self-referral is rejected. Rewards the signup only —
    never a review. Returns {ok, new, sharer_reward, referee_reward, sharer_available}."""
    code = _ref_clean_code(ref_code)
    referee_hash = _ref_email_hash(referee_email)
    if not code or not referee_hash:
        return {"ok": False, "new": False, "error": "missing ref code or valid referee email"}
    if referral_code(referee_email) == code:                 # can't refer yourself
        return {"ok": False, "new": False, "error": "self-referral ignored"}
    before = _ref_replay().get(code, {"signups": set()})
    if referee_hash in before["signups"]:                    # already counted — idempotent
        st = referral_status(code)
        return {"ok": True, "new": False, "sharer_reward": 0, "referee_reward": 0,
                "sharer_available": st["available_credits"]}
    _ref_append({"event": "signup", "code": code, "referee_hash": referee_hash,
                 "brand": brand or "", "source": source or "", "ts": _now()})
    if log:
        try:
            from core.funnel_tracker import log_referral_signup
            log_referral_signup(code, lane=f"referral:{source or 'signup'}",
                                product=brand or "", detail=f"reward={SHARER_REWARD}")
        except Exception:
            pass
    st = referral_status(code)
    return {"ok": True, "new": True, "sharer_reward": SHARER_REWARD,
            "referee_reward": REFEREE_WELCOME, "sharer_available": st["available_credits"]}


def redeem_referral_credit(email_or_code: str, n: int = 1) -> dict:
    """Spend earned bonus scans. Real consumable: decrements available, append-only audit.
    Returns {ok, redeemed, remaining}."""
    code = _ref_clean_code(email_or_code) or referral_code(email_or_code)
    if not code:
        return {"ok": False, "error": "Enter a valid email."}
    st = referral_status(code)
    n = max(1, int(n or 1))
    if st["available_credits"] < n:
        return {"ok": False, "error": "not enough bonus scans yet",
                "available_credits": st["available_credits"]}
    _ref_append({"event": "redeem", "code": code, "n": n, "ts": _now()})
    try:
        from core.funnel_tracker import log_event as _ft
        _ft("referral_redeem", lane="referral", variant=code, detail=f"n={n}")
    except Exception:
        pass
    return {"ok": True, "redeemed": n, "remaining": st["available_credits"] - n}


if __name__ == "__main__":
    import json
    import sys
    args = sys.argv[1:]
    if len(args) >= 1 and args[0] == "refer":               # python3 -m core.proof_offers refer <email> [referee]
        sharer = args[1] if len(args) > 1 else "owner@example.com"
        print("code:", referral_code(sharer))
        print("link:", referral_link(sharer))
        if len(args) > 2:
            print("signup:", _json.dumps(record_referral_signup(referral_code(sharer), args[2],
                                          source="cli", log=False)))
        print("status:", _json.dumps(referral_status(sharer)))
        raise SystemExit(0)
    if len(args) >= 2 and args[0] == "sample":
        print(json.dumps(give_sample(args[1], email=args[2] if len(args) > 2 else "", log=False), indent=2))
    elif len(args) >= 2:
        print(json.dumps(run_try_me(args[0], args[1],
                                    unlocked=("--full" in args), log=False), indent=2))
    else:
        print("usage: python3 -m core.proof_offers <brand> <domain> [--full]")
        print("       python3 -m core.proof_offers sample <brand> [email]")
