#!/usr/bin/env python3
"""Supervised spend gate for the revenue demon — money is safety-critical, so this
FAILS CLOSED (the opposite of outreach_governor, which fails open).

Three independent guards, all must pass before a dollar can move:
  1. kill-switches absent  (data/hustle/REVENUE_DEMON_OFF, REVENUE_DEMON_SPEND_OFF)
  2. channel armed         (REVENUE_DEMON_ARMED_CHANNELS csv; empty => NO paid spend)
  3. under the local-day budget cap (REVENUE_DEMON_DAILY_BUDGET_USD, default 10)
  + a wired payment rail must exist for the channel (none wired today => always stage)
  + the hash-chained ledger must verify (a break freezes all spend + pages critical)

Anything that does not clear ALL of the above is STAGED to SPEND_APPROVALS.md for the
owner instead of charged — and an alert is routed (his choice: queue + ping). Nothing
here ever charges a card on its own until a rail is added to ``_rail_for``.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

HUSTLE = ROOT / "data" / "hustle"
LEDGER = HUSTLE / "spend_ledger.jsonl"
APPROVALS = HUSTLE / "SPEND_APPROVALS.md"
OFF = HUSTLE / "REVENUE_DEMON_OFF"
SPEND_OFF = HUSTLE / "REVENUE_DEMON_SPEND_OFF"

GENESIS = "0" * 64


def _budget_cap() -> float:
    try:
        return max(0.0, float(os.environ.get("REVENUE_DEMON_DAILY_BUDGET_USD", "10")))
    except Exception:
        return 0.0  # unparseable => no spend (fail closed)


def _armed_channels() -> set[str]:
    raw = os.environ.get("REVENUE_DEMON_ARMED_CHANNELS", "")
    return {c.strip() for c in raw.split(",") if c.strip()}


def _rail_for(channel: str):
    """Return a callable(amount_usd, ref)->external_ref that actually moves money for
    ``channel``, or None if no rail is wired. NOTHING is wired today, so every spend
    stages for approval. Wiring a real ad/registrar API here is a deliberate,
    owner-approved step — never add one implicitly."""
    return None


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _today_key() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d")


def _entry_hash(prev_hash: str, entry: dict) -> str:
    body = {k: entry[k] for k in entry if k != "hash"}
    payload = prev_hash + json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _read_ledger() -> list[dict]:
    if not LEDGER.exists():
        return []
    rows = []
    for line in LEDGER.read_text().splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def verify_chain(rows: list[dict] | None = None) -> tuple[bool, str]:
    """Recompute the hash chain end to end. A break = tampering/corruption."""
    rows = _read_ledger() if rows is None else rows
    prev = GENESIS
    for i, row in enumerate(rows):
        if row.get("prev_hash") != prev:
            return False, f"prev_hash mismatch at row {i}"
        if _entry_hash(prev, row) != row.get("hash"):
            return False, f"hash mismatch at row {i}"
        prev = row["hash"]
    return True, "ok"


def _spent_today(rows: list[dict]) -> float:
    today = _today_key()
    total = 0.0
    for r in rows:
        if str(r.get("ts", ""))[:10] == today:
            total += float(r.get("amount_usd", 0) or 0)
    return total


def can_spend(channel: str, amount_usd: float) -> tuple[bool, str]:
    """FAIL-CLOSED preflight. Returns (allowed, reason). Any error => denied."""
    try:
        if OFF.exists():
            return False, "REVENUE_DEMON_OFF present"
        if SPEND_OFF.exists():
            return False, "REVENUE_DEMON_SPEND_OFF present"
        amount_usd = float(amount_usd)
        if amount_usd <= 0:
            return False, "non-positive amount"
        if channel not in _armed_channels():
            return False, f"channel '{channel}' not armed (REVENUE_DEMON_ARMED_CHANNELS)"
        rows = _read_ledger()
        ok, why = verify_chain(rows)
        if not ok:
            _freeze(why)
            return False, f"ledger chain broken: {why}"
        cap = _budget_cap()
        if _spent_today(rows) + amount_usd > cap:
            return False, f"would exceed daily cap ${cap:.2f}"
        if _rail_for(channel) is None:
            return False, f"no payment rail wired for '{channel}'"
        return True, "ok"
    except Exception as e:  # money path: any surprise => deny
        return False, f"spend preflight error: {str(e)[:120]}"


def _freeze(reason: str) -> None:
    try:
        from core.alert_gateway import route
        route("critical", "revenue_demon",
              "revenue spend FROZEN: ledger chain broken",
              f"{reason}\nClear data/hustle/spend_ledger.jsonl after auditing to unfreeze.")
    except Exception:
        pass


def record_spend(channel: str, amount_usd: float, ref: str = "") -> dict:
    """Append a hashed entry. Call ONLY after a real charge confirmed."""
    rows = _read_ledger()
    ok, why = verify_chain(rows)
    if not ok:
        _freeze(why)
        raise RuntimeError(f"refusing to append to a broken ledger: {why}")
    prev = rows[-1]["hash"] if rows else GENESIS
    entry = {"ts": _now_iso(), "channel": channel,
             "amount_usd": round(float(amount_usd), 2), "ref": ref, "prev_hash": prev}
    entry["hash"] = _entry_hash(prev, entry)
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a") as f:
        f.write(json.dumps(entry) + "\n")
    try:
        os.chmod(LEDGER, 0o600)
    except Exception:
        pass
    return entry


def _stage_approval(channel: str, amount_usd: float, hypothesis: str,
                    expected_return: str, kill_criteria: str, reason: str) -> None:
    APPROVALS.parent.mkdir(parents=True, exist_ok=True)
    header = f"## {_today_key()} — spend approvals"
    block = (
        f"\n- [ ] **${float(amount_usd):.2f} · {channel}** — {hypothesis}\n"
        f"  - expected return: {expected_return}\n"
        f"  - kill criteria: {kill_criteria}\n"
        f"  - why staged (not auto): {reason}\n"
        f"  - to approve: arm the rail, then `REVENUE_DEMON_ARMED_CHANNELS+={channel}` "
        f"and confirm budget; proposed {_now_iso()}\n"
    )
    existing = APPROVALS.read_text() if APPROVALS.exists() else ""
    if header not in existing:
        block = f"\n{header}\n" + block
    with APPROVALS.open("a") as f:
        f.write(block)


def propose_spend(channel: str, amount_usd: float, hypothesis: str = "",
                  expected_return: str = "", kill_criteria: str = "") -> dict:
    """The demon's one entry point for money. Auto-charges ONLY if every guard passes
    AND a rail is wired; otherwise stages to SPEND_APPROVALS + pings the owner.
    Returns {action: 'spent'|'staged', ...}."""
    allowed, reason = can_spend(channel, amount_usd)
    if allowed:
        rail = _rail_for(channel)  # not None by definition when allowed
        try:
            ext_ref = rail(float(amount_usd), hypothesis) or ""
            entry = record_spend(channel, amount_usd, ref=str(ext_ref))
            return {"action": "spent", "channel": channel,
                    "amount_usd": entry["amount_usd"], "ref": entry["ref"]}
        except Exception as e:
            reason = f"rail execution failed: {str(e)[:120]}"  # fall through to staging
    _stage_approval(channel, amount_usd, hypothesis, expected_return, kill_criteria, reason)
    try:
        from core.alert_gateway import route
        route("warn", "revenue_demon",
              f"revenue spend approval needed: ${float(amount_usd):.2f} on {channel}",
              f"{hypothesis}\nexpected: {expected_return}\nstaged because: {reason}\n"
              f"see data/hustle/SPEND_APPROVALS.md")
    except Exception:
        pass
    return {"action": "staged", "channel": channel,
            "amount_usd": round(float(amount_usd), 2), "reason": reason}


def status() -> dict:
    rows = _read_ledger()
    ok, why = verify_chain(rows)
    return {"armed_channels": sorted(_armed_channels()), "daily_cap_usd": _budget_cap(),
            "spent_today_usd": round(_spent_today(rows), 2), "ledger_rows": len(rows),
            "chain_ok": ok, "chain_note": why,
            "spend_off": SPEND_OFF.exists(), "demon_off": OFF.exists()}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="revenue-demon spend gate")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--propose", nargs=2, metavar=("CHANNEL", "AMOUNT"),
                    help="dry propose a spend (stages unless armed+rail)")
    a = ap.parse_args()
    if a.verify:
        print(json.dumps(dict(zip(("ok", "note"), verify_chain())), indent=2))
    elif a.propose:
        print(json.dumps(propose_spend(a.propose[0], float(a.propose[1]),
                                       hypothesis="cli dry propose"), indent=2))
    else:
        print(json.dumps(status(), indent=2))
