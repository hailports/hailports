#!/usr/bin/env python3
"""social_governor — the anti-ban brain in front of every PUBLIC social action.

Analogous to core.outreach_governor (which guards the cold EMAIL lane), but for
SOCIAL actions across the faceless @hailports surfaces (X / TikTok / YouTube /
Reddit). One stable API every poster + engager + the orchestrator consults BEFORE
acting, so the aggregate footprint can be near-constant through waking hours while
no single platform/action-type ever exceeds a human-safe rate.

WHY rolling windows (not just per-run caps): the existing engage agent has good
per-RUN caps, but two producers (the hourly job + the presence orchestrator) firing
the same X session can stack into a burst. This governor is the SHARED ceiling: a
per-platform, per-ACTION-TYPE rolling window (per-hour AND per-day) plus a jittered
min-gap, so the combined output of every caller stays under the envelope.

Four levers, one API:
  1. Rolling-window budgets — per (platform, action): a per-DAY cap and a per-HOUR
     cap, counted over real 24h / 1h sliding windows (not calendar buckets).
  2. Human jitter — a min-gap between same-type actions, JITTERED ±30-40% off a
     time-seeded hash so the cadence never looks robotic; quiet-hours that are
     "mostly off overnight" but allow the occasional low-risk off-hours action.
  3. Warm-up ramps — per-platform account-age scaling. A fresh/reactivated account
     (TikTok just un-dormanted) starts at a fraction of its caps and ramps to full
     over ~1-2 weeks. Aged accounts (X) run at full.
  4. Per-target cooldowns — e.g. per-subreddit on Reddit (fast bans) so we never
     hit one community twice inside a long cooldown.

CONTRACT:
  * can_act FAILS CLOSED when it has POSITIVE evidence of over-budget (a real
    rolling-window breach, a min-gap violation, a quiet-hour suppression, an unknown
    action, or the global pause kill-switch). Over-budget => skip, never queue.
  * It FAILS OPEN only when state is genuinely empty/unreadable (no events yet =
    nothing recent = safe to allow one). It can only ever TIGHTEN: each caller's own
    local caps stay the hard floor; the governor denies earlier, never permits more.
  * record_act is best-effort and never raises into a live action loop.

API (stable):
  can_act(platform, action, *, target=None) -> Decision
  record_act(platform, action, *, target=None) -> None
  status(platform=None) -> dict          # budgets + live usage (for the orchestrator + CLI)
  headroom(platform, action) -> float    # 0..1 fraction of the DAILY cap still free

State persists in data/hustle/social_governor_state.json (atomic write; pruned to
the last ~26h of events). Global pause: touch data/hustle/SOCIAL_GOVERNOR_PAUSE.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, date
from pathlib import Path

ROOT = Path(os.environ.get("CLAUDE_STACK_DIR", Path(__file__).resolve().parent.parent))
STATE_FILE = ROOT / "data" / "hustle" / "social_governor_state.json"
PAUSE_FLAG = ROOT / "data" / "hustle" / "SOCIAL_GOVERNOR_PAUSE"

# ── budgets ────────────────────────────────────────────────────────────────────
# Per (platform, action): day = rolling-24h cap, hour = rolling-1h cap, gap = min
# minutes between two of this action (jittered at check time). These are the
# human-safe ENVELOPE; the warm-up factor only ever scales them DOWN for a young
# account, never up. Tunable via SOCIAL_GOV_<PLATFORM>_<ACTION>_<DAY|HOUR|GAP>.
_BUDGETS: dict[str, dict[str, dict[str, float]]] = {
    # X — faceless free account. Posting can't be "always on" (a free acct that
    # posts 30x/day reads as a bot); presence comes from ENGAGEMENT, which tolerates
    # far higher volume because it surfaces via OTHERS' notifications.
    # likes carry no text + are how humans naturally burst while scrolling, so they get
    # NO min-gap — the hourly/daily rolling caps are their real ceiling. Higher-risk text
    # actions (reply/quote) + network actions (follow) keep meaningful jittered gaps.
    "x": {
        "post":   {"day": 10, "hour": 2,  "gap": 30},
        # CLEARED + VERIFIED (2026-06-29): X reviewed @hailports, found NO spam/manipulation,
        # removed the temporary reach-label, and the account is now blue-check verified. Relaxed
        # OUT of recovery mode to a healthy verified-account cadence — likes/replies/quotes were
        # never the problem and verification buys reach latitude. FOLLOW stays conservative on
        # purpose: "automated following" was the exact flagged behavior, so it remains curated-only
        # (see _should_follow) + slow-gapped regardless of verification. The warm-up ramp (below)
        # still holds everything a bit lower for ~2 weeks as the account settles post-flag.
        # aggressive-but-safe (2026-07-06): likes lead (no text = lowest risk), replies moderate.
        # FOLLOW deliberately UNCHANGED — automated-follow was THE spam-flag trigger.
        "reply":  {"day": 55, "hour": 8,  "gap": 5},
        "like":   {"day": 80, "hour": 16, "gap": 0},
        "follow": {"day": 8,  "hour": 2,  "gap": 3600},   # HELD: curated targets only (the flag trigger)
        "quote":  {"day": 8,  "hour": 2,  "gap": 25},
    },
    # TikTok — VIDEO platform; far more post-tolerant than X text, so the ban-safe ceiling is
    # raised to churn shorts hard (2026-06-28). It JUST reactivated after ~2mo dormant, so the
    # warm-up ramp (below) is the REAL near-term limiter — it holds the effective cap to ~half the
    # envelope for the first ~2 weeks, then opens to the full safe-max as the account ages.
    "tiktok": {
        # INSANE-but-safe bump (2026-06-29): envelope raised hard to churn shorts; the WARM-UP RAMP
        # (floor 0.34, started 06-26) is what keeps it ban-safe — it climbs INTO this ceiling over
        # ~2 weeks instead of spiking a just-reactivated account. Effective today ≈ 0.5×15 ≈ 7/day,
        # ramping to the full 15/day. gap 45m + hour cap 3 keep bursts human; content variety
        # (edu/spectacle/receipts/CTA mix) is the other ban-guard (TikTok bans duplicate spam).
        # FULL ENGAGEMENT envelope (2026-06-29): likes lead (lowest risk), comments strong, posts hard;
        # FOLLOW is the one conservative lever — automated follow-churn is the #1 TikTok flag trigger
        # (same as X), so it's curated-target only + a long gap, NOT a fast spray. The warm-up ramp
        # below still scales the whole envelope up over ~2 weeks.
        "post":    {"day": 15, "hour": 3, "gap": 45},
        "like":    {"day": 110, "hour": 18, "gap": 0},
        "comment": {"day": 45, "hour": 8, "gap": 8},
        "follow":  {"day": 6,  "hour": 2,  "gap": 1800},
        "dm":      {"day": 12, "hour": 3,  "gap": 180},   # warm (own followers) but DM=top ban trigger -> capped + gapped
    },
    # YouTube — SHORT (upload) is the most post-tolerant surface (aged faceless channel safely
    # publishes ~a dozen/day). ENGAGEMENT (comment/like) is a DIFFERENT risk profile: YouTube polices
    # comment-spam HARD and worse on a channel that has never engaged before, and comments are the #1
    # engage-side ban trigger — so the comment envelope is deliberately CONSERVATIVE and new-engager-
    # safe (2026-07-03): day 6, hour 2, min-gap 15m keeps it well under the spam line while still being
    # the reach lever. The agent (hailports_youtube_engagement.py) layers its OWN account-age warm-up
    # (caps_for_age, comment floor 2/day) on top so first-time commenting ramps from a low floor. LIKE
    # carries no authored text (lowest risk) so it runs heavier; SUBSCRIBE is a curated-seed trickle.
    "youtube": {
        "short":     {"day": 12, "hour": 3, "gap": 25},
        "comment":   {"day": 6,  "hour": 2, "gap": 15},
        "like":      {"day": 45, "hour": 10, "gap": 0},
        "subscribe": {"day": 4,  "hour": 1, "gap": 120},
    },
    # Reddit — fast, unforgiving bans. Value-first: comment-mostly, posts rare,
    # plus a hard per-subreddit cooldown (target=subreddit).
    "reddit": {
        "comment": {"day": 6,  "hour": 2, "gap": 25},
        "post":    {"day": 1,  "hour": 1, "gap": 240},
        "vote":    {"day": 20, "hour": 8, "gap": 0},
    },
    # LinkedIn — ONE budget shared by TWO lanes on the SAME real-name account:
    #   agents/linkedin_lane.py        (real-name authority: like/comment/invite/repost/post)
    #   agents/hailports_linkedin.py   (faceless company page: page_* actions)
    # LinkedIn's enforcement counts ACTIONS PER ACCOUNT, not per lane. A ban takes out
    # both the authority engine AND the page it admins, so ACCOUNT_DAY_CAP (below) is the
    # real ceiling — these per-action budgets only shape the mix under it.
    "linkedin": {
        # real-name lane
        "like":         {"day": 25, "hour": 6, "gap": 0},
        "comment":      {"day": 5,  "hour": 2, "gap": 30},
        "invite":       {"day": 15, "hour": 3, "gap": 300},
        "repost":       {"day": 2,  "hour": 1, "gap": 240},
        "post":         {"day": 1,  "hour": 1, "gap": 720},
        # faceless company-page lane (acting AS the page)
        "page_post":    {"day": 1,  "hour": 1, "gap": 240},
        "page_comment": {"day": 8,  "hour": 2, "gap": 20},
        "page_react":   {"day": 25, "hour": 6, "gap": 0},
        "page_follow":  {"day": 5,  "hour": 2, "gap": 900},
    },
}

# Account-level 24h ceiling across ALL actions on a platform. Community-observed LinkedIn
# enforcement threshold is ~150 total actions/account/24h drawn from a SHARED budget; we sit
# well under it. None => no account-level ceiling (per-action budgets are the only limit).
ACCOUNT_DAY_CAP: dict[str, int] = {
    "linkedin": int(os.environ.get("SOCIAL_GOV_LINKEDIN_ACCOUNT_DAY", "100")),
}

# low-risk actions that MAY occasionally fire during quiet hours (carry no authored
# text / minimal footprint) — so overnight isn't robotically perfect-silent.
_LOW_RISK = {
    "x": {"like"},
    "tiktok": {"like"},
    "youtube": {"like"},
    "reddit": {"vote"},
    "linkedin": {"like", "page_react"},
}

# per-platform quiet hours [start, end) in LOCAL time + the small probability a
# low-risk action slips through overnight (time-seeded, so it varies but is bounded).
_QUIET = {   # quiet hours DISABLED per owner 2026-07-03 (24/7). start==end => _in_quiet_hours() always False
    "x":       {"start": 0, "end": 0, "offhours_prob": 1.0},
    "tiktok":  {"start": 0, "end": 0, "offhours_prob": 1.0},
    "youtube": {"start": 0, "end": 0, "offhours_prob": 1.0},
    "reddit":  {"start": 0, "end": 0, "offhours_prob": 1.0},
    # LinkedIn keeps real quiet hours: it runs on the owner's REAL-NAME account, and a
    # professional account acting at 03:00 local reads as a bot to both LinkedIn and humans.
    "linkedin": {"start": 22, "end": 7, "offhours_prob": 0.05},
}

# warm-up ramp per platform: caps scale by factor in [floor .. 1.0], reaching 1.0
# `days` after `start`. start dates are env-tunable (a reactivation resets the ramp).
_WARMUP = {
    "x":       {"start": os.environ.get("SOCIAL_GOV_X_START", "2026-06-29"),       "days": 14, "floor": 0.55},
    "tiktok":  {"start": os.environ.get("SOCIAL_GOV_TIKTOK_START", "2026-06-26"),  "days": 14, "floor": 0.70},  # raised 0.34->0.70: established MONETIZED acct (not fresh) can run near ceiling
    "youtube": {"start": os.environ.get("SOCIAL_GOV_YT_START", "2026-05-01"),      "days": 14, "floor": 0.50},
    "reddit":  {"start": os.environ.get("SOCIAL_GOV_REDDIT_START", "2026-06-25"),  "days": 21, "floor": 0.30},
    # brand-new company page + a real-name account that has never been automated at volume:
    # longest ramp, lowest floor in the stack. LinkedIn bans harder than it warns.
    "linkedin": {"start": os.environ.get("SOCIAL_GOV_LINKEDIN_START", "2026-07-10"), "days": 21, "floor": 0.25},
}

# per-target cooldown (seconds) — e.g. don't comment in the same subreddit within 6h.
PER_TARGET_COOLDOWN = {
    ("reddit", "comment"): int(os.environ.get("SOCIAL_GOV_REDDIT_SUB_COOLDOWN", str(6 * 3600))),
    ("reddit", "post"):    int(os.environ.get("SOCIAL_GOV_REDDIT_POST_COOLDOWN", str(48 * 3600))),
}

_PRUNE_SEC = 26 * 3600  # keep a touch over 24h so the rolling-day window is always complete


@dataclass
class Decision:
    allow: bool
    reason: str
    platform: str = ""
    action: str = ""
    used_hour: int = 0
    used_day: int = 0
    cap_hour: int = 0
    cap_day: int = 0
    retry_after_sec: int = 0   # hint for the orchestrator (when a window/min-gap clears)
    warmup_factor: float = 1.0


# ── time + env helpers (overridable for the selftest / simulator) ───────────────

_NOW_OVERRIDE: datetime | None = None


def _now() -> datetime:
    return _NOW_OVERRIDE if _NOW_OVERRIDE is not None else datetime.now(timezone.utc)


def _local_hour() -> int:
    # local wall-clock hour drives quiet-hours (the override carries its own tz/offset)
    n = _NOW_OVERRIDE if _NOW_OVERRIDE is not None else datetime.now()
    return n.hour


def _budget(platform: str, action: str) -> dict | None:
    b = (_BUDGETS.get(platform) or {}).get(action)
    if not b:
        return None
    # env overrides (rarely needed; defaults are the policy)
    out = dict(b)
    for k in ("day", "hour", "gap"):
        ev = os.environ.get(f"SOCIAL_GOV_{platform.upper()}_{action.upper()}_{k.upper()}")
        if ev:
            try:
                out[k] = float(ev)
            except Exception:
                pass
    return out


def _warmup_factor(platform: str) -> float:
    w = _WARMUP.get(platform)
    if not w:
        return 1.0
    try:
        start = date.fromisoformat(str(w["start"]))
        today = (_NOW_OVERRIDE.date() if _NOW_OVERRIDE is not None else date.today())
        days_active = max(0, (today - start).days)
        span = max(1, int(w["days"]))
        floor = float(w["floor"])
        return max(floor, min(1.0, floor + (1.0 - floor) * (days_active / span)))
    except Exception:
        return 1.0


def _eff_caps(platform: str, action: str, b: dict) -> tuple[int, int]:
    f = _warmup_factor(platform)
    # ceil so a ramped account still gets at least 1 of an action whose envelope is >0
    return max(1, math.ceil(b["day"] * f)), max(1, math.ceil(b["hour"] * f))


# ── deterministic time-seeded jitter (Math.random-free; reproducible per seed) ──

def _seed_unit(*parts) -> float:
    """A stable float in [0,1) from a hash of the parts. Same inputs => same value,
    so a given min-gap (seeded on the last-action epoch) is consistent within a check
    but varies across actions/platforms/times — human-looking, not robotic."""
    h = hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()
    return int(h[:12], 16) / float(0x1000000000000)


def _jittered_gap_sec(platform: str, action: str, base_min: float, last_epoch: float) -> float:
    # ±35% jitter seeded on the prior action's timestamp -> stable for this gap
    j = 0.65 + 0.70 * _seed_unit(platform, action, int(last_epoch))
    return base_min * 60.0 * j


def _offhours_allowed(platform: str, action: str) -> bool:
    q = _QUIET.get(platform) or {}
    prob = float(q.get("offhours_prob", 0.0))
    if prob <= 0 or action not in _LOW_RISK.get(platform, set()):
        return False
    # one decision per (hour-bucket, platform, action) -> "occasional", not flickering
    bucket = (_NOW_OVERRIDE or datetime.now()).strftime("%Y%m%d%H")
    return _seed_unit("offhours", platform, action, bucket) < prob


def _in_quiet_hours(platform: str) -> bool:
    q = _QUIET.get(platform)
    if not q:
        return False
    h = _local_hour()
    s, e = int(q["start"]), int(q["end"])
    return (s <= h < e) if s <= e else (h >= s or h < e)


def _secs_to_quiet_end(platform: str) -> int:
    q = _QUIET.get(platform) or {"end": 7}
    h = _local_hour()
    e = int(q["end"])
    return max(60, ((e - h) % 24) * 3600)


# ── state (rolling event timestamps per key; atomic; pruned) ────────────────────

_STATE: dict | None = None


def _load_state() -> dict:
    global _STATE
    if _STATE is not None:
        return _STATE
    st = {"events": {}, "updated": None}
    try:
        if STATE_FILE.exists():
            loaded = json.loads(STATE_FILE.read_text())
            if isinstance(loaded, dict) and isinstance(loaded.get("events"), dict):
                st = loaded
                st.setdefault("events", {})
    except Exception:
        pass  # corrupt => start clean (empty = nothing recent = safe to allow one)
    _STATE = st
    return st


def _save_state(st: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        st["updated"] = _now().isoformat()
        tmp = STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(st, indent=2, sort_keys=True))
        os.replace(tmp, STATE_FILE)
    except Exception:
        pass  # never raise into a live action loop


def _key(platform: str, action: str, target: str | None = None) -> str:
    k = f"{platform}:{action}"
    return f"{k}:{target.strip().lower()}" if target else k


def account_day_total(platform: str, st: dict | None = None) -> int:
    """Total actions recorded on `platform` in the rolling 24h, across every action AND
    every lane sharing the account. Counts only un-targeted keys (`platform:action`);
    record_act() writes a second `platform:action:target` key that would double-count."""
    try:
        st = st if st is not None else _load_state()
        now = _now().timestamp()
        total = 0
        for key, raw in (st.get("events") or {}).items():
            if key.count(":") != 1 or not key.startswith(f"{platform}:"):
                continue
            for v in raw or []:
                try:
                    if (now - float(v)) <= 86400:
                        total += 1
                except Exception:
                    continue
        return total
    except Exception:
        return 0


def _events(st: dict, key: str) -> list[float]:
    """Epoch-second timestamps for a key, parsed + pruned to the rolling window."""
    raw = st["events"].get(key) or []
    now = _now().timestamp()
    out = []
    for v in raw:
        try:
            out.append(float(v))
        except Exception:
            continue
    return [t for t in out if (now - t) <= _PRUNE_SEC]


def _counts(events: list[float]) -> tuple[int, int, float | None]:
    now = _now().timestamp()
    h1 = sum(1 for t in events if (now - t) <= 3600)
    h24 = sum(1 for t in events if (now - t) <= 86400)
    last = max(events) if events else None
    return h1, h24, last


# ── public API ──────────────────────────────────────────────────────────────

def can_act(platform: str, action: str, *, target: str | None = None) -> Decision:
    """Gate ONE social action. Fails CLOSED on positive over-budget evidence;
    fails OPEN only when there's no recent history to judge against."""
    platform = (platform or "").strip().lower()
    action = (action or "").strip().lower()

    if PAUSE_FLAG.exists():
        return Decision(False, "global_pause:SOCIAL_GOVERNOR_PAUSE", platform, action)

    b = _budget(platform, action)
    if not b:
        # unknown action => fail CLOSED (never permit an unbudgeted social action)
        return Decision(False, f"unknown_action:{platform}:{action}", platform, action)

    cap_day, cap_hour = _eff_caps(platform, action, b)
    wf = round(_warmup_factor(platform), 3)

    # quiet hours — suppress unless a seeded off-hours allowance lets a low-risk action through
    if _in_quiet_hours(platform) and not _offhours_allowed(platform, action):
        return Decision(False, f"quiet_hours:{platform}", platform, action,
                        cap_hour=cap_hour, cap_day=cap_day,
                        retry_after_sec=_secs_to_quiet_end(platform), warmup_factor=wf)

    try:
        st = _load_state()
        ev = _events(st, _key(platform, action))
        h1, h24, last = _counts(ev)
    except Exception:
        # state unreadable => no evidence => fail OPEN (allow one); local caps still bind
        return Decision(True, "fail_open:no_state", platform, action,
                        cap_hour=cap_hour, cap_day=cap_day, warmup_factor=wf)

    now = _now().timestamp()

    acct_cap = ACCOUNT_DAY_CAP.get(platform)
    if acct_cap:
        acct_used = account_day_total(platform, st)
        if acct_used >= acct_cap:
            return Decision(False, f"account_day_cap {acct_used}/{acct_cap}", platform, action,
                            used_hour=h1, used_day=h24, cap_hour=cap_hour, cap_day=cap_day,
                            retry_after_sec=3600, warmup_factor=wf)

    if h24 >= cap_day:
        oldest_in_day = min(t for t in ev if (now - t) <= 86400)
        return Decision(False, f"day_cap {h24}/{cap_day}", platform, action,
                        used_hour=h1, used_day=h24, cap_hour=cap_hour, cap_day=cap_day,
                        retry_after_sec=int(max(60, 86400 - (now - oldest_in_day))), warmup_factor=wf)

    if h1 >= cap_hour:
        oldest_in_hr = min(t for t in ev if (now - t) <= 3600)
        return Decision(False, f"hour_cap {h1}/{cap_hour}", platform, action,
                        used_hour=h1, used_day=h24, cap_hour=cap_hour, cap_day=cap_day,
                        retry_after_sec=int(max(30, 3600 - (now - oldest_in_hr))), warmup_factor=wf)

    # jittered min-gap between same-type actions (anti-burst, human cadence)
    if last is not None:
        need = _jittered_gap_sec(platform, action, b["gap"], last)
        waited = now - last
        if waited < need:
            return Decision(False, f"min_gap {int(waited)}s<{int(need)}s", platform, action,
                            used_hour=h1, used_day=h24, cap_hour=cap_hour, cap_day=cap_day,
                            retry_after_sec=int(need - waited), warmup_factor=wf)

    # per-target cooldown (e.g. same subreddit) — only on positive evidence
    cd = PER_TARGET_COOLDOWN.get((platform, action))
    if cd and target:
        tev = _events(st, _key(platform, action, target))
        if tev:
            tlast = max(tev)
            if (now - tlast) < cd:
                return Decision(False, f"target_cooldown {int(now - tlast)}s<{cd}s ({target})",
                                platform, action, used_hour=h1, used_day=h24,
                                cap_hour=cap_hour, cap_day=cap_day,
                                retry_after_sec=int(cd - (now - tlast)), warmup_factor=wf)

    return Decision(True, "ok", platform, action, used_hour=h1, used_day=h24,
                    cap_hour=cap_hour, cap_day=cap_day, warmup_factor=wf)


