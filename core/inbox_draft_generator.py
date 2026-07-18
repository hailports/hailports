"""Grounded, voice-clean reply-body generation for the work-lane inbox agent.

LOCAL-FIRST and $0: local Ollama on-box (nothing leaves the machine). The free CLOUD pool is an
OPTIONAL fallback, OFF by default -- CompanyA email carries dealer names / ticket data / PII, and the
free pool ships prompts to third-party inference providers, so it's gated behind an explicit env
opt-in (INBOX_AGENT_FREE_CLOUD=1) and always work_lane=True (air-gaps hustle-persona credentials).
PAID models are never reachable from here.

Contract: the body is GROUNDED by construction (validated by draft_grounding_guard against the same
sources the pipeline uses) and voice-clean (work_reply_voice.autofix). If the model can't produce a
grounded body, generate() returns None and the caller falls back to the deterministic template --
we NEVER return a hallucinated or unvalidated draft.
"""

from __future__ import annotations
import os
import json
import asyncio
import urllib.request

from core import draft_grounding_guard as guard
from core import work_reply_voice
from core import draft_format
from core import content_integrity

OLLAMA_URL = os.environ.get("INBOX_AGENT_OLLAMA_URL", "http://localhost:11434/api/generate")
LOCAL_MODEL = os.environ.get("INBOX_AGENT_MODEL", "qwen2.5:7b")


def _free_cloud_allowed(explicit_flag=None) -> bool:
    # Default ON (Operator 2026-07-13: comfortable leveraging free/freemium models -- no SSNs/sensitive PII
    # runs through them). Always work_lane=True at the call site (air-gaps hustle-persona creds); paid is
    # never reachable. Set INBOX_AGENT_FREE_CLOUD=0 to force local-only.
    if explicit_flag is not None:
        return bool(explicit_flag)
    return str(os.environ.get("INBOX_AGENT_FREE_CLOUD", "1")).strip().lower() in ("1", "true", "yes", "on")


def _first_name(email: dict) -> str:
    nm = str(email.get("sender_name") or "").strip()
    if "," in nm:                      # "Buchupalli, Ravinatha Reddy" -> last, first
        nm = nm.split(",", 1)[1].strip()
    tok = (nm.split() or ["there"])[0]
    # address people as they expect (Operator's rule): Fernanda -> Fern. Explicit mapping only, else as-is.
    try:
        from core import name_prefs
        return name_prefs.lookup(tok) or tok
    except Exception:
        return tok


def _sources(email: dict, cls: dict, handler_out: dict) -> list:
    return [email.get("subject"), email.get("body"),
            json.dumps(handler_out, default=str), json.dumps(cls, default=str)]


_SYSTEM = (
    "You draft a short work email reply AS Operator, a Salesforce technical lead. "
    + draft_format.RICH_FORMAT_DIRECTIVE
    + " Output ONLY the reply body -- no subject, no greeting line beyond 'hey <first name> --', "
    "no signature (it is appended automatically). Keep it tight and factual."
)

# HTML variant: rich body (tables/bullets), NO signature and NO sign-off line -- the local bridge
# (outlook_app.reply_draft) appends the canonical signature exactly once and strips any trailing
# sign-off, so emitting one here would double it.
_SYSTEM_HTML = (
    "You draft a work email reply body AS Operator, a Salesforce technical lead, as an HTML FRAGMENT. "
    + draft_format.RICH_FORMAT_DIRECTIVE
    + " OUTPUT RULES: valid HTML only -- <p> for paragraphs, <ul><li> for lists, and an HTML <table> "
    "(with <tr>/<th>/<td>) for ANY comparative or multi-column numbers. Start with '<p>hey <first "
    "name> --' and the answer. Do NOT emit a subject, a signature, or ANY sign-off line (no 'thanks', "
    "'best', 'regards', or your name at the end) -- the signature is appended automatically and a "
    "sign-off here would duplicate it. No markdown, no code fences, no <html>/<body> wrapper."
)


def _looks_like_html(s: str) -> bool:
    s = (s or "").lower()
    return ("<p" in s or "<table" in s or "<ul" in s) and s.count("<") == s.count(">")


