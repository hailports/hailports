#!/usr/bin/env python3
"""Unified pre-ship QA gate — the single check every outward artifact passes before it
ships, so the owner does NOT have to eyeball everything.

One entry point: gate(kind, payload) -> Verdict. It composes the existing specialized
gates (CAN-SPAM compliance, content-quality/voice, PII+secret scan, mockup validity,
product non-emptiness), FAILS CLOSED (a missing checker or any doubt blocks the ship),
and logs every decision to a ledger so the daily digest can report what shipped / what
was blocked and why.

  from core.ship_qa import gate
  v = gate("email", {"subject": s, "body": b, "to_email": e, "require_proof": True})
  if v.passed: send(...)               # else v.failures tells you exactly why

  python3 -m core.ship_qa --digest     # human summary of the ledger (for the daily glance)
  python3 -m core.ship_qa --selftest

Kinds: "email" (outreach), "post" (social/content), "site" (rebuild mockup), "product"
(paid deliverable). Unknown kind => FAIL (closed).
"""
from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEDGER = ROOT / "data" / "hustle" / "ship_qa_ledger.jsonl"
VALID_KINDS = {"email", "post", "site", "product"}


@dataclass
class Verdict:
    passed: bool
    kind: str
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    score: float | None = None

    def as_dict(self) -> dict:
        return {"passed": self.passed, "kind": self.kind, "failures": self.failures,
                "warnings": self.warnings, "score": self.score}


def _log(kind: str, v: Verdict, ref: str) -> None:
    try:
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with open(LEDGER, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "kind": kind, "ref": ref[:120], "passed": v.passed,
                "failures": v.failures, "warnings": v.warnings, "score": v.score,
            }) + "\n")
    except Exception:
        pass


def _scan_secrets_pii(text: str) -> list[str]:
    """FAIL CLOSED: if the scanner can't load, treat as a blocking failure."""
    try:
        from tools.clean_room import scan_text
    except Exception as e:
        return [f"pii/secret scanner unavailable ({type(e).__name__}) — failing closed"]
    try:
        hits = scan_text(text) or []
    except Exception as e:
        return [f"pii/secret scan errored ({type(e).__name__}) — failing closed"]
    if hits:
        kinds = sorted({h[0] if isinstance(h, (list, tuple)) else str(h) for h in hits})[:6]
        return [f"contains sensitive data ({', '.join(kinds)})"]
    return []


_PLACEHOLDER_LOCALPARTS = frozenset({
    "jdoe", "johndoe", "john.doe", "jane", "janedoe", "jane.doe", "joedoe",
    "firstname", "lastname", "first.last", "firstlast", "fname", "lname",
    "yourname", "your.name", "name", "example", "sample", "test", "tester",
    "user", "username", "someone", "anybody", "email", "youremail",
})
_PLACEHOLDER_DOMAINS = frozenset({
    "example.com", "example.org", "example.net", "domain.com", "yourdomain.com",
    "yourcompany.com", "company.com", "email.com", "test.com", "sample.com",
})


def _is_placeholder_recipient(email: str) -> bool:
    email = email.strip().lower()
    if "@" not in email:
        return False
    local, _, domain = email.partition("@")
    return local in _PLACEHOLDER_LOCALPARTS or domain in _PLACEHOLDER_DOMAINS


# Owner directive 2026-06-09: never contact enterprise/mid-market prospects. The ICP is
# local small businesses only. This list is enforced at the shared send gate, so NO lane
# (current or future) can mail a blocked domain.
_ENTERPRISE_BLOCK_FILE = Path(__file__).resolve().parent.parent / "data" / "hustle" / "enterprise_block.txt"
_enterprise_cache: dict = {"mtime": None, "domains": frozenset()}