def record_act(platform: str, action: str, *, target: str | None = None) -> None:
    """Record a real action so the rolling windows + min-gap + cooldowns advance.
    Best-effort; never raises."""
    try:
        platform = (platform or "").strip().lower()
        action = (action or "").strip().lower()
        st = _load_state()
        now = _now().timestamp()
        for key in filter(None, (_key(platform, action), _key(platform, action, target) if target else None)):
            lst = [t for t in (st["events"].get(key) or []) if _is_recent(t, now)]
            lst.append(now)
            st["events"][key] = lst[-200:]
        _save_state(st)
    except Exception:
        pass


def _is_recent(t, now: float) -> bool:
    try:
        return (now - float(t)) <= _PRUNE_SEC
    except Exception:
        return False


def headroom(platform: str, action: str) -> float:
    """Fraction of the DAILY cap still free (0..1). The orchestrator weights toward
    lanes with headroom and backs off when this nears 0."""
    try:
        b = _budget(platform, action)
        if not b:
            return 0.0
        cap_day, _ = _eff_caps(platform, action, b)
        _, h24, _ = _counts(_events(_load_state(), _key(platform, action)))
        return max(0.0, min(1.0, (cap_day - h24) / cap_day)) if cap_day else 0.0
    except Exception:
        return 1.0