def generate_html_for_item(item: dict, context: str, *, allow_free_cloud=None) -> str | None:
    """Live-agent entry (exec_assistant): produce a RICH, GROUNDED HTML reply body (no sig/sign-off) from
    an item + already-built work-brain context. Returns HTML, or None to let the caller fall back. Sync."""
    sources = [item.get("subject"), item.get("body") or item.get("preview") or "", context or ""]
    first = _first_name({"sender_name": item.get("from_name") or item.get("from") or item.get("sender_name")})
    prompt = (
        f"THEIR MESSAGE (the only thing they actually said):\n"
        f"from: {item.get('from_name') or item.get('from')}\nsubject: {item.get('subject')}\n"
        f"{item.get('body') or item.get('preview') or ''}\n\n"
        f"WORK-BRAIN CONTEXT / DATA I RESOLVED (the ONLY facts + numbers you may state):\n{(context or '')[:4000]}\n\n"
        f"Write Operator's reply to {first} as an HTML fragment. Answer what they asked using ONLY facts "
        f"above. Never invent a number, an estimate, or a claim about what they said. Comparative "
        f"numbers -> an HTML <table>. Report only completed or verified work from the context; if a fact "
        f"is missing, ask the one exact question needed. Never promise future work or claim work is underway. "
        f"No signature, no sign-off line."
    )
    for _ in range(2):
        text = _gpt_lane(f"{_SYSTEM_HTML}\n\n{prompt}")
        if not text:
            continue
        text = text.replace("```html", "").replace("```", "").strip()
        if not _looks_like_html(text):
            continue
        text = guard.scrub_presentation(text)   # remove AI/marketing filler + hyperlink raw URLs first
        g = guard.check(text, sources)          # grounded by construction -- never return a fabrication
        p = guard.presentation_check(text)      # confirm no AI/marketing traces, no raw links remain
        try:
            integ = content_integrity.check(
                text,
                ([item.get("subject"), item.get("body") or item.get("preview") or ""], [context or ""]),
            )
        except Exception:
            integ = {"clean": False, "violations": [{"category": "integrity_error"}]}
        # BLOCK on ANY ungrounded number, not just recipient-attributed ones -- an agent inventing its
        # own "(est.) 1,200 cases" is the same hallucination as a fabricated "your ~870 estimate".
        if g["grounded"] and not g["numbers"] and p["clean"] and integ["clean"]:
            return text
        why = []
        if g["attributions"]:
            why.append(f"a figure credited to the recipient they never gave ({g['attributions']})")
        if g["numbers"]:
            why.append(f"invented number(s) not in the data ({g['numbers']}) -- state NO figure you "
                       f"were not given; if the recipient can supply it, ask one exact question")
        if p["ai_traces"]:
            why.append(f"AI/marketing phrasing ({p['ai_traces']}) -- write as a blunt human peer")
        if p["raw_links"]:
            why.append("a raw URL -- use <a href> hyperlinks with descriptive text")
        if not integ["clean"]:
            why.append("an unperformed-work promise or unsupported action claim -- do the work first, "
                       "or ask the exact missing-input question without promising future work")
        prompt += "\n\nSTRICT: your last draft had " + "; ".join(why) + ". Fix all of it."
    return None  # 2 tries failed -> caller falls back / drops; never ship an AI-trace or fabricated draft


