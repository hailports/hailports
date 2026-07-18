#!/usr/bin/env python3
"""Always-on first-mover opportunity radar.

Every run it analyzes ONE rotating market/industry slice (so it sweeps the whole landscape over
days, cheaply) for QUICK-MOVER / FIRST-MOVER openings: where demand is already proven but supply is
weak, absent, or not-yet-productized. It maintains a ranked board, dedupes against what it's already
found, and pings the owner ONLY when a genuinely-new high-confidence opening appears (no noise).

Discipline: an opportunity must carry a concrete demand signal (a real query pattern, a repeated
public ask, a complaint pattern, a live trend). Imagined demand is dropped. These are HYPOTHESES to
verify+attack (via the first-mover-hunt workflow) — the radar's job is to never let one slip by.

launchd: com.claude-stack.opportunity-radar (every ~8h, rotating focus).
"""
import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(os.environ.get("CLAUDE_STACK_DIR", Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(ROOT))
from dotenv import dotenv_values

ENV = ROOT / ".env"
BOARD = ROOT / "data" / "hustle" / "opportunity_radar.jsonl"
STATE = ROOT / "data" / "hustle" / "opportunity_radar_state.json"

# Rotated one-per-run so the radar sweeps the whole landscape over days without one huge expensive call.
FOCUS = [
    "local home & trade services (HVAC, plumbing, electrical, roofing, landscaping)",
    "restaurants, cafes, bars & food trucks", "med spas, salons, beauty & wellness",
    "dentists, clinics & local healthcare", "lawyers, accountants & professional services",
    "real estate, mortgage & property management", "fitness, gyms & coaching",
    "auto repair, dealers & detailing", "e-commerce & DTC brands", "B2B SaaS & startups",
    "marketing agencies & freelancers", "nonprofits, churches & community orgs",
    "the AI-shift wave: new needs from AI search / AI agents / AI compliance for SMBs",
    "regulatory & deadline-driven windows (privacy, ADA web accessibility, AI disclosure, tax/seasonal)",
    "creators, coaches & info-product sellers", "construction, contractors & home builders",
    "events, weddings & hospitality", "pet services, vets & boarding",
    "education, tutoring & childcare", "financial advisors & insurance",
]


def _openrouter(prompt: str, model: str = "openai/gpt-4o-mini", temperature: float = 0.2) -> str | None:
    try:
        from core.paid_llm_guard import require_paid_llm_api
        require_paid_llm_api("opportunity_radar OpenRouter")
    except Exception:
        return None  # paid off/over daily cap -> caller skips this cycle (no bleed)
    env = dotenv_values(ENV)
    keys = [v for k, v in env.items() if "OPENROUTER" in k.upper() and v and v.startswith("sk-")]
    keys += [os.environ.get("OPENROUTER_API_KEY", "")]
    seen = set()
    for key in [k for k in keys if k and not (k in seen or seen.add(k))]:
        try:
            req = urllib.request.Request(
                "https://openrouter.ai/api/v1/chat/completions",
                data=json.dumps({"model": model, "temperature": temperature,
                                 "messages": [{"role": "user", "content": prompt}]}).encode(),
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
            r = json.loads(urllib.request.urlopen(req, timeout=90).read())
            return r["choices"][0]["message"]["content"]
        except Exception:
            continue
    return None


# Short query keywords per focus — what to pull REAL public posts about (grounds the scan so demand
# evidence is a verbatim quote from a real person, not an LLM invention). Falls back to the focus text.
_FOCUS_QUERY = {
    "the AI-shift wave: new needs from AI search / AI agents / AI compliance for SMBs": "AI agents small business",
    "B2B SaaS & startups": "SaaS onboarding churn",
    "marketing agencies & freelancers": "agency clients retainer",
    "e-commerce & DTC brands": "shopify conversion",
    "regulatory & deadline-driven windows (privacy, ADA web accessibility, AI disclosure, tax/seasonal)": "ADA accessibility lawsuit website",
}


def _strip_html(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", s or "")).strip()


def _hn_signals(q: str, limit: int) -> list[str]:
    """Real HN comments — B2B/tech/founder pain. Strong for AI/SaaS/SF-CPQ foci."""
    url = ("https://hn.algolia.com/api/v1/search?tags=comment&hitsPerPage="
           f"{limit}&query={urllib.parse.quote(q)}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "opportunity-radar/1.0"})
        hits = json.loads(urllib.request.urlopen(req, timeout=20).read()).get("hits", [])
    except Exception:
        return []
    out = []
    for h in hits:
        txt = re.sub(r"\s+", " ", _strip_html(h.get("comment_text") or h.get("story_text") or h.get("title") or ""))
        if len(txt) >= 60:
            out.append(txt[:600])
    return out


def _gnews_signals(q: str, limit: int) -> list[str]:
    """Real, dated news/trade-press headlines+blurbs (Google News RSS, no-auth) — covers local-SMB,
    regulatory & trend signals where HN is thin (Reddit's free JSON is 403-blocked, so this is the 2nd feed)."""
    url = f"https://news.google.com/rss/search?q={urllib.parse.quote(q)}&hl=en-US&gl=US&ceid=US:en"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 opportunity-radar"})
        xml = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "replace")
    except Exception:
        return []
    out = []
    for m in re.findall(r"<item>(.*?)</item>", xml, re.S)[:limit]:
        t = re.search(r"<title>(.*?)</title>", m, re.S)
        d = re.search(r"<description>(.*?)</description>", m, re.S)
        txt = re.sub(r"\s+", " ", _strip_html((t.group(1) if t else "") + " — " + _strip_html(d.group(1) if d else "")))
        if len(txt) >= 40:
            out.append(txt[:400])
    return out


def _fetch_signals(focus: str, limit: int = 25, budget_chars: int = 6000) -> list[str]:
    """Pull REAL recent public signals (HN Algolia + Google News RSS, free/no-auth) so the scan grounds
    every claim in a quotable source. Empty list => fail closed (no LLM, no slop)."""
    q = _FOCUS_QUERY.get(focus) or " ".join(re.sub(r"\(.*?\)", "", focus).split()[:5])
    raw = _hn_signals(q, limit) + _gnews_signals(q, limit)
    out, used, seen = [], 0, set()
    for txt in raw:
        key = txt[:80].lower()
        if key in seen:
            continue
        seen.add(key)
        if used + len(txt) > budget_chars:
            break
        out.append(txt)
        used += len(txt)
    return out


def _seen_names() -> set[str]:
    out = set()
    if BOARD.exists():
        for line in BOARD.read_text(errors="ignore").splitlines():
            try:
                out.add(json.loads(line).get("name", "").strip().lower())
            except Exception:
                continue
    return out


def _norm(s: str) -> str:
    return "".join(c for c in s.lower() if c.isalnum() or c == " ").strip()


def main() -> None:
    state = {}
    try:
        state = json.loads(STATE.read_text())
    except Exception:
        pass
    idx = int(state.get("focus_idx", 0)) % len(FOCUS)
    focus = FOCUS[idx]

    # GROUNDING (the fix for fabricated demand): pull real public posts first. No corpus => fail closed:
    # emit nothing rather than let the model invent demand it can't cite. This also gates the auto-attack.
    signals = _fetch_signals(focus)
    if not signals:
        STATE.write_text(json.dumps({"focus_idx": (idx + 1) % len(FOCUS),
                                     "last_run": datetime.now(timezone.utc).isoformat()}))
        print(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "focus": focus,
                          "found": 0, "new": 0, "skipped": "no_grounding_signals"}))
        return
    corpus = "\n\n".join(f"[POST {i+1}] {s}" for i, s in enumerate(signals))

    prompt = (
        "You are a ruthless first-mover opportunity scout for a small AI-leveraged team that ships proof-first "
        "products fast (a free tool/scan that proves a problem, then a low-ticket paid fix).\n\n"
        f"FOCUS: {focus}.\n\n"
        "Below are REAL, recent public posts from people in or near this space. Extract opportunities ONLY from "
        "what these posts actually show.\n\n"
        f"=== REAL POSTS ===\n{corpus}\n=== END POSTS ===\n\n"
        "HARD RULES:\n"
        "1. Every opportunity's \"demand_evidence\" MUST be a VERBATIM quote (copied word-for-word) from the posts "
        "above, with the [POST N] tag. If you cannot quote a real post that shows the pain/demand, DROP it.\n"
        "2. Do NOT invent demand, paraphrase, or generalize beyond the posts. No quote = no opportunity.\n"
        "3. It must be something WE can SELL on a 'free tool that proves the problem -> $19-199 paid fix' model.\n"
        "4. If the posts show no real sellable pain, return an empty array []. Returning nothing is CORRECT and "
        "expected — better than a fabricated opportunity.\n\n"
        "Return ONLY a JSON array, each item: {\"name\": str, \"demand_evidence\": str (VERBATIM quote incl [POST N]), "
        "\"supply_gap\": str, \"who_buys\": str, \"product_to_build\": str, \"wedge\": str, \"price_point\": str, "
        "\"buildable\": \"fast\"|\"medium\"|\"hard\", \"first_mover\": \"high\"|\"medium\"|\"low\", "
        "\"confidence\": \"high\"|\"medium\"|\"low\"}.")
    raw = _openrouter(prompt)
    if not raw:
        print(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "focus": focus, "error": "no_llm"}))
        return
    try:
        s = raw[raw.index("["):raw.rindex("]") + 1]
        opps = json.loads(s)
    except Exception:
        print(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "focus": focus, "error": "parse"}))
        opps = []

    # Deterministic grounding check: the model's "verbatim quote" must ACTUALLY appear in the corpus.
    # A run of real quoted words from demand_evidence must be found in the real posts, else it's
    # fabricated and dropped. This is the hard guard behind the prompt's soft instruction.
    corpus_norm = _norm(corpus)

    def _is_grounded(evidence: str) -> bool:
        ev = _norm(re.sub(r"\[post\s*\d+\]", "", evidence, flags=re.I))
        words = ev.split()
        if len(words) < 6:
            return False
        # any 6-word verbatim window from the claimed quote present in the real corpus
        return any(" ".join(words[i:i + 6]) in corpus_norm for i in range(len(words) - 5))

    seen = _seen_names()
    new, high, dropped = [], [], 0
    BOARD.parent.mkdir(parents=True, exist_ok=True)
    with BOARD.open("a") as f:
        for o in opps:
            if not isinstance(o, dict):
                continue
            name = str(o.get("name", "")).strip()
            if not name or _norm(name) in {_norm(x) for x in seen}:
                continue
            if not _is_grounded(str(o.get("demand_evidence", ""))):
                dropped += 1  # unverifiable "quote" => fabricated demand => drop
                continue
            rec = {"ts": datetime.now(timezone.utc).isoformat(), "focus": focus, **o}
            f.write(json.dumps(rec) + "\n")
            new.append(rec)
            seen.add(name.lower())
            if str(o.get("confidence", "")).lower() == "high" and str(o.get("first_mover", "")).lower() in ("high", "medium"):
                high.append(rec)

    # AUTO-ATTACK, don't text: the moment a new opening lands on the board, fire the wedge factory so
    # buildable openings become live free->paid tools immediately (it self-gates to safe/fast/auto-
    # buildable opps and self-throttles to MAX_PER_DAY). The board stays the durable hand-off for the
    # heavier builders. No owner ping — the stack acts on the corner instead of notifying about it.
    if new:
        try:
            from core import wedge_factory
            attack = wedge_factory.spawn_top()
            print(json.dumps({"auto_attack": attack}))
        except Exception as e:
            print("auto-attack trigger failed:", e)

    STATE.write_text(json.dumps({"focus_idx": (idx + 1) % len(FOCUS),
                                 "last_run": datetime.now(timezone.utc).isoformat()}))
    print(json.dumps({"focus": focus, "signals": len(signals), "found": len(opps),
                      "dropped_ungrounded": dropped, "new": len(new), "high_conf_new": len(high)}))


if __name__ == "__main__":
    main()