def status(platform: str | None = None) -> dict:
    """Live budget snapshot for the orchestrator + CLI (read-only, $0)."""
    out: dict = {"now": _now().isoformat(), "local_hour": _local_hour(),
                 "paused": PAUSE_FLAG.exists(), "platforms": {}}
    st = _load_state()
    plats = [platform] if platform else list(_BUDGETS.keys())
    for p in plats:
        if p not in _BUDGETS:
            continue
        wf = round(_warmup_factor(p), 3)
        pinfo = {"warmup_factor": wf, "quiet_now": _in_quiet_hours(p), "actions": {}}
        for a, b in _BUDGETS[p].items():
            be = _budget(p, a)
            cap_day, cap_hour = _eff_caps(p, a, be)
            h1, h24, last = _counts(_events(st, _key(p, a)))
            pinfo["actions"][a] = {
                "used_hour": h1, "cap_hour": cap_hour,
                "used_day": h24, "cap_day": cap_day,
                "headroom_day": round((cap_day - h24) / cap_day, 2) if cap_day else 0.0,
                "min_gap_min": be["gap"],
                "last_ago_min": round((_now().timestamp() - last) / 60, 1) if last else None,
            }
        out["platforms"][p] = pinfo
    return out


# ── selftest / CLI ────────────────────────────────────────────────────────────