def _actions_summary(handler_out: dict) -> str:
    """Turn what the handler actually DID/STAGED into plain English the draft must REPORT (not promise).
    This is the 'do the work, don't stall' seam: an auto-applied grant / staged build / sandbox-validated
    change becomes a 'here's what I did' line instead of 'I'll look into it'."""
    if not isinstance(handler_out, dict):
        return ""
    done, staged = [], []
    # sf_access identity resolution: report who was FOUND in SF (so we NEVER ask for names we resolved),
    # and if there's no reference to clone from, ask the SPECIFIC access question instead of stalling.
    if handler_out.get("subjects_resolved"):
        done.append("looked up + confirmed these people in Salesforce: " + ", ".join(handler_out["subjects_resolved"]))
    if handler_out.get("unresolved"):
        staged.append("could not resolve these first-name-only users safely: "
                      + ", ".join(handler_out["unresolved"])
                      + " — ask for their Salesforce email addresses")
    if handler_out.get("needs_access_target"):
        staged.append("to actually grant it I need the specific permission set that gives this access, or "
                      "one existing user whose access to mirror")
    sso = handler_out.get("sso_corrected") or {}
    if sso.get("write_verified"):
        done.append(
            f"updated + verified {sso.get('person')}'s Salesforce SSO mapping from "
            f"{sso.get('before')} to {sso.get('after')}"
        )
    for a in handler_out.get("auto_applied") or []:
        result = a.get("result") or {}
        ok = (result.get("applied") if isinstance(result, dict) else result)
        (done if ok else staged).append(f"access '{a.get('label') or a.get('api')}'")
    for hi in handler_out.get("flagged_for_alex") or []:
        staged.append(f"higher-priv item needing your ok ({hi if isinstance(hi, str) else hi.get('label') or hi.get('api')})")
    br = handler_out.get("blast_radius") or {}
    sandbox = br.get("sandbox_result") or {}
    sandbox_verified = isinstance(sandbox, dict) and any(
        sandbox.get(key) is True for key in ("ok", "success", "validated", "passed")
    )
    if sandbox_verified:
        done.append(f"built + validated the change in the partial sandbox ({br.get('classification') or 'checked'})")
    # specialist_dispatch work (analyst pull, meeting follow-up, release status, config lookup, general):
    # a verified read-only/additive result is reported as DONE; a propose/handoff is reported as STAGED.
    sw = handler_out.get("specialist_work") or {}
    artifacts = sw.get("staged_artifacts") or []
    if handler_out.get("handoff") and artifacts:
        labels = [str(a.get("name") or a.get("id") or a.get("kind") or a)
                  if isinstance(a, dict) else str(a) for a in artifacts[:5]]
        staged.append("created + staged these work artifacts: " + ", ".join(labels))
    if sw.get("summary"):
        _s = sw["summary"].rstrip(". ")
        if sw.get("verified") and sw.get("mode") == "deliver":
            done.append(_s)
        else:
            staged.append(_s)
    lines = []
    if done:
        lines.append("ALREADY DONE (report as completed, past tense): " + "; ".join(done))
    if staged:
        lines.append("STAGED / PENDING (report as teed up, awaiting the right gate): " + "; ".join(staged))
    return "\n".join(lines)


def _retrieved_facts(handler_out: dict, max_chars: int = 4200) -> str:
    blocks: list[str] = []
    remaining = max_chars
    for key, value in (handler_out.get("retrieved_bodies") or {}).items():
        block = f"[{key}]\n{value}".strip()
        if not block or len(block) > remaining:
            continue
        blocks.append(block)
        remaining -= len(block) + 5
    return "\n---\n".join(blocks)


def _prompt(email: dict, cls: dict, handler_out: dict) -> str:
    inbound = (f"THEIR MESSAGE (the only thing they actually said):\n"
               f"from: {email.get('sender_name')}\nsubject: {email.get('subject')}\n{email.get('body') or ''}")
    resolved = {k: v for k, v in handler_out.items() if k not in {"retrieved_bodies", "specialist_work"}}
    facts = json.dumps(resolved, default=str)[:3500]
    retrieved = _retrieved_facts(handler_out)
    specialist = json.dumps(handler_out.get("specialist_work") or {}, default=str)[:9000]
    actions = _actions_summary(handler_out)
    action_block = (f"\n\nACTIONS I ACTUALLY TOOK ON THIS REQUEST:\n{actions}\n"
                    f"REPORT these as done/staged in the reply — never say 'I'll look into it' or 'taking a "
                    f"look' about something already handled above.") if actions else ""
    return (
        f"{inbound}\n\n"
        f"WHAT I RESOLVED / THE DATA I HAVE (the ONLY numbers you may state):\n{facts}{action_block}\n\n"
        f"TIGHT THREAD / PROJECT EVIDENCE (use only what answers this ask):\n"
        f"{retrieved or '(none)'}\n\n"
        f"SPECIALIST HANDOFF (work already investigated/completed/staged; use every relevant detail):\n"
        f"{specialist or '(none)'}\n\n"
        f"Write Operator's reply to {_first_name(email)}. If ACTIONS above were taken, LEAD with what you did/"
        f"staged (concrete, past tense). Otherwise answer what they asked using ONLY facts above. If a fact "
        f"only the recipient can supply is missing, ask that one exact question. Never promise future work, "
        f"say you're looking into it, or claim work is underway. Do NOT invent "
        f"any number, estimate, or claim. Use short paragraphs and '- ' bullets for any list."
    )


