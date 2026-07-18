#!/usr/bin/env python3
"""content_learning.py — the CONTENT learn loop that makes the authority engine SMARTER.

The faceless authority play posts transferable CRAFT ("safe secrets") across channels. This module
closes the loop: it watches what each posted piece ACTUALLY earns (views/likes/replies/reads) and
folds that back into a learned policy, so the cross-channel poster favors the topics + formats +
channels that PROVABLY land authority and skips the combos that are proven dead.

It COMPOSES with core.strategist_memory (the cortex's working learn ledger) — it imports it and
reuses record()/recent_plays()/learn()/policy_weight()/is_dead_play() verbatim; it NEVER edits it.
The trick: one posted piece is recorded under THREE facets so strategist_memory.learn() produces a
per-(type,lane) policy along every dimension we want to pick on:

    craft_post   :: <channel>   -> per-CHANNEL authority weight
    craft_topic  :: <topic>     -> per-TOPIC (which safe-secret) weight
    craft_format :: <format>    -> per-FORMAT (thread / short / deep-dive ...) weight

All three share ONE computed outcome. The outcome is a REAL data join, not a guess: for each post we
read the channel's own metric file (case_study_metrics.jsonl for X, a local hp stats cache for the
site, youtube/tiktok metric/state files) and return a verdict:

    productive  (GOOD)  the piece landed — engagement or reach cleared the bar
    dead        (BAD)   measured + old enough + went nowhere
    pending             not seen yet / too young / channel has no metrics yet -> re-join next sweep

'productive'/'dead' map onto strategist_memory's own GOOD/BAD/TERMINAL verdict sets, so learn()
scores them and its re-join sweep leaves them alone. 'pending' records nothing, so an unmeasured
post never drags a topic down. Only $0 data joins here — no LLM, no network in the steady state.

pick_winning(kind, candidates) reads the learned policy and ranks: proven winners first, proven-dead
sunk/flagged. With NO data every candidate weighs 1.0 in stable input order — it degrades to a
harmless pass-through instead of crashing. That is the whole point: the engine starts neutral and
gets opinionated only where the data earns it.

    from core.content_learning import record_post, sync, pick_winning

CLI:
    python3 -m core.content_learning --selftest          # proves the loop end-to-end ($0, isolated)
    python3 -m core.content_learning --sync              # join live metrics -> outcomes -> policy
    python3 -m core.content_learning --picks topics a b c # rank candidates by learned authority
    python3 -m core.content_learning --summary           # what the engine has learned so far
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_DEFAULT_ROOT = Path(__file__).resolve().parents[1]

__all__ = [
    "record_post", "sync", "pick_winning", "alive", "best", "summary",
    "engagement_for", "verdict_for", "CHANNELS", "FACET_FOR",
]

# One posted piece -> three learnable facets. The value that varies per dimension is carried in the
# strategist play's `lane`, which is exactly what strategist_memory.learn() aggregates on.
FACET_FOR = {"channels": "craft_post", "topics": "craft_topic", "formats": "craft_format"}
_FACET_ORDER = ("channels", "topics", "formats")

# Verdicts we PERSIST as outcomes. Both are terminal in strategist_memory, so its cortex re-join
# sweep won't clobber them; 'pending' is never written (an unmeasured post must not count as bad).
_GOOD, _BAD = "productive", "dead"
_TERMINAL = {_GOOD, _BAD}

# Canonical channel names + the aliases callers actually pass in.
CHANNELS = ("x", "site", "youtube", "tiktok", "devto")
_CHANNEL_ALIAS = {
    "x": "x", "x_craft": "x", "twitter": "x", "tweet": "x", "hailports_x": "x",
    "site": "site", "web": "site", "www": "site", "homepage": "site",
    "youtube": "youtube", "yt": "youtube", "shorts": "youtube", "short": "youtube",
    "tiktok": "tiktok", "tt": "tiktok",
    "devto": "devto", "dev.to": "devto", "dev_to": "devto", "blog": "devto",
}

# Engagement-signal field names we harvest generically from any channel's metric row. As a channel's
# metric file grows richer fields, this loop gets smarter with ZERO code change.
_REACH_KEYS = ("views", "impressions", "plays", "play_count", "video_views", "reads", "hits",
               "reach", "pageviews", "unique_opens", "opens")
_INTERACTION_KEYS = ("likes", "like_count", "favorites", "diggs", "replies", "reply_count",
                     "comments", "comment_count", "retweets", "reposts", "shares", "share_count",
                     "clicks", "unique_clicks", "reactions", "saves", "bookmarks")

# Land bars. A piece "landed" if it cleared either the interaction floor OR the reach floor. Weak +
# old = dead; weak + young = pending (give it time). Tunable via env for live ops, fixed in tests.
LAND_INTERACTIONS = int(os.environ.get("CONTENT_LAND_INTERACTIONS", "3"))
LAND_REACH = int(os.environ.get("CONTENT_LAND_REACH", "1000"))
MIN_AGE_MIN = int(os.environ.get("CONTENT_MIN_AGE_MIN", "180"))  # don't call a piece dead too early


# ─────────────────────────── paths (env-redirectable for isolated tests) ───────────────────────────
def _root() -> Path:
    return Path(os.environ.get("CLAUDE_STACK_DIR", str(_DEFAULT_ROOT)))


def _hustle() -> Path:
    return _root() / "data" / "hustle"


def _metric_file(channel: str) -> Path:
    h = _hustle()
    return {
        "x": h / "case_study_metrics.jsonl",
        "site": h / "hp_stats.json",
        "youtube": h / "youtube_metrics.json",
        "tiktok": h / "tiktok_metrics.json",
        "devto": h / "devto_metrics.json",
    }.get(channel, h / f"{channel}_metrics.json")


# ─────────────────────────── strategist_memory composition (import, never edit) ───────────────────────────
def _sm():
    """The one dependency. Imported lazily so path/env overrides in --selftest take effect and so a
    broken sibling can't take import of this module down."""
    if str(_root()) not in sys.path:
        sys.path.insert(0, str(_root()))
    import core.strategist_memory as sm  # noqa: WPS433
    return sm


