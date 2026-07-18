#!/usr/bin/env python3
"""Eternal guardian — closes the silent-death risks an unattended machine faces.

One sweep (every 6h) watches the three things that quietly kill a self-running stack and
can't be caught by liveness/restart healers:

  1. DOMAINS expiring  — RDAP expiry for every owned domain; tiered alerts well before lapse
                         (45/30/14/7/3/1 days). A lapsed domain silently 404s every storefront.
  2. SESSIONS rotting  — login/cookie freshness for the CDP profiles posting/selling. Stale =
                         the lane has quietly stopped; fires the known self-heal + alerts.
  3. EARNING silent    — if the money loop produces NOTHING for >48h (no outreach sent, no
                         funnel hit), something upstream broke without erroring. Alert.
  4. AUTOFLOW hung     — an autonomous autoflow mode that logged START but no DONE for >2h has
                         silently died (whole run killed before it could finish or rerun). The
                         daily-cap slot was NOT charged (fix in hailports_autoflow.sh), but the
                         cycle produced nothing and won't self-restart until its next tick — so
                         kickstart the launchd job now (the "improve hung 6h" symptom).

Alerts route through the central alert gateway (allow-list/dedup/digest) — routine/early-warning
checks ride as "warn" (silent digest); only imminent emergencies (domain <=7d, etc.) page as
"critical". Recoveries clear the symptom (healed). State in ~/.legacy so it never spams the
same tier twice.

  python3 -m core.eternal_guardian            # one sweep
  python3 -m core.eternal_guardian --report   # show status, send nothing
"""
from __future__ import annotations
import json, os, re, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = Path(os.path.expanduser("~/.legacy"))
STATE = STATE_DIR / "eternal_guardian.json"

# Every owned domain whose lapse would silently 404 a storefront. Config, not code —
# supply your own via GUARDIAN_DOMAINS (comma-separated) or edit the default list.
DOMAINS = [d.strip() for d in os.environ.get(
    "GUARDIAN_DOMAINS", "example.com, shop.example.com").split(",") if d.strip()]
DOMAIN_TIERS = [45, 30, 14, 7, 3, 1]

# Retained-login CDP profiles whose cookie file staleness means a posting/revenue lane
# has quietly gone dark. Tuple = (name, cookie_path, max_age_days, optional_self_heal_cmd).
# These are examples — point them at your own profiles.
SESSIONS = [
    ("storefront", "~/.chrome-cdp-profile-storefront/Default/Cookies", 21, ""),
    ("social", "~/.chrome-cdp-profile-social/Default/Cookies", 14,
     str(ROOT / ".venv/bin/python") + " -m agents.harvest_session"),
]
EARNING_SIGNALS = [   # any one of these being touched recently = the money loop is alive
    "data/outreach_queue.jsonl",
    "products/self_serve/reports/scans.jsonl",
    "products/self_serve/funnel_scans.jsonl",  # live funnel activity
    "data/PIPELINE_BOARD.md",
]
EARNING_MAX_QUIET_HRS = 48

# Autonomous autoflow loops (scripts/hailports_autoflow.sh). Each logs `=== <ts> autoflow <mode>
# START ===` then `=== <ts> autoflow <mode> DONE rc=N ===` to data/hustle/logs/autoflow_<mode>.log,
# and is driven by launchd job com.claude-stack.hailports-<mode>. A START with no matching DONE for
# >AUTOFLOW_STUCK_HRS = the run died mid-flight (whole script killed) — nothing reruns it on its own.
AUTOFLOW_MODES = ["builder", "guardian", "content-edge", "improve", "stream-improve"]
AUTOFLOW_STUCK_HRS = 2
AUTOFLOW_OFF = ROOT / "data/hustle/AUTOFLOW_OFF"


def _now():
    return datetime.now(timezone.utc)


def _curl(url: str, t: int = 20) -> str:
    try:
        return subprocess.run(["curl", "-sL", "--max-time", str(t),
                               "-H", "Accept: application/rdap+json", url],
                              capture_output=True, text=True, timeout=t + 4).stdout
    except Exception:
        return ""


def _route(severity: str, subject: str, body: str, issue_key: str, healed: bool = False) -> None:
    """All notifications go through the central alert gateway (allow-list/dedup/digest).
    Routine/early-warning checks ride as "warn" (silent digest); only imminent
    emergencies escalate to "critical". Never falls back to direct texting."""
    try:
        from core import alert_gateway
        alert_gateway.route(severity, source="eternal_guardian", subject=subject,
                            body=body, issue_key=issue_key, healed=healed)
    except Exception:
        pass


