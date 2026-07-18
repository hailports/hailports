#!/usr/bin/env python3
"""qchat_relay.py — owner-reply -> web-session relay (inbound leg of the qualifier chat).

The qualifier handoff (qchat_backend._handoff) texts the owner (XPHONEX) a lead
with a 4-hex token, e.g.  "— reply  #3F2A <your reply>  to answer live".

This reads the owner's iMessage replies from chat.db (read-only), matches the
"#TOKEN ..." prefix to a qualifier session, and writes the reply into that session's
msgs table as role='owner'. The web widget's /api/qchat/poll then surfaces it to the
visitor as "Sam" — LIVE feel — with the token stripped so no internal detail leaks.

Reuses the exact chat.db poll + last_rowid bookkeeping pattern of tools/imsg_bridge.py.
Run on a short launchd interval (e.g. every 20-30s) for snappy delivery, or call
relay_once() from the existing imsg_bridge pass. Async fallback: if the visitor has
left, the row is still stored + the owner already has their contact, so the reply is
delivered out-of-band (Sam emails/texts them); the widget replays it if they return.

$0 / local-first. Owner-only handle is honored — nothing else is read.

The owner's "#TOKEN ..." reply is no longer relayed VERBATIM. It is treated as a
SHORTHAND DIRECTIVE and composed into a polished, on-brand, ANON customer-facing reply
via core.llm_router.try_local_then_api (local Ollama first; paid path OFF). The brand's
voice/persona (from core.brand_registry by the session's brand) + the conversation context
are passed in; the model is told to invent NOTHING (no price/promise/claim the owner didn't
say — if the directive is ambiguous it writes a brief honest clarifier, never fabricates),
and never to reveal the owner/AI. The output is grounded-guarded like prospect_enrichment
(any $ amount / link / email / guarantee in the reply must appear in the directive or
context, else it's rejected). If the local model is down OR the directive starts with the
literal verbatim marker '=' , we post the text as-is. The #TOKEN->session mapping + status
flip are unchanged.
"""
from __future__ import annotations

import os
import re
import json
import time
import sqlite3
from pathlib import Path

ROOT = Path(os.environ.get("CLAUDE_STACK_DIR", Path(__file__).resolve().parents[1]))
QDB = ROOT / "data" / "hustle" / "qchat.db"
CHAT_DB = os.path.expanduser("~/Library/Messages/chat.db")
STATE = os.path.expanduser("~/.qchat_relay_state.json")
OWNER = "XPHONEX"                       # only this handle is honored
TOKEN_RE = re.compile(r"#\s*([0-9A-Fa-f]{4})\b\s*(.*)", re.S)


def _load_state() -> dict:
    try:
        return json.load(open(STATE))
    except Exception:
        return {}


def _save_state(s: dict):
    try:
        json.dump(s, open(STATE, "w"))
    except Exception:
        pass


def _qdb():
    QDB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(QDB, timeout=10)
    c.execute("PRAGMA journal_mode=WAL")
    return c


# Neutral, anon storefront voice used when the session's brand can't be resolved.
# "Sam" is the universal qchat persona the widget already shows; brand_name is left
# empty so we never invent a brand the owner didn't name.
_DEFAULT_BRAND = {
    "persona_name": "Sam",
    "brand_name": "",
    "voice": ("friendly, plain-spoken small-business helper — concise, honest, no hype, "
              "no jargon, no emojis"),
    "angle": "",
}

_CLOSE_CMDS = ("/close", "close", "/done")
_INTENT_LABEL = {
    "scan": "a free site / org scan", "buy": "pricing", "leads": "lead lists",
    "broken": "something broken on their site", "looking": "just looking",
}

# Grounding guard (like prospect_enrichment): a composed reply may not introduce a price,
# link, email, percentage, or guarantee the owner's directive / the conversation didn't.
_MONEY_RE = re.compile(
    r"\$\s?\d[\d,]*(?:\.\d+)?|\b\d+\s*(?:dollars|usd|/mo|/month|per month|a month|k/mo)\b", re.I)
_PCT_RE = re.compile(r"\b\d{1,3}\s?%")
_URL_RE = re.compile(r"https?://\S+|\b[\w.-]+\.(?:com|net|org|io|dev|app|co|ai)\b", re.I)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.\w+")
_GUARANTEE_RE = re.compile(r"\b(guarantee[ds]?|promise[ds]?|money[- ]back|refund(?:ed)?)\b", re.I)


