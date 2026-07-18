#!/usr/bin/env python3
"""GEO / AI-Visibility probe — does an LLM cite THIS business for "best [category] in [city]"?

The free-tool hook for the $39 GEO Fix Kit (forked from the broken-site-rescue engine).
Given business + city + category, it runs a fixed bank of "best <category> in <city>"-style
prompts against a local model (Ollama-first, $0), parses which businesses the model names and
in what order, then scores the target A-F vs the competitors the model actually cites, and
emits the gaps the Fix Kit fills.

Model layer (truthful by construction):
  1. REAL hosted frontier assistants — queried where keys exist (default: OpenAI GPT-4o mini,
     Anthropic Claude 3.5 Haiku, Google Gemini 3.5 Flash, all reached via OPENROUTER_API_KEY;
     direct GEMINI_API_KEY used too if GEO_USE_GEMINI_DIRECT=1). The exact models that actually
     answered are recorded in `sources` so downstream copy can name them honestly.
  2. If NO hosted key works, fall back to the LOCAL open model (Ollama/qwen) — framed strictly
     as a "simulation", never as a real-assistant answer.
  3. If no LLM at all is reachable, a $0 deterministic SearXNG snapshot still yields a grounded,
     in-city leaderboard so a scan never errors.

Cost is kept sane: a few buyer prompts (DEFAULT_N_PROMPTS) across a small model set, and the
per-market answers are CACHED on (category, city, model-set). "Best plumber in Omaha" is the
same market for every Omaha plumber, so the whole town is scored off one cached generation and
repeat scans in a cached market cost $0. The cache key isolates each CITY in its own slug
segment and tags the backend, so cities can never cross-contaminate and a qwen cache is never
reused for a hosted scan.

  python3 -m core.geo_visibility_probe "Joe's Plumbing" "Omaha NE" plumber
  python3 -m core.geo_visibility_probe --self-test
  python3 -m core.geo_visibility_probe "Acme Dental" "Austin TX" dentist --json --refresh
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(os.environ.get("CLAUDE_STACK_DIR", Path(__file__).resolve().parents[1]))
CACHE_DIR = ROOT / "data" / "hustle" / "geo_cache"

# Query bank — the real searches a buyer types into ChatGPT/Gemini/Perplexity. Kept fixed so
# the cache key is stable and grades are reproducible (a screenshot defends a dispute).
QUERY_TEMPLATES = (
    "Best {category} in {city}?",
    "Who is the top-rated {category} in {city}? List a few by name.",
    "Recommend a good {category} in {city}.",
    "I need a {category} in {city} — who should I call? Name some businesses.",
    "What are the most popular {category} businesses in {city}?",
    "If I'm looking for a {category} near {city}, who are the best options?",
)
DEFAULT_N_PROMPTS = 4  # buyer prompts per scan; run against each hosted model (cost-bounded)
TOP_K = 8  # we only read the first ~8 names the model lists (what a buyer actually sees)

# Lines that are framing, not a business name.
_SKIP_PREFIX = (
    "here are", "here is", "sure", "based on", "i don't", "i do not", "as an ai",
    "note:", "please", "of course", "certainly", "the best", "top ", "some of",
    "keep in mind", "disclaimer", "unfortunately", "i'm not", "i am not", "while i",
    "however", "additionally", "in summary", "to find", "you can", "these are",
)
_GENERIC_TOKENS = {
    "the", "a", "an", "and", "of", "in", "for", "best", "top", "rated", "service",
    "services", "company", "co", "llc", "inc", "ltd", "corp", "group", "near", "me",
}


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(s or "").lower()).strip("-")


def _norm_name(s: str) -> str:
    s = re.sub(r"[^a-z0-9 ]", " ", str(s or "").lower())
    toks = [t for t in s.split() if t and t not in {"llc", "inc", "ltd", "co", "corp", "the"}]
    return " ".join(toks).strip()


def _name_tokens(s: str) -> set[str]:
    return {t for t in _norm_name(s).split() if t not in _GENERIC_TOKENS and len(t) > 1}


def _same_business(a: str, b: str) -> bool:
    na, nb = _norm_name(a), _norm_name(b)
    if not na or not nb:
        return False
    if na == nb or na in nb or nb in na:
        return True
    ta, tb = _name_tokens(a), _name_tokens(b)
    if not ta or not tb:
        return False
    overlap = len(ta & tb) / max(1, min(len(ta), len(tb)))
    return overlap >= 0.6


def _extract_businesses(text: str) -> list[str]:
    """Ordered, de-duped list of business names the model named, top-first.

    Heuristic, model-agnostic: walk numbered/bulleted/bold lines, strip markdown and any
    trailing description after a separator, drop framing lines and obvious non-names."""
    out: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        # strip list markers: "1.", "1)", "- ", "* ", "• "
        line = re.sub(r"^\s*(?:\d{1,2}[.)]|[-*•])\s+", "", line)
        line = line.strip()
        low = line.lower()
        if any(low.startswith(p) for p in _SKIP_PREFIX):
            continue
        # take the name up to the first separator that introduces a description
        name = re.split(r"\s*(?:[:–—]| - |\(|•| \| )", line, maxsplit=1)[0]
        # strip markdown bold/italic and stray quotes
        name = re.sub(r"[*_`\"]", "", name).strip().strip(".").strip()
        if not name or len(name) < 2 or len(name) > 60:
            continue
        if not re.match(r"^[A-Za-z0-9]", name):
            continue
        # must look like a name (has a letter; not a whole sentence)
        if len(name.split()) > 8:
            continue
        if not any(_same_business(name, o) for o in out):
            out.append(name)
        if len(out) >= TOP_K:
            break
    return out


# ---------------------------------------------------------------------------- LLM layer

def _ollama_generate(prompt: str, *, timeout_s: float = 40.0) -> str | None:
    try:
        from core import local_client
    except Exception:
        return None

    async def _run():
        try:
            if not await local_client.is_available():
                return None
        except Exception:
            return None
        return await local_client.generate(
            prompt, max_tokens=350, temperature=0.4,
            system="You are a local-search assistant. Answer with a short numbered list of "
                   "real business names only, most relevant first. No preamble.",
        )

    try:
        return asyncio.run(asyncio.wait_for(_run(), timeout=timeout_s))
    except RuntimeError:
        # already inside a loop (rare for this CLI/path) — spin a private loop
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(asyncio.wait_for(_run(), timeout=timeout_s))
        except Exception:
            return None
        finally:
            loop.close()
    except Exception:
        return None


# --- REAL hosted frontier assistants -----------------------------------------------------
# These are genuine consumer assistants (OpenAI / Anthropic / Google), reached via OpenRouter.
# Naming them in `sources` is truthful: the business names below ARE those models' real output.
_DEFAULT_HOSTED = "openai/gpt-4o-mini,anthropic/claude-3.5-haiku,google/gemini-3.5-flash"
_MODEL_LABELS = {
    "openai/gpt-4o-mini": "OpenAI GPT-4o mini",
    "openai/gpt-4o": "OpenAI GPT-4o",
    "anthropic/claude-3.5-haiku": "Anthropic Claude 3.5 Haiku",
    "anthropic/claude-3-haiku": "Anthropic Claude 3 Haiku",
    "google/gemini-3.5-flash": "Google Gemini 3.5 Flash",
    "google/gemini-3.1-flash-lite": "Google Gemini 3.1 Flash Lite",
}
_GEO_SYSTEM = (
    "You are a local-search assistant. Name ONLY real businesses that actually operate in the "
    "specified city. Reply with a short numbered list of business names, most relevant first. "
    "No preamble, no disclaimers, no businesses from other cities."
)


def _local_model_name() -> str:
    return os.environ.get("CLAUDE_STACK_LOCAL_MODEL", os.environ.get("LOCAL_MODEL", "qwen2.5:7b"))


def _label_for(model_id: str) -> str:
    return _MODEL_LABELS.get(model_id, model_id)


_OR_GOOD_KEY: str | None = None  # cached working key (avoids retrying dead keys every call)


def _openrouter_keys() -> list[str]:
    """All candidate OpenRouter keys, ordered, de-duped. The shell sometimes exports a STALE
    key that shadows the valid one in .env (load_dotenv won't override it), so we collect both
    and let the caller try each — otherwise the whole probe silently degrades to 'simulation'."""
    cands: list[str] = []
    env = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if env:
        cands.append(env)
    try:
        for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
            if line.startswith("OPENROUTER_API_KEY="):
                v = line.split("=", 1)[1].strip().strip('"').strip("'")
                if v:
                    cands.append(v)
    except Exception:
        pass
    seen: set[str] = set()
    return [k for k in cands if not (k in seen or seen.add(k))]


def _openrouter_key() -> str:
    keys = _openrouter_keys()
    return keys[0] if keys else ""


def _hosted_model_ids() -> list[str]:
    """The configured set of REAL hosted models to query (empty => hosted disabled/unavailable)."""
    if os.environ.get("GEO_DISABLE_HOSTED", "") == "1":
        return []
    if not _openrouter_key():
        return []
    raw = os.environ.get("GEO_HOSTED_MODELS", _DEFAULT_HOSTED)
    return [m.strip() for m in raw.split(",") if m.strip()]


def _openrouter_chat(prompt: str, model: str, *, timeout_s: float = 45.0) -> str | None:
    global _OR_GOOD_KEY
    import urllib.error
    import urllib.request
    keys = _openrouter_keys()
    if not keys:
        return None
    # try the known-good key first, then the rest (auth failures fall through to the next key)
    ordered = ([_OR_GOOD_KEY] if _OR_GOOD_KEY in keys else []) + [k for k in keys if k != _OR_GOOD_KEY]
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": _GEO_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 350, "temperature": 0.3,
    }).encode()
    for key in ordered:
        try:
            req = urllib.request.Request(
                "https://openrouter.ai/api/v1/chat/completions", data=body,
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://scannerapp.dev",
                    "X-Title": "scannerapp GEO probe",
                },
            )
            with urllib.request.urlopen(req, timeout=timeout_s) as r:
                data = json.loads(r.read().decode("utf-8", "ignore"))
            _OR_GOOD_KEY = key
            return ((data.get("choices") or [{}])[0].get("message") or {}).get("content")
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):  # dead/rotated key — try the next candidate
                continue
            return None  # model/rate error — don't burn the rest of the keys
        except Exception:
            return None
    return None


def _gemini_direct(prompt: str, *, timeout_s: float = 45.0) -> str | None:
    """Best-effort direct Google Gemini call (off by default — key is often quota-limited)."""
    if os.environ.get("GEO_USE_GEMINI_DIRECT", "") != "1":
        return None
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        return None
    try:
        import urllib.request
        model = os.environ.get("GEO_GEMINI_DIRECT_MODEL", "gemini-2.0-flash")
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/{model}"
               f":generateContent?key={key}")
        body = json.dumps({
            "system_instruction": {"parts": [{"text": _GEO_SYSTEM}]},
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": 350, "temperature": 0.3},
        }).encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            data = json.loads(r.read().decode("utf-8", "ignore"))
        parts = (((data.get("candidates") or [{}])[0].get("content") or {}).get("parts") or [{}])
        return parts[0].get("text")
    except Exception:
        return None


def _searx_fallback(category: str, city: str) -> str | None:
    """$0 grounded fallback when no LLM is reachable: read the public SERP and synthesize a
    name list from the organic results so a scan still grades instead of erroring."""
    try:
        sys.path.insert(0, str(ROOT))
        from agents.local_biz_scraper import _searx  # noqa
        results = _searx(f"best {category} in {city}") or []
        names = []
        for r in results[:TOP_K]:
            # _searx yields (domain, title) TUPLES, not dicts — calling .get() here crashed the
            # "$0 scan never errors" path with AttributeError exactly when the cheap path was needed.
            if isinstance(r, (list, tuple)):
                title = r[1] if len(r) > 1 else ""
            elif isinstance(r, dict):
                title = r.get("title") or ""
            else:
                title = str(r)
            title = title.split("|")[0].split(" - ")[0].strip()
            if title and len(title) < 60:
                names.append(title)
    except Exception:
        return None
    if not names:
        return None
    return "\n".join(f"{i+1}. {n}" for i, n in enumerate(names))


# ---------------------------------------------------------------------------- market cache

def _backend_tag() -> str:
    """Stable tag for the cache key so different model backends never share a cache file
    (a qwen 'simulation' market must never be reused for a real hosted scan)."""
    ids = _hosted_model_ids()
    if ids:
        h = hashlib.sha1(",".join(sorted(ids)).encode()).hexdigest()[:8]
        return f"hosted-{h}"
    return "local-" + _slug(_local_model_name())


def _cache_path(category: str, city: str) -> Path:
    # Each component is slugged on its own and joined with "__" so a city can never bleed into
    # the category (or another city) via slug collision — the wrong-city leaderboard bug.
    key = f"{_slug(category)}__{_slug(city)}__{_backend_tag()}"
    return CACHE_DIR / f"{key}.json"


def query_market(category: str, city: str, *, n_prompts: int = DEFAULT_N_PROMPTS,
                 refresh: bool = False, offline_stub: dict | None = None) -> dict:
    """Run (or load cached) the prompt bank for a (category, city) MARKET. The result is
    business-agnostic and reusable to score every business in that market.

    Each buyer prompt is asked to EVERY configured real hosted assistant, so one market holds
    answers from multiple genuine models. The exact models that replied are listed in `sources`.
    Cache key isolates the city, so a Seattle market can never return Los Angeles competitors."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _cache_path(category, city)
    if not refresh and path.exists():
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            if cached.get("answers"):
                cached["from_cache"] = True
                return cached
        except Exception:
            pass

    prompts = [t.format(category=category, city=city) for t in QUERY_TEMPLATES[:n_prompts]]
    answers: list[dict] = []
    sources: set[str] = set()
    models_queried: list[str] = []

    if offline_stub is not None:
        for p in prompts:
            text = offline_stub.get(p) or offline_stub.get("*")
            src = "self-test stub (simulation)"
            sources.add(src)
            answers.append({"prompt": p, "model": "stub", "model_label": src,
                            "answer": text or "", "businesses": _extract_businesses(text or ""),
                            "source": src})
    else:
        hosted = _hosted_model_ids()
        for p in prompts:
            got_any = False
            for model_id in hosted:
                text = _openrouter_chat(p, model_id)
                if not (text and text.strip()):
                    continue
                got_any = True
                label = _label_for(model_id)
                if model_id not in models_queried:
                    models_queried.append(model_id)
                sources.add(label)
                answers.append({"prompt": p, "model": model_id, "model_label": label,
                                "answer": text, "businesses": _extract_businesses(text),
                                "source": label})
            # best-effort direct Gemini (only if explicitly enabled and it returns)
            gtxt = _gemini_direct(p)
            if gtxt and gtxt.strip():
                got_any = True
                label = "Google Gemini (direct API)"
                if "gemini-direct" not in models_queried:
                    models_queried.append("gemini-direct")
                sources.add(label)
                answers.append({"prompt": p, "model": "gemini-direct", "model_label": label,
                                "answer": gtxt, "businesses": _extract_businesses(gtxt),
                                "source": label})
            if not got_any:
                # No hosted model answered — local fallback, framed strictly as a SIMULATION.
                text = _ollama_generate(p)
                if text and text.strip():
                    label = f"Local open model ({_local_model_name()}) — simulation"
                    sources.add(label)
                    answers.append({"prompt": p, "model": _local_model_name(),
                                    "model_label": label, "answer": text,
                                    "businesses": _extract_businesses(text), "source": label})
                else:
                    text = _searx_fallback(category, city)
                    if text and text.strip():
                        label = "Web search snapshot (no AI model) — simulation"
                        sources.add(label)
                        answers.append({"prompt": p, "model": "searx",
                                        "model_label": label, "answer": text,
                                        "businesses": _extract_businesses(text), "source": label})

    hosted_used = bool(models_queried) and any(
        a["model"] not in ("stub", "searx", _local_model_name()) for a in answers)
    market = {
        "category": category, "city": city,
        "n_prompts": len(prompts),
        "n_answers": len(answers),
        "backend": "hosted" if hosted_used else ("stub" if offline_stub is not None else "local"),
        "is_simulation": not hosted_used,  # downstream copy gates "simulation" framing on this
        "model": ", ".join(sorted(sources)) or _local_model_name(),
        "models_queried": models_queried,
        "sources": sorted(sources),
        "answers": answers,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "from_cache": False,
    }
    if offline_stub is None:  # never cache synthetic self-test data
        try:
            path.write_text(json.dumps(market, indent=2), encoding="utf-8")
        except Exception:
            pass
    return market


# ---------------------------------------------------------------------------- scoring

def grade_from_score(score: float) -> str:
    if score >= 85:
        return "A"
    if score >= 70:
        return "B"
    if score >= 55:
        return "C"
    if score >= 40:
        return "D"
    return "F"


def _rank_in(target: str, businesses: list[str]):
    for i, b in enumerate(businesses):
        if _same_business(target, b):
            return i + 1  # 1-based
    return None


def score_business(business: str, city: str, category: str, *,
                   n_prompts: int = DEFAULT_N_PROMPTS, refresh: bool = False,
                   offline_stub: dict | None = None) -> dict:
    """Full A-F AI-visibility verdict for one business in its market."""
    market = query_market(category, city, n_prompts=n_prompts, refresh=refresh, offline_stub=offline_stub)
    answers = market["answers"]
    n = max(1, len(answers))

    ranks: list[int] = []
    per_prompt_pts = 0.0
    competitor_counts: dict[str, int] = {}
    competitor_rank_sum: dict[str, float] = {}
    for a in answers:
        biz = a["businesses"]
        for i, b in enumerate(biz):
            if _same_business(business, b):
                continue
            key = b.strip()
            competitor_counts[key] = competitor_counts.get(key, 0) + 1
            competitor_rank_sum[key] = competitor_rank_sum.get(key, 0.0) + (i + 1)
        r = _rank_in(business, biz)
        if r is not None:
            ranks.append(r)
            # earlier rank = more points; absent = 0
            per_prompt_pts += max(0.0, (TOP_K - (r - 1)) / TOP_K)

    appearances = len(ranks)
    visibility_score = round(100.0 * per_prompt_pts / n, 1)
    grade = grade_from_score(visibility_score)
    avg_rank = round(sum(ranks) / len(ranks), 1) if ranks else None

    # merge competitor name variants, rank by (appearances desc, avg rank asc)
    merged: dict[str, dict] = {}
    for name, cnt in competitor_counts.items():
        placed = False
        for key in list(merged):
            if _same_business(name, key):
                merged[key]["count"] += cnt
                merged[key]["rank_sum"] += competitor_rank_sum[name]
                placed = True
                break
        if not placed:
            merged[name] = {"count": cnt, "rank_sum": competitor_rank_sum[name]}
    leaderboard = sorted(
        ({"name": k, "appearances": v["count"], "avg_rank": round(v["rank_sum"] / v["count"], 1)}
         for k, v in merged.items()),
        key=lambda x: (-x["appearances"], x["avg_rank"]),
    )[:5]

    gaps = _build_gaps(business, city, category, appearances, n, leaderboard)

    return {
        "business": business, "city": city, "category": category,
        "grade": grade,
        "visibility_score": visibility_score,
        "appearances": appearances,
        "prompts_run": n,
        "avg_rank": avg_rank,
        "leaderboard": leaderboard,
        "gaps": gaps,
        "model": market.get("model"),
        "sources": market.get("sources"),
        "models_queried": market.get("models_queried", []),
        "backend": market.get("backend"),
        "is_simulation": market.get("is_simulation", True),
        "from_cache": market.get("from_cache", False),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "_market": market,  # raw answers — screenshot evidence for disputes
    }


def _build_gaps(business: str, city: str, category: str, appearances: int, n: int,
                leaderboard: list[dict]) -> list[str]:
    """Deterministic, honest gap list — each one is what the Fix Kit actually addresses."""
    gaps: list[str] = []
    if appearances == 0:
        gaps.append(
            f"AI assistants never named {business} in {appearances}/{n} answers for "
            f"\"{category} in {city}\" — you are invisible to AI search.")
    elif appearances < n:
        gaps.append(
            f"{business} appeared in only {appearances}/{n} AI answers — inconsistent, "
            f"easily missed when a buyer asks once.")
    if leaderboard:
        rivals = ", ".join(c["name"] for c in leaderboard[:3])
        gaps.append(f"Competitors the AI cites instead: {rivals}.")
    gaps.append(
        "No llms.txt found — AI models have no machine-readable profile of your business to "
        "quote (the Fix Kit ships a correct one).")
    gaps.append(
        "Likely missing LocalBusiness JSON-LD schema — without it AI can't reliably extract "
        "your services, hours, and service area to cite you.")
    gaps.append(
        "No GEO content answering the exact buyer questions (\"best " + category +
        " in " + city + "\") that AI pulls from — the Fix Kit ships 5 ready templates.")
    return gaps


# ---------------------------------------------------------------------------- self-test

def _stub_market() -> dict:
    """Synthetic per-prompt answers so scoring can be exercised end-to-end with no LLM/net."""
    strong = "\n".join([
        "1. Summit Dental Care", "2. Bright Smile Austin", "3. Acme Dental",
        "4. Capital City Orthodontics", "5. Lone Star Family Dentistry",
    ])
    return {"*": strong}


def self_test() -> int:
    samples = [
        ("Acme Dental", "Austin TX", "dentist"),       # present mid-pack -> C/D
        ("Nowhere Plumbing LLC", "Reno NV", "plumber"),  # absent -> F
        ("Summit Dental Care", "Austin TX", "dentist"),  # rank 1 -> high
    ]
    stub = _stub_market()
    ok = True
    for biz, city, cat in samples:
        try:
            r = score_business(biz, city, cat, offline_stub=stub)
            assert r["grade"] in {"A", "B", "C", "D", "F"}, "bad grade"
            assert 0 <= r["visibility_score"] <= 100, "score out of range"
            assert isinstance(r["leaderboard"], list), "no leaderboard"
            assert len(r["gaps"]) >= 3, "gaps missing"
            print(f"[OK] {biz:<22} {cat:<8} {city:<10} -> grade {r['grade']} "
                  f"score {r['visibility_score']:<5} appears {r['appearances']}/{r['prompts_run']} "
                  f"avg_rank {r['avg_rank']}")
        except Exception as e:
            ok = False
            print(f"[FAIL] {biz}: {type(e).__name__}: {e}")
    # one real Ollama smoke (best-effort; non-fatal if local box is busy/offline)
    try:
        live = score_business("Joe's Plumbing", "Omaha NE", "plumber", refresh=True)
        print(f"[live] ollama/searx end-to-end -> grade {live['grade']} score {live['visibility_score']} "
              f"sources {live['sources']} (leaderboard {len(live['leaderboard'])})")
    except Exception as e:
        print(f"[live] skipped (non-fatal): {type(e).__name__}: {e}")
    print("SELF-TEST:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv=None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    if "--self-test" in argv:
        return self_test()
    as_json = "--json" in argv
    refresh = "--refresh" in argv
    pos = [a for a in argv if not a.startswith("--")]
    if len(pos) < 3:
        print('usage: python3 -m core.geo_visibility_probe "<business>" "<city>" <category> '
              "[--json] [--refresh]  |  --self-test")
        return 1
    business, city, category = pos[0], pos[1], pos[2]
    r = score_business(business, city, category, refresh=refresh)
    if as_json:
        r.pop("_market", None)
        print(json.dumps(r, indent=2))
    else:
        print(f"\n{business} — AI visibility for \"{category} in {city}\"")
        print(f"  GRADE: {r['grade']}   score {r['visibility_score']}/100   "
              f"cited in {r['appearances']}/{r['prompts_run']} AI answers   "
              f"avg rank {r['avg_rank']}   (model {r['model']}, sources {r['sources']})")
        print("  Who the AI names instead:")
        for c in r["leaderboard"]:
            print(f"    - {c['name']}  ({c['appearances']}x, avg rank {c['avg_rank']})")
        print("  Gaps the $39 Fix Kit closes:")
        for g in r["gaps"]:
            print(f"    • {g}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
