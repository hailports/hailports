#!/usr/bin/env python3
"""safety_monitors.py — recurring SAFETY MONITORS (the honest, paid, $0-detection product).

The free probes already tell a prospect what's wrong RIGHT NOW (the proof-first try-me).
This module turns those same deterministic probes into a RECURRING subscription: for a
domain a customer opted in to monitor, re-run the probe on a schedule and alert ONLY on a
genuinely NEW real finding (a regression / a newly-actionable state) — never re-spamming
pre-existing state, never fabricating risk. The free fix stays free; what's paid is the
ongoing watch + done-for-you, $4–49/mo on the ONE live Stripe account (acct …AemilvQSfv).

Three lanes, each a thin recurring wrapper over an already-built probe:
  secrets      -> core.exposure_scan._exposed_file_findings   (.env/.git/creds served publicly)
  email_auth   -> agents.email_auth_probe.probe               (SPF/DMARC regression)
  domain_ssl   -> agents.domain_expiry_monitor (RDAP) + core.web_failure_probe (TLS)

Public API:
  run_monitor(lane, domain, *, subscriber=None, alert=True, persist=True) -> dict
        Re-probe, diff against the stored baseline, return findings + new_findings, and
        (when a subscriber + new findings) fire the alert hook. First run for a domain just
        records the baseline silently (the try-me already disclosed current state).
  alert_hook(lane, domain, new_findings, *, subscriber) -> dict
        Build an honest CAN-SPAM re-engage alert (proof + the FREE fix), stage it to the
        gated send queue, log a funnel re-engage event, best-effort ping the operator.
        NEVER auto-sends to the customer (the gated generic sender drains the queue).
  trial_cta(lane, email="") -> dict       free-trial Stripe Checkout for the monitoring tier.
  subscribe / subscribers / run_all       subscription registry + batch runner.

All heavy imports are lazy so importing this module never fails on a sibling's deps. $0/no-LLM.

  python3 -m core.safety_monitors --lane secrets --domain example.com
  python3 -m core.safety_monitors --run-all
  python3 -m core.safety_monitors --subscribe domain_ssl example.com you@example.com
  python3 -m core.safety_monitors --cta secrets you@example.com
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUBSCRIBERS = ROOT / "data" / "hustle" / "safety_subscribers.json"
STATE = ROOT / "data" / "hustle" / "safety_monitor_state.json"
ALERT_QUEUE = ROOT / "data" / "hustle" / "safety_alert_queue.jsonl"

LANES = ("secrets", "email_auth", "domain_ssl")
_SEV_RANK = {"critical": 4, "expired": 4, "high": 3, "medium": 2, "low": 1, "info": 0, "ok": 0}

# Anon sender (same verified scannerapp-family pool the cold engine warms). Recurring,
# automated, no human in the loop = anonymity-safe.
FROM_EMAIL = "user@example.com"
FROM_NAME = "scannerapp Team"
BRAND = "scannerapp"

# Per-lane product metadata: the recurring offer, the FREE-fix promise, the proof_offers
# try-me kind to surface, and the verify-it-yourself line. Honest pricing ($4–49/mo).
MONITORS = {
    "secrets": {
        "title": "Exposed secrets / .git monitor",
        "price": "$24–49/mo",
        "tier": "monitoring",
        "trial_days": 14,
        "try_me_kind": "exposure",
        "free_fix": "Block public access to the file (deny in the web server / move it out of webroot) — free.",
        "verify": "Open the URL in a browser — if it loads the file, anyone can.",
    },
    "email_auth": {
        "title": "Email-spoofing (SPF/DMARC) watch",
        "price": "$9–29/mo",
        "tier": "monitoring",
        "trial_days": 14,
        "try_me_kind": "email_auth",
        "free_fix": "Publish the exact SPF/DMARC TXT record we hand you — free.",
        "verify": "Run `dig +short TXT _dmarc.{d}` and `dig +short TXT {d}` — same result.",
    },
    "domain_ssl": {
        "title": "Domain + SSL expiry watch",
        "price": "$4–19/mo",
        "tier": "monitoring",
        "trial_days": 14,
        "try_me_kind": "site",
        "free_fix": "Renew at your registrar / reissue the cert (free via Let's Encrypt) and turn on auto-renew.",
        "verify": "Check the padlock and run a public WHOIS/RDAP lookup — the dates match.",
    },
}


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
    if "@" in s:
        s = s.split("@")[-1]
    return s


def _load_json(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default
    except Exception:
        return default


def _save_json(p: Path, obj) -> None:
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(obj, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(p)
    except Exception:
        pass


def _funnel(stage: str, **kw) -> None:
    try:
        from core.funnel_tracker import log_event
        log_event(stage, **kw)
    except Exception:
        pass


def _f(fid, sev, title, proof, fix, free=True):
    return {"id": fid, "severity": sev, "title": title, "proof": proof, "fix": fix, "free": bool(free)}


# ── per-lane probes → unified finding shape (each id is STABLE so a re-run of the same
#    state yields the same id and therefore does NOT re-alert) ─────────────────────────

def _probe_secrets(domain: str) -> tuple[list[dict], dict]:
    try:
        from core.exposure_scan import _exposed_file_findings, _is_public
    except Exception:
        return [], {"error": "exposure_scan unavailable"}
    if not _is_public(domain):
        return [], {"error": "host is not a public internet domain (refused)"}
    out = []
    for f in _exposed_file_findings(domain):
        out.append(_f(f.get("id", "exposed"), f.get("severity", "critical"),
                      f.get("title", "Publicly exposed file"), f.get("proof", ""),
                      f.get("fix", MONITORS["secrets"]["free_fix"]), free=True))
    return out, {}


def _probe_email_auth(domain: str) -> tuple[list[dict], dict]:
    try:
        from agents.email_auth_probe import probe as _ea
        r = _ea(domain)
    except Exception:
        return [], {"error": "email_auth_probe unavailable"}
    # Stable id per gap CLASS so a persistent gap keeps one id (no re-alert) but a NEW
    # regression (DMARC dropped, SPF removed, policy weakened) surfaces as a new id.
    def _gap_id(text: str) -> str:
        t = text.lower()
        if "no spf" in t:
            return "spf_missing"
        if "no dmarc" in t:
            return "dmarc_missing"
        if "monitor-only" in t or "p=none" in t:
            return "dmarc_p_none"
        if "no dkim" in t:
            return "dkim_missing"
        return "email_auth_other"
    out = []
    fixes = r.get("fix_records") or []
    for i, gap in enumerate(r.get("gaps") or []):
        fx = fixes[i] if i < len(fixes) else None
        if fx and fx.get("type") == "TXT":
            fix = f"Publish this TXT record on `{fx.get('host')}`: {fx.get('value')} ({fx.get('note','')})"
        else:
            fix = MONITORS["email_auth"]["free_fix"]
        out.append(_f(_gap_id(gap), r.get("severity", "high"), "Email-auth gap", gap, fix, free=True))
    return out, {"spf": r.get("spf"), "dmarc": r.get("dmarc"), "dkim": r.get("dkim")}


def _band(value: int, bands: tuple[int, ...]) -> int | None:
    """Tightest threshold the value has crossed (e.g. 12 days vs (30,14,7) -> 14)."""
    crossed = [b for b in bands if value <= b]
    return min(crossed) if crossed else None


def _probe_domain_ssl(domain: str) -> tuple[list[dict], dict]:
    out: list[dict] = []
    meta: dict = {}
    free = MONITORS["domain_ssl"]["free_fix"]
    # Domain registration (RDAP) — only an actionable band fires, never a healthy date.
    try:
        from agents.domain_expiry_monitor import _fetch_rdap, _expiry_date
        rdap = _fetch_rdap(domain)
        exp = _expiry_date(rdap) if rdap else None
        if exp:
            days = (exp - datetime.now(timezone.utc)).days
            meta["domain_days_left"] = days
            if days < 0:
                out.append(_f("domain_expired", "critical", "Domain registration EXPIRED",
                              f"Domain expired {-days} day(s) ago ({exp.date()}); if it's past your "
                              f"registrar's grace window it can drop.", free))
            else:
                b = _band(days, (30, 14, 7))
                if b is not None:
                    out.append(_f(f"domain_expiry_{b}", "high" if b > 7 else "critical",
                                  "Domain expiring soon",
                                  f"Domain registration expires in {days} day(s) ({exp.date()}).", free))
    except Exception:
        pass
    # TLS cert — already-expired / invalid / imminent (silent-renewal-failure early warning).
    try:
        from core.web_failure_probe import _ssl_days_left_detailed
        days, reason = _ssl_days_left_detailed(domain)
        if reason:  # cert present but invalid
            out.append(_f("ssl_invalid", "high", "TLS certificate invalid", reason, free))
        elif isinstance(days, int):
            meta["ssl_days_left"] = days
            if days < 0:
                out.append(_f("ssl_expired", "critical", "TLS certificate EXPIRED",
                              f"Cert expired {-days} day(s) ago — visitors get a full-page 'Not secure' "
                              f"warning.", free))
            else:
                b = _band(days, (14, 7))
                if b is not None:
                    out.append(_f(f"ssl_expiry_{b}", "high",
                                  "TLS certificate expiring imminently",
                                  f"Cert expires in {days} day(s) — renew before it lapses (no warning yet).",
                                  free))
    except Exception:
        pass
    return out, meta


_PROBES = {
    "secrets": _probe_secrets,
    "email_auth": _probe_email_auth,
    "domain_ssl": _probe_domain_ssl,
}


# ── state ────────────────────────────────────────────────────────────────────
def _state_key(lane: str, domain: str) -> str:
    return f"{lane}:{domain}"


def _get_state() -> dict:
    return _load_json(STATE, {})


def _subscriber_for(lane: str, domain: str) -> dict | None:
    subs = _load_json(SUBSCRIBERS, {})
    rec = (subs.get(lane) or {}).get(domain)
    return rec if isinstance(rec, dict) else None


# ── the recurring monitor ─────────────────────────────────────────────────────
def run_monitor(lane: str, domain: str, *, subscriber: dict | None = None,
                alert: bool = True, persist: bool = True) -> dict:
    """Re-run the probe for a subscribed domain and alert ONLY on a genuinely NEW finding.

    First run for a (lane, domain) records the baseline silently — the free try-me already
    disclosed current state, so we never alert on pre-existing findings as if they're new.
    Returns findings + new_findings + resolved_ids; fires alert_hook when there are new
    findings and a subscriber email is known. Never raises."""
    lane = (lane or "").strip().lower()
    domain = _norm_domain(domain)
    res = {"lane": lane, "domain": domain, "ts": _now(), "ok": False,
           "findings": [], "new_findings": [], "resolved_ids": [], "alerted": False}
    if lane not in _PROBES:
        res["error"] = f"unknown lane '{lane}' (valid: {', '.join(LANES)})"
        return res
    if not domain or "." not in domain:
        res["error"] = "invalid domain"
        return res

    findings, meta = _PROBES[lane](domain)
    res["meta"] = meta
    if meta.get("error"):
        res["error"] = meta["error"]
        return res
    res["ok"] = True
    findings.sort(key=lambda f: -_SEV_RANK.get(f.get("severity", "info"), 0))
    res["findings"] = findings

    state = _get_state()
    key = _state_key(lane, domain)
    prior = state.get(key) or {}
    baseline_set = bool(prior.get("baseline_set"))
    known = set(prior.get("known_ids") or [])
    current = {f["id"] for f in findings}

    new_ids = current - known
    resolved = known - current
    res["resolved_ids"] = sorted(resolved)
    new_findings = [f for f in findings if f["id"] in new_ids]
    # On the very first run we only establish the baseline — no alert for state the
    # try-me already showed. Genuinely-new findings on later runs are the product.
    res["baseline"] = not baseline_set
    if baseline_set:
        res["new_findings"] = new_findings

    if persist:
        state[key] = {"known_ids": sorted(current), "baseline_set": True,
                      "last_run": res["ts"], "last_severity": findings[0]["severity"] if findings else "ok"}
        _save_json(STATE, state)

    _funnel("monitor_run", lane=f"safety:{lane}", company=domain, product=BRAND,
            detail=f"findings={len(findings)} new={len(res['new_findings'])} baseline={res['baseline']}")

    if alert and res["new_findings"]:
        sub = subscriber or _subscriber_for(lane, domain)
        if sub and sub.get("email"):
            res["alert"] = alert_hook(lane, domain, res["new_findings"], subscriber=sub)
            res["alerted"] = True
    return res


def alert_hook(lane: str, domain: str, new_findings: list[dict], *, subscriber: dict) -> dict:
    """Build an honest re-engage alert for a NEW finding and STAGE it (never auto-sends).

    The proof + the genuinely-FREE fix go in the body; the paid value is the ongoing watch
    they already subscribed to + an optional 'want us to handle it?' reply. CAN-SPAM-rendered,
    logged to the funnel, best-effort operator ping. The gated generic sender drains the queue."""
    info = MONITORS.get(lane, {})
    email = (subscriber.get("email") or "").strip()
    brand = subscriber.get("brand") or BRAND
    n = len(new_findings)
    title = info.get("title", "safety monitor")
    subject = f"[{brand}] New {'finding' if n == 1 else f'{n} findings'} on {domain}"[:80]
    lines = [f"Hi,", "",
             f"You're monitoring {domain} with our {title}. Our latest automated check found "
             f"something new since the last clean run:", ""]
    for f in new_findings:
        lines += [f"  - {f['title']}: {f['proof']}",
                  f"    Free fix (do it yourself any time): {f['fix']}", ""]
    lines += ["We flagged this the moment it appeared so you can act early — that's the whole point "
              "of the watch.",
              "If you'd rather we handle it for you, just reply and we'll take it from here.", "",
              "Nothing else changed on this check.", "",
              f"— {brand}"]
    body = "\n".join(lines)

    payload = {"lane": lane, "domain": domain, "to_email": email, "ts": _now(),
               "new_finding_ids": [f["id"] for f in new_findings], "subject": subject}
    try:
        from agents.core import canspam
        rendered = canspam.render(subject=subject, body=body, from_email=FROM_EMAIL,
                                  from_name=FROM_NAME, brand=brand, to_email=email)
        payload.update({"subject": rendered["subject"], "body": rendered["body"],
                        "headers": rendered.get("headers", {}), "from": rendered.get("from", FROM_NAME),
                        "postal_gate": canspam.postal_address() is None})
    except Exception:
        payload.update({"body": body, "from": FROM_NAME})

    try:
        ALERT_QUEUE.parent.mkdir(parents=True, exist_ok=True)
        with ALERT_QUEUE.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload) + "\n")
        payload["staged"] = True
    except Exception:
        payload["staged"] = False

    _funnel("re_engage", lane=f"safety:{lane}", email=email, company=domain, product=brand,
            detail=f"new={len(new_findings)} staged={payload.get('staged')}")
    try:  # best-effort operator heads-up; never blocks
        from tools.imsg_bridge import send_imessage
        send_imessage(f"[safety:{lane}] NEW finding on {domain} ({n}) -> alert staged for {email}")
    except Exception:
        pass
    return payload


# ── subscription registry + batch ─────────────────────────────────────────────
def subscribe(lane: str, domain: str, email: str, *, tier: str = "", brand: str = "") -> dict:
    """Register a domain for a monitor lane. Establishes the baseline (silent) immediately so
    the first real ALERT is a genuinely new finding, not pre-existing state."""
    lane = (lane or "").strip().lower()
    domain = _norm_domain(domain)
    if lane not in _PROBES or not domain or "@" not in (email or ""):
        return {"error": "need a valid lane, domain and email"}
    subs = _load_json(SUBSCRIBERS, {})
    subs.setdefault(lane, {})[domain] = {
        "email": email.strip().lower(), "since": _now(),
        "tier": tier or MONITORS.get(lane, {}).get("tier", "monitoring"),
        "brand": brand or BRAND,
    }
    _save_json(SUBSCRIBERS, subs)
    base = run_monitor(lane, domain, alert=False, persist=True)  # silent baseline
    _funnel("subscribe", lane=f"safety:{lane}", email=email, company=domain, product=brand or BRAND)
    return {"subscribed": True, "lane": lane, "domain": domain,
            "baseline_findings": len(base.get("findings", []))}


def subscribers(lane: str = "") -> list[dict]:
    subs = _load_json(SUBSCRIBERS, {})
    out = []
    for ln, doms in subs.items():
        if lane and ln != lane:
            continue
        for dom, rec in (doms or {}).items():
            out.append({"lane": ln, "domain": dom, **(rec or {})})
    return out


def run_all(lane: str = "") -> list[dict]:
    """Re-run every subscribed monitor (a cron entry point). Alerts only on new findings."""
    out = []
    for s in subscribers(lane):
        out.append(run_monitor(s["lane"], s["domain"], subscriber=s))
    return out


# ── Stripe trial CTA (reuses the live-account trial wrapper) ───────────────────
def trial_cta(lane: str, email: str = "") -> dict:
    """Free-trial Checkout for a monitor lane on the ONE live account (acct …AemilvQSfv).
    Returns {checkout_url,...} or {error} if the monitoring price isn't set yet (honest)."""
    info = MONITORS.get((lane or "").strip().lower())
    if not info:
        return {"error": f"unknown lane '{lane}'"}
    try:
        from products.self_serve.trial_checkout import create_trial_session
    except Exception as e:
        return {"error": f"trial_checkout unavailable: {e}"}
    r = create_trial_session(email, info["tier"], trial_days=info["trial_days"],
                             metadata={"safety_lane": lane})
    r["lane"] = lane
    r["price"] = info["price"]
    return r