def _load() -> dict:
    STATE_DIR.mkdir(mode=0o700, exist_ok=True)
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {"domain_alerted": {}, "session_alerted": {}, "earning_alerted": None,
                "autoflow_alerted": {}}


def _save(d: dict):
    STATE.write_text(json.dumps(d, indent=2))


def _whois_expiry_days(domain: str):
    """Fallback for TLDs without RDAP (e.g. .us)."""
    try:
        out = subprocess.run(["whois", domain], capture_output=True, text=True, timeout=20).stdout
    except Exception:
        return None
    m = re.search(r"(?:Registry Expiry Date|Registrar Registration Expiration Date|"
                  r"Expiration Date|Expiry Date|paid-till)\s*:?\s*([0-9]{4}-[0-9]{2}-[0-9]{2})", out, re.I)
    if not m:
        return None
    try:
        exp = datetime.fromisoformat(m.group(1) + "T00:00:00+00:00")
        return (exp - _now()).total_seconds() / 86400.0
    except Exception:
        return None


def _domain_expiry_days(domain: str):
    raw = _curl(f"https://rdap.org/domain/{domain}")
    if raw.strip():
        try:
            for ev in json.loads(raw).get("events", []):
                if ev.get("eventAction") == "expiration":
                    exp = datetime.fromisoformat(ev["eventDate"].replace("Z", "+00:00"))
                    return (exp - _now()).total_seconds() / 86400.0
        except Exception:
            pass
    return _whois_expiry_days(domain)   # RDAP missing/failed -> whois


def check_domains(st: dict) -> list[dict]:
    alerts = []
    for d in DOMAINS:
        days = _domain_expiry_days(d)
        if days is None:
            continue
        tier = next((t for t in DOMAIN_TIERS if days <= t), None)
        if tier is None:
            if st["domain_alerted"].pop(d, None) is not None:   # healthy again -> reset + clear symptom
                alerts.append({"severity": "info", "issue_key": f"domain-{d}", "healed": True,
                               "text": f"DOMAIN {d} renewed / no longer near expiry."})
            continue
        if st["domain_alerted"].get(d) != tier:    # only alert once per tier crossed
            st["domain_alerted"][d] = tier
            # imminent (<=7d) = page; earlier tiers = silent digest
            sev = "critical" if days <= 7 else "warn"
            alerts.append({"severity": sev, "issue_key": f"domain-{d}", "healed": False,
                           "text": f"DOMAIN {d} expires in {int(days)}d — turn on auto-renew / renew now (storefronts go dark if it lapses)."})
    return alerts


def check_sessions(st: dict) -> list[dict]:
    alerts = []
    for name, path, max_days, heal in SESSIONS:
        p = Path(os.path.expanduser(path))
        if not p.exists():
            continue
        age_days = (time.time() - p.stat().st_mtime) / 86400.0
        if age_days <= max_days:
            if st["session_alerted"].pop(name, None) is not None:   # fresh again -> clear symptom
                alerts.append({"severity": "info", "issue_key": f"session-{name}", "healed": True,
                               "text": f"SESSION {name} fresh again — lane recovered."})
            continue
        if heal:
            try:
                subprocess.run(heal.split(), cwd=str(ROOT), capture_output=True, timeout=120)
            except Exception:
                pass
        if not st["session_alerted"].get(name):
            st["session_alerted"][name] = _now().isoformat()
            # early-warning: auto-heal already attempted -> silent digest
            alerts.append({"severity": "warn", "issue_key": f"session-{name}", "healed": False,
                           "text": f"SESSION {name} stale {int(age_days)}d — lane may be dark; tried auto-heal, may need a one-time login."})
    return alerts


def check_earning(st: dict) -> list[dict]:
    newest = 0.0
    for rel in EARNING_SIGNALS:
        p = ROOT / rel
        if p.exists():
            newest = max(newest, p.stat().st_mtime)
    if newest == 0.0:
        return []
    quiet_hrs = (time.time() - newest) / 3600.0
    if quiet_hrs <= EARNING_MAX_QUIET_HRS:
        if st.get("earning_alerted") is not None:   # loop alive again -> clear symptom
            st["earning_alerted"] = None
            return [{"severity": "info", "issue_key": "earning-loop", "healed": True,
                     "text": "EARNING loop active again — money engine recovered."}]
        st["earning_alerted"] = None
        return []
    today = _now().date().isoformat()
    if st.get("earning_alerted") == today:
        return []
    st["earning_alerted"] = today
    # soft heuristic, not an imminent emergency -> silent digest
    return [{"severity": "warn", "issue_key": "earning-loop", "healed": False,
             "text": f"EARNING loop quiet {int(quiet_hrs)}h — no outreach/funnel activity. The money engine may be stalled."}]