# ─────────────────────────── util ───────────────────────────
def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_ts(s: Any):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def _age_minutes(ts: Any) -> float | None:
    dt = _parse_ts(ts)
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 60.0)


def _norm_hash(text: str) -> str:
    return hashlib.sha1(" ".join((text or "").split()).lower().encode("utf-8")).hexdigest()[:16]


def canon_channel(channel: str) -> str:
    return _CHANNEL_ALIAS.get(str(channel or "").strip().lower(), str(channel or "").strip().lower())


def _int(v: Any) -> int:
    try:
        return int(float(v))
    except Exception:
        return 0


def _extract(row: dict) -> tuple[int, int]:
    """(reach, interactions) harvested generically from one metric row."""
    reach = sum(_int(row.get(k)) for k in _REACH_KEYS if k in row)
    inter = sum(_int(row.get(k)) for k in _INTERACTION_KEYS if k in row)
    return reach, inter


def _iter_rows(path: Path):
    """Yield metric rows from a .jsonl (one obj/line) or .json (list, or dict-of-rows) file.
    Anything unparseable yields nothing — a channel with no readable metrics degrades to 'pending'."""
    if not path.exists():
        return
    try:
        raw = path.read_text(errors="ignore")
    except Exception:
        return
    if path.suffix == ".jsonl":
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if isinstance(obj, dict):
                yield obj
        return
    try:
        data = json.loads(raw)
    except Exception:
        return
    if isinstance(data, list):
        for obj in data:
            if isinstance(obj, dict):
                yield obj
    elif isinstance(data, dict):
        # a dict of rows keyed by id/slug (e.g. site stats {"paths": {slug: {...}}}) or a flat map.
        containers = [data]
        for k in ("paths", "pages", "posts", "items", "rows", "by_id", "stats"):
            if isinstance(data.get(k), dict):
                containers.append(data[k])
            elif isinstance(data.get(k), list):
                for obj in data[k]:
                    if isinstance(obj, dict):
                        yield obj
        for cont in containers:
            for key, val in cont.items():
                if isinstance(val, dict):
                    # fold the key in as a possible id/slug so match-by-key works.
                    yield {"_key": str(key), **val}