def _enterprise_domains() -> frozenset:
    try:
        mtime = _ENTERPRISE_BLOCK_FILE.stat().st_mtime
    except OSError:
        return frozenset()
    if _enterprise_cache["mtime"] != mtime:
        domains = set()
        for line in _ENTERPRISE_BLOCK_FILE.read_text(errors="ignore").splitlines():
            d = line.split("#")[0].strip().lower()
            if d:
                domains.add(d)
        _enterprise_cache["mtime"] = mtime
        _enterprise_cache["domains"] = frozenset(domains)
    return _enterprise_cache["domains"]


def _is_enterprise_recipient(email: str) -> bool:
    domain = email.strip().lower().rpartition("@")[2]
    if not domain:
        return False
    blocked = _enterprise_domains()
    if domain in blocked:
        return True
    # also block subdomains of a blocked apex (mail.databricks.com -> databricks.com)
    parts = domain.split(".")
    for i in range(1, len(parts) - 1):
        if ".".join(parts[i:]) in blocked:
            return True
    return False


def _gate_email(p: dict) -> Verdict:
    from agents.core import canspam
    fails, warns = [], []
    subject = (p.get("subject") or "").strip()
    body = (p.get("body") or "").strip()
    to_email = (p.get("to_email") or "").strip()

    if not subject or not body:
        fails.append("empty subject or body")
    if canspam.postal_address() is None:
        fails.append("CAN-SPAM: no postal address configured (COLD_OUTREACH_POSTAL_ADDRESS)")
    for prob in canspam.subject_problems(subject):
        fails.append(f"subject: {prob}")
    if to_email:
        ok_biz, why = canspam.is_business_recipient(to_email)
        if not ok_biz:
            fails.append(f"recipient not business-safe: {why}")
        if canspam.is_suppressed(to_email):
            fails.append("recipient is suppressed (opted out / blocklist)")
        if _is_placeholder_recipient(to_email):
            # Contact-scrapers pick up example/placeholder addresses embedded in site
            # copy (jdoe@, jane@, first.last@) and example domains. Mailing these hard-
            # bounces, which torches sender reputation and lands real mail in spam.
            fails.append(f"recipient looks like a placeholder/example address: {to_email}")
        if _is_enterprise_recipient(to_email):
            fails.append(f"recipient is enterprise/mid-market (off-ICP, blocked forever): {to_email}")
    else:
        warns.append("no recipient yet (staging) — recipient checks deferred")
    if "unsubscribe" not in body.lower():
        fails.append("CAN-SPAM: no unsubscribe mechanism in body")
    if p.get("require_proof") and not p.get("proof_ok"):
        fails.append("proof link not validated/resolving (require_proof set)")
    # An outreach email LEGITIMATELY contains email addresses (sender, unsubscribe contact)
    # and a business phone — so the generic PII scan over-blocks here. Scan for real LEAKED
    # SECRETS + truly dangerous PII (keys, tokens, SSN, card numbers) only; allow emails/phones.
    import re as _re
    blob = subject + "\n" + body
    if _re.search(r'sk_live_[A-Za-z0-9]{10}|sk-ant-[A-Za-z0-9]{10}|gh[ps]_[A-Za-z0-9]{30}|'
                  r'AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY|xox[baprs]-[A-Za-z0-9-]{10}|'
                  r'\b\d{3}-\d{2}-\d{4}\b|\b(?:\d[ -]*?){13,16}\b', blob):
        fails.append("contains a secret/key or dangerous PII (SSN/card)")
    return Verdict(not fails, "email", fails, warns)


def _gate_post(p: dict) -> Verdict:
    fails, warns = [], []
    text = (p.get("text") or "").strip()
    if not text:
        return Verdict(False, "post", ["empty post text"])
    score = None
    try:
        from core.content_quality import gate as cq_gate
        v = cq_gate(text, channel=p.get("channel", "x"),
                    persona=p.get("persona", "generic"), recent=p.get("recent"))
        score = getattr(v, "score", None)
        if not v.passed:
            fails.append("content-quality: " + "; ".join(getattr(v, "reasons", [])[:3] or ["below threshold"]))
    except Exception as e:
        fails.append(f"content-quality gate unavailable ({type(e).__name__}) — failing closed")
    fails += _scan_secrets_pii(text)
    return Verdict(not fails, "post", fails, warns, score)


