#!/usr/bin/env python3
"""Outcome -> router learning sidecar (fail-soft, never rewrites the router).

core/router.py picks a model from ~20 hand-tuned regex banks and learns NOTHING from
whether the choice was any good. This sidecar closes that loop WITHOUT touching the
router's decision tree:

  - record_route(...)  appends one compact outcome row per route to a jsonl ledger,
                       fingerprinted into a coarse bucket (length band + keyword class)
                       so rows aggregate across similar prompts.
  - bias(text, base)   reads the ledger and, ONLY on strong evidence that the base
                       choice's tier chronically underperforms for this bucket, nudges
                       one tier UP the ladder ['local','haiku','sonnet','opus']. Anything
                       ambiguous, thin, or already at/above sonnet -> returns base unchanged.
  - stats(bucket=None) per-bucket outcome rates for inspection.

HARD invariants (see bias()):
  * bias only ever moves UP the ladder, so it can never downgrade a write/complex prompt
    to local, and never picks 'local'/'haiku' for an opus base (opus/sonnet bases no-op).
  * escalation targets come from the router's own known-emittable set (its freepool
    constants), so bias can never select a model the router doesn't already produce.
  * opus is emergency-gated in this stack (paid_model_or_local sends opus -> local), so
    bias NEVER auto-promotes INTO opus; sonnet is the ceiling.
  * every path is wrapped: any import/exception returns the safe default (base_choice /
    None / {}) and never raises into the router.

  python3 -m core.router_learning --selftest   # proves flip + no-op + no-opus-downgrade on a TEMP ledger
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_LEDGER = ROOT / "data" / "learning" / "router_outcomes.jsonl"

# Tier ladder (worst -> best). bias() may only ever step +1 along this, capped at 'sonnet'.
LADDER = ["local", "haiku", "sonnet", "opus"]

# Evidence thresholds — deliberately conservative (when in doubt, do nothing).
MIN_SAMPLE = 8          # scored rows for this bucket+tier before we'll act
BAD_THRESHOLD = 0.5     # chronic = a majority of scored routes went bad
MAX_READ = 8000         # tail cap so aggregation stays bounded regardless of ledger size
MAX_BYTES = 2_000_000   # rotate the ledger past this so it can't grow unbounded

# Outcomes that count as the route having underperformed.
BAD_OUTCOMES = {"reask", "error", "user_redo", "escalated"}

# Coarse keyword classes for the bucket fingerprint. First match wins; order is only about
# how rows group, not about safety (bias never downgrades regardless of class).
_RX_SYSTEM = re.compile(
    r"\b(salesforce|sfdc|sf-\d+|monday|outlook|inbox|calendar|zoom|"
    r"revenue|pipeline|gumroad|dashboard|meeting|schedule)\b", re.I)
_RX_CODE = re.compile(
    r"\b(debug|refactor|traceback|exception|stack\s*trace|python|pytest|deploy|"
    r"launchctl|repo|codebase|crash|patch|regex|compile|import\s+error)\b", re.I)
_RX_WRITE = re.compile(
    r"\b(send|reply|forward|draft|compose|write\s+an?\s+email|schedule|book|invite|"
    r"create|update|delete|cancel|reschedule|approve|reject)\b", re.I)
_RX_ANALYZE = re.compile(
    r"\b(analyze|analyse|compare|plan|design|architect|strategy|strategic|review|"
    r"summari[sz]e|assess|deep\s+dive|explain\s+why|proposal)\b", re.I)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ledger_path() -> Path:
    # env override lets the selftest (and any tooling) point at a temp ledger without
    # ever touching real state.
    override = os.environ.get("ROUTER_LEARNING_LEDGER")
    return Path(override) if override else _DEFAULT_LEDGER


# ---------------------------------------------------------------------------
# Fingerprinting: prompt -> coarse "band:class" bucket so rows aggregate.
# ---------------------------------------------------------------------------
def _length_band(text: str) -> str:
    n = len(str(text or ""))
    if n < 40:
        return "xs"
    if n < 160:
        return "s"
    if n < 600:
        return "m"
    return "l"


def _keyword_class(text: str) -> str:
    t = str(text or "")
    if _RX_SYSTEM.search(t):
        return "system"
    if _RX_CODE.search(t):
        return "code"
    if _RX_WRITE.search(t):
        return "write"
    if _RX_ANALYZE.search(t):
        return "analyze"
    return "chat"


def _bucket(text: str) -> str:
    return f"{_length_band(text)}:{_keyword_class(text)}"


# ---------------------------------------------------------------------------
# Tier classification + escalation targets (sourced from the router's known set).
# ---------------------------------------------------------------------------
def _tier_of(model) -> str | None:
    """Map a concrete router model string to a ladder tier. Never raises."""
    m = str(model or "").strip().lower()
    if not m:
        return None
    if m.startswith(("local:", "free:", "ollama:")):
        return "local"
    if m.startswith("freepool:"):
        # free cloud pool: balanced ~ haiku tier, strong ~ sonnet tier.
        return "haiku" if "balanced" in m else "sonnet"
    if m.startswith("chatgpt:"):
        return "sonnet"  # gpt work quarterback sits at the strong-mid tier
    try:
        from core import router as R  # exact match against the router's own constants
        for tier, const in (("haiku", "HAIKU"), ("sonnet", "SONNET"), ("opus", "OPUS")):
            val = getattr(R, const, None)
            if val and m == str(val).strip().lower():
                return tier
    except Exception:
        pass
    if "opus" in m or "pro" in m:
        return "opus"
    if "haiku" in m or "nano" in m or "mini" in m:
        return "haiku"
    if "sonnet" in m:
        return "sonnet"
    return "sonnet"  # unknown paid model -> treat as mid, never as local


def _ladder_targets() -> dict:
    """Concrete, in-set model the router already emits for each escalation tier.

    Escalations resolve to the router's FREE pool constants: strictly stronger than
    local, cost-free, and they never bypass the paid/opus gates. Falls back to the
    literal freepool tokens (which ARE R.FREE_BALANCED/R.FREE_STRONG) if the router
    can't be imported, so bias() still works and still stays in-set."""
    try:
        from core import router as R
        return {
            "haiku": getattr(R, "FREE_BALANCED", "freepool:balanced"),
            "sonnet": getattr(R, "FREE_STRONG", "freepool:strong"),
        }
    except Exception:
        return {"haiku": "freepool:balanced", "sonnet": "freepool:strong"}