def wiring_note(lane: str) -> str:
    info = MONITORS.get((lane or "").strip().lower(), {})
    return (f"try-me: core.proof_offers.run_try_me(brand, domain, kind='{info.get('try_me_kind','')}'); "
            f"subscribe: core.safety_monitors.subscribe('{lane}', domain, email); "
            f"trial CTA: core.safety_monitors.trial_cta('{lane}', email)  [{info.get('price','')}]")


def _cli(argv):
    if "--run-all" in argv:
        rows = run_all()
        print(json.dumps([{k: v for k, v in r.items() if k != "findings"} for r in rows], indent=2))
        return 0
    if "--run" in argv:
        rows = run_all(argv[argv.index("--run") + 1])
        print(json.dumps([{k: v for k, v in r.items() if k != "findings"} for r in rows], indent=2))
        return 0
    if "--subscribe" in argv:
        i = argv.index("--subscribe")
        print(json.dumps(subscribe(argv[i + 1], argv[i + 2], argv[i + 3]), indent=2))
        return 0
    if "--cta" in argv:
        i = argv.index("--cta")
        em = argv[i + 2] if len(argv) > i + 2 else ""
        print(json.dumps(trial_cta(argv[i + 1], em), indent=2))
        return 0
    if "--lane" in argv and "--domain" in argv:
        lane = argv[argv.index("--lane") + 1]
        domain = argv[argv.index("--domain") + 1]
        print(json.dumps(run_monitor(lane, domain, alert=False), indent=2))
        return 0
    print(__doc__.strip().splitlines()[0])
    print("usage: --lane <lane> --domain <d> | --run-all | --run <lane> | "
          "--subscribe <lane> <domain> <email> | --cta <lane> [email]")
    print("lanes:", ", ".join(f"{k} ({v['price']})" for k, v in MONITORS.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