def _row_matches(row: dict, post_id: str, text_hash: str, slug: str) -> bool:
    if post_id:
        for k in ("id", "post_id", "tweet_id", "video_id", "_key"):
            if str(row.get(k) or "") == post_id:
                return True
    if slug:
        for k in ("slug", "path", "page", "url", "_key"):
            rv = str(row.get(k) or "")
            if rv and (rv == slug or rv.endswith("/" + slug) or slug in rv):
                return True
    if text_hash:
        for k in ("text", "title", "body", "caption"):
            if row.get(k) and _norm_hash(row[k]) == text_hash:
                return True
    return False


# ─────────────────────────── the join: post -> real engagement ───────────────────────────
def engagement_for(join: dict) -> dict:
    """Read the channel's own metric file and total the real engagement for this post.
    join = {channel, post_id?, text?/text_hash?, slug?}. Pure $0 data read; no network, no LLM.
    Returns {found, reach, interactions, source}. found=False => not seen yet (channel may simply
    have no per-post metrics available), which the verdict treats as 'pending'."""
    channel = canon_channel(join.get("channel", ""))
    post_id = str(join.get("post_id") or "").strip()
    slug = str(join.get("slug") or "").strip()
    text_hash = join.get("text_hash") or (_norm_hash(join["text"]) if join.get("text") else "")
    path = _metric_file(channel)

    found = False
    reach = inter = 0
    # Metric files are SNAPSHOT LOGS: the collector re-appends a row per post on every sweep, so
    # one post appears hundreds of times with monotonically growing counters. Summing matched rows
    # multiplied a post's reach by its snapshot count (measured: 103x on case_study_metrics.jsonl —
    # 436 real views reported as 45,028), which made dead posts look productive and taught the
    # learner to reinforce content that never landed. Counters are cumulative, so take the max.
    for row in _iter_rows(path):
        if not _row_matches(row, post_id, text_hash, slug):
            continue
        found = True
        r, i = _extract(row)
        reach = max(reach, r)
        inter = max(inter, i)
    return {
        "found": found,
        "reach": reach if found else None,
        "interactions": inter if found else None,
        "source": path.name,
    }


def verdict_for(join: dict, *, posted_ts: Any = None, min_age_min: int = MIN_AGE_MIN) -> dict:
    """Turn the engagement join into a landed/dead/pending verdict + the numbers behind it."""
    eng = engagement_for(join)
    reach = eng["reach"] or 0
    inter = eng["interactions"] or 0
    age = _age_minutes(posted_ts)
    if not eng["found"]:
        verdict = "pending"                                   # not in the channel's metrics yet
    elif inter >= LAND_INTERACTIONS or reach >= LAND_REACH:
        verdict = _GOOD                                       # it landed
    elif age is None or age >= min_age_min:
        verdict = _BAD                                        # measured, old enough, went nowhere
    else:
        verdict = "pending"                                   # measured but still young — wait
    return {
        "metric": "engagement",
        "channel": canon_channel(join.get("channel", "")),
        "reach": eng["reach"],
        "interactions": eng["interactions"],
        "score": inter + reach // 100,                        # transparent, monotonic in both signals
        "verdict": verdict,
        "age_min": round(age, 1) if age is not None else None,
        "joined_from": eng["source"],
    }