# ---------------------------------------------------------------------------
# Ledger IO — append-only, tail-bounded, rotating. Never raises.
# ---------------------------------------------------------------------------
def _is_scored(row: dict) -> bool:
    return row.get("outcome") is not None or bool(row.get("escalated"))


def _is_bad(row: dict) -> bool:
    if row.get("escalated"):
        return True
    return str(row.get("outcome") or "").strip().lower() in BAD_OUTCOMES


def _read_rows() -> list:
    try:
        path = _ledger_path()
        if not path.exists():
            return []
        lines = path.read_text(errors="ignore").splitlines()[-MAX_READ:]
        out = []
        for ln in lines:
            ln = ln.strip()
            if not ln:
                continue
            try:
                row = json.loads(ln)
                if isinstance(row, dict):
                    out.append(row)
            except Exception:
                continue
        return out
    except Exception:
        return []


def record_route(text, chosen_model, *, outcome=None, cost=None, latency_ms=None, escalated=False) -> None:
    """Append one compact outcome row for a route. Fail-soft; NEVER raises.

    outcome is free-form (e.g. 'ok'|'reask'|'error'|'user_redo'|'escalated'). We store the
    coarse bucket + tier, not the raw prompt, so rows aggregate and stay compact/private."""
    try:
        path = _ledger_path()
        row = {
            "ts": _now(),
            "bucket": _bucket(text),
            "tier": _tier_of(chosen_model),
            "model": str(chosen_model or "")[:80],
            "outcome": (str(outcome).strip().lower() if outcome is not None else None),
            "cost": cost,
            "latency_ms": latency_ms,
            "escalated": bool(escalated),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if path.exists() and path.stat().st_size > MAX_BYTES:
                os.replace(path, path.with_suffix(path.suffix + ".1"))
        except Exception:
            pass
        with path.open("a") as fh:
            fh.write(json.dumps(row, separators=(",", ":")) + "\n")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Aggregation + inspection.
# ---------------------------------------------------------------------------
def _empty_agg() -> dict:
    return {"n": 0, "scored": 0, "bad": 0, "bad_rate": 0.0, "outcomes": {}, "tiers": {}}


def stats(bucket=None) -> dict:
    """Per-bucket outcome rates. With a bucket, returns that bucket's aggregate; without,
    a {bucket: aggregate} map. Never raises."""
    try:
        agg: dict = {}
        for r in _read_rows():
            b = r.get("bucket") or "?"
            if bucket is not None and b != bucket:
                continue
            a = agg.setdefault(b, _empty_agg())
            a["n"] += 1
            o = r.get("outcome")
            if o is not None:
                a["outcomes"][o] = a["outcomes"].get(o, 0) + 1
            t = r.get("tier") or "?"
            ta = a["tiers"].setdefault(t, {"n": 0, "scored": 0, "bad": 0, "bad_rate": 0.0})
            ta["n"] += 1
            if _is_scored(r):
                a["scored"] += 1
                ta["scored"] += 1
                if _is_bad(r):
                    a["bad"] += 1
                    ta["bad"] += 1
        for a in agg.values():
            a["bad_rate"] = round(a["bad"] / a["scored"], 3) if a["scored"] else 0.0
            for ta in a["tiers"].values():
                ta["bad_rate"] = round(ta["bad"] / ta["scored"], 3) if ta["scored"] else 0.0
        if bucket is not None:
            return agg.get(bucket, _empty_agg())
        return agg
    except Exception:
        return _empty_agg() if bucket is not None else {}


# ---------------------------------------------------------------------------
# The bias: one conservative tier bump on strong evidence, else base unchanged.
# ---------------------------------------------------------------------------
def bias(text, base_choice) -> str:
    """Return a better-tier model for this bucket ONLY on strong evidence the base tier
    chronically underperforms; otherwise return base_choice unchanged.

    Safety: only steps +1 up the ladder (never downgrades), only for a 'local'/'haiku'
    base (sonnet/opus/unknown bases no-op), caps at sonnet (never auto-promotes into the
    emergency-gated opus tier), and only ever returns a model the router already emits.
    Never raises."""
    try:
        base_tier = _tier_of(base_choice)
        # No-op unless the base sits at a tier we're allowed to lift (local or haiku).
        # This also guarantees: opus base is never touched, sonnet+ is never promoted into
        # opus, and a write/complex prompt (whose base is never local) is never downgraded.
        if base_tier not in ("local", "haiku"):
            return base_choice

        bstats = stats(_bucket(text))
        tstats = (bstats.get("tiers") or {}).get(base_tier) or {}
        if tstats.get("scored", 0) < MIN_SAMPLE:
            return base_choice                      # thin data -> trust the router
        if tstats.get("bad_rate", 0.0) < BAD_THRESHOLD:
            return base_choice                      # base tier is doing fine here

        target_tier = LADDER[LADDER.index(base_tier) + 1]   # local->haiku, haiku->sonnet
        target = _ladder_targets().get(target_tier)
        if not target:
            return base_choice
        # Belt-and-suspenders: the resolved target must be a STRICTLY higher tier.
        if LADDER.index(_tier_of(target) or base_tier) <= LADDER.index(base_tier):
            return base_choice
        return target
    except Exception:
        return base_choice


# ---------------------------------------------------------------------------
# Selftest — seeds a TEMP ledger and proves flip / no-op / no-opus-downgrade.
# ---------------------------------------------------------------------------
def _selftest() -> int:
    import tempfile

    fails = []
    with tempfile.TemporaryDirectory() as td:
        ledger = Path(td) / "router_outcomes.jsonl"
        os.environ["ROUTER_LEARNING_LEDGER"] = str(ledger)

        chronic = "quick chatty prompt"          # -> s:chat, seeded chronically bad on local
        thin = "another quick chatty ask here"   # -> s:chat too... use a distinct class instead
        thin = "debug this python traceback fast"  # -> s:code, only a few rows
        okbucket = "compare these two plans please and analyze tradeoffs briefly"  # analyze, good
        assert _bucket(chronic) != _bucket(thin), "buckets should differ"

        # 1) chronic underperformance on the LOCAL tier for the chatty bucket.
        for i in range(12):
            record_route(chronic, "local:qwen2.5:7b",
                         outcome=("reask" if i % 3 else "ok"), escalated=(i % 4 == 0))
        # 2) thin data: only 3 rows in a different bucket.
        for _ in range(3):
            record_route(thin, "local:qwen2.5:7b", outcome="reask")
        # 3) plenty of rows but the tier is doing fine (mostly ok).
        for i in range(12):
            record_route(okbucket, "freepool:balanced", outcome=("ok" if i % 5 else "reask"))
        # 4) an opus bucket that is ALSO chronically bad — must still never downgrade.
        opus_prompt = "architect a full multi-service migration plan " + ("x" * 200)
        for _ in range(12):
            record_route(opus_prompt, "openai/gpt-5.5-pro", outcome="reask", escalated=True)

        targets = _ladder_targets()

        # Assertion A: chronic local bucket flips UP one tier (local -> haiku target).
        out_a = bias(chronic, "local:qwen2.5:7b")
        if out_a != targets["haiku"]:
            fails.append(f"A: chronic bucket did not escalate: got {out_a!r} want {targets['haiku']!r}")
        if _tier_of(out_a) != "haiku":
            fails.append(f"A: escalation target not haiku-tier: {out_a!r}->{_tier_of(out_a)}")

        # Assertion B: thin data is a no-op.
        out_b = bias(thin, "local:qwen2.5:7b")
        if out_b != "local:qwen2.5:7b":
            fails.append(f"B: thin data was not a no-op: {out_b!r}")

        # Assertion C: healthy tier is a no-op even with plenty of samples.
        out_c = bias(okbucket, "freepool:balanced")
        if out_c != "freepool:balanced":
            fails.append(f"C: healthy bucket was not a no-op: {out_c!r}")

        # Assertion D: opus base is NEVER downgraded, even on a chronically-bad bucket.
        out_d = bias(opus_prompt, "openai/gpt-5.5-pro")
        if out_d != "openai/gpt-5.5-pro" or _tier_of(out_d) != "opus":
            fails.append(f"D: opus prompt was downgraded: {out_d!r}")

        # Assertion E: bias never returns 'local' for a haiku base (upward-only).
        for _ in range(12):
            record_route("short haiky prompt", "freepool:balanced", outcome="error")
        out_e = bias("short haiky prompt", "freepool:balanced")
        if _tier_of(out_e) not in ("sonnet",) or out_e != targets["sonnet"]:
            fails.append(f"E: haiku base did not step to sonnet: {out_e!r}")

        # Assertion F: stats() shape is sane and non-raising.
        st = stats(_bucket(chronic))
        if not (st.get("n") and "tiers" in st and st.get("scored")):
            fails.append(f"F: stats() shape wrong: {st!r}")

        os.environ.pop("ROUTER_LEARNING_LEDGER", None)

    if fails:
        print("SELFTEST FAIL")
        for f in fails:
            print("  -", f)
        return 1
    print("SELFTEST PASS: flip on chronic bucket, no-op on thin/healthy data, "
          "no opus downgrade, upward-only escalation, stats sane.")
    return 0


def main(argv: list[str]) -> int:
    if "--selftest" in argv:
        return _selftest()
    if "--stats" in argv:
        print(json.dumps(stats(), indent=2))
        return 0
    print("usage: python3 -m core.router_learning [--selftest|--stats]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