def _selftest() -> int:
    global _STATE, _NOW_OVERRIDE
    import tempfile
    failures: list[str] = []
    orig_state_file = STATE_FILE

    def reset(at: datetime):
        global _STATE, _NOW_OVERRIDE
        _STATE = {"events": {}, "updated": None}
        _NOW_OVERRIDE = at

    # midday, fixed clock so quiet-hours don't interfere with the budget checks
    noon = datetime(2026, 6, 27, 13, 0, 0, tzinfo=timezone.utc)
    # Hermetic config: pin known test values so the selftest exercises the LOGIC
    # deterministically, independent of the live hand-tuned budgets/ramps/quiet-hours
    # (which drift as accounts age, get flagged, or move to 24/7). Restored in `finally`.
    orig_budgets = {p: {a: dict(b) for a, b in acts.items()} for p, acts in _BUDGETS.items()}
    orig_quiet = {k: dict(v) for k, v in _QUIET.items()}
    orig_warmup = {k: dict(v) for k, v in _WARMUP.items()}
    try:
        globals()["STATE_FILE"] = Path(tempfile.mkdtemp()) / "social_gov_state.json"
        for p in _WARMUP:                       # full ramp so caps sit at their envelope
            _WARMUP[p] = {"start": "2000-01-01", "days": 1, "floor": 1.0}
        for p in _QUIET:                        # no quiet-hour interference at midday
            _QUIET[p] = {"start": 0, "end": 0, "offhours_prob": 1.0}
        _BUDGETS["x"]["follow"]["gap"] = 3      # small gap so the day-cap test fits in 24h

        # 1. in-budget allow on a clean slate
        reset(noon)
        d = can_act("x", "like")
        if not d.allow:
            failures.append(f"clean-slate like denied: {d.reason}")

        # 2. min-gap fails closed right after an action; allows once jittered gap passes
        reset(noon)
        record_act("x", "reply")
        _NOW_OVERRIDE = noon + timedelta(seconds=20)
        if can_act("x", "reply").allow:
            failures.append("min-gap did not fail closed immediately after a reply")
        _NOW_OVERRIDE = noon + timedelta(minutes=30)  # well past the jittered reply gap
        if not can_act("x", "reply").allow:
            failures.append("min-gap never cleared after ample time")

        # 3. rolling per-HOUR cap fails closed (spread past min-gap so only the hour cap binds)
        reset(noon)
        cap_hour = _eff_caps("x", "like", _budget("x", "like"))[1]
        for i in range(cap_hour):
            _NOW_OVERRIDE = noon + timedelta(minutes=2 * i)
            if not can_act("x", "like").allow:
                failures.append(f"like #{i} blocked before hitting the hour cap")
            record_act("x", "like")
        _NOW_OVERRIDE = noon + timedelta(minutes=2 * cap_hour)
        over = can_act("x", "like")
        if over.allow or "hour_cap" not in over.reason:
            failures.append(f"hour cap did not fail closed: {over.reason}")

        # 4. rolling per-DAY cap fails closed; an event aging out past 24h frees a slot.
        #    space wide enough that ONLY the day cap binds (never the per-hour cap).
        reset(noon)
        cap_day, cap_hour_f = _eff_caps("x", "follow", _budget("x", "follow"))
        gap_min = _budget("x", "follow")["gap"]
        step = max(gap_min + 3, math.ceil(60 / cap_hour_f) + 2)  # < cap_hour per rolling hour
        for i in range(cap_day):
            _NOW_OVERRIDE = noon + timedelta(minutes=step * i)
            d = can_act("x", "follow")
            if not d.allow:
                failures.append(f"follow #{i} blocked before the day cap (reason {d.reason})")
            record_act("x", "follow")
        last_t = noon + timedelta(minutes=step * (cap_day - 1))
        _NOW_OVERRIDE = last_t + timedelta(minutes=gap_min + 5)
        dcap = can_act("x", "follow")
        if dcap.allow or "day_cap" not in dcap.reason:
            failures.append(f"day cap did not fail closed: {dcap.reason}")
        # jump >24h past the FIRST follow so it ages out -> a daily slot frees
        _NOW_OVERRIDE = noon + timedelta(hours=24, minutes=10)
        if not can_act("x", "follow").allow:
            failures.append("daily slot did not free after the oldest event aged out of 24h")

        # 5. quiet hours suppress; a non-low-risk action is never allowed off-hours.
        #    Pin a real quiet window for the test (the shipped default may run 24/7,
        #    which is a tuning choice, not the behavior under test here).
        _QUIET["x"] = {"start": 22, "end": 7, "offhours_prob": 0.05}
        reset(datetime(2026, 6, 27, 3, 0, 0))  # 3am local (naive override -> .hour=3)
        if can_act("x", "post").allow:
            failures.append("quiet hours did not suppress a post at 3am")
        # a low-risk like is *sometimes* allowed off-hours but must be governed by the seeded prob
        allowed_nights = 0
        for h in range(0, 7):
            _NOW_OVERRIDE = datetime(2026, 6, 27, h, 0, 0)
            _STATE = {"events": {}, "updated": None}
            if can_act("x", "like").allow:
                allowed_nights += 1
        if allowed_nights >= 6:
            failures.append(f"off-hours allowance not rare enough ({allowed_nights}/7 night-hours open)")

        # 6. warm-up ramp: a freshly-reactivated platform caps below envelope; aged = full.
        #    Pin the ramp windows for the test (live start-dates drift as accounts age).
        _WARMUP["tiktok"] = {"start": "2026-06-26", "days": 14, "floor": 0.34}  # fresh: ~day 1
        _WARMUP["x"] = {"start": "2026-06-01", "days": 14, "floor": 0.55}       # aged: fully ramped
        reset(noon)
        f_tiktok = _warmup_factor("tiktok")   # start 2026-06-26, today 2026-06-27 => ~day 1 => near floor
        f_x = _warmup_factor("x")             # start 2026-06-01 => fully ramped => 1.0
        if not (0.30 <= f_tiktok < 0.6):
            failures.append(f"tiktok warm-up factor off ramp: {f_tiktok}")
        if f_x <= f_tiktok:
            failures.append(f"aged X not ramped above fresh TikTok: x={f_x} tiktok={f_tiktok}")
        tt_day = _eff_caps("tiktok", "post", _budget("tiktok", "post"))[0]
        if tt_day >= _BUDGETS["tiktok"]["post"]["day"]:
            failures.append(f"tiktok post cap not reduced by warm-up: {tt_day}")

        # 7. per-subreddit cooldown: same sub fails closed, a different sub is fine.
        #    advance 90min so the first comment leaves the rolling-HOUR window (reddit's
        #    ramped hour cap is tiny) but stays inside the 6h per-sub cooldown.
        reset(noon)
        record_act("reddit", "comment", target="r/saas")
        _NOW_OVERRIDE = noon + timedelta(minutes=90)
        if can_act("reddit", "comment", target="r/saas").allow:
            failures.append("reddit per-subreddit cooldown did not fail closed on the same sub")
        if not can_act("reddit", "comment", target="r/selfhosted").allow:
            failures.append("reddit cooldown wrongly blocked a different subreddit")

        # 8. unknown action + global pause both fail closed
        reset(noon)
        if can_act("x", "telepathy").allow:
            failures.append("unknown action did not fail closed")
        orig_pause = globals()["PAUSE_FLAG"]
        try:
            pf = Path(tempfile.mkdtemp()) / "PAUSE"
            pf.write_text("x")
            globals()["PAUSE_FLAG"] = pf
            if can_act("x", "like").allow:
                failures.append("global pause kill-switch did not fail closed")
        finally:
            globals()["PAUSE_FLAG"] = orig_pause
    finally:
        globals()["STATE_FILE"] = orig_state_file
        _BUDGETS.clear(); _BUDGETS.update(orig_budgets)
        _QUIET.clear(); _QUIET.update(orig_quiet)
        _WARMUP.clear(); _WARMUP.update(orig_warmup)
        _STATE = None
        _NOW_OVERRIDE = None

    if failures:
        print("SOCIAL_GOVERNOR SELFTEST FAILED:")
        for f in failures:
            print("  -", f)
        return 1
    print("SOCIAL_GOVERNOR SELFTEST PASSED (8 checks: in-budget allow, jittered min-gap, "
          "rolling hour cap, rolling day cap + age-out, quiet-hours + rare off-hours allowance, "
          "warm-up ramp, per-subreddit cooldown, unknown-action/global-pause fail-closed)")
    return 0


def _cli(argv=None) -> int:
    import sys
    argv = argv if argv is not None else sys.argv[1:]
    if "--selftest" in argv:
        return _selftest()
    if "--status" in argv:
        i = argv.index("--status")
        plat = argv[i + 1] if i + 1 < len(argv) and not argv[i + 1].startswith("-") else None
        print(json.dumps(status(plat), indent=2))
        return 0
    print(json.dumps(status(), indent=2))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_cli())