# ─────────────────────────── record a posted piece (3 facets, shared identity) ───────────────────────────
def _post_key(channel: str, topic: str, fmt: str, post_id: str, text_hash: str, ts: str) -> str:
    seed = "|".join([channel, topic, fmt, post_id or text_hash or "", ts])
    return hashlib.sha256(seed.encode()).hexdigest()[:16]


def _facets(channel: str, topic: str, fmt: str, params: dict) -> list[dict]:
    return [
        {"type": "craft_post", "lane": channel, "params": params},
        {"type": "craft_topic", "lane": topic, "params": params},
        {"type": "craft_format", "lane": fmt, "params": params},
    ]


def record_post(channel: str, secret_id: str, topic: str, fmt: str, *,
                post_id: str | None = None, text: str | None = None,
                slug: str | None = None, posted_ts: str | None = None) -> dict:
    """Record a freshly-posted craft piece as a strategist play under all three facets so the learn
    loop can weigh channel, topic, AND format. Idempotent per (channel,topic,format,post,ts):
    a piece already recorded is not double-logged. Returns {post_key, facet_ids, deduped}."""
    sm = _sm()
    channel = canon_channel(channel)
    topic = str(topic or secret_id or "").strip() or "unknown"
    fmt = str(fmt or "").strip() or "post"
    ts = posted_ts or _now()
    pid = str(post_id or "").strip()
    thash = _norm_hash(text) if text else ""
    pkey = _post_key(channel, topic, fmt, pid, thash, ts)

    params = {
        "secret_id": str(secret_id or topic),
        "topic": topic,
        "format": fmt,
        "channel": channel,
        "post_id": pid or None,
        "text_hash": thash or None,
        "slug": (slug or "").strip() or None,
        "posted_ts": ts,
        "post_key": pkey,
    }

    facets = _facets(channel, topic, fmt, params)
    # dedup on the channel facet's identity (type+params) — the other two share the same params.
    if sm.already_played(facets[0]):
        return {"post_key": pkey, "facet_ids": [sm.play_id(f) for f in facets], "deduped": True}

    ids = []
    for f in facets:
        f["status"] = "posted"
        row = sm.record(f, event="play")
        ids.append(row["id"])
    return {"post_key": pkey, "facet_ids": ids, "deduped": False}


# ─────────────────────────── sync: join live metrics -> outcomes -> policy ───────────────────────────
def _craft_records(sm) -> list[dict]:
    return [r for r in sm.recent_plays(n=100_000) if r.get("type") in set(FACET_FOR.values())]


def sync(*, min_age_min: int = MIN_AGE_MIN, learn: bool = True) -> dict:
    """Join every recorded craft piece to its channel's real engagement, attach a terminal outcome
    to each facet that has now settled (landed/dead), then fold it into the learned policy via
    strategist_memory.learn(). Pieces that are still pending are left to re-join next sweep.
    $0: pure data joins + a policy recompute. Returns a summary."""
    sm = _sm()
    groups: dict[str, list[dict]] = {}
    for rec in _craft_records(sm):
        pkey = (rec.get("params") or {}).get("post_key") or rec.get("id")
        groups.setdefault(pkey, []).append(rec)

    settled = pending = skipped = 0
    for pkey, members in groups.items():
        params = members[0].get("params") or {}
        v = verdict_for(params, posted_ts=params.get("posted_ts"), min_age_min=min_age_min)
        if v["verdict"] not in _TERMINAL:
            pending += 1
            continue
        wrote = False
        for m in members:
            oc = m.get("outcome") or {}
            if isinstance(oc, dict) and oc.get("verdict") in _TERMINAL:
                continue  # already settled — don't re-write (keeps our terminal verdict authoritative)
            sm.record(m, outcome=dict(v), event="outcome")
            wrote = True
        if wrote:
            settled += 1
        else:
            skipped += 1

    policy = sm.learn() if learn else None
    return {
        "posts": len(groups),
        "settled": settled,
        "pending": pending,
        "already_settled": skipped,
        "policy_keys": len((policy or {}).get("by_key", {})) if policy else None,
        "ts": _now(),
    }