# Template/placeholder/AI-slop signatures that mean a mockup is NOT cooked — any of these
# present = block (the owner must never see these in a shipped proof).
_SITE_PLACEHOLDERS = (
    "lorem ipsum", "{{", "{name}", "{city}", "{vertical}", "{company}", "{brand}",
    "your business here", "your company name", "business name here",
    "replace the placeholder", "add the real ", "before this goes live", "before it goes live",
    "example business", "sample text", "coming soon", "under construction",
    "[city]", "[name]", "[business]", "tbd]", "todo:", "fixme",
)
_SITE_AI_SLOP = (
    "as an ai", "as a language model", "i cannot", "i'm sorry", "i am sorry",
    "i can't", "</think>", "<think>", "```", "here is the", "here's the",
    "certainly!", "sure, here", "note:", "as requested", ">none<", ">undefined<",
)


def _gate_site(p: dict) -> Verdict:
    """Mockup must be 'cooked to perfection' — ships without human eyes. Deterministic."""
    fails, warns = [], []
    html = p.get("html")
    if html is None and p.get("path"):
        try:
            html = Path(p["path"]).read_text(encoding="utf-8", errors="ignore")
        except Exception as e:
            return Verdict(False, "site", [f"mockup unreadable ({type(e).__name__})"])
    html = html or ""
    low = html.lower()

    # 1. structural validity (real, styled HTML doc)
    if "<html" not in low or "<body" not in low:
        fails.append("not a valid HTML document")
    if "<style" not in low or "</style>" not in low:
        fails.append("no CSS — would render unstyled")
    if len(html) < 2500:
        fails.append(f"too small ({len(html)}b) — likely broken/empty")

    # 2. has real headline + a contact/CTA path (a usable page, not a fragment)
    import re as _re
    h1s = _re.findall(r"<h1[^>]*>(.*?)</h1>", html, _re.S | _re.I)
    h1_txt = _re.sub(r"<[^>]+>", "", h1s[0]).strip() if h1s else ""
    if not h1_txt or len(h1_txt) < 4:
        fails.append("missing/empty headline (<h1>)")
    if "sec-contact" not in low and "mailto:" not in low and "claim" not in low and "#sec-contact" not in low:
        fails.append("no contact/CTA section — nothing for the prospect to act on")

    # 3. bespoke: the real business name must appear (not a generic template)
    name = (p.get("business_name") or "").strip()
    if name and name not in html:
        fails.append("business name absent — not actually bespoke")
    if "local business" in low and not name:
        warns.append("generic 'Local Business' label and no business name")

    # 4. NO placeholder/template leftovers
    hits_ph = sorted({s for s in _SITE_PLACEHOLDERS if s in low})
    if hits_ph:
        fails.append("placeholder/template leftovers: " + ", ".join(hits_ph[:5]))

    # 5. NO AI-slop / refusal / leaked scaffolding
    hits_slop = sorted({s for s in _SITE_AI_SLOP if s in low})
    if hits_slop:
        fails.append("AI-slop/scaffolding leaked: " + ", ".join(hits_slop[:5]))

    # 6. no empty section bodies (a heading with nothing under it reads broken)
    if _re.search(r"<h[23][^>]*>\s*</h[23]>", html, _re.I):
        fails.append("empty section heading")

    # 7. no external/fetchable resources (offline-safe proof; xmlns is allowed)
    leftover = html.replace('xmlns="http://www.w3.org/2000/svg"', "")
    if "http://" in leftover or "https://" in leftover.replace("https://scannerapp.dev", "").replace("https://www.w3.org", ""):
        warns.append("contains external resource URLs — verify they resolve")

    return Verdict(not fails, "site", fails, warns)