def _autoflow_last_events(mode: str):
    """Newest START and DONE (naive local datetimes, matching the log's %F %T stamps) or (None, None)."""
    log = ROOT / f"data/hustle/logs/autoflow_{mode}.log"
    if not log.exists():
        return None, None
    try:
        lines = log.read_text(errors="replace").splitlines()
    except Exception:
        return None, None
    pat = re.compile(r"=== (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) autoflow "
                     + re.escape(mode) + r" (START|DONE)\b")
    start = done = None
    # scan the whole (bounded ~4000-line) log: a HUNG run keeps appending agent output after its
    # START and never truncates, so a tail-only window could scroll the orphaned START out of view.
    for ln in lines:
        m = pat.search(ln)
        if not m:
            continue
        try:
            ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
        except Exception:
            continue
        if m.group(2) == "START":
            start = ts
        else:
            done = ts
    return start, done


def check_autoflow(st: dict, act: bool = True) -> list[dict]:
    """A hailports autoflow mode that logged START with no matching DONE for >2h has silently died;
    kickstart its launchd job (reuses the repo's watchdog kickstart idiom). Respects AUTOFLOW_OFF.
    act=False (report mode) reports the hang but performs no kickstart / no state mutation."""
    alerts: list[dict] = []
    if AUTOFLOW_OFF.exists():   # global kill-switch — do not resurrect the loops while halted
        if act:
            st["autoflow_alerted"] = {}
        return alerts
    seen = st.setdefault("autoflow_alerted", {})
    now = datetime.now()   # local wall-clock: the log stamps are local %F %T, not UTC
    for mode in AUTOFLOW_MODES:
        start, done = _autoflow_last_events(mode)
        if start is None:
            continue
        if done is not None and done >= start:   # latest run completed -> healthy
            if act and seen.pop(mode, None) is not None:
                alerts.append({"severity": "info", "issue_key": f"autoflow-{mode}", "healed": True,
                               "text": f"AUTOFLOW {mode} completed again — loop recovered."})
            continue
        hrs = (now - start).total_seconds() / 3600.0
        if hrs < AUTOFLOW_STUCK_HRS:
            continue   # still inside its normal run window (one attempt is timeout-capped < 2h)
        if not act:
            alerts.append({"severity": "warn", "issue_key": f"autoflow-{mode}", "healed": False,
                           "text": f"AUTOFLOW {mode} logged START {int(hrs)}h ago with no DONE — silent-death (report-only, not kickstarted)."})
            continue
        if seen.get(mode) == start.isoformat():
            continue   # already kickstarted this exact stuck run
        seen[mode] = start.isoformat()
        label = f"com.claude-stack.hailports-{mode}"
        try:
            subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}"],
                           capture_output=True, timeout=20)
        except Exception:
            pass
        alerts.append({"severity": "warn", "issue_key": f"autoflow-{mode}", "healed": False,
                       "text": f"AUTOFLOW {mode} logged START {int(hrs)}h ago with no DONE — silent-death; kickstarted {label}."})
    return alerts


def sweep(report_only: bool = False) -> dict:
    st = _load()
    records = (check_domains(st) + check_sessions(st) + check_earning(st)
               + check_autoflow(st, act=not report_only))
    if not report_only:
        for r in records:   # each concern routes through the gateway (allow-list/dedup/digest)
            _route(r["severity"], subject="⚙️ Eternal guardian: " + r["text"],
                   body="", issue_key=r["issue_key"], healed=r.get("healed", False))
        _save(st)
    alerts = [r["text"] for r in records if not r.get("healed")]
    return {"alerts": alerts, "checked": len(DOMAINS)}


if __name__ == "__main__":
    r = sweep(report_only="--report" in sys.argv)
    if r["alerts"]:
        print("\n".join(r["alerts"]))
    else:
        print(f"all clear — {r['checked']} domains, {len(SESSIONS)} sessions, "
              f"earning loop healthy, {len(AUTOFLOW_MODES)} autoflow modes")
