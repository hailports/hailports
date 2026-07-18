#!/usr/bin/env python3
"""Unified social STRATEGY layer — one coherent, high-performing playbook every
faceless surface pulls from so posts + replies drive followers and profile→site
traffic instead of each channel improvising.

The posters/repliers (hailports / persona3 / persona1 across X, TikTok, YouTube) call
plan(surface, persona) to get the *current best* strategy for their situation:
optimal hooks + formats, posting times/cadence, a CTA style that earns profile
clicks without spamming, and which big-account/trend targets to engage — sourced
from core.viral_tactics.playbook (the proven engagement mechanics) fused with each
channel's OWN engagement history (what actually got likes/follows here).

Design:
  * Deterministic-first. No network, no model. Reads local jsonl/json only.
  * Anonymity-safe by construction. Every persona is faceless + OPSEC-isolated;
    the consistency check enforces personas stay DISTINCT (no cross-brand bleed)
    while all follow the same proven mechanics. Chains core.anon_scrub for the
    identity/house-of-brands wall.
  * Hustle-lane only. Never touches work/CompanyA surfaces or the operator identity.

    from core.social_strategy import plan, consistency_check
    strat = plan("X", "persona1")                 # -> concrete strategy dict + prompt_snippet
    ok = consistency_check({"persona1": draft})   # -> flags cross-brand bleed / de-anon

    python3 -m core.social_strategy          # smoke: prints plans + a bleed demo
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from core import viral_tactics  # noqa: E402  proven engagement mechanics

# anon_scrub is the real identity/house-of-brands wall; import best-effort so the
# strategy layer still functions (with its own cross-brand check) if it moves.
try:
    from core import anon_scrub  # noqa: E402
except Exception:  # pragma: no cover
    anon_scrub = None

HUSTLE = BASE / "data" / "hustle"


# --------------------------------------------------------------------------- #
# Persona registry — each is a SEPARATE faceless identity. The whole point of the
# firewall: they share proven MECHANICS but never share a voice, a site, or a
# brand marker. `forbidden` = other-brand tokens that must NEVER appear in this
# persona's output (cross-brand bleed = a de-anonymization vector).
# --------------------------------------------------------------------------- #
_ALL_BRAND_TOKENS = {"hailports", "persona3", "persona1", "persona1 furad", "ima_furad"}

PERSONAS: dict[str, dict] = {
    "hailports": {
        "handle": "@hailports",
        "voice": "faceless ops/infra operator — dry, receipts-first, shows the broken thing then the fix; never hype, never 'AI' as a selling word",
        "niche": "site reliability / broken-site rescue / outage + GEO teardowns",
        "site": "hailports.com",
        "cta_style": "soft resource CTA — 'full teardown w/ screenshots → hailports.com'; link parked in profile/reply, never the post body",
        "surfaces": ("X", "tiktok", "youtube"),
        # accounts/trends whose audience overlaps the offer (adjacency > raw size)
        "targets": ["indie-hacker + web-dev builders", "site-outage / downtime threads",
                    "SEO / GEO / core-web-vitals accounts", "small-biz 'my site is broken' posts"],
        "engagement_sources": {
            "X": "hailports_engage_state.json",
            "tiktok": "hailports_tiktok_engage_state.json",
            "youtube": "hailports_youtube_engage_state.json",
        },
        "forbidden": sorted(_ALL_BRAND_TOKENS - {"hailports"}),
    },
    "persona3": {
        "handle": "@persona3",
        "voice": "deadpan Texas A&M sports-twitter dunk account — savage, specific, quotable; never a slur, never punches at fans personally, never claps at fellow Aggies",
        "niche": "SEC / Aggie sports takes, rivalry dunks (Longhorns/Cowboys/Mavs/Rangers)",
        "site": None,  # OPSEC: pure reach/follow persona — NEVER links to any Operator infra
        "cta_style": "NO link, ever — reach + follows only; the payoff is the quote-tweet, profile is the only landing",
        "surfaces": ("X",),
        "targets": ["A&M / SEC beat writers + big fan accounts", "live game / rivalry-news threads",
                    "national CFB/CBB takes to dunk-quote"],
        "engagement_sources": {"X": "persona3_bangers.jsonl"},
        "forbidden": sorted(_ALL_BRAND_TOKENS - {"persona3"}),
    },
    "persona1": {
        "handle": "@Ima_Furad",
        "voice": "faceless indie AI-builder building in public — warm, specific, ships receipts (numbers, demos, before/after); no operator identity, no employer, never 'a team'",
        "niche": "faceless AI content builds, tools, build-in-public metrics",
        "site": "persona1.com",
        "cta_style": "build-in-public CTA — 'building this at persona1.com', promise depth not a sale; link in bio + soft caption mention",
        "surfaces": ("X", "tiktok", "youtube"),
        "targets": ["indie-AI-builder + faceless-content creators", "AI-tool launch / build-in-public threads",
                    "small creators asking 'how do you make these' (not mega accounts)"],
        "engagement_sources": {
            "X": "ima_engagement_received.jsonl",
            "tiktok": "ima_engagement_received.jsonl",
            "youtube": "ima_engagement_received.jsonl",
        },
        "forbidden": sorted(_ALL_BRAND_TOKENS - {"persona1", "persona1 furad", "ima_furad"}),
    },
}

_PERSONA_ALIASES = {
    "hailports": "hailports", "hailport": "hailports", "@hailports": "hailports",
    "persona3": "persona3", "@persona3": "persona3", "gary": "persona3",
    "persona1": "persona1", "ima_furad": "persona1", "persona1": "persona1", "@ima_furad": "persona1", "persona1 furad": "persona1",
}

# --------------------------------------------------------------------------- #
# Surface config — cadence, best posting windows, and native formats per surface.
# Deterministic best-practice defaults (US-audience prime windows, local time);
# the volume CEILING itself lives in core.social_governor — this is the *shape*
# of the mix under that ceiling, not a second limiter.
# --------------------------------------------------------------------------- #
SURFACES: dict[str, dict] = {
    "X": {
        "kind": "text",
        "cadence": "1–3 posts/day + reply-engage 30–60 min after each; ride others' velocity in the first 10 min",
        "best_times": ["08:00–10:00", "12:00–13:00", "17:00–19:00"],  # weekday local
        "formats": ["single-idea post", "hook→payoff thread", "additive reply under a bigger post"],
        # which viral_tactics contexts this surface exercises
        "contexts": ["post", "big_account"],
        "hook_note": "line one must stand alone on the timeline — write it to make sense with the body collapsed",
    },
    "tiktok": {
        "kind": "video",
        "cadence": "1–2 shorts/day; warm INTO the governor ceiling, don't spike",
        "best_times": ["11:00–13:00", "18:00–21:00"],
        "formats": ["3-sec visual hook + text overlay", "before/after reveal", "1-idea build clip w/ payoff caption"],
        "contexts": ["post"],
        "hook_note": "the first 3 seconds ARE the hook — open on the result/curiosity gap, not an intro",
    },
    "youtube": {
        "kind": "video",
        "cadence": "1 short/day (most post-tolerant surface); layer channel-age warm-up",
        "best_times": ["14:00–16:00", "19:00–22:00"],
        "formats": ["short w/ text hook in frame 1", "quiet demo w/ on-screen result", "loopable 1-idea clip"],
        "contexts": ["post"],
        "hook_note": "title + first frame together are the hook; the thumbnail-in-motion has to promise the payoff",
    },
}

_SURFACE_ALIASES = {
    "x": "X", "twitter": "X", "x/twitter": "X",
    "tiktok": "tiktok", "tt": "tiktok",
    "youtube": "youtube", "yt": "youtube", "shorts": "youtube", "youtube shorts": "youtube",
}


def _norm_persona(p: str) -> str:
    return _PERSONA_ALIASES.get((p or "").strip().lower().lstrip("@"), (p or "").strip().lower())


def _norm_surface(s: str) -> str:
    return _SURFACE_ALIASES.get((s or "").strip().lower(), (s or "").strip())


# --------------------------------------------------------------------------- #
# Engagement history — "what actually performed HERE". Deterministic reads only;
# every source is optional and any failure degrades to an empty signal so plan()
# always returns the proven-mechanics playbook even with no history yet.
# --------------------------------------------------------------------------- #
def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows


def _read_json(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _proven_signals(persona: str, surface: str) -> dict:
    """Distill this channel's OWN history into concrete 'this worked here' signals:
    top-performing shapes and an honest growth-trend read the poster can lean on."""
    pcfg = PERSONAS[persona]
    src = pcfg["engagement_sources"].get(surface)
    out = {"top_shapes": [], "growth": None, "note": ""}
    if not src:
        return out
    path = HUSTLE / src

    if persona == "persona3":
        # bangers carry a per-line score + archetype (the reusable SHAPE that landed)
        rows = _read_jsonl(path)
        best: dict[str, int] = {}
        cnt: dict[str, int] = {}
        for r in rows:
            a = (r.get("archetype") or "").strip()
            if not a:
                continue
            s = int(r.get("score") or 0)
            best[a] = max(best.get(a, 0), s)
            cnt[a] = cnt.get(a, 0) + 1
        ranked = sorted(best, key=lambda a: (best[a], cnt[a]), reverse=True)
        out["top_shapes"] = [f"{a} (best={best[a]}, n={cnt[a]})" for a in ranked[:5]]
        out["note"] = "proven banger shapes — reuse the SHAPE, always fresh content"
        return out

    if persona == "persona1":
        # follower/likes time-series per platform -> honest growth-trend read
        rows = [r for r in _read_jsonl(path) if r.get("platform") == surface.lower()]
        rows = [r for r in rows if isinstance(r.get("followers"), (int, float))]
        if len(rows) >= 2:
            f0, f1 = rows[0]["followers"], rows[-1]["followers"]
            delta = f1 - f0
            out["growth"] = {"first": f0, "last": f1, "delta": delta}
            if delta > 0:
                out["note"] = f"followers +{delta} on {surface} — current mix is working, iterate the winners"
            else:
                out["note"] = (f"followers {delta} on {surface} (flat/declining) — current cadence isn't "
                               "converting; lean HARDER on the proven hooks below, don't post more")
        return out

    if persona == "hailports":
        # engage_state is activity counters, not per-post scores — surface it as an
        # activity read so the poster knows the lane is warm without over-claiming.
        st = _read_json(path)
        if isinstance(st, dict):
            daily = st.get("daily")
            seen = st.get("seen")
            n_seen = len(seen) if isinstance(seen, (list, dict)) else None
            out["note"] = (f"lane active (daily_state={bool(daily)}, "
                           f"{n_seen if n_seen is not None else '?'} targets tracked) — "
                           "no per-post scoring here, so weight the proven mechanics below")
        return out

    return out


# --------------------------------------------------------------------------- #
# plan() — the current best strategy for a surface+persona.
# --------------------------------------------------------------------------- #
def plan(surface: str, persona: str, *, limit: int = 6) -> dict:
    """Current best strategy for (surface, persona).

    Fuses core.viral_tactics.playbook (proven mechanics) with this channel's own
    engagement history and the persona/surface config into one concrete plan:
    hooks, formats, cadence, best_times, cta, targets, tactics, proven signals,
    and a ready-to-inline `prompt_snippet` for the poster/replier.

    Raises ValueError for an unknown persona or a persona not allowed on surface.
    """
    p = _norm_persona(persona)
    s = _norm_surface(surface)
    if p not in PERSONAS:
        raise ValueError(f"unknown persona {persona!r}; known: {sorted(set(PERSONAS))}")
    if s not in SURFACES:
        raise ValueError(f"unknown surface {surface!r}; known: {sorted(SURFACES)}")
    pcfg = PERSONAS[p]
    scfg = SURFACES[s]
    if s not in pcfg["surfaces"]:
        raise ValueError(f"persona {p!r} does not run on surface {s!r}; runs on {list(pcfg['surfaces'])}")

    # 1) proven MECHANICS from viral_tactics, for every context this surface uses
    tactics: list[dict] = []
    seen_ids: set[str] = set()
    snippet_blocks: list[str] = []
    for ctx in scfg["contexts"]:
        pb = viral_tactics.playbook(ctx, limit=limit)
        for t in pb.get("tactics", []):
            tid = t.get("id") or t.get("tactic", "")
            if tid in seen_ids:
                continue
            seen_ids.add(tid)
            tactics.append({"context": pb["context"], **t})
        if pb.get("prompt_snippet"):
            snippet_blocks.append(pb["prompt_snippet"])

    # 2) proven signals from THIS channel's own history
    proven = _proven_signals(p, s)

    # 3) hooks/formats — surface-native formats + the top hook tactics distilled
    hooks = [t["tactic"] for t in tactics if t.get("category") in ("hook", "structure")][:4]

    strategy = {
        "surface": s,
        "persona": p,
        "handle": pcfg["handle"],
        "voice": pcfg["voice"],
        "niche": pcfg["niche"],
        "kind": scfg["kind"],
        "hooks": hooks,
        "hook_note": scfg["hook_note"],
        "formats": scfg["formats"],
        "cadence": scfg["cadence"],
        "best_times": scfg["best_times"],
        "cta": pcfg["cta_style"],
        "site": pcfg["site"],
        "targets": pcfg["targets"],
        "tactics": tactics,
        "proven": proven,
        "prompt_snippet": _build_snippet(p, s, pcfg, scfg, snippet_blocks, proven),
    }
    return strategy


def _build_snippet(p: str, s: str, pcfg: dict, scfg: dict,
                   tactic_blocks: list[str], proven: dict) -> str:
    """One paste-ready block the poster/replier inlines into its prompt. Persona
    voice + surface mechanics + the proven playbook, anonymity-safe."""
    lines = [
        f"SOCIAL STRATEGY — {pcfg['handle']} on {s} ({scfg['kind']}):",
        f"- voice: {pcfg['voice']}",
        f"- hook: {scfg['hook_note']}",
        f"- cadence: {scfg['cadence']}",
        f"- best windows (local): {', '.join(scfg['best_times'])}",
        f"- CTA: {pcfg['cta_style']}",
        f"- engage targets: {'; '.join(pcfg['targets'])}",
    ]
    if proven.get("top_shapes"):
        lines.append(f"- proven shapes HERE: {'; '.join(proven['top_shapes'])}")
    if proven.get("note"):
        lines.append(f"- channel read: {proven['note']}")
    block = "\n".join(lines)
    if tactic_blocks:
        block += "\n\n" + "\n\n".join(tactic_blocks)
    return block


# --------------------------------------------------------------------------- #
# Cross-channel consistency check — personas stay DISTINCT + faceless (no
# cross-brand bleed) while all follow proven mechanics.
# --------------------------------------------------------------------------- #
def consistency_check(drafts: dict[str, str]) -> dict:
    """Screen a batch of persona→text drafts before they post.

    drafts: {persona: draft_text}. Flags, per persona:
      * cross_brand_bleed — the text names ANOTHER persona's brand/domain (the
        firewall violation that de-anonymizes the house of brands),
      * anon_leak        — core.anon_scrub verdict (operator identity, employer,
        house-of-brands reveal, PII),
      * unknown_persona  — draft keyed to a persona we don't manage.

    Returns {ok, checked, violations:[...]}. ok is False if ANY violation fires.
    """
    violations: list[dict] = []
    checked = 0
    for persona_raw, text in (drafts or {}).items():
        p = _norm_persona(persona_raw)
        text = text or ""
        low = text.lower()
        if p not in PERSONAS:
            violations.append({"persona": persona_raw, "type": "unknown_persona",
                               "detail": f"not a managed persona (known: {sorted(set(PERSONAS))})"})
            continue
        checked += 1

        # cross-brand bleed: this persona must never name another persona's brand
        for token in PERSONAS[p]["forbidden"]:
            if token in low:
                violations.append({
                    "persona": p, "type": "cross_brand_bleed", "marker": token,
                    "detail": f"{p} draft references foreign brand {token!r} — breaks persona isolation/anonymity",
                })

        # operator-identity + house-of-brands wall. NOTE: we deliberately do NOT
        # run anon_scrub.verdict()/find_leaks() here — those flag EVERY registered
        # brand domain, which would (correctly, for outbound) reject a persona
        # naming its OWN site. A persona posting its own persona1.com/hailports.com
        # is expected; naming ANOTHER persona's is the leak (caught above). So we
        # chain only the identity + house-of-brands layers, which don't touch a
        # persona's own domain.
        if anon_scrub is not None:
            try:
                id_leaks = anon_scrub.find_identity_leaks(text)
            except Exception:
                id_leaks = []
            if id_leaks:
                violations.append({"persona": p, "type": "anon_leak",
                                   "detail": f"operator/employer identity: {', '.join(sorted(set(id_leaks)))}"})
            hob = getattr(anon_scrub, "_METHOD_RE", None)
            if hob is not None:
                try:
                    m = hob.search(text)
                except Exception:
                    m = None
                if m:
                    violations.append({"persona": p, "type": "house_of_brands_reveal",
                                       "detail": f"de-anon method/portfolio tell: {m.group(0)!r}"})

    return {"ok": not violations, "checked": checked, "violations": violations}


# --------------------------------------------------------------------------- #
# CLI / smoke
# --------------------------------------------------------------------------- #
def _selftest() -> int:
    fails = []

    # plan() returns concrete strategy w/ real viral_tactics for the two named pairs
    for surface, persona in (("X", "persona1"), ("tiktok", "hailports")):
        st = plan(surface, persona)
        print(f"\n=== plan({surface}, {persona}) — {len(st['tactics'])} tactics, "
              f"{len(st['hooks'])} hooks ===")
        print(st["prompt_snippet"])
        if not st["tactics"]:
            fails.append(f"plan({surface},{persona}) pulled 0 tactics from viral_tactics")
        if not st["prompt_snippet"]:
            fails.append(f"plan({surface},{persona}) empty prompt_snippet")
        # every tactic came from the viral_tactics playbook (has a real 'why')
        if not all(t.get("tactic") for t in st["tactics"]):
            fails.append(f"plan({surface},{persona}) has a tactic without text")

    # persona not allowed on a surface is rejected (persona3 is X-only)
    try:
        plan("tiktok", "persona3")
        fails.append("plan(tiktok, persona3) should have raised — persona3 is X-only")
    except ValueError:
        pass

    # consistency: clean, distinct drafts pass
    clean = consistency_check({
        "persona1": "shipped a faceless short pipeline this week, full build → persona1.com",
        "hailports": "your checkout page 500s on mobile. here's the fix, teardown → hailports.com",
        "persona3": "calling it now: they finish .500 and act surprised.",
    })
    print(f"\n=== consistency_check(clean) -> ok={clean['ok']} ===")
    if not clean["ok"]:
        fails.append(f"clean drafts flagged: {clean['violations']}")

    # inject a cross-brand bleed — persona1 naming hailports must flag
    bled = consistency_check({
        "persona1": "loved building this, check my other project hailports.com too",
    })
    print(f"=== consistency_check(bleed) -> ok={bled['ok']} violations={bled['violations']} ===")
    if bled["ok"] or not any(v["type"] == "cross_brand_bleed" for v in bled["violations"]):
        fails.append("cross-brand bleed (persona1 -> hailports) NOT flagged")

    if fails:
        print("\nSMOKE FAILURES:")
        for f in fails:
            print("  -", f)
        return 1
    print("\nSMOKE OK — plan() + consistency_check() green")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