def _brand_for_session(c, sid: str) -> dict:
    """Resolve the session's brand voice/persona. The session row carries no brand column,
    so we look for a brand hint in its captured attribution (brand/utm_source/c) and resolve
    it through core.brand_registry; otherwise the neutral anon default voice is used."""
    try:
        from core import brand_registry
    except Exception:
        return _DEFAULT_BRAND
    try:
        r = c.execute("SELECT attribution FROM sessions WHERE sid=?", (sid,)).fetchone()
        attr = json.loads(r[0]) if r and r[0] else {}
    except Exception:
        attr = {}
    for v in (attr.get("brand"), attr.get("utm_source"), attr.get("c")):
        if not v:
            continue
        try:
            b = brand_registry.get_brand_by_key(str(v)) or brand_registry.get_brand(str(v))
        except Exception:
            b = None
        if b:
            return b
    return _DEFAULT_BRAND


def _build_transcript(c, sid: str) -> str:
    """Compact conversation context for the composer: the qualifier summary the visitor
    gave, plus the live back-and-forth so far (visitor + prior owner turns)."""
    parts: list[str] = []
    try:
        r = c.execute("SELECT intent, business, website, message FROM sessions WHERE sid=?",
                      (sid,)).fetchone()
        if r:
            intent, business, website, message = r
            if intent:
                parts.append(f"Customer came in about: {_INTENT_LABEL.get(intent, intent)}.")
            if business:
                parts.append(f"Their business: {business}.")
            if website and website != "no website":
                parts.append(f"Their site: {website}.")
            if message:
                parts.append(f"They added: {message}")
    except Exception:
        pass
    try:
        rows = c.execute("SELECT role, body FROM msgs WHERE sid=? ORDER BY id ASC LIMIT 40",
                         (sid,)).fetchall()
        for role, body in rows:
            if body:
                parts.append(("You: " if role == "owner" else "Customer: ") + body)
    except Exception:
        pass
    return "\n".join(parts)


def _grounded_reply(reply: str, factblob: str) -> bool:
    """Reject fabrication: every money amount, percentage, link, email, and guarantee word
    in the composed reply must already appear in the directive/conversation. Keeps 'invent
    nothing' a hard, checkable rule rather than a hope."""
    fb = factblob.lower()
    fb_nospace = re.sub(r"\s+", "", fb)
    for tok in _MONEY_RE.findall(reply) + _PCT_RE.findall(reply):
        if re.sub(r"\s+", "", tok.lower()) not in fb_nospace:
            return False
    for tok in _URL_RE.findall(reply) + _EMAIL_RE.findall(reply):
        if tok.lower() not in fb:
            return False
    for tok in _GUARANTEE_RE.findall(reply):
        if tok.lower() not in fb:
            return False
    return True


async def _compose_reply(brand: dict, transcript: str, directive: str) -> str | None:
    """Local-first, $0. Compose the owner's shorthand directive into an on-brand,
    anon, grounded customer reply. Returns the reply text or None (caller falls back to
    posting the directive as-is). api_fn returns None => never escalates to a paid API."""
    try:
        from core.llm_router import try_local_then_api
    except Exception:
        return None
    persona = brand.get("persona_name") or "Sam"
    brand_name = brand.get("brand_name") or ""
    voice = brand.get("voice") or _DEFAULT_BRAND["voice"]
    factblob = directive + "\n" + transcript
    who = f"{persona} at {brand_name}" if brand_name else persona
    system = (f"You are {who}, the human contact answering a customer in a live website "
              f"chat. Voice: {voice}. You type like one real person — never reveal that "
              f"anyone is relaying a message, and never mention AI, automation, or owners.")
    prompt = (
        "Turn the OWNER'S SHORTHAND NOTE below into ONE short, warm, on-brand reply to the "
        "customer, in the brand voice.\n"
        "HARD RULES:\n"
        "- Use ONLY what the note says plus the CONVERSATION. Invent NOTHING: no price, "
        "discount, timeline, feature, promise, guarantee, or claim the owner did not state.\n"
        "- If the note is too vague to answer safely, instead write a brief, honest one-line "
        "clarifier inviting them to say more — never make something up.\n"
        "- No outcome or results guarantees. No hype, no emojis, no marketing-speak.\n"
        "- Never mention AI, automation, or that a message is being relayed; sound human.\n"
        "- Keep it under ~60 words.\n\n"
        f"BRAND VOICE: {voice}\n\n"
        f"CONVERSATION SO FAR:\n{transcript or '(none yet)'}\n\n"
        f"OWNER'S SHORTHAND NOTE: {directive}\n\n"
        'Return ONLY a JSON object: {"reply": "<the customer-facing message>"}'
    )

    def _valid(text: str) -> bool:
        try:
            obj = json.loads(text)
        except Exception:
            return False
        r = obj.get("reply")
        if not isinstance(r, str) or not r.strip() or len(r) > 700:
            return False
        return _grounded_reply(r, factblob)

    async def api_fn():
        return None  # local / free only — NEVER pay to compose a reply

    try:
        text, _src = await try_local_then_api(
            prompt=prompt, api_fn=api_fn, validator=_valid,
            displaced_tier="sonnet", source="qchat_relay:compose",
            local_model="quality", local_timeout=90.0, max_tokens=400,
            allow_api_fallback=False, system=system,
        )
    except Exception:
        return None
    if not text or not _valid(text):
        return None
    try:
        return json.loads(text)["reply"].strip()
    except Exception:
        return None