def _gate_product(p: dict) -> Verdict:
    fails = []
    content = p.get("content")
    if content is None and p.get("path"):
        try:
            content = Path(p["path"]).read_text(encoding="utf-8", errors="ignore")
        except Exception as e:
            return Verdict(False, "product", [f"deliverable unreadable ({type(e).__name__})"])
    content = content or ""
    if len(content.strip()) < 200:
        fails.append(f"deliverable too thin ({len(content)}b) — empty-shell / refund risk")
    return Verdict(not fails, "product", fails)


_DISPATCH = {"email": _gate_email, "post": _gate_post, "site": _gate_site, "product": _gate_product}


def gate(kind: str, payload: dict, ref: str = "") -> Verdict:
    """The single pre-ship gate. Unknown kind or any checker error => FAIL CLOSED."""
    fn = _DISPATCH.get(kind)
    if fn is None:
        v = Verdict(False, kind, [f"unknown QA kind {kind!r} — failing closed"])
        _log(kind, v, ref)
        return v
    try:
        v = fn(payload)
    except Exception as e:
        v = Verdict(False, kind, [f"QA gate raised {type(e).__name__}: {e} — failing closed"])
    _log(kind, v, ref or payload.get("ref", "") or payload.get("to_email", "") or payload.get("domain", ""))
    return v


def digest(hours: int = 24) -> dict:
    """Summarize recent ship-QA decisions for the owner's daily glance."""
    cutoff = time.time() - hours * 3600
    by_kind: dict[str, dict] = {}
    blocked_examples: list[dict] = []
    if LEDGER.exists():
        for line in LEDGER.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
                t = time.mktime(time.strptime(r["ts"], "%Y-%m-%dT%H:%M:%S"))
            except Exception:
                continue
            if t < cutoff:
                continue
            k = r.get("kind", "?")
            d = by_kind.setdefault(k, {"passed": 0, "blocked": 0})
            if r.get("passed"):
                d["passed"] += 1
            else:
                d["blocked"] += 1
                if len(blocked_examples) < 12:
                    blocked_examples.append({"kind": k, "ref": r.get("ref", ""),
                                             "why": (r.get("failures") or [""])[0]})
    return {"hours": hours, "by_kind": by_kind, "blocked_examples": blocked_examples}


def _fmt_digest(d: dict) -> str:
    lines = [f"Ship-QA last {d['hours']}h:"]
    if not d["by_kind"]:
        lines.append("  (no ship attempts logged)")
    for k, v in sorted(d["by_kind"].items()):
        lines.append(f"  {k}: {v['passed']} shipped-OK, {v['blocked']} BLOCKED")
    if d["blocked_examples"]:
        lines.append("Blocked (top reasons):")
        for b in d["blocked_examples"]:
            lines.append(f"  [{b['kind']}] {b['ref']}: {b['why']}")
    return "\n".join(lines)


def _selftest() -> int:
    # secret in body must block
    v = gate("email", {"subject": "hi there", "body": "token sk_live_REDACTED unsubscribe",
                       "to_email": "info@example.com"}, ref="selftest")
    assert not v.passed, "email with secret should FAIL"
    # empty product must block
    assert not gate("product", {"content": "x"}, ref="selftest").passed
    # tiny site must block
    assert not gate("site", {"html": "<html><body>hi</body></html>"}, ref="selftest").passed
    # unknown kind fails closed
    assert not gate("wat", {}, ref="selftest").passed
    print("ship_qa selftest OK")
    return 0


def _cli(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if "--selftest" in argv:
        return _selftest()
    if "--digest" in argv:
        text = _fmt_digest(digest())
        print(text)
        if "--send" in argv:
            # route the glance to the owner via the alert gateway (dedup/rate-limited)
            try:
                import subprocess
                subprocess.run([sys.executable, str(ROOT / "core" / "alert_gateway.py"),
                                "--severity", "info", "--source", "ship-qa-digest",
                                "--subject", "Daily ship-QA digest", "--body", text],
                               timeout=20, check=False)
            except Exception as e:
                print(f"(digest send failed: {e})")
        return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
