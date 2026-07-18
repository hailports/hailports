#!/usr/bin/env python3
"""Human-rhythm pacing engine — the safety core of the social-automation product.

Crude bots get accounts banned: fixed sleeps and flat daily volumes look exactly
like automation. This module makes activity look human so client accounts survive
(the actual thing buyers pay for). Five layers, all $0 / stdlib-only:

  1. GAUSSIAN delays   — inter-action gaps are truncated-normal, not constant, so
                         the time signature between actions has natural variance.
  2. WARMUP curve      — a new/cold account ramps its allowed volume from ~15% to
                         100% over `ramp_days`; you never blast a fresh account.
  3. CIRCADIAN windows — activity probability tracks a human day (trough 1–6am,
                         peaks late-morning + evening), so there's no 3am posting.
  4. DAILY BUDGETS     — per-account, per-action caps = base × warmup × daily jitter,
                         so volume differs day to day instead of a robotic constant.
  5. REST jitter       — occasional quiet hours / light days, like a real person.

Persistence is a tiny JSON ledger (first-seen per account + today's action counts).
Deterministic when seeded, so it's testable.

CLI:
  python3 -m core.human_rhythm plan --account persona1 --kind engage     # show today's plan
  python3 -m core.human_rhythm gate --account persona1 --kind engage     # ok/blocked now
  python3 -m core.human_rhythm simulate --account persona1 --days 14      # warmup curve
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = Path(os.environ.get("HUMAN_RHYTHM_STATE", ROOT / "data" / "social" / "rhythm_state.json"))

# Per-action-kind tuning: (mean_gap_s, sd_s, floor_s, ceil_s, base_daily_cap).
# Gaps are between consecutive actions of that kind; caps are per account per day
# at FULL warmup (a fresh account gets a fraction of this).
KINDS: dict[str, tuple[float, float, float, float, int]] = {
    "engage":  (95.0,  55.0, 25.0,  420.0, 40),   # likes/comments
    "comment": (140.0, 80.0, 40.0,  600.0, 18),
    "post":    (5400.0, 2700.0, 900.0, 21600.0, 6),  # original posts (hours apart)
    "follow":  (160.0, 90.0, 45.0,  700.0, 25),
    "dm":      (220.0, 120.0, 60.0, 900.0, 12),
}
DEFAULT_RAMP_DAYS = int(os.environ.get("HUMAN_RHYTHM_RAMP_DAYS", "14"))
WARMUP_FLOOR = 0.15  # a brand-new account starts at 15% of full volume


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _load() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def _save(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, STATE_FILE)  # atomic — a crash mid-write never corrupts the ledger


def _acct(state: dict, account: str) -> dict:
    a = state.setdefault(account, {})
    a.setdefault("first_seen", _today())
    today = _today()
    if a.get("day") != today:
        a["day"] = today
        a["actions"] = {}
        # one jitter seed per account per day → today's budgets/windows are stable
        # within the day but differ day to day.
        a["day_seed"] = random.randint(1, 2**31 - 1)
    a.setdefault("actions", {})
    a.setdefault("day_seed", random.randint(1, 2**31 - 1))
    return a


def _day_rng(a: dict, salt: str = "") -> random.Random:
    return random.Random(f"{a.get('day_seed')}-{a.get('day')}-{salt}")


def account_age_days(a: dict) -> int:
    try:
        first = date.fromisoformat(a["first_seen"])
        return max(0, (date.fromisoformat(_today()) - first).days)
    except Exception:
        return 0


def warmup_factor(age_days: int, ramp_days: int = DEFAULT_RAMP_DAYS) -> float:
    """0.15 → 1.0 over ramp_days, ease-out so it accelerates gently then levels off."""
    if ramp_days <= 0:
        return 1.0
    t = min(1.0, age_days / ramp_days)
    eased = 1 - (1 - t) ** 2  # ease-out quad
    return round(WARMUP_FLOOR + (1.0 - WARMUP_FLOOR) * eased, 4)


def circadian_weight(dt: Optional[datetime] = None) -> float:
    """Activity likelihood in [0,1] by local hour: deep trough overnight, twin
    peaks late-morning (~11) and evening (~20)."""
    dt = dt or datetime.now()
    h = dt.hour + dt.minute / 60.0
    morning = math.exp(-((h - 11.0) ** 2) / (2 * 3.0 ** 2))
    evening = math.exp(-((h - 20.0) ** 2) / (2 * 3.0 ** 2))
    w = max(morning, evening)
    if h < 6:      # 1–6am: near-silent
        w *= 0.06
    elif h < 8:
        w *= 0.4
    return round(min(1.0, w), 4)


def daily_budget(account: str, kind: str, state: Optional[dict] = None) -> int:
    """base_cap × warmup × daily jitter (0.7–1.15). At least 1 once warmed in."""
    own = state is None
    state = state if state is not None else _load()
    a = _acct(state, account)
    base = KINDS.get(kind, KINDS["engage"])[4]
    wf = warmup_factor(account_age_days(a))
    jitter = _day_rng(a, f"budget-{kind}").uniform(0.7, 1.15)
    budget = int(round(base * wf * jitter))
    # occasional "light day" (~1 in 7) — a real person isn't equally active daily
    if _day_rng(a, f"lightday-{kind}").random() < 0.14:
        budget = int(round(budget * 0.5))
    if own:
        _save(state)
    return max(1 if wf > WARMUP_FLOOR else 0, budget)


def gate(account: str, kind: str, dt: Optional[datetime] = None) -> tuple[bool, str]:
    """Should this account do one `kind` action right now? Checks daily budget,
    circadian window, and rest jitter. Pure read (does not consume budget)."""
    state = _load()
    a = _acct(state, account)
    done = int(a["actions"].get(kind, 0))
    budget = daily_budget(account, kind, state)
    if done >= budget:
        _save(state)
        return False, f"daily budget reached ({done}/{budget} {kind})"
    cw = circadian_weight(dt)
    roll = _day_rng(a, f"circ-{kind}-{done}").random()
    _save(state)
    if roll > cw:
        return False, f"outside human activity window (circadian {cw:.2f} < {roll:.2f})"
    return True, f"ok ({done}/{budget} {kind}, circadian {cw:.2f})"


def next_delay(account: str, kind: str, dt: Optional[datetime] = None) -> float:
    """Truncated-gaussian seconds to wait before the next action of this kind.
    Stretched when circadian activity is low (slower in off-peak hours)."""
    mean, sd, lo, hi, _ = KINDS.get(kind, KINDS["engage"])
    d = random.gauss(mean, sd)
    cw = circadian_weight(dt)
    d *= 1.0 + (1.0 - cw) * 0.8  # off-peak → up to ~1.8× slower
    return float(min(hi * 1.8, max(lo, d)))


def sleep_before_next(account: str, kind: str) -> float:
    d = next_delay(account, kind)
    time.sleep(d)
    return d


def record(account: str, kind: str, n: int = 1) -> dict:
    """Consume budget after an action actually happened."""
    state = _load()
    a = _acct(state, account)
    a["actions"][kind] = int(a["actions"].get(kind, 0)) + n
    a["last_action"] = datetime.now(timezone.utc).isoformat()
    _save(state)
    return {"account": account, "kind": kind, "today": a["actions"][kind],
            "budget": daily_budget(account, kind, state)}


def _cli() -> int:
    p = argparse.ArgumentParser(description="Human-rhythm pacing engine")
    sub = p.add_subparsers(dest="cmd", required=True)
    for c in ("plan", "gate", "record"):
        sp = sub.add_parser(c)
        sp.add_argument("--account", required=True)
        sp.add_argument("--kind", default="engage", choices=list(KINDS))
    sim = sub.add_parser("simulate")
    sim.add_argument("--account", required=True)
    sim.add_argument("--kind", default="engage", choices=list(KINDS))
    sim.add_argument("--days", type=int, default=14)
    args = p.parse_args()

    if args.cmd == "plan":
        st = _load(); a = _acct(st, args.account); _save(st)
        print(json.dumps({
            "account": args.account, "kind": args.kind,
            "age_days": account_age_days(a),
            "warmup": warmup_factor(account_age_days(a)),
            "daily_budget": daily_budget(args.account, args.kind),
            "done_today": a["actions"].get(args.kind, 0),
            "circadian_now": circadian_weight(),
            "sample_next_delays_s": [round(next_delay(args.account, args.kind), 1) for _ in range(5)],
        }, indent=2))
    elif args.cmd == "gate":
        ok, why = gate(args.account, args.kind)
        print(json.dumps({"ok": ok, "reason": why}, indent=2)); return 0 if ok else 1
    elif args.cmd == "record":
        print(json.dumps(record(args.account, args.kind), indent=2))
    elif args.cmd == "simulate":
        print(f"warmup ramp for {args.account} ({args.kind}):")
        for d in range(args.days + 1):
            wf = warmup_factor(d)
            print(f"  day {d:>2}: warmup {wf:>5.2f}  ~budget {int(round(KINDS[args.kind][4]*wf))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
