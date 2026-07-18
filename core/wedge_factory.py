#!/usr/bin/env python3
"""Auto-spawn neutral free->staged-paid wedges from the opportunity radar — no agent in the loop.

The radar (data/hustle/opportunity_radar.jsonl) surfaces demand/supply-gap opportunities. This
factory turns the SAFE, AUTO-BUILDABLE ones into live tools on products/self_serve/tools_wedges.py
WITHOUT writing any new python: it maps a radar opportunity to a pure-DATA registry entry
(data/hustle/wedge_registry.json) that the generic wedge builder serves at /tools/<slug>.

Hard rules baked into the gate:
  * buildable == "fast"           — only opportunities that ARE a single generated info-document.
  * sellable                      — has a described free->paid upsell + a price point.
  * NOT a licensed-advice core    — anything whose VALUE is a professional determination
                                    (diagnosis, legal/medical/tax opinion, eligibility ruling,
                                    "review service", "consultation", "dispute resolution") is
                                    REJECTED. Survivors are reframed as general information and
                                    always carry a category disclaimer.
  * auto-buildable                — REJECTS anything needing live data, scheduling, reminders,
                                    persistence, locators, price comparison, integrations, or a
                                    stateful backend. Those are buildable-by-a-human, NOT by this
                                    pure-LLM-text factory. (Honest limitation — see can_autobuild.)
  * NO new Stripe object          — price/paid_name are staged metadata only; the existing
                                    /tools/<slug>/get route owner-gates the paid offer.

spawn_top() registers AT MOST ONE opportunity per day (state file), highest-confidence first,
skipping anything already registered. Run by com.claude-stack.wedge-spawner (daily, RunAtLoad).

CLI:  python -m core.wedge_factory spawn_top      # daily picker (the launchd entrypoint)
      python -m core.wedge_factory list           # show the current registry
      python -m core.wedge_factory register -     # register one opp from stdin JSON (testing)
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RADAR = ROOT / "data" / "hustle" / "opportunity_radar.jsonl"
REGISTRY = ROOT / "data" / "hustle" / "wedge_registry.json"
STATE = ROOT / "data" / "hustle" / ".wedge_spawner_state.json"

# A wedge whose CORE deliverable is a licensed/professional determination — rejected outright.
_LICENSED_CORE = (
    "diagnos", "prescri", "treatment plan", "tele-dent", "teledent", "telehealth", "tele-health",
    "consultation", "eligibility check", "insurance eligibility", "medical advice", "legal advice",
    "review service", "document review service", "dispute resolution", "represent you",
    "file a claim", "lawsuit", "tax filing", "prepare your return", "audit defense",
)
# A wedge that needs live data / state / integrations — buildable by a human, NOT by this factory.
_NEEDS_BACKEND = (
    "reminder", "schedul", "sms", "locator", "price comparison", "compare price", "comparison tool",
    "tracker", "logging app", "real-time", "live data", "integration", "marketplace", "listing",
    "appointment", "booking", "aggregat", "crm", "monitor", "directory of", "lead matching",
    "contractor matching", "tele",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _text_blob(opp: dict) -> str:
    return " ".join(str(opp.get(k, "")) for k in
                    ("name", "wedge", "product_to_build", "supply_gap", "demand_evidence")).lower()


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    s = re.sub(r"-(for|the|a|an|of|to|your|with)-", "-", s)
    return s[:60].strip("-") or "wedge"


# ── classification + disclaimers ─────────────────────────────────────────────────────────────
def _category(opp: dict) -> str:
    f = (opp.get("focus", "") + " " + opp.get("name", "")).lower()
    if any(w in f for w in ("law", "legal", "attorney", "compliance")):
        return "legal"
    if any(w in f for w in ("account", "tax", "bookkeep", "cpa", "financ")):
        return "tax"
    if any(w in f for w in ("dent", "clinic", "healthcare", "medical", "patient", "therap")):
        return "health"
    if any(w in f for w in ("spa", "salon", "beauty", "skincare", "wellness", "cosmet")):
        return "beauty"
    return "general"


_DISCLAIMERS = {
    "legal": ("This tool provides general information for educational purposes only and is NOT legal "
              "advice. It does not create an attorney-client relationship. Laws and requirements vary "
              "by jurisdiction — consult a licensed attorney before relying on anything here."),
    "tax": ("This tool provides general educational information and automated estimates only — it is "
            "NOT tax, accounting, or financial advice and is not a guarantee of any outcome or "
            "savings. Consult a licensed CPA or tax professional about your specific situation."),
    "health": ("This tool provides general informational and educational content only and is NOT "
               "medical, dental, or health advice, diagnosis, or treatment. Consult a licensed "
               "healthcare professional for any condition, symptom, or personal health decision."),
    "beauty": ("This is general cosmetic and wellness information only and is NOT medical or "
               "dermatological advice. Patch-test new products and consult a licensed professional "
               "for any skin condition, allergy, pregnancy, or persistent concern."),
    "general": ("This tool provides general information for educational purposes only and is NOT "
                "professional, legal, financial, or other licensed advice, nor a guarantee of any "
                "outcome. Verify with a qualified professional before relying on it."),
}


# ── gates ────────────────────────────────────────────────────────────────────────────────────
def is_sellable(opp: dict) -> bool:
    """A described free->paid wedge with a price point."""
    has_price = bool(str(opp.get("price_point", "")).strip())
    blob = _text_blob(opp)
    has_upsell = has_price or any(w in blob for w in ("paid", "$", "subscription", "premium", "upsell"))
    return has_price and has_upsell


def is_licensed_core(opp: dict) -> bool:
    return any(kw in _text_blob(opp) for kw in _LICENSED_CORE)


def can_autobuild(opp: dict) -> bool:
    """True only if the deliverable is a single generated informational document (no backend)."""
    return not any(kw in _text_blob(opp) for kw in _NEEDS_BACKEND)


def gate(opp: dict) -> tuple[bool, str]:
    """Full eligibility gate. Returns (ok, reason). reason explains the FIRST failure."""
    if not opp.get("name"):
        return False, "missing name"
    if str(opp.get("buildable", "")).lower() != "fast":
        return False, f"not fast-buildable (buildable={opp.get('buildable')!r})"
    if not is_sellable(opp):
        return False, "not sellable (no price point / upsell)"
    if is_licensed_core(opp):
        return False, "licensed-advice core (cannot be reframed as general info)"
    if not can_autobuild(opp):
        return False, "needs a stateful/live-data backend (not a pure-LLM document — human build only)"
    return True, "ok"


# ── opportunity -> registry entry ──────────────────────────────────────────────────────────────
def _clean(s: str) -> str:
    return (str(s or "")).replace("{", "(").replace("}", ")").strip()


def _first_sentence(s: str, n: int = 160) -> str:
    s = _clean(s)
    cut = re.split(r"(?<=[.!?])\s", s, maxsplit=1)[0]
    return (cut[:n]).strip()


def _paid_name(name: str) -> str:
    base = re.sub(r"\b(Free|Generator|Builder|Finder|Checker|System|App|Tool)\b", "", name,
                  flags=re.I)
    base = re.sub(r"\s+for\s+.*$", "", base, flags=re.I).strip(" -–—")
    base = re.sub(r"\s{2,}", " ", base).strip()
    return (base or "Complete Guide") + " — Complete Edition"


def opp_to_entry(opp: dict) -> dict:
    """Map a radar opportunity to a pure-DATA registry entry (no python needed to serve it)."""
    name = _clean(opp.get("name"))
    focus = _clean(opp.get("focus"))
    who = _clean(opp.get("who_buys"))
    ptb = _clean(opp.get("product_to_build") or opp.get("wedge"))
    cat = _category(opp)
    price = str(opp.get("price_point", "")).strip()
    if not price.startswith("$"):
        price = "$49"

    system_prompt = (
        f"You are a general-information assistant for the topic '{name}', serving {who or 'people'} "
        f"in the area of {focus or 'their field'}. Based on the user's input, produce a tailored, "
        f"practical, GENERAL-information result (the free version of: {ptb}). Be specific and "
        "genuinely useful, but keep everything strictly general and educational — never give a "
        "professional, licensed, legal, medical, or tax determination, diagnosis, guarantee, or "
        "individualized advice.")
    user_template = (
        f"Topic: {name}\nField: {focus}\nWho this is for: {who}\nGoal: {ptb}\n"
        "User's situation: {subject}\nExtra context: {context}\n"
        "Produce the tailored, specific, general-information result now.")

    fb = {"items": [
        {"title": "Clarify your goal and scope",
         "detail": _first_sentence(ptb) or "Define exactly what outcome you want before you start."},
        {"title": "Map the key steps",
         "detail": "Break the work into a short, ordered checklist so nothing critical is missed."},
        {"title": "Document and standardize",
         "detail": "Write down your process so it is repeatable and easy to hand off."},
        {"title": "Verify with a qualified professional",
         "detail": "Confirm anything regulated or high-stakes with a licensed professional in your field."},
    ]}

    return {
        "name": name,
        "blurb": (_first_sentence(opp.get("wedge") or ptb) or
                  "Tell us about your situation and get a tailored result — free."),
        "fields": [
            ["subject", f"Your situation or business type (e.g. {_first_sentence(who, 48) or 'your business'})"],
            ["context", "Location, size, or other context (optional)"],
        ],
        "lane": f"tools:{cat}",
        "system_prompt": system_prompt,
        "user_template": user_template,
        "result_key": "items",
        "render": "list",
        "preview_n": 3,
        "paid_name": _paid_name(name),
        "price": price,
        "paid_blurb": (f"The complete, detailed edition of the result above — every point expanded "
                       f"with specifics, templates, and the steps to act on it. ({_first_sentence(ptb, 120)})"),
        "disclaimer": _DISCLAIMERS[cat],
        "category": cat,
        "source_opp": name,
        "created": _now(),
        "_paid_staged": True,
    }


# ── registry io ────────────────────────────────────────────────────────────────────────────────
def load_registry() -> dict:
    if REGISTRY.exists():
        try:
            d = json.loads(REGISTRY.read_text(errors="ignore"))
            if isinstance(d, dict) and isinstance(d.get("wedges"), dict):
                return d
        except Exception:
            pass
    return {"version": 1, "wedges": {}, "updated": _now()}


def _save_registry(reg: dict) -> None:
    reg["updated"] = _now()
    REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    tmp = REGISTRY.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(reg, indent=2, ensure_ascii=False))
    os.replace(tmp, REGISTRY)


def _quality_legal_gate(entry: dict) -> tuple[bool, str]:
    """Independent QUALITY+LEGAL+identity gate on the rendered entry text."""
    if not (entry.get("disclaimer") or "").strip():
        return False, "no disclaimer"
    text = " ".join(str(entry.get(k, "")) for k in
                    ("name", "blurb", "system_prompt", "paid_name", "paid_blurb", "disclaimer"))
    # identity / PII — respect pii_guard (never leak owner/BrandA onto an anon surface)
    try:
        from core import pii_guard
        findings = pii_guard._scan_pii(text, "wedge") + pii_guard._public_identity(text, "wedge")
        if findings:
            return False, f"pii_guard flagged: {findings[:2]}"
    except Exception:
        pass
    # ship_guard quality gate (slop/identity/unfinished)
    try:
        from core.ship_guard import ship_ok
        ok, msgs = ship_ok(text)
        if not ok:
            return False, f"ship_guard: {[m for m in msgs if not m.startswith('(warn)')][:2]}"
    except Exception:
        pass
    return True, "ok"


def register_opportunity(opp: dict) -> dict:
    """Validate one opportunity through every gate and, if it passes, write it into the registry.
    Returns a status dict (always — never raises). Idempotent: an already-registered slug is a noop.
    NO Stripe object is created; price/paid_name are staged metadata only."""
    ok, reason = gate(opp)
    if not ok:
        return {"ok": False, "stage": "gate", "reason": reason, "name": opp.get("name")}

    entry = opp_to_entry(opp)
    slug = slugify(entry["name"])

    qok, qreason = _quality_legal_gate(entry)
    if not qok:
        return {"ok": False, "stage": "quality_legal", "reason": qreason, "slug": slug}

    reg = load_registry()
    if slug in reg["wedges"]:
        return {"ok": True, "stage": "exists", "slug": slug, "reason": "already registered (noop)"}

    reg["wedges"][slug] = entry
    _save_registry(reg)

    try:
        from core.funnel_tracker import log_event
        log_event("other:wedge_spawned", lane=entry["lane"], product=slug,
                  detail=f"auto-registered from radar: {entry['name']}")
    except Exception:
        pass

    return {"ok": True, "stage": "registered", "slug": slug, "name": entry["name"],
            "price": entry["price"], "paid_staged": True, "url": f"/tools/{slug}"}


# ── daily picker ─────────────────────────────────────────────────────────────────────────────
def _read_radar() -> list[dict]:
    out = []
    if not RADAR.exists():
        return out
    for line in RADAR.read_text(errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def _score(opp: dict) -> tuple:
    rank = {"high": 2, "medium": 1, "low": 0}
    return (rank.get(str(opp.get("confidence", "")).lower(), 0),
            rank.get(str(opp.get("first_mover", "")).lower(), 0))


def _load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(errors="ignore"))
        except Exception:
            pass
    return {"last_date": "", "registered": []}


def _save_state(st: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(st, indent=2))
    os.replace(tmp, STATE)


def candidates() -> list[dict]:
    """High-confidence, fast, sellable, gate-passing radar opps NOT already registered, ranked."""
    reg = load_registry()
    have_slugs = set(reg["wedges"].keys())
    have_src = {(v.get("source_opp") or "").lower() for v in reg["wedges"].values()}
    out = []
    seen = set()
    for opp in _read_radar():
        name = (opp.get("name") or "").strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        if str(opp.get("confidence", "")).lower() != "high":
            continue
        ok, _ = gate(opp)
        if not ok:
            continue
        if slugify(name) in have_slugs or name.lower() in have_src:
            continue
        out.append(opp)
    out.sort(key=_score, reverse=True)
    return out


def _reload_self_serve() -> bool:
    """Best-effort hot-load: restart the self-serve agent so a just-registered wedge is served
    immediately (the registry is merged at import). Never raises; returns True on a clean kick."""
    import subprocess
    try:
        uid = os.getuid()
        r = subprocess.run(
            ["launchctl", "kickstart", "-k", f"gui/{uid}/com.claude-stack.self-serve"],
            capture_output=True, timeout=20)
        return r.returncode == 0
    except Exception:
        return False


def spawn_top() -> dict:
    """Register AT MOST ONE new wedge per day, highest-confidence first. Idempotent per day."""
    st = _load_state()
    if st.get("last_date") == _today():
        return {"ok": False, "reason": "already spawned today", "date": _today(),
                "registered_today": st.get("registered", [])[-1:] if st.get("registered") else []}
    cands = candidates()
    if not cands:
        # Honest no-op: nothing new clears the safety/auto-build gate today.
        st["last_date"] = _today()
        _save_state(st)
        return {"ok": False, "reason": "no eligible new opportunity today",
                "scanned": len(_read_radar()), "date": _today()}
    for opp in cands:
        res = register_opportunity(opp)
        if res.get("ok") and res.get("stage") == "registered":
            st["last_date"] = _today()
            st.setdefault("registered", []).append(
                {"date": _today(), "slug": res["slug"], "name": res["name"]})
            _save_state(st)
            res["served_live"] = _reload_self_serve()  # close the loop: make it live now
            return res
    # all candidates already existed (race) — still consume the day
    st["last_date"] = _today()
    _save_state(st)
    return {"ok": False, "reason": "all candidates already registered", "date": _today()}


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "spawn_top"
    if cmd == "spawn_top":
        print(json.dumps(spawn_top(), indent=2))
        return 0
    if cmd == "list":
        reg = load_registry()
        print(json.dumps({"count": len(reg["wedges"]),
                          "slugs": list(reg["wedges"].keys())}, indent=2))
        return 0
    if cmd == "candidates":
        print(json.dumps([c.get("name") for c in candidates()], indent=2))
        return 0
    if cmd == "register":
        raw = sys.stdin.read() if (len(argv) > 2 and argv[2] == "-") else (argv[2] if len(argv) > 2 else "")
        try:
            opp = json.loads(raw)
        except Exception as e:
            print(json.dumps({"ok": False, "reason": f"bad json: {e}"}))
            return 1
        print(json.dumps(register_opportunity(opp), indent=2))
        return 0
    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