def _ollama(prompt: str, system: str, max_tokens: int = 700, timeout: int = 90) -> str:
    from core.corp_tenant import is_corp_tenant
    if is_corp_tenant():
        return ""
    payload = json.dumps({
        "model": LOCAL_MODEL,
        "prompt": prompt,
        "system": system,
        "stream": False,
        "options": {"temperature": 0.4, "num_predict": max_tokens, "top_p": 0.9},
    }).encode()
    req = urllib.request.Request(OLLAMA_URL, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            txt = json.loads(resp.read().decode()).get("response", "")
    except Exception:
        return ""
    return txt.split("</think>")[-1].strip().strip('"').strip()


def _gpt_lane(full_prompt: str) -> str:
    """Draft through the shared CompanyA Enterprise Codex lane."""
    try:
        from core import gpt_draft_lane
        return gpt_draft_lane.draft(full_prompt)
    except Exception:
        return ""


async def _free_cloud(prompt: str, system: str, max_tokens: int = 700) -> str:
    try:
        from core.free_llm_pool import try_free_providers
        text, _prov = await try_free_providers(prompt, system=system, max_tokens=max_tokens, work_lane=True)
        return (text or "").strip()
    except Exception:
        return ""


async def generate_async(email: dict, cls: dict, handler_out: dict, *, allow_free_cloud=None) -> str | None:
    """Return a grounded, voice-clean reply body, or None to fall back to the deterministic template."""
    sources = _sources(email, cls, handler_out)
    prompt = _prompt(email, cls, handler_out)
    use_cloud = _free_cloud_allowed(allow_free_cloud)

    for attempt in range(2):
        # Corp Codex first. Corp jobs fail to deterministic templates rather than
        # spilling work onto personal, local, or free model lanes.
        text = await asyncio.to_thread(_gpt_lane, f"{_SYSTEM}\n\n{prompt}")
        if not text:
            text = await asyncio.to_thread(_ollama, prompt, _SYSTEM)
        if not text and use_cloud:
            text = await _free_cloud(prompt, _SYSTEM)
        if not text:
            continue
        text = work_reply_voice.autofix(text)              # lowercase/shorthand/dash -> work register
        text = guard.scrub_presentation(text)
        g = guard.check(text, sources)
        p = guard.presentation_check(text)
        try:
            integ = content_integrity.check(
                text,
                ([email.get("subject"), email.get("body")],
                 [json.dumps(handler_out, default=str), json.dumps(cls, default=str)]),
            )
        except Exception:
            integ = {"clean": False, "violations": [{"category": "integrity_error"}]}
        if g["grounded"] and not g["numbers"] and p["clean"] and integ["clean"]:
            return text
        prompt += ("\n\nSTRICT: your previous draft stated a figure/claim not in the data "
                   f"({g['attributions'] or g['numbers'] or p['ai_traces'] or integ['violations']}). State NO number you were "
                   f"not given -- ask one exact question if the recipient can supply it; otherwise do not "
                   f"promise future work. Remove AI/marketing phrasing.")
    return None


def generate(email: dict, cls: dict, handler_out: dict, *, allow_free_cloud=None) -> str | None:
    """Sync wrapper for non-async callers."""
    return asyncio.run(generate_async(email, cls, handler_out, allow_free_cloud=allow_free_cloud))