# ─────────────────────────── pick_winning: read the policy, favor proven, skip dead ───────────────────────────
def pick_winning(kind: str, candidates: list[str], *, min_n_dead: int = 3,
                 drop_dead: bool = False) -> list[dict]:
    """Rank candidate topics|formats|channels by learned authority. Reads strategist_memory's policy:
    weight >1 = proven to land, <1 = tends dead, 1.0 = unknown (neutral). Proven-dead combos are
    flagged (and dropped if drop_dead). With no learning yet every candidate is neutral 1.0 and the
    input order is preserved — a safe pass-through, never a crash.

    kind: 'topics' | 'formats' | 'channels' (aliases 'topic'/'format'/'channel' accepted)."""
    kind = kind if kind in FACET_FOR else {"topic": "topics", "format": "formats",
                                           "channel": "channels"}.get(kind, kind)
    facet = FACET_FOR.get(kind)
    if not facet:
        raise ValueError(f"pick_winning kind must be one of {list(FACET_FOR)} (got {kind!r})")
    try:
        sm = _sm()
    except Exception:
        # composition dependency missing -> stay neutral rather than crash the poster.
        return [{"value": c, "weight": 1.0, "dead": False, "note": "no policy"} for c in candidates]

    ranked = []
    for c in candidates:
        lane = canon_channel(c) if kind == "channels" else str(c)
        try:
            w = float(sm.policy_weight(facet, lane))
            dead = bool(sm.is_dead_play(facet, lane, min_n=min_n_dead))
        except Exception:
            w, dead = 1.0, False
        ranked.append({"value": c, "weight": round(w, 3), "dead": dead})
    # winners first (higher weight), proven-dead sunk to the bottom; stable for equal keys so a
    # no-data call returns the caller's original order untouched.
    ranked.sort(key=lambda r: (not r["dead"], r["weight"]), reverse=True)
    if drop_dead:
        ranked = [r for r in ranked if not r["dead"]]
    return ranked


def alive(kind: str, candidates: list[str], *, min_n_dead: int = 3) -> list[str]:
    """Just the candidates that aren't proven-dead, best-first. What a poster loops over."""
    return [r["value"] for r in pick_winning(kind, candidates, min_n_dead=min_n_dead, drop_dead=True)]


def best(kind: str, candidates: list[str]) -> str | None:
    """The single most-proven candidate (skipping dead). None only if every candidate is dead."""
    ranked = pick_winning(kind, candidates, drop_dead=True)
    return ranked[0]["value"] if ranked else None


# ─────────────────────────── observability ───────────────────────────
def summary() -> dict:
    """What the engine has learned: per-facet weights + which combos are proven dead."""
    try:
        sm = _sm()
        pol = sm._read_json(sm.STRATEGIST_POLICY, {}) if hasattr(sm, "_read_json") else {}
    except Exception as e:
        return {"error": str(e)}
    out: dict[str, Any] = {"updated": pol.get("updated"), "by_kind": {}}
    by_key = pol.get("by_key") or {}
    for kind, facet in FACET_FOR.items():
        rows = []
        for key, st in by_key.items():
            if not key.startswith(facet + "::"):
                continue
            rows.append({"value": key.split("::", 1)[1], "n": st.get("n"),
                         "score": st.get("score"), "weight": st.get("weight")})
        rows.sort(key=lambda r: (r.get("weight") or 0), reverse=True)
        out["by_kind"][kind] = rows
    return out


