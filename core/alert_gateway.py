"""core/alert_gateway.py — the single gate every alert routes through.

Why this exists: alerters used to each call iMessage directly with their own
(often missing) dedup, so a flapping issue, a per-interval probe failure, or a
chronic-failure escalation produced a flood of texts. This module is the one
choke point that decides whether an alert pages Operator NOW, gets deduped, or rolls
into a periodic digest.

Policy (the guardrail):
  1. severity="critical" AND not self-healed -> page immediately (iMessage +
     best-effort Telegram + email), subject to a GLOBAL rate-limit (max N/hour).
     Overflow past the rate cap rolls into the digest instead of paging.
  2. severity="critical" repeated for the same issue_key inside the crit cooldown
     -> deduped (the flap/repeat is dropped, not re-paged).
  3. severity="warn"/"info" (low-sev) -> never page; coalesce into a 15-min
     DIGEST, one line per issue_key.
  4. healed=True -> register a fix: the issue's symptom alerts are suppressed for
     SUPPRESS_AFTER_FIX, and if it was actively paged a one-line recovery note
     rolls into the digest (not an immediate page).

Design notes:
  - Hard dependencies are stdlib only (json/os/time/fcntl/subprocess/hashlib/
    argparse). iMessage delivery is osascript. Telegram/email are best-effort
    lazy imports of core.alert's transport primitives, so a broken stack can't
    stop a CRIT from texting. This keeps the gateway usable as a true choke point
    even from the stdlib-only stack_heartbeat and from bash via the CLI.
  - Multi-process safe: state persists to data/alert_gateway_state.json with
    fcntl locking (every alerter runs in its own launchd job, concurrently).
  - Modeled on the proven hysteresis in engage-health-monitor.py: heal-first,
    per-issue re-alert cooldown, recovered/grace handling.

Reversible: one module + one state file + small edits in the alerter senders.
Disabling is `ALERT_GW_PASSTHROUGH=1` (every route() pages immediately, i.e. the
old behavior) or just reverting the sender edits.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import re
import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
STATE_FILE = BASE_DIR / "data" / "alert_gateway_state.json"

ALEX_PHONE = "XPHONEX"

# --- tunables (env-overridable so they stay configurable without code edits) ---
def _envf(name, default):
    try:
        return float(os.environ.get(name, "").strip() or default)
    except Exception:
        return default


MAX_PAGES_PER_HOUR = _envf("ALERT_GW_MAX_PER_HOUR", 6)      # global rate-limit
# Email is the noisiest leg (the owner was getting a flood of "[docsapp]" alert
# mails). iMessage is the guaranteed primary leg for every alert; email is now a
# strictly-capped secondary that only rides along with genuine CRIT pages, and
# the periodic digest is iMessage-only. Set ALERT_GW_DIGEST_EMAIL=1 to restore
# the old behavior of also emailing the digest.
MAX_EMAIL_PER_DAY = _envf("ALERT_GW_MAX_EMAIL_PER_DAY", 8)  # cap the email leg
_DIGEST_EMAIL = os.environ.get("ALERT_GW_DIGEST_EMAIL") == "1"
DIGEST_WINDOW = _envf("ALERT_GW_DIGEST_WINDOW", 15 * 60)    # batch low-sev/overflow
CRIT_COOLDOWN = _envf("ALERT_GW_CRIT_COOLDOWN", 6 * 3600)   # re-page same crit at most every N
WARN_COOLDOWN = _envf("ALERT_GW_WARN_COOLDOWN", 24 * 3600)  # re-digest same warn at most every N
SUPPRESS_AFTER_FIX = _envf("ALERT_GW_SUPPRESS_AFTER_FIX", 10 * 60)
PENDING_CAP = 80

_DRY = os.environ.get("ALERT_GW_DRY") == "1"
_PASSTHROUGH = os.environ.get("ALERT_GW_PASSTHROUGH") == "1"

LEVELS = ("info", "warn", "critical")

# In dry-run, deliveries are appended here instead of sent (for the self-test).
SENT_LOG: list[dict] = []


_IMSG_SCRIPT = (
    'on run {targetPhone, targetMsg}\n'
    '  tell application "Messages"\n'
    '    set svc to 1st service whose service type = iMessage\n'
    '    send targetMsg to buddy targetPhone of svc\n'
    '  end tell\n'
    'end run'
)


# Volatile numbers (disk=14.5G, ram_free=4.0G, ages, counts) make every flap of the
# SAME underlying issue hash differently -> it dodges dedup and re-pages/re-digests.
# Collapse digit-runs so a flapping resource alert shares ONE issue_key.
def _shape(text: str) -> str:
    return re.sub(r"\d+(?:\.\d+)?", "#", str(text))


def _issue_key(source, subject, issue_key):
    if issue_key:
        return str(issue_key)
    raw = f"{source}|{_shape(subject)}".encode()
    return "auto:" + hashlib.sha1(raw).hexdigest()[:16]


def _norm_level(level):
    level = (level or "warn").lower()
    if level in ("crit", "critical"):
        return "critical"
    if level in ("warn", "warning"):
        return "warn"
    if level in ("info", "ok", "notice"):
        return "info"
    return "warn"


@contextmanager
def _locked_state():
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not STATE_FILE.exists():
        STATE_FILE.write_text("{}")
    fd = os.open(str(STATE_FILE), os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        with os.fdopen(fd, "r+") as f:
            try:
                state = json.load(f) or {}
            except json.JSONDecodeError:
                state = {}
            state.setdefault("issues", {})       # issue_key -> {...}
            state.setdefault("pages", [])         # page timestamps (rate-limit window)
            state.setdefault("fixes", {})         # issue_key -> ts of last heal
            state.setdefault("pending", [])       # digest queue
            state.setdefault("last_digest", 0.0)
            state.setdefault("email_day", {})     # "YYYY-MM-DD" -> emails sent (cap)
            yield state
            f.seek(0)
            f.truncate()
            json.dump(state, f, indent=2)
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except Exception:
            pass


# ----------------------------- transports ----------------------------------

# ZERO-NOISE policy (owner 2026-06-08): iMessage ONLY for a real revenue event, a
# deal Operator must help close, or a true emergency. Everything else self-heals + logs
# silently — the stack runs uninterrupted and NEVER texts for routine ISSUES (warn/info).
# 2026-07-09: a CRITICAL survival alert (route severity="critical") now ALWAYS pages —
# it's page-worthy by the module's own contract; the lexicon was wrongly re-muting it,
# so domain-expiry / dead-auth / card-expiry / autologin died SILENTLY. Survival lexicon
# below is belt-and-suspenders for any path that isn't force-paged. Routine noise (warn/
# info) still never pages — the no-noise rule holds; only true emergencies get through.
_IMSG_ALLOW = ("revenue", "sale", "sold", " paid", "payment", "purchase", "stripe",
               "checkout", "first dollar", "new customer", "subscrib", " order",
               "deal", "close the", "close this", "hot lead", "ready to buy",
               "wants to buy", "wants to talk", "booked a", "book a call", "demo booked",
               "emergency", "🚨", "data loss", "account ban", "suspended", "backups down",
               "backups failing", "all backups", "machine down", "machine offline", "$",
               # survival-critical vocabulary (untended-stack anchors):
               "domain expir", "renew", "re-login", "relogin", "re-auth", "reauth",
               "auth expired", "auth failed", "login required", "sign in again",
               "token expired", "invalid_grant", "codex auth", "card expir",
               "autologin", "login window", "credit low", "balance low", "session expired",
               "session rot", "blackout", "untended", "unattended")

def _imsg_allowed(subject, body):
    if os.environ.get("ALERT_GW_FORCE_IMSG") == "1":
        return True
    t = f"{subject} {body}".lower()
    return any(k in t for k in _IMSG_ALLOW)

def _send_imessage(subject, body, force=False):
    msg = f"🔔 {subject}\n{body}"[:600]
    if not force and not _imsg_allowed(subject, body):
        SENT_LOG.append({"transport": "imessage-suppressed", "subject": subject, "body": body})
        return True  # silently logged; stack self-heals, no text for routine issues
    if _DRY:
        SENT_LOG.append({"transport": "imessage", "subject": subject, "body": body})
        return True
    try:
        r = subprocess.run(
            ["/usr/bin/osascript", "-e", _IMSG_SCRIPT, ALEX_PHONE, msg],
            capture_output=True, text=True, timeout=25,
        )
        return r.returncode == 0
    except Exception:
        return False


def _send_telegram_email(subject, body):
    """Best-effort secondary legs. Lazy import so a broken stack never blocks the
    iMessage leg, and so importing this module never pulls in core.alert eagerly
    (avoids an import cycle: core.alert routes THROUGH this module)."""
    if _DRY:
        SENT_LOG.append({"transport": "telegram_email", "subject": subject})
        return
    try:
        from core.alert import _send_telegram, _send_email
        _send_telegram(subject, body)
        _send_email(f"[docsapp] {subject}", body)
    except Exception:
        pass


def _deliver_immediate(subject, body, email=True, force=False):
    """iMessage always (guaranteed leg). Email only when the daily cap allows it
    (decided by the caller, since the budget lives in the locked state).
    force=True bypasses the money/emergency lexicon — used for CRITICAL pages, which
    are page-worthy by contract (survival alerts must not be silently muted)."""
    ok = _send_imessage(subject, body, force=force)
    if email:
        _send_telegram_email(subject, body)
    return ok


def _deliver_digest(text):
    if _DRY:
        SENT_LOG.append({"transport": "digest", "body": text})
        return
    _send_imessage("Stack digest", text)
    # Digest is iMessage-only by default — it used to email every flush (~every
    # 10min via the flush job), which was the bulk of the email flood.
    if _DIGEST_EMAIL:
        _send_telegram_email("Stack digest", text)


# ------------------------------ core routing -------------------------------

def _gc(state, now):
    state["pages"] = [t for t in state["pages"] if now - t < 3600]
    cutoff = now - max(CRIT_COOLDOWN, WARN_COOLDOWN) * 2
    state["issues"] = {
        k: v for k, v in state["issues"].items()
        if v.get("last_seen", 0) >= cutoff
    }
    fix_cut = now - SUPPRESS_AFTER_FIX * 2
    state["fixes"] = {k: t for k, t in state["fixes"].items() if t >= fix_cut}
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    state["email_day"] = {d: n for d, n in state.get("email_day", {}).items() if d == today}


def _claim_email_budget(state, now):
    """Reserve one slot of today's email budget. Returns True if granted.
    iMessage is unaffected; this only gates the noisy email leg."""
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    sent = state.setdefault("email_day", {}).get(today, 0)
    if sent >= MAX_EMAIL_PER_DAY:
        return False
    state["email_day"][today] = sent + 1
    return True


def _refund_email_budget(state, now):
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    cur = state.setdefault("email_day", {}).get(today, 0)
    if cur > 0:
        state["email_day"][today] = cur - 1


def _format_digest(pending, now):
    crit = [e for e in pending if e["severity"] == "critical"]
    warn = [e for e in pending if e["severity"] == "warn"]
    info = [e for e in pending if e["severity"] == "info"]
    rec = [e for e in pending if e["severity"] == "recovered"]
    lines = [f"Stack digest ({len(pending)} item(s), last {int(DIGEST_WINDOW/60)}min)"]
    for label, group in (("RATE-LIMITED CRIT", crit), ("WARN", warn),
                         ("RECOVERED", rec), ("INFO", info)):
        if not group:
            continue
        lines.append(f"\n— {label} ({len(group)}) —")
        # Collapse near-identical repeats (same source + same subject shape) into one
        # counted line so a flap doesn't fill the digest with 11 identical crits.
        collapsed: dict = {}
        for e in group:
            sig = (e["source"], _shape(e["subject"]))
            collapsed.setdefault(sig, []).append(e)
        for _sig, es in sorted(collapsed.items(), key=lambda kv: -max(x["ts"] for x in kv[1])):
            latest = max(es, key=lambda x: x["ts"])
            age = int((now - latest["ts"]) / 60)
            tag = f"({age}m, ×{len(es)})" if len(es) > 1 else f"({age}m)"
            lines.append(f"  • [{latest['source']}] {tag} {latest['subject']}")
    return "\n".join(lines)


def _maybe_flush(state, now):
    """If the digest window elapsed, pop pending and return digest text to send
    (outside the lock). Returns None if nothing to flush."""
    if not state["last_digest"]:
        # Fresh state: start the window now instead of flushing a 1-item digest
        # on the very first low-sev alert.
        state["last_digest"] = now
        return None
    if now - state["last_digest"] < DIGEST_WINDOW:
        return None
    pending = state["pending"]
    if not pending:
        state["last_digest"] = now
        return None
    state["pending"] = []
    state["last_digest"] = now
    # mark digested issues so WARN_COOLDOWN dedup works
    for e in pending:
        rec = state["issues"].get(e["issue_key"])
        if rec is not None:
            rec["last_digested"] = now
    return _format_digest(pending, now)


def route(severity, source, subject, body="", issue_key=None, healed=False,
          service=None):
    """The single entry point. Returns a dict describing what happened
    (action + issue_key) — handy for logging and the self-test.

    severity: "critical" | "warn" | "info"
    source:   short alerter name (e.g. "stack_heartbeat", "disk_guardian")
    subject:  one-line summary (also the dedup fallback key)
    issue_key: stable dedup key; defaults to hash(source+subject)
    healed:   True => this is a recovery/self-heal signal, suppress the symptom
    """
    now = time.time()
    sev = _norm_level(severity)
    ik = _issue_key(source, subject, issue_key)

    if _PASSTHROUGH:
        _deliver_immediate(subject, body)
        return {"action": "passthrough_paged", "issue_key": ik}

    to_page = None
    page_email = False   # whether this page also rides the (capped) email leg
    digest_text = None
    action = "noop"
    prev_last_paged = 0  # for rollback if the page fails to deliver

    with _locked_state() as state:
        _gc(state, now)

        if healed:
            state["fixes"][ik] = now
            rec = state["issues"].get(ik)
            was_active = bool(rec and rec.get("active"))
            if rec is not None:
                rec["active"] = False
                rec["last_seen"] = now
            if was_active:
                state["pending"].append({
                    "severity": "recovered", "source": source, "issue_key": ik,
                    "subject": f"recovered: {subject}", "ts": now,
                })
                action = "healed_recovery_digested"
            else:
                action = "healed_suppressed"
            digest_text = _maybe_flush(state, now)
        else:
            # suppressed if a heal just landed for this issue
            if now - state["fixes"].get(ik, 0) < SUPPRESS_AFTER_FIX:
                action = "suppressed_recent_fix"
                digest_text = _maybe_flush(state, now)
            else:
                rec = state["issues"].setdefault(ik, {"first_seen": now})
                rec["last_seen"] = now
                rec["active"] = True
                rec["severity"] = sev
                rec["source"] = source
                rec["subject"] = subject

                if sev == "critical":
                    if now - rec.get("last_paged", 0) < CRIT_COOLDOWN:
                        action = "deduped_crit"
                    elif len(state["pages"]) >= MAX_PAGES_PER_HOUR:
                        # over the global rate cap -> digest instead of page
                        if not any(p["issue_key"] == ik for p in state["pending"]):
                            state["pending"].append({
                                "severity": "critical", "source": source,
                                "issue_key": ik, "subject": subject, "ts": now,
                            })
                        action = "rate_limited_to_digest"
                    else:
                        prev_last_paged = rec.get("last_paged", 0)
                        rec["last_paged"] = now
                        state["pages"].append(now)
                        to_page = (subject, body)
                        page_email = _claim_email_budget(state, now)
                        action = "paged"
                else:
                    # low-sev: digest only, deduped by issue_key
                    if now - rec.get("last_digested", 0) < WARN_COOLDOWN:
                        action = "deduped_lowsev"
                    elif any(p["issue_key"] == ik for p in state["pending"]):
                        action = "already_pending"
                    else:
                        if len(state["pending"]) >= PENDING_CAP:
                            state["pending"] = state["pending"][-(PENDING_CAP - 1):]
                        state["pending"].append({
                            "severity": sev, "source": source, "issue_key": ik,
                            "subject": subject, "ts": now,
                        })
                        action = "queued_digest"
                digest_text = _maybe_flush(state, now)

    if to_page:
        # Only keep the page committed if delivery actually succeeded. A failed
        # iMessage would otherwise be deduped for CRIT_COOLDOWN and the live
        # incident silently dropped — so roll back last_paged/pages to let the
        # next tick retry.
        if not _deliver_immediate(*to_page, email=page_email, force=True):  # critical => always page (contract)
            with _locked_state() as state:
                rec = state["issues"].get(ik)
                if rec is not None and rec.get("last_paged") == now:
                    rec["last_paged"] = prev_last_paged
                try:
                    state["pages"].remove(now)
                except ValueError:
                    pass
                if page_email:
                    _refund_email_budget(state, now)
            action = "page_delivery_failed_retry"
    if digest_text:
        _deliver_digest(digest_text)

    return {"action": action, "issue_key": ik, "severity": sev}


# ----------------------------- convenience ---------------------------------

def page_critical(source, subject, body="", issue_key=None, healed=False):
    return route("critical", source, subject, body, issue_key=issue_key, healed=healed)


def notify(source, subject, body="", issue_key=None, level="warn", healed=False):
    return route(level, source, subject, body, issue_key=issue_key, healed=healed)


def register_fix(source, issue_key=None, subject=""):
    """A self-healer calls this on a verified heal so the matching alert drops."""
    return route("info", source, subject or "fix", "", issue_key=issue_key, healed=True)


def flush():
    """Force a digest flush now (for a launchd tick if ever wanted)."""
    now = time.time()
    text = None
    with _locked_state() as state:
        _gc(state, now)
        if state["pending"]:
            text = _format_digest(state["pending"], now)
            for e in state["pending"]:
                rec = state["issues"].get(e["issue_key"])
                if rec is not None:
                    rec["last_digested"] = now
            state["pending"] = []
        state["last_digest"] = now
    if text:
        _deliver_digest(text)
    return {"flushed": bool(text)}


def _cli():
    p = argparse.ArgumentParser(description="Central alert gateway")
    p.add_argument("--severity", default="warn")
    p.add_argument("--source", default="cli")
    p.add_argument("--subject", default="")
    p.add_argument("--body", default="")
    p.add_argument("--issue-key", default=None)
    p.add_argument("--healed", action="store_true")
    p.add_argument("--flush", action="store_true")
    args = p.parse_args()
    if args.flush:
        print(json.dumps(flush()))
        return 0
    res = route(args.severity, args.source, args.subject, args.body,
                issue_key=args.issue_key, healed=args.healed)
    print(json.dumps(res))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
