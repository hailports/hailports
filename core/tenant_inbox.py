#!/usr/bin/env python3
"""Tenant inbox agent — the generic per-person inbox assistant.

Replicates the CAPABILITY of Operator's inbox pipeline (read unread → decide if it needs a
reply → draft in the person's voice → verify → stage to their review queue) WITHOUT any of
his employer/CRM business logic. Parameterized by a TenantProfile, so the same code
serves Operator2 (tenant #2) or anyone onboarded. Draft-ONLY: it never sends — every reply is
staged to the tenant's own review queue for them to send, same fence as the owner pipeline.

Reader (read_unread) drives the tenant's CONNECTED accounts via their captured session; it
returns a not-connected status until onboarding has captured a session. classify/draft/verify/
stage are pure and fully testable with injected emails.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from core import tenant_profile as tp

ROOT = Path(__file__).resolve().parent.parent

# Voice specs (Phase D replaces the named default with one derived from the person's real
# sent mail). Kept deliberately generic + warm; first-person AS the tenant, never as Operator.
_VOICE_SPECS = {
    "neutral_warm": ("Write a short, warm, professional email reply in first person as {name}. "
                     "Plain and direct, no corporate filler, no over-formality. 2-5 sentences. "
                     "Sound like a real busy person, not an assistant."),
    "nicole_neutral_warm": ("Write a short, warm, professional email reply in first person as "
                            "{name}. Friendly and concise, no corporate filler. 2-5 sentences."),
}

_NO_REPLY_HINTS = ("no-reply", "noreply", "donotreply", "notification", "newsletter",
                   "unsubscribe", "receipt", "do not reply")
_QUESTION_RX = re.compile(r"\?|\b(can you|could you|would you|let me know|please|when|are you|"
                          r"do you|will you|what time|confirm|available|thoughts|circle back)\b", re.I)


def _text_of(email: dict) -> str:
    return f"{email.get('subject','')}\n{email.get('body') or email.get('snippet','')}".strip()


def classify(email: dict) -> dict:
    """Decide if this email wants a human reply. Deterministic, $0 — no LLM."""
    sender = str(email.get("from") or email.get("sender") or "").lower()
    body = _text_of(email).lower()
    if any(h in sender for h in _NO_REPLY_HINTS) or any(h in body for h in _NO_REPLY_HINTS):
        return {"needs_reply": False, "reason": "automated/no-reply"}
    if email.get("is_read"):
        return {"needs_reply": False, "reason": "already read"}
    if _QUESTION_RX.search(_text_of(email)):
        return {"needs_reply": True, "reason": "question/request detected"}
    # short direct message addressed to the person likely wants an ack
    if len(body) < 1200 and (email.get("to_me") or email.get("direct")):
        return {"needs_reply": True, "reason": "direct message"}
    return {"needs_reply": False, "reason": "no clear ask"}


def _run_async(coro):
    """Await a coroutine from sync code, whether or not a loop is already running."""
    import asyncio
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(1) as ex:
            return ex.submit(lambda: asyncio.run(coro)).result()
    return (loop or asyncio.new_event_loop()).run_until_complete(coro)


def _generate(system: str, prompt: str, max_tokens: int = 400, *, local_ok: bool = False) -> str:
    """Draft generation for tenant agents. FREE-CLOUD-FIRST and, by default, free-cloud-ONLY:
    tenant work-AI runs on the free pool (groq/cerebras/deepseek/…) to keep the mini's CPU/RAM
    pressure LOW (Operator 2026-07-16 — the box is overloaded; her stuff must not add local load).
    Local is only used when explicitly allowed (local_ok=True); otherwise a free-pool miss
    returns "" and the email is deferred/escalated rather than burning local compute."""
    try:
        from core.free_llm_pool import try_free_providers
        r = _run_async(try_free_providers(prompt, system=system, max_tokens=max_tokens))
        if isinstance(r, dict):
            txt = r.get("text") or r.get("content")
        elif isinstance(r, (tuple, list)):
            txt = r[0] if r else None   # try_free_providers returns (text, provider)
        else:
            txt = r
        if txt and str(txt).strip():
            return str(txt).strip()
    except Exception:
        pass
    if local_ok:
        try:
            from core import local_client
            out = _run_async(local_client.chat([{"role": "user", "content": prompt}],
                                               system=system, max_tokens=max_tokens))
            if out and str(out).strip():
                return str(out).strip()
        except Exception:
            pass
    return ""


def draft_reply(email: dict, profile: "tp.TenantProfile") -> str:
    """Draft a reply in the tenant's voice. Grounds the small model with the fable scaffold."""
    spec = _VOICE_SPECS.get(profile.voice_profile) or _VOICE_SPECS["neutral_warm"]
    system = spec.format(name=profile.display_name)
    first = str(email.get("from_name") or email.get("from") or "there").split()[0]
    task = (f"Reply to this email from {first}. Answer their actual ask directly.\n\n"
            f"SUBJECT: {email.get('subject','')}\n\nBODY:\n{email.get('body') or email.get('snippet','')}")
    try:
        from core import fable_runtime
        scaffold = fable_runtime.scaffold_for(task)
        if scaffold:
            system = f"{system}\n\n{scaffold}"
    except Exception:
        pass
    return _clean_draft(_generate(system, task))