def _compose_sync(brand: dict, transcript: str, directive: str) -> str | None:
    try:
        import asyncio
        return asyncio.run(_compose_reply(brand, transcript, directive))
    except RuntimeError:   # already inside an event loop — skip compose, fall back verbatim
        return None
    except Exception:
        return None


def _deliver(token: str, text: str) -> bool:
    """Map token -> session, compose the owner's shorthand directive into an on-brand reply
    (local-$0), append it, flip status live. Returns True if matched. Control commands and
    the '=' verbatim marker (and a downed/ungrounded model) post the text as-is."""
    text = (text or "").strip()
    if not text:
        return False
    c = _qdb()
    try:
        row = c.execute("SELECT sid, status FROM sessions WHERE token=?",
                        (token.upper(),)).fetchone()
        if not row:
            return False
        sid = row[0]

        if text.lower() in _CLOSE_CMDS:                 # control command — close the thread
            body, new_status = text, "closed"
        elif text[:1] == "=":                            # literal verbatim marker
            body = text[1:].strip()
            if not body:
                return False
            new_status = "live"
        else:                                            # shorthand directive -> compose
            brand = _brand_for_session(c, sid)
            transcript = _build_transcript(c, sid)
            composed = _compose_sync(brand, transcript, text)
            body = composed or text                      # model down/ungrounded -> as-is
            new_status = "live"

        c.execute("INSERT INTO msgs(sid,role,body,ts) VALUES(?,?,?,?)",
                  (sid, "owner", body, time.time()))
        c.execute("UPDATE sessions SET status=?, updated=? WHERE sid=?",
                  (new_status, time.time(), sid))
        c.commit()
        return True
    finally:
        c.close()


def relay_once() -> int:
    """One pass: ingest new owner replies, deliver matched ones. Returns # delivered."""
    if not os.path.exists(CHAT_DB):
        return 0
    st = _load_state()
    conn = sqlite3.connect("file:%s?mode=ro" % CHAT_DB, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        hrows = conn.execute("SELECT ROWID FROM handle WHERE id=? OR id=?",
                             (OWNER, OWNER.replace("+1", ""))).fetchall()
        hids = [r["ROWID"] for r in hrows]
        if not hids:
            return 0
        ph = ",".join("?" * len(hids))
        maxrow = conn.execute("SELECT MAX(ROWID) AS m FROM message").fetchone()["m"] or 0
        if "last_rowid" not in st:               # first run: don't replay history
            _save_state({"last_rowid": maxrow})
            return 0
        last = st["last_rowid"]
        rows = conn.execute(
            "SELECT ROWID, text FROM message WHERE ROWID > ? AND is_from_me = 0 "
            "AND handle_id IN (%s) AND text IS NOT NULL ORDER BY ROWID ASC" % ph,
            [last] + hids).fetchall()
    finally:
        conn.close()

    delivered = 0
    new_last = st.get("last_rowid", 0)
    for r in rows:
        new_last = max(new_last, r["ROWID"])
        m = TOKEN_RE.match((r["text"] or "").strip())
        if not m:
            continue                              # not a qualifier reply -> leave for imsg_bridge
        if _deliver(m.group(1), m.group(2)):
            delivered += 1
    _save_state({"last_rowid": max(new_last, st.get("last_rowid", 0))})
    return delivered


if __name__ == "__main__":
    print(json.dumps({"delivered": relay_once()}))