# ─────────────────────────── selftest (isolated, $0, proves the loop) ───────────────────────────
def _selftest() -> int:
    import tempfile

    fails: list[str] = []

    def check(name, cond, detail=""):
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}{(' — ' + detail) if detail else ''}")
        if not cond:
            fails.append(name)

    tmp = Path(tempfile.mkdtemp(prefix="content_learning_selftest_"))
    (tmp / "data" / "hustle").mkdir(parents=True, exist_ok=True)
    os.environ["CLAUDE_STACK_DIR"] = str(tmp)  # redirects our metric-file + strategist paths

    # Isolate strategist_memory's ledger + policy into the temp dir (compose, don't pollute prod).
    sm = _sm()
    sm.MEMORY = tmp / "strategist_memory.jsonl"
    sm.STRATEGIST_POLICY = tmp / "strategist_policy.json"

    old_ts = "2000-01-01T00:00:00+00:00"  # ancient so weak posts are old enough to be judged 'dead'

    # 0) GRACEFUL WITH NO DATA: pick_winning before anything is learned = neutral pass-through.
    print("0) degrades gracefully with no learning yet:")
    fresh = pick_winning("topics", ["caching", "hype", "gates"])
    check("no-data ranks return, all neutral 1.0", len(fresh) == 3 and all(r["weight"] == 1.0 for r in fresh),
          str([r["weight"] for r in fresh]))
    check("no-data preserves input order (stable)", [r["value"] for r in fresh] == ["caching", "hype", "gates"])
    check("no-data flags nothing dead", not any(r["dead"] for r in fresh))
    check("best() on no data returns first candidate", best("topics", ["caching", "hype"]) == "caching")

    # 1) Feed SYNTHETIC X engagement: a clear winner topic/format vs a clear dead one.
    metrics = tmp / "data" / "hustle" / "case_study_metrics.jsonl"
    rows = []
    #   winners: topic=caching, format=deep_dive — high engagement (>=LAND_INTERACTIONS) & reach
    for i in range(3):
        rows.append({"id": f"win{i}", "text": f"win {i}", "views": 5000,
                     "likes": 40, "replies": 12, "retweets": 18})
    #   dead: topic=hype, format=hot_take — measured, ancient, ~zero engagement (>= min_n_dead=3)
    for i in range(3):
        rows.append({"id": f"dead{i}", "text": f"dead {i}", "views": 8,
                     "likes": 0, "replies": 0, "retweets": 0})
    metrics.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    for i in range(3):
        record_post("x_craft", "tool_result_cache", "caching", "deep_dive",
                    post_id=f"win{i}", posted_ts=old_ts)
    for i in range(3):
        record_post("x", "hype_take", "hype", "hot_take",
                    post_id=f"dead{i}", posted_ts=old_ts)

    # 2) SYNC: join metrics -> outcomes -> learned policy.
    res = sync(min_age_min=0)
    print(f"\n1) sync joined real engagement -> policy: {json.dumps(res)}")
    check("all six posts settled to a terminal verdict", res["settled"] == 6, str(res))
    check("policy learned along multiple facet keys", (res["policy_keys"] or 0) >= 4, str(res["policy_keys"]))

    # 3) THE CORE CLAIM: a high-engagement topic outweighs a dead one, and dead is flagged.
    print("\n2) learned policy favors what landed, skips what died:")
    tpk = pick_winning("topics", ["hype", "caching"])
    w = {r["value"]: r for r in tpk}
    check("winning topic weight > dead topic weight",
          w["caching"]["weight"] > w["hype"]["weight"],
          f"caching={w['caching']['weight']} vs hype={w['hype']['weight']}")
    check("winning topic weight > neutral 1.0", w["caching"]["weight"] > 1.0, str(w["caching"]["weight"]))
    check("dead topic weight < neutral 1.0", w["hype"]["weight"] < 1.0, str(w["hype"]["weight"]))
    check("dead topic flagged dead", w["hype"]["dead"] and not w["caching"]["dead"])
    check("winner ranks first", tpk[0]["value"] == "caching")

    # 4) same signal on the FORMAT dimension (independent facet).
    fpk = pick_winning("formats", ["hot_take", "deep_dive"])
    wf = {r["value"]: r for r in fpk}
    check("winning format outweighs dead format",
          wf["deep_dive"]["weight"] > wf["hot_take"]["weight"] and wf["hot_take"]["dead"],
          f"deep_dive={wf['deep_dive']['weight']} vs hot_take={wf['hot_take']['weight']}")

    # 5) alive()/best() actually skip the dead combo.
    check("alive() drops the dead topic", alive("topics", ["hype", "caching"]) == ["caching"])
    check("best() picks the proven topic", best("topics", ["hype", "caching"]) == "caching")

    # 6) unknown candidate stays neutral even after learning (no false-dead), and a channel with NO
    #    metric file at all degrades to neutral rather than erroring.
    mixed = pick_winning("topics", ["caching", "brand_new_topic"])
    wm = {r["value"]: r for r in mixed}
    check("unseen topic stays neutral 1.0 post-learning", wm["brand_new_topic"]["weight"] == 1.0
          and not wm["brand_new_topic"]["dead"])
    ch = pick_winning("channels", ["x", "tiktok"])  # tiktok has no metrics file -> neutral, no crash
    wc = {r["value"]: r for r in ch}
    check("channel with no metrics file degrades to neutral", wc["tiktok"]["weight"] == 1.0
          and not wc["tiktok"]["dead"])

    # 7) idempotency: re-recording + re-syncing the same posts changes nothing.
    dup = record_post("x_craft", "tool_result_cache", "caching", "deep_dive",
                      post_id="win0", posted_ts=old_ts)
    check("re-record is deduped", dup["deduped"] is True)
    res2 = sync(min_age_min=0)
    check("re-sync settles nothing new (idempotent)", res2["settled"] == 0, str(res2))
    tpk2 = pick_winning("topics", ["hype", "caching"])
    w2 = {r["value"]: r for r in tpk2}
    check("weights stable across re-sync",
          w2["caching"]["weight"] == w["caching"]["weight"] and w2["hype"]["weight"] == w["hype"]["weight"])

    # 8) summary is well-formed.
    s = summary()
    check("summary reports per-kind learned rows",
          bool(s.get("by_kind", {}).get("topics")) and bool(s["by_kind"].get("formats")))

    print()
    if fails:
        print(f"SELFTEST FAILED ({len(fails)}): {fails}")
        return 1
    print("SELFTEST PASSED — the content loop degrades gracefully with no data, joins REAL per-post "
          "engagement into strategist_memory's policy across channel/topic/format facets, ranks a "
          "proven-winning topic+format above a proven-dead one, skips the dead combos, stays neutral "
          "on unseen candidates + metric-less channels, and is idempotent across re-syncs. $0.")
    return 0


# ─────────────────────────── CLI ───────────────────────────
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Content learn loop — makes the authority engine smarter ($0)")
    ap.add_argument("--selftest", action="store_true", help="prove the loop end-to-end (isolated, $0)")
    ap.add_argument("--sync", action="store_true", help="join live metrics -> outcomes -> learned policy")
    ap.add_argument("--summary", action="store_true", help="print what the engine has learned")
    ap.add_argument("--picks", nargs="+", metavar=("KIND", "CAND"),
                    help="rank candidates: --picks topics a b c")
    ap.add_argument("--min-age", type=int, default=MIN_AGE_MIN, help="min minutes before a weak post is 'dead'")
    args = ap.parse_args(argv)

    if args.selftest:
        return _selftest()
    if args.sync:
        print(json.dumps(sync(min_age_min=args.min_age), indent=2))
        return 0
    if args.summary:
        print(json.dumps(summary(), indent=2))
        return 0
    if args.picks:
        kind, cands = args.picks[0], args.picks[1:]
        print(json.dumps(pick_winning(kind, cands), indent=2))
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