def _clean_draft(text: str) -> str:
    """Strip model artifacts: wrapping quotes/parens and a leading 'Subject:' line
    (reply bodies don't carry a subject)."""
    d = (text or "").strip()
    d = re.sub(r'^[(\["\']+', "", d).strip()
    d = re.sub(r'[)\]"\']+$', "", d).strip()
    d = re.sub(r'^\s*subject:.*(?:\n|$)', "", d, flags=re.I).strip()
    return d.replace("\\n", "\n").strip()


def verify(draft: str, email: dict) -> dict:
    """Cheap safety/quality gate. Never ships an empty or obviously broken draft."""
    d = (draft or "").strip()
    if len(d) < 8:
        return {"ok": False, "reason": "empty/too short"}
    if any(t in d.lower() for t in ("as an ai", "language model", "i cannot", "[insert")):
        return {"ok": False, "reason": "AI-tell / placeholder"}
    return {"ok": True, "reason": "passed"}


def stage(profile: "tp.TenantProfile", email: dict, draft: str, meta: dict | None = None) -> bool:
    """Append a DRAFT-ONLY entry to the tenant's own review queue. Never sends."""
    from core import tenant_firewall as fw
    try:
        q = fw.guard_path(profile.tenant_id, profile.review_queue)  # fail-closed: must be in her sandbox
    except fw.TenantFirewallError:
        return False
    try:
        q.parent.mkdir(parents=True, exist_ok=True)
        entry = {"tenant": profile.tenant_id, "status": "draft", "ts": None,
                 "from": email.get("from"), "subject": email.get("subject"),
                 "message_id": email.get("id") or email.get("message_id"),
                 "draft": draft, "meta": meta or {}}
        with q.open("a") as fh:
            fh.write(json.dumps(entry) + "\n")
        try:
            q.chmod(0o600)
        except Exception:
            pass
        return True
    except Exception:
        return False


def read_unread(profile: "tp.TenantProfile", limit: int = 20) -> dict:
    """Read unread mail for the tenant's connected accounts via their captured session.
    Returns {connected, emails, status}. Live webmail scraping (OWA/Gmail via storage_state)
    lights up once onboarding has captured a session — until then, reports not-connected."""
    try:
        from tools import tenant_secrets
        connected = []
        for acct in profile.accounts:
            if acct.get("provider") in ("microsoft", "google"):
                p = tenant_secrets.session_path(profile.tenant_id, acct["key"])
                if p.exists() and p.stat().st_size > 100:
                    connected.append(acct["key"])
        if not connected:
            return {"connected": False, "emails": [],
                    "status": "no account connected yet — onboarding must capture a session first"}
        # TODO(live): drive OWA/Gmail with the captured storage_state to list unread.
        # Interface is ready; the scrape is built against a real captured session.
        return {"connected": True, "emails": [], "status": f"connected: {connected} (live read pending)"}
    except Exception as exc:
        return {"connected": False, "emails": [], "status": f"reader error: {exc}"}


def process_tenant_inbox(tenant_id: str, emails: list[dict] | None = None, dry: bool = True) -> dict:
    """Run the full pipeline for a tenant. Pass `emails` to test; else reads their inbox."""
    from core import tenant_firewall as fw
    fw.assert_tenant(tenant_id)  # FAIL-CLOSED: never run this pipeline as the owner
    profile = tp.load_by_tenant(tenant_id) or tp.load(tenant_id)
    if not profile:
        return {"ok": False, "error": f"no tenant profile for {tenant_id}"}
    fw.assert_tenant(profile.tenant_id)  # profile's tenant must also be non-owner
    if not profile.inbox_bot_enabled:
        return {"ok": False, "error": f"inbox bot not enabled for {tenant_id}"}
    src = "injected"
    if emails is None:
        rr = read_unread(profile)
        if not rr["connected"]:
            return {"ok": True, "tenant": tenant_id, "connected": False, "status": rr["status"],
                    "drafted": 0, "results": []}
        emails, src = rr["emails"], "live"
    results = []
    drafted = 0
    for em in emails:
        cls = classify(em)
        if not cls["needs_reply"]:
            results.append({"subject": em.get("subject"), "action": "skip", "reason": cls["reason"]})
            continue
        draft = draft_reply(em, profile)
        v = verify(draft, em)
        if not v["ok"]:
            results.append({"subject": em.get("subject"), "action": "escalate", "reason": v["reason"]})
            continue
        staged = (not dry) and stage(profile, em, draft, {"classify": cls})
        drafted += 1
        results.append({"subject": em.get("subject"), "action": "drafted",
                        "staged": bool(staged), "draft_preview": draft[:120]})
    return {"ok": True, "tenant": tenant_id, "connected": True, "source": src,
            "drafted": drafted, "results": results}


if __name__ == "__main__":
    import sys
    tid = sys.argv[1] if len(sys.argv) > 1 else "Operator2"
    print(json.dumps(process_tenant_inbox(tid, dry=True), indent=2, default=str))
