#!/usr/bin/env python3
"""Viral-engagement TACTICS harvester + playbook.

Scrapes what actually drives X/social engagement, follows, and profile→site
traffic, distills it into concrete reusable tactics, and hands the reply/post
generators (hailports + persona3) an injectable playbook to raise engagement.

Two surfaces:
  harvest()          -> mine tactics from local intel (X_INTEL_HACKS.md, banked
                        bangers), an evergreen seed corpus, and — best-effort —
                        public write-ups. Deduped-append to viral_tactics.jsonl.
  playbook(context)  -> the top applicable tactics for a situation
                        ("reply" | "post" | "big_account"), plus a ready-to-paste
                        prompt snippet the generators inline into their prompt.

Faceless/anonymity-safe, hustle-lane only. No tactic references any operator
identity, employer, or real name — extraction hard-drops anything that would.
Deterministic extraction is the default; cheap local-model summarization is a
best-effort enrichment that never blocks.

    python3 -m core.viral_tactics                 # harvest (dry) + demo playbook
    python3 -m core.viral_tactics --harvest       # harvest only
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

HUSTLE = BASE / "data" / "hustle"
INTEL = HUSTLE / "X_INTEL_HACKS.md"
BANGERS = HUSTLE / "persona3_bangers.jsonl"
OUT = HUSTLE / "viral_tactics.jsonl"

# Reuse the stack's token-set similarity for dedup instead of a second copy.
try:
    from core.content_quality import _jaccard  # noqa: E402
except Exception:  # pragma: no cover - keep harvest working if import moves
    def _jaccard(a: str, b: str) -> float:
        sa, sb = set(a.lower().split()), set(b.lower().split())
        if not sa or not sb:
            return 0.0
        return len(sa & sb) / len(sa | sb)

# Local-model summarizer — best-effort only. Never blocks a harvest.
try:
    from core.content_generator import _ollama  # noqa: E402
except Exception:  # pragma: no cover
    def _ollama(*_a, **_k) -> str:
        return ""

# Contexts a tactic can apply to.
CTX_REPLY = "reply"          # replying under someone else's post
CTX_POST = "post"            # our own standalone post/thread
CTX_BIG = "big_account"      # replying/engaging under a large account for reach
ALL_CTX = (CTX_REPLY, CTX_POST, CTX_BIG)

# Anonymity guard — a tactic must never smuggle in operator identity or a
# face-reveal instruction that breaks the faceless posture. These kill a tactic
# at extraction time (belt-and-suspenders; the poster's gate is the real wall).
_IDENTITY_KILL = re.compile(
    r"\b(Operator|Operator|CompanyA|selfie|your face|face reveal|real name|"
    r"my linkedin|dox|show yourself)\b",
    re.I,
)

DEDUP_MAX_JACCARD = 0.62   # a new tactic must differ this much from banked ones
SOFT_CAP = 500             # curated playbook, not a landfill


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _tid(tactic: str) -> str:
    return hashlib.sha1(tactic.lower().strip().encode()).hexdigest()[:12]


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def _anon_ok(text: str) -> bool:
    return not _IDENTITY_KILL.search(text or "")


# --------------------------------------------------------------------------- #
# Evergreen seed corpus — the well-established, publicly-documented viral levers.
# Deterministic, anonymity-safe, and the backbone playbook() leans on even before
# any live harvest. Each: (tactic, category, contexts, template, why, specificity)
# --------------------------------------------------------------------------- #
_SEED: list[tuple] = [
    # ---- hook formats ----
    ("Open with a concrete number or result, not a claim — specificity is the hook",
     "hook", (CTX_POST, CTX_REPLY, CTX_BIG),
     "{metric} in {timeframe}. here's the {n}-step play:",
     "numbered/quantified openers stop the scroll and set an information-gap the reader has to close",
     0.9),
    ("Lead with the counterintuitive claim, then earn it in the body",
     "hook", (CTX_POST,),
     "everyone says {common_belief}. it's backwards. {contrarian_truth}",
     "pattern-interrupt hooks beat agreeable ones for saves/quotes because they create a stakes-y disagreement",
     0.85),
    ("Name the exact pain in the first line so the target self-selects",
     "hook", (CTX_POST, CTX_REPLY),
     "if your {surface} still {failure}, this is why —",
     "a precisely-named pain makes the right reader feel seen and unlocks the click; vague hooks get scrolled",
     0.85),
    ("First line must stand alone — write it to make sense with the rest collapsed",
     "structure", (CTX_POST,),
     None,
     "on the timeline only line one shows; if it needs the body to make sense it dies before the 'more'",
     0.8),
    # ---- post structure ----
    ("One idea per post; break multi-point content into a thread with a payoff line 1",
     "structure", (CTX_POST,),
     None,
     "single-idea posts get shared because they're quotable; multi-idea posts dilute the save trigger",
     0.75),
    ("End a value post with a soft, earned CTA — a resource, not a pitch",
     "cta", (CTX_POST,),
     "full breakdown w/ the screenshots is on the profile → {site}",
     "a CTA that offers more depth (not a sale) converts profile clicks to site traffic without killing reach",
     0.8),
    ("Put the link in a reply or the profile, not the post body",
     "cta", (CTX_POST,),
     None,
     "in-body links suppress reach on X; parking the link one hop away preserves distribution and still routes traffic",
     0.85),
    ("Ask one specific question at the end to farm replies",
     "engagement", (CTX_POST,),
     "what's the one {thing} you'd add?",
     "replies weight the algorithm harder than likes; a narrow question is easier to answer than an open one",
     0.7),
    # ---- reply / big-account tactics ----
    ("Reply within the first ~10 minutes on a fast-growing post",
     "timing", (CTX_REPLY, CTX_BIG),
     None,
     "early replies ride the post's own velocity; the top reply on a viral post can out-reach an original post",
     0.9),
    ("Add a distinct data point or angle the original missed — don't just agree",
     "reply", (CTX_REPLY, CTX_BIG),
     "the piece nobody's saying: {angle}",
     "additive replies get liked by the author + the audience and earn profile clicks; 'great post!' gets ignored",
     0.9),
    ("Answer a question the OP left open, completely, in the reply itself",
     "reply", (CTX_REPLY, CTX_BIG),
     None,
     "a self-contained useful reply gets screenshotted/quoted, which is what actually pulls people to your profile",
     0.85),
    ("Target big accounts whose audience overlaps your offer, not the biggest raw follower count",
     "reply", (CTX_BIG,),
     None,
     "reach only converts to traffic when the audience is adjacent to what the profile/site sells",
     0.8),
    ("Match the thread's register, then out-specific it — never generic praise under a big post",
     "reply", (CTX_BIG,),
     None,
     "generic replies are invisible under big posts; the specific/contrarian one is what climbs to the top",
     0.8),
    # ---- timing / cadence ----
    ("Post when your target audience is online, then reply-engage for the next 30–60 min",
     "timing", (CTX_POST,),
     None,
     "early engagement in the first hour sets the post's ceiling; staying to reply compounds it",
     0.7),
    ("Repost your best-performing idea reworded a week later — winners repeat",
     "structure", (CTX_POST,),
     None,
     "the audience turns over; a proven hook keeps working, and iterating the winner beats chasing new ones",
     0.65),
    # ---- profile→traffic ----
    ("Make the profile bio a one-line promise + the link — the landing page for every reply click",
     "cta", (CTX_POST, CTX_REPLY, CTX_BIG),
     None,
     "every good reply sends people to the profile; a clear promise + link is what turns that visit into a site click",
     0.8),
]


# --------------------------------------------------------------------------- #
# Extractors
# --------------------------------------------------------------------------- #
_CATEGORY_HINTS = {
    "hook": ("hook", "first line", "opener", "headline", "attention"),
    "cta": ("cta", "call to action", "link", "profile", "traffic", "click", "convert"),
    "timing": ("timing", "early", "first hour", "minute", "when to post", "reply first"),
    "reply": ("reply", "reply guy", "comment", "respond"),
    "engagement": ("engagement", "reply", "quote", "save", "share", "retweet", "viral"),
    "structure": ("thread", "structure", "format", "one idea", "hook"),
}


def _guess_category(text: str) -> str:
    t = (text or "").lower()
    best, score = "engagement", 0
    for cat, hints in _CATEGORY_HINTS.items():
        s = sum(1 for h in hints if h in t)
        if s > score:
            best, score = cat, s
    return best


def _guess_contexts(text: str, category: str) -> list[str]:
    t = (text or "").lower()
    ctx = set()
    if any(k in t for k in ("reply", "comment", "respond", "under a", "early")):
        ctx.add(CTX_REPLY)
    if any(k in t for k in ("big account", "large account", "viral post", "top reply", "reach")):
        ctx.update((CTX_REPLY, CTX_BIG))
    if any(k in t for k in ("post", "thread", "tweet", "hook", "cta", "bio", "profile")):
        ctx.add(CTX_POST)
    if category in ("hook", "structure", "cta"):
        ctx.add(CTX_POST)
    if category == "reply":
        ctx.update((CTX_REPLY, CTX_BIG))
    return sorted(ctx) or [CTX_POST]


def _extract_intel(md_text: str) -> list[dict]:
    """Deterministically pull growth/engagement tactics from X_INTEL_HACKS.md.

    The file is `## [score] Title` blocks with **field:** lines. We keep only the
    engagement/growth-flavored ones and reshape the 'experiment to run' into an
    imperative tactic.
    """
    out = []
    blocks = re.split(r"\n##\s+", "\n" + md_text)
    for b in blocks:
        b = b.strip()
        if not b or b.startswith("#"):
            continue
        title = _clean(b.split("\n", 1)[0])
        title = re.sub(r"^\[[\d.]+\]\s*", "", title)
        if not title:
            continue

        def _field(name):
            m = re.search(rf"\*\*{name}:\*\*\s*(.+)", b)
            return _clean(m.group(1)) if m else ""

        cat_raw = _field("category").lower()
        exp = _field("experiment to run")
        result = _field("claimed result")
        spec_m = re.search(r"\*\*specificity:\*\*\s*([\d.]+)", b)
        spec = float(spec_m.group(1)) if spec_m else 0.5

        blob = f"{title} {exp}".lower()
        # Only harvest growth/engagement/traffic tactics — skip pure SEO-plumbing,
        # cost, or stack items that don't inform how to write a post/reply.
        engagement_like = (
            cat_raw in ("growth",)
            or any(k in blob for k in (
                "viral", "engagement", "hook", "reply", "follower", "audience",
                "thread", "post", "traffic", "profile", "click", "faceless",
                "went viral", "reach", "impression"))
        )
        if not engagement_like:
            continue
        # Demote pure SEO/GSC plumbing — it's growth-adjacent but doesn't inform
        # how to WRITE a post/reply, so it shouldn't out-rank copy tactics. Keep
        # only if it also carries a clear engagement/hook signal.
        seo_plumbing = any(k in blob for k in (
            "search console", "gsc", "serp", "conversion tracking", "google ads",
            "backlink", "sitemap", "crawl"))
        engagement_signal = any(k in blob for k in (
            "hook", "viral", "reply", "thread", "follower", "audience",
            "faceless", "went viral", "engagement"))
        if seo_plumbing and not engagement_signal:
            continue

        tactic = exp or title
        # Reshape a raw experiment sentence into a compact imperative.
        tactic = re.sub(r"^(try:|try to|go to|check|1\.)\s*", "", tactic, flags=re.I).strip()
        tactic = tactic[0].upper() + tactic[1:] if tactic else tactic
        if not tactic or not _anon_ok(tactic):
            continue

        cat = _guess_category(blob)
        out.append({
            "tactic": _clean(tactic),
            "category": cat,
            "contexts": _guess_contexts(blob, cat),
            "template": None,
            "why": _clean(result) or "surfaced from practitioner X intel as a growth lever",
            "source": "x_intel",
            "specificity": round(min(0.95, max(0.3, spec)), 2),
        })
    return out


# Reusable hook/opener STRUCTURES detected in banked banger lines. We don't reuse
# the insult content (that's persona3-specific) — we extract the *shape* that made
# a short line land, which is a reusable reply pattern.
_HOOK_PATTERNS = [
    (re.compile(r"^(that|this) (was|is) (a )?", re.I),
     "Open the reply by naming the OP's move flatly, then land the twist",
     "reply"),
    (re.compile(r"\b(you'?re just|you'?re the kinda|imagine)\b", re.I),
     "Frame the reply as a crisp characterization the audience recognizes",
     "reply"),
    (re.compile(r"\b(and\?|nobody|ok and)\b", re.I),
     "Dismissive one-beat replies punch above their length — say less, imply more",
     "reply"),
]


def _extract_bangers(texts: list[str]) -> list[dict]:
    out, seen = [], set()
    for t in texts:
        t = _clean(t)
        if not t or not _anon_ok(t):
            continue
        for pat, tactic, cat in _HOOK_PATTERNS:
            if pat.search(t) and tactic not in seen:
                seen.add(tactic)
                out.append({
                    "tactic": tactic,
                    "category": "reply",
                    "contexts": [CTX_REPLY, CTX_BIG],
                    "template": None,
                    "why": "reverse-engineered from a banked banger that scored well — reusable reply shape",
                    "source": "banger",
                    "specificity": 0.6,
                })
    return out


def _seed_records() -> list[dict]:
    out = []
    for tactic, cat, ctx, tmpl, why, spec in _SEED:
        if not _anon_ok(tactic):
            continue
        out.append({
            "tactic": tactic,
            "category": cat,
            "contexts": list(ctx),
            "template": tmpl,
            "why": why,
            "source": "seed",
            "specificity": spec,
        })
    return out


def _summarize_tactic(raw: str) -> str:
    """Best-effort: compress a long raw write-up into one imperative tactic.

    Cheap local model; returns "" on any failure so callers fall back to
    deterministic text. Never blocks a harvest.
    """
    raw = _clean(raw)
    if not raw:
        return ""
    prompt = (
        "Rewrite the following into ONE short imperative tactic (max 18 words) for "
        "growing engagement/followers on X. No hashtags, no names, no first person. "
        "Just the tactic.\n\n" + raw[:600]
    )
    try:
        out = _clean(_ollama(prompt, temperature=0.3, max_tokens=60))
    except Exception:
        return ""
    if not out or not _anon_ok(out) or len(out) > 160:
        return ""
    return out


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #
def _load_banked() -> list[dict]:
    if not OUT.exists():
        return []
    rows = []
    for line in OUT.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows


def _is_dup(tactic: str, banked_tactics: list[str]) -> bool:
    tl = tactic.lower()
    for bt in banked_tactics:
        if _jaccard(tl, bt.lower()) >= DEDUP_MAX_JACCARD:
            return True
    return False


def harvest(*, use_web: bool = False, use_model: bool = False) -> dict:
    """Mine viral tactics from all local sources (+ optional web/model) and
    deduped-append to viral_tactics.jsonl.

    use_web/use_model default OFF so a scheduled run is deterministic, cheap, and
    offline-safe. Returns a summary dict.
    """
    HUSTLE.mkdir(parents=True, exist_ok=True)
    candidates: list[dict] = []

    # 1) evergreen seed corpus (always)
    candidates += _seed_records()

    # 2) practitioner X intel
    if INTEL.exists():
        try:
            candidates += _extract_intel(INTEL.read_text())
        except Exception:
            pass

    # 3) banked bangers -> reusable reply shapes
    if BANGERS.exists():
        texts = []
        for line in BANGERS.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                texts.append(json.loads(line).get("text", ""))
            except Exception:
                continue
        candidates += _extract_bangers(texts)

    # 4) optional best-effort web harvest (guarded; never required)
    if use_web:
        candidates += _harvest_web()

    # 5) optional model enrichment of low-specificity raw tactics
    if use_model:
        for c in candidates:
            if c["source"] in ("x_intel",) and c["specificity"] < 0.6:
                better = _summarize_tactic(c["tactic"] + ". " + c.get("why", ""))
                if better:
                    c["tactic"] = better

    banked = _load_banked()
    banked_tactics = [r.get("tactic", "") for r in banked]
    seen_ids = {r.get("id") for r in banked}

    added, skipped = 0, 0
    new_rows = []
    for c in candidates:
        tactic = _clean(c.get("tactic", ""))
        if not tactic or not _anon_ok(tactic):
            skipped += 1
            continue
        tid = _tid(tactic)
        if tid in seen_ids or _is_dup(tactic, banked_tactics + [r["tactic"] for r in new_rows]):
            skipped += 1
            continue
        rec = {
            "id": tid,
            "tactic": tactic,
            "category": c.get("category", "engagement"),
            "contexts": c.get("contexts", [CTX_POST]),
            "template": c.get("template"),
            "why": _clean(c.get("why", "")),
            "source": c.get("source", "seed"),
            "specificity": round(float(c.get("specificity", 0.5)), 2),
            "ts": _now(),
        }
        new_rows.append(rec)
        seen_ids.add(tid)
        added += 1

    if new_rows:
        with OUT.open("a") as f:
            for r in new_rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    total = len(banked) + added
    if total > SOFT_CAP:
        _trim_to_cap()

    return {
        "candidates": len(candidates),
        "added": added,
        "skipped_dup_or_empty": skipped,
        "total_banked": min(total, SOFT_CAP),
        "path": str(OUT),
    }


def _trim_to_cap() -> None:
    rows = _load_banked()
    rows.sort(key=lambda r: r.get("specificity", 0), reverse=True)
    rows = rows[:SOFT_CAP]
    with OUT.open("w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _harvest_web() -> list[dict]:
    """Best-effort pull of a couple public 'what makes posts go viral' pages.

    Uses stdlib urllib only (no external deps, works in a scheduled context).
    Any failure yields []. Extraction is deterministic: pull sentence-level
    imperatives that mention engagement levers, then anonymity-filter.
    """
    import urllib.request

    urls = [
        # Public, evergreen growth write-ups. Kept minimal + best-effort.
        "https://buffer.com/resources/twitter-engagement/",
        "https://buffer.com/resources/how-to-go-viral/",
    ]
    out = []
    lever = re.compile(
        r"\b(hook|reply|thread|first line|early|profile|link|question|cta|"
        r"quote|retweet|repost|specific|contrarian|number)\b", re.I)
    for u in urls:
        try:
            req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=12) as resp:
                html = resp.read().decode("utf-8", "ignore")
        except Exception:
            continue
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"&[a-z]+;", " ", text)
        for sent in re.split(r"(?<=[.!?])\s+", text):
            sent = _clean(sent)
            if not (25 <= len(sent) <= 160) or not lever.search(sent):
                continue
            if not _anon_ok(sent):
                continue
            cat = _guess_category(sent)
            out.append({
                "tactic": sent,
                "category": cat,
                "contexts": _guess_contexts(sent, cat),
                "template": None,
                "why": f"public growth write-up ({u.split('/')[2]})",
                "source": f"web:{u.split('/')[2]}",
                "specificity": 0.5,
            })
            if len(out) >= 40:
                break
    return out


# --------------------------------------------------------------------------- #
# Playbook
# --------------------------------------------------------------------------- #
_CTX_NORMALIZE = {
    "reply": CTX_REPLY, "comment": CTX_REPLY, "respond": CTX_REPLY,
    "post": CTX_POST, "tweet": CTX_POST, "thread": CTX_POST,
    "big": CTX_BIG, "big_account": CTX_BIG, "big account": CTX_BIG,
    "large_account": CTX_BIG, "viral": CTX_BIG,
}


def _norm_ctx(context: str) -> str:
    c = (context or "").strip().lower()
    key = c.replace(" ", "_")
    if key in _CTX_NORMALIZE:
        return _CTX_NORMALIZE[key]
    if c in _CTX_NORMALIZE:
        return _CTX_NORMALIZE[c]
    # Phrase/free-text: infer from keywords. "big/large/viral account" wins over
    # a bare "reply" so "reply under a big account" -> big_account.
    if any(k in c for k in ("big", "large", "viral", "top reply")):
        return CTX_BIG
    if any(k in c for k in ("reply", "comment", "respond")):
        return CTX_REPLY
    return CTX_POST


def _all_tactics() -> list[dict]:
    """Banked tactics if present, else the in-memory corpus (seed + local
    extraction) so playbook() works even before the first harvest run."""
    rows = _load_banked()
    if rows:
        return rows
    mem = _seed_records()
    if INTEL.exists():
        try:
            mem += _extract_intel(INTEL.read_text())
        except Exception:
            pass
    for i, m in enumerate(mem):
        m.setdefault("id", _tid(m["tactic"]))
    return mem


def playbook(context: str = "post", *, limit: int = 6,
             category: str | None = None) -> dict:
    """Top applicable tactics for a situation, ranked, plus an injectable snippet.

    context: "reply" | "post" | "big_account" (loose synonyms accepted).
    Returns {context, tactics: [...], prompt_snippet: str} — generators inline
    prompt_snippet into their system/user prompt to raise engagement + drive
    profile→site traffic. Anonymity-safe by construction.
    """
    ctx = _norm_ctx(context)
    rows = _all_tactics()

    scored = []
    for r in rows:
        contexts = r.get("contexts", [CTX_POST])
        if ctx not in contexts:
            continue
        if category and r.get("category") != category:
            continue
        tactic = r.get("tactic", "")
        if not tactic or not _anon_ok(tactic):
            continue
        # Rank: specificity, with a nudge for tactics that name THIS context
        # exclusively (more targeted) and for source credibility.
        spec = float(r.get("specificity", 0.5))
        focus = 0.08 if contexts == [ctx] or (ctx == CTX_BIG and CTX_BIG in contexts) else 0.0
        src_bonus = {"seed": 0.04, "x_intel": 0.03, "web": 0.01, "banger": 0.0}.get(
            r.get("source", "").split(":")[0], 0.0)
        scored.append((round(spec + focus + src_bonus, 3), r))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = [r for _, r in scored[:limit]]

    lines = []
    for r in top:
        line = f"- {r['tactic']}"
        if r.get("template"):
            line += f"  (template: {r['template']})"
        lines.append(line)
    header = {
        CTX_REPLY: "ENGAGEMENT PLAYBOOK — writing a reply (raise reach + profile clicks):",
        CTX_POST: "ENGAGEMENT PLAYBOOK — writing a post (stop the scroll + drive profile→site):",
        CTX_BIG: "ENGAGEMENT PLAYBOOK — replying under a big account (climb to top reply, pull traffic):",
    }[ctx]
    snippet = header + "\n" + "\n".join(lines) if lines else ""

    return {
        "context": ctx,
        "tactics": top,
        "prompt_snippet": snippet,
    }


# --------------------------------------------------------------------------- #
# CLI / smoke
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="Viral tactics harvester + playbook")
    ap.add_argument("--harvest", action="store_true", help="run harvest only")
    ap.add_argument("--web", action="store_true", help="include best-effort web harvest")
    ap.add_argument("--model", action="store_true", help="enrich w/ local model summaries")
    ap.add_argument("--context", default=None, help="show playbook for a context")
    args = ap.parse_args()

    if args.context:
        pb = playbook(args.context)
        print(json.dumps(pb, indent=2, ensure_ascii=False))
        return 0

    summary = harvest(use_web=args.web, use_model=args.model)
    print("HARVEST:", json.dumps(summary, ensure_ascii=False))
    if args.harvest:
        return 0

    for ctx in ("reply", "post", "big_account"):
        pb = playbook(ctx, limit=4)
        print(f"\n=== playbook({ctx}) — {len(pb['tactics'])} tactics ===")
        print(pb["prompt_snippet"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
