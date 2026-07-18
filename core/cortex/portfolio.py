#!/usr/bin/env python3
"""portfolio.py — the PORTFOLIO CONTROLLER: reallocate effort across channels from measured outcome.

The parked strategist (core/revenue_strategist_loop.py) tunes copy/volume/harvest WITHIN a
channel; it has no notion of which whole CHANNEL deserves more effort. The revenue scoreboard
(data/hustle/revenue_scoreboard.jsonl) records real per-surface outcome every few minutes but is
read only by dashboards. This closes that gap: it folds the scoreboard window + the strategist
brief into one state, then emits an EFFORT POLICY — a weight in [0,1] and a grounded verdict per
channel — that a live loop reads via effort_for() to scale its own volume/frequency.

ANTI-THEATER: the whole point is effort_for(). A "reallocation report" nobody consumes is theater;
a sender loop multiplying its daily cap by effort_for('broken_site') is a live control signal.

GROUNDING (mirrors the strategist's discipline): every verdict cites a REAL number pulled from the
scoreboard/brief window. No fabricated justification. A channel with sustained-zero outcome over
the window is 'starve' or (over a long-enough span) 'kill-candidate' — NEVER auto-killed; kill is
owner-routed. BrandA / any named-or-identity lane is kept ENTIRELY out of the policy (lane
separation is absolute), so a produced policy is always reversible + no-spend + faceless-only, i.e.
apply_is_safe() is True by construction. apply_is_safe() rejects any policy that would kill, spend,
or touch a named lane.

KILL SWITCH: if data/hustle/CORTEX_OFF exists, reallocate() is a no-op that returns the prior policy.

Self-contained + fail-soft: only imports stdlib + (fail-soft) core.revenue_strategist_loop.build_brief;
no import from the rest of core/cortex. Any import/exception returns a safe default, never raises.

    python3 -m core.cortex.portfolio --selftest   # hermetic proof on temp copies, no side effects
    python3 -m core.cortex.portfolio              # print live portfolio state (read-only)
    python3 -m core.cortex.portfolio --dry        # print the policy reallocate() WOULD write (temp)
    python3 -m core.cortex.portfolio --write      # compute + write data/hustle/effort_policy.json
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
HUSTLE = ROOT / "data" / "hustle"

SCOREBOARD = HUSTLE / "revenue_scoreboard.jsonl"
EFFORT_POLICY = HUSTLE / "effort_policy.json"
CORTEX_OFF = HUSTLE / "CORTEX_OFF"

DEFAULT_WINDOW = 24            # scoreboard rows folded per reallocation
KILL_MIN_OBS = 6              # need >= this many all-zero observations to even consider kill-candidate
KILL_MIN_SPAN_H = 24         # ...AND the zero must persist at least this many wall-clock hours

SCALE_WEIGHT = 1.0
HOLD_ALIVE_WEIGHT = 0.6      # producing but not growing
HOLD_DROP_WEIGHT = 0.4       # produced earlier in window, currently zero (watch, don't starve yet)
STARVE_WEIGHT = 0.25         # sustained zero, short window
KILL_CAND_WEIGHT = 0.1       # sustained zero over a long span -> owner should decide (never auto-killed)

# Lanes carrying a real/named identity — mirror strategist_memory.NAMED_OR_IDENTITY_LANES. NEVER
# appear in a produced policy; apply_is_safe() rejects any policy touching one.
NAMED_OR_IDENTITY_LANES = {"BrandA", "branda", "Operator", "CompanyA", "named", "identity"}  # pii-allow: employer/identity lane-exclusion guardrail

# Effort surfaces sourced from the scoreboard (group, metric-key). All faceless. Terminal money
# metrics (stripe revenue) are context, not a throttleable surface, so they're not here.
SCOREBOARD_CHANNELS = {
    "intent_harvest":   ("leads", "intent_leads_new_24h"),
    "intent_outreach":  ("distribution", "intent_replies_24h"),
    "content_reels":    ("distribution", "reels_posted_24h"),
    "content_comments": ("distribution", "comments_posted_24h"),
    "persona3":         ("distribution", "persona3_posted_24h"),
    "self_serve":       ("funnel", "check_captures_24h"),
}
# Effort surfaces whose outcome number lives in the strategist brief (brief["funnel"][key]). Kept
# separate so build_brief() can fold the scoreboard-only signal WITHOUT recursing back through here.
BRIEF_CHANNELS = {
    "broken_site":      ("funnel", "broken_site_real_replies"),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _num(x):
    """Coerce to a clean number: int when whole (so a cited '226' matches state), else rounded."""
    try:
        f = float(x)
    except (TypeError, ValueError):
        return 0
    return int(f) if f == int(f) else round(f, 3)


def _parse_ts(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _tail_scoreboard(window: int) -> list:
    """Last `window` scoreboard rows, oldest-first. Fail-soft: [] on any error."""
    try:
        rows = []
        for line in SCOREBOARD.read_text(errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
        return rows[-window:]
    except Exception:
        return []


def _safe_brief() -> dict:
    """Fail-soft bind to the strategist brief (mirror of the _bind fallback pattern)."""
    try:
        from core.revenue_strategist_loop import build_brief
        b = build_brief()
        return b if isinstance(b, dict) else {}
    except Exception:
        return {}


def _span_hours(rows: list) -> int:
    ts = [_parse_ts(r.get("ts")) for r in rows if isinstance(r, dict)]
    ts = [t for t in ts if t]
    if len(ts) < 2:
        return 0
    return int(round((max(ts) - min(ts)).total_seconds() / 3600.0))


# ---------------------------------------------------------------------------
# Per-channel outcome tally + verdict. Every verdict cites the channel's own real numbers
# (latest/first/peak/obs) so it's grounded and units-safe (each channel judged vs itself).
# ---------------------------------------------------------------------------
def _tally(series: list, source: str, key: str, span_h: int) -> dict:
    series = [_num(v) for v in series]
    obs = len(series)
    peak = max(series) if series else 0
    latest = series[-1] if series else 0
    first = series[0] if series else 0
    nonzero = sum(1 for v in series if v > 0)

    if peak <= 0:
        long_enough = obs >= KILL_MIN_OBS and span_h >= KILL_MIN_SPAN_H
        if nonzero == 0 and long_enough:
            verdict, weight = "kill-candidate", KILL_CAND_WEIGHT
            rat = f"0 outcome across all {obs} observations spanning ~{span_h}h — sustained dead"
        else:
            verdict, weight = "starve", STARVE_WEIGHT
            rat = f"0 outcome across {obs} observation(s) in window"
    elif latest > first:
        verdict, weight = "scale", SCALE_WEIGHT
        rat = f"latest {latest} up from {first} (peak {peak}) over {obs} observations — growing"
    elif latest > 0:
        verdict, weight = "hold", HOLD_ALIVE_WEIGHT
        rat = f"latest {latest} vs first {first} (peak {peak}) over {obs} observations — producing, not growing"
    else:  # latest == 0 but peak > 0: produced earlier in the window
        verdict, weight = "hold", HOLD_DROP_WEIGHT
        rat = f"dropped to {latest} but peaked {peak} within {obs} observations — watch, don't starve yet"

    return {
        "verdict": verdict, "weight": weight, "rationale_core": rat,
        "metric": {"source": source, "key": key, "latest": latest, "first": first,
                   "peak": peak, "obs": obs, "nonzero_obs": nonzero, "series": series},
    }


def scoreboard_channel_signal(window: int = DEFAULT_WINDOW) -> dict:
    """Per-channel outcome tally over the scoreboard window ONLY (no brief). This is the signal
    core/revenue_strategist_loop.build_brief() folds in — kept brief-free to avoid recursion."""
    try:
        rows = _tail_scoreboard(window)
        span_h = _span_hours(rows)
        out = {}
        for ch, (group, key) in SCOREBOARD_CHANNELS.items():
            series = [(r.get(group, {}) or {}).get(key, 0) for r in rows if isinstance(r, dict)]
            out[ch] = _tally(series, f"scoreboard.{group}", key, span_h)
        return out
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# read_portfolio_state — fold scoreboard window + brief + per-channel tallies into one dict.
# ---------------------------------------------------------------------------
def read_portfolio_state(window: int = DEFAULT_WINDOW) -> dict:
    try:
        rows = _tail_scoreboard(window)
        span_h = _span_hours(rows)
        latest_row = rows[-1] if rows else {}
        brief = _safe_brief()
        bfunnel = (brief.get("funnel") or {}) if isinstance(brief, dict) else {}

        channels = scoreboard_channel_signal(window)
        for ch, (group, key) in BRIEF_CHANNELS.items():
            v = bfunnel.get(key)
            series = [] if v is None else [v]
            channels[ch] = _tally(series, f"brief.{group}", key, span_h)

        stripe = (latest_row.get("stripe") or {}) if isinstance(latest_row, dict) else {}
        return {
            "generated_at": _now(),
            "window_rows": len(rows),
            "window_span_hours": span_h,
            "channels": channels,
            "portfolio": {
                "revenue_usd_24h": _num(stripe.get("revenue_usd_24h", 0)),
                "real_sessions_24h": _num(stripe.get("sessions_created_real_24h", 0)),
                "completed_sessions_24h": _num(stripe.get("sessions_completed_24h", 0)),
            },
            "gates_blocking": (latest_row.get("gates_blocking") or []) if isinstance(latest_row, dict) else [],
            "binding_constraint": (brief.get("binding_constraint") or {}) if isinstance(brief, dict) else {},
        }
    except Exception as e:
        return {"generated_at": _now(), "channels": {}, "error": str(e)[:200]}


# ---------------------------------------------------------------------------
# Grounding — every verdict's rationale must cite a real number that appears in state.
# ---------------------------------------------------------------------------
def _state_facts(state: dict) -> set:
    """Every real number in state's VALUES (not keys), as strings, incl. 0. A rationale citing a
    number outside this set is ungrounded."""
    facts: set = set()

    def walk(v):
        if isinstance(v, bool):
            return
        if isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, (list, tuple)):
            for x in v:
                walk(x)
        elif isinstance(v, (int, float)):
            f = float(v)
            facts.add(str(int(f)) if f == int(f) else str(round(f, 3)))
        elif isinstance(v, str):
            for n in re.findall(r"\d+\.?\d*", v):
                facts.add(n)

    walk(state)
    return {f for f in facts if f}


def _grounded(rationale: str, facts: set) -> bool:
    cited = set(re.findall(r"\d+\.?\d*", str(rationale)))
    return bool(cited & facts)


# ---------------------------------------------------------------------------
# reallocate — turn state into an effort policy, write it atomically.
# ---------------------------------------------------------------------------
def _read_prior_policy() -> dict:
    try:
        return json.loads(EFFORT_POLICY.read_text())
    except Exception:
        return {}


def reallocate(state: dict | None = None) -> dict:
    try:
        # KILL SWITCH: no-op, return the prior policy unchanged.
        if CORTEX_OFF.exists():
            prior = _read_prior_policy()
            prior["_cortex_off"] = True
            return prior

        if state is None:
            state = read_portfolio_state()
        facts = _state_facts(state)
        gates_n = len(state.get("gates_blocking") or [])

        items = {}
        for ch, tally in (state.get("channels") or {}).items():
            if not isinstance(tally, dict):
                continue
            verdict = tally.get("verdict", "hold")
            weight = tally.get("weight", 1.0)
            rat = tally.get("rationale_core") or "no signal"
            if verdict in ("starve", "kill-candidate") and gates_n:
                rat += f" (note: {gates_n} gates blocking — see state.gates_blocking before acting)"
            items[ch] = {
                "lane": "faceless",                 # named/identity lanes are structurally excluded
                "weight": round(float(weight), 3),
                "verdict": verdict,
                "rationale": rat,
                "grounded": _grounded(rat, facts),
                "metric": tally.get("metric", {}),
            }

        counts: dict = {}
        for it in items.values():
            counts[it["verdict"]] = counts.get(it["verdict"], 0) + 1

        policy = {
            "generated_at": _now(),
            "source": "core/cortex/portfolio.py",
            "reversible": True,             # effort weights only; set back to 1.0 to reverse
            "spend": 0,
            "window_rows": state.get("window_rows"),
            "window_span_hours": state.get("window_span_hours"),
            "items": items,
            "summary": {"verdict_counts": counts,
                        "revenue_usd_24h": (state.get("portfolio") or {}).get("revenue_usd_24h"),
                        "gates_blocking": gates_n},
        }
        _atomic_write(EFFORT_POLICY, json.dumps(policy, indent=2, ensure_ascii=False) + "\n")
        return policy
    except Exception as e:
        prior = _read_prior_policy()
        prior["_error"] = str(e)[:200]
        return prior


# ---------------------------------------------------------------------------
# apply_is_safe — reversible + no named/identity lane + no spend + no kill/new-channel. Fail-CLOSED.
# ---------------------------------------------------------------------------
def apply_is_safe(policy: dict) -> bool:
    try:
        if not isinstance(policy, dict):
            return False
        if policy.get("irreversible") or policy.get("reversible") is False:
            return False
        for k in ("spend", "budget", "kill", "new_channel"):
            if policy.get(k):
                return False
        for _ch, it in (policy.get("items") or {}).items():
            if not isinstance(it, dict):
                return False
            lane = str(it.get("lane", "")).lower()
            if any(n in lane for n in NAMED_OR_IDENTITY_LANES):
                return False
            if str(it.get("verdict", "")).lower() == "kill":      # 'kill' executes; 'kill-candidate' only flags
                return False
            if str(it.get("action", "")).lower() in ("kill", "spend", "new_channel", "register"):
                return False
            if it.get("spend") or it.get("budget"):
                return False
            w = it.get("weight")
            if not isinstance(w, (int, float)) or isinstance(w, bool) or not (0.0 <= float(w) <= 1.0):
                return False
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# effort_for — the consumer API. A live loop multiplies its volume/frequency by this.
# ---------------------------------------------------------------------------
def effort_for(channel: str) -> float:
    """Current effort weight for a channel in [0,1]; 1.0 if unknown/off/error (fail-open: an
    unknown channel keeps running at full effort — the controller never silently starves a loop
    it has no opinion on)."""
    try:
        data = json.loads(EFFORT_POLICY.read_text())
        it = (data.get("items") or {}).get(channel)
        if isinstance(it, dict) and isinstance(it.get("weight"), (int, float)) and not isinstance(it["weight"], bool):
            return max(0.0, min(1.0, float(it["weight"])))
    except Exception:
        pass
    return 1.0


# ---------------------------------------------------------------------------
# self-test — hermetic, on temp paths, no side effects on real files.
# ---------------------------------------------------------------------------
def _selftest() -> int:
    global SCOREBOARD, EFFORT_POLICY, CORTEX_OFF
    import shutil

    fails = []
    td = Path(tempfile.mkdtemp(prefix="portfolio_selftest_"))
    orig = (SCOREBOARD, EFFORT_POLICY, CORTEX_OFF)
    try:
        # 0. read_portfolio_state on REAL state is read-only (no write side effects).
        real = read_portfolio_state()
        assert isinstance(real, dict) and "channels" in real, real

        # redirect all writeable paths to temp for the rest.
        EFFORT_POLICY = td / "effort_policy.json"
        CORTEX_OFF = td / "CORTEX_OFF"
        SCOREBOARD = td / "sb.jsonl"

        # synthetic state: a growing channel, a producing-flat channel, a short zero, and a
        # long sustained-zero channel (obs+span past the kill thresholds).
        span = KILL_MIN_SPAN_H + 2
        state = {
            "generated_at": _now(), "window_rows": 8, "window_span_hours": span,
            "channels": {
                "intent_harvest":  _tally([10, 20, 30], "scoreboard.leads", "intent_leads_new_24h", span),
                "intent_outreach": _tally([5, 5, 5], "scoreboard.distribution", "intent_replies_24h", span),
                "content_reels":   _tally([0, 0], "scoreboard.distribution", "reels_posted_24h", 1),
                "persona3":        _tally([0] * KILL_MIN_OBS, "scoreboard.distribution", "persona3_posted_24h", span),
            },
            "portfolio": {"revenue_usd_24h": 0}, "gates_blocking": ["g1"], "binding_constraint": {},
        }

        pol = reallocate(state)
        # writes effort_policy.json (atomically) to temp
        assert EFFORT_POLICY.exists(), "effort_policy.json not written"
        disk = json.loads(EFFORT_POLICY.read_text())
        assert disk.get("generated_at") and disk.get("items"), disk
        assert "generated_at" in pol and pol["items"] == disk["items"]

        # every verdict grounded in a real number from state + every item carries a rationale
        facts = _state_facts(state)
        for ch, it in pol["items"].items():
            assert it.get("rationale"), f"{ch} missing rationale"
            assert it["grounded"] is True, f"{ch} verdict ungrounded: {it['rationale']}"
            assert _grounded(it["rationale"], facts), f"{ch} rationale cites no state number"
            assert 0.0 <= it["weight"] <= 1.0, it

        # a sustained-zero channel is starve/kill-candidate, NOT killed, and keeps a >0 trickle
        it_dg = pol["items"]["persona3"]
        assert it_dg["verdict"] in ("starve", "kill-candidate"), it_dg
        assert it_dg["verdict"] == "kill-candidate", "long sustained-zero should flag kill-candidate"
        assert it_dg["weight"] > 0, "kill-candidate must not be auto-killed (weight > 0)"
        it_cr = pol["items"]["content_reels"]
        assert it_cr["verdict"] == "starve", it_cr  # short window -> starve, not kill-candidate
        # a growing channel scales
        assert pol["items"]["intent_harvest"]["verdict"] == "scale", pol["items"]["intent_harvest"]

        # effort_for reads the weight back; unknown channel -> 1.0 default
        assert abs(effort_for("intent_harvest") - 1.0) < 1e-9
        assert abs(effort_for("persona3") - KILL_CAND_WEIGHT) < 1e-9
        assert effort_for("no_such_channel") == 1.0

        # apply_is_safe: a real produced policy is safe...
        assert apply_is_safe(pol) is True, "produced policy should be applyable"
        # ...but BrandA / kill / spend policies are rejected
        assert apply_is_safe({"items": {"x": {"lane": "BrandA", "weight": 0.5, "verdict": "hold"}}}) is False
        assert apply_is_safe({"items": {"x": {"lane": "faceless", "weight": 0.0, "verdict": "kill"}}}) is False
        assert apply_is_safe({"items": {"x": {"lane": "faceless", "weight": 0.5, "verdict": "hold", "spend": 10}}}) is False
        assert apply_is_safe({"spend": 5, "items": {}}) is False
        assert apply_is_safe({"reversible": False, "items": {}}) is False
        assert apply_is_safe({"items": {"x": {"lane": "faceless", "weight": 1.4, "verdict": "hold"}}}) is False
        # kill-candidate (a flag, not an executed kill) is still safe to apply (down-weight is reversible)
        assert apply_is_safe({"items": {"x": {"lane": "faceless", "weight": 0.1, "verdict": "kill-candidate"}}}) is True

        # KILL SWITCH: CORTEX_OFF -> reallocate is a no-op returning prior policy, no overwrite
        before = EFFORT_POLICY.read_text()
        CORTEX_OFF.write_text("off")
        noop = reallocate({"channels": {"intent_harvest": _tally([999], "s", "k", 1)}})
        assert noop.get("_cortex_off") is True, "kill switch not honored"
        assert EFFORT_POLICY.read_text() == before, "reallocate wrote despite CORTEX_OFF"
        CORTEX_OFF.unlink()

        # no named/identity lane ever appears in a produced policy
        assert all(pol["items"][c]["lane"] == "faceless" for c in pol["items"]), "identity lane leaked"

    except AssertionError as e:
        fails.append(str(e))
    except Exception as e:
        fails.append(f"{type(e).__name__}: {e}")
    finally:
        SCOREBOARD, EFFORT_POLICY, CORTEX_OFF = orig
        shutil.rmtree(td, ignore_errors=True)

    if fails:
        print("SELFTEST FAILED:")
        for f in fails:
            print("  -", f)
        return 1
    print("SELFTEST OK — reallocate grounds every verdict, writes effort_policy.json atomically, "
          "flags zero-outcome as starve/kill-candidate (not killed), effort_for round-trips, "
          "apply_is_safe rejects BrandA/kill/spend, kill switch honored.")
    return 0


def main(argv: list) -> int:
    if "--selftest" in argv:
        return _selftest()
    if "--write" in argv:
        print(json.dumps(reallocate(), indent=2, ensure_ascii=False))
        return 0
    if "--dry" in argv:
        global EFFORT_POLICY
        save = EFFORT_POLICY
        EFFORT_POLICY = Path(tempfile.mkdtemp(prefix="portfolio_dry_")) / "effort_policy.json"
        try:
            print(json.dumps(reallocate(), indent=2, ensure_ascii=False))
        finally:
            EFFORT_POLICY = save
        return 0
    print(json.dumps(read_portfolio_state(), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
