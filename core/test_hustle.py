"""Experiment ledger for test-mode hustle/revenue efforts.

This is the place for ideas that are not ready for real money or full
automation yet: paper trading bots, channel tests, source experiments, and
small proof runs. The portal can promote only the tests that are actually
working.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core import BASE_DIR


TEST_PATH = BASE_DIR / "data" / "hustle" / "test_efforts.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read() -> dict[str, Any]:
    try:
        if TEST_PATH.exists():
            data = json.loads(TEST_PATH.read_text())
            return data if isinstance(data, dict) else {"experiments": []}
    except Exception:
        pass
    return {"experiments": []}


def _write(data: dict[str, Any]) -> dict[str, Any]:
    TEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    data["updated_at"] = _now()
    tmp = TEST_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True, default=str))
    tmp.replace(TEST_PATH)
    return data


def load_tests() -> dict[str, Any]:
    data = _read()
    experiments = data.get("experiments")
    if not isinstance(experiments, list):
        experiments = []
        data["experiments"] = experiments
    summary = {
        "total": len(experiments),
        "running": sum(1 for e in experiments if e.get("status") in {"new", "running", "testing"}),
        "approval_candidates": sum(1 for e in experiments if e.get("status") == "approval_candidate"),
        "approved": sum(1 for e in experiments if e.get("status") == "approved"),
        "killed": sum(1 for e in experiments if e.get("status") == "killed"),
    }
    return {"experiments": experiments, "summary": summary, "path": str(TEST_PATH), "updated_at": data.get("updated_at")}


def upsert_experiment(
    *,
    key: str,
    title: str,
    lane: str,
    hypothesis: str,
    status: str = "running",
    sources: list[dict[str, Any]] | None = None,
    metrics: dict[str, Any] | None = None,
    evidence: list[str] | None = None,
    notes: str = "",
) -> dict[str, Any]:
    data = _read()
    rows = data.setdefault("experiments", [])
    if not isinstance(rows, list):
        rows = []
        data["experiments"] = rows
    existing = None
    for row in rows:
        if isinstance(row, dict) and row.get("key") == key:
            existing = row
            break
    if existing is None:
        existing = {
            "id": f"test-{uuid.uuid4().hex[:10]}",
            "key": key,
            "created_at": _now(),
            "decision_log": [],
        }
        rows.insert(0, existing)

    old_status = existing.get("status")
    existing.update({
        "title": title,
        "lane": lane,
        "hypothesis": hypothesis,
        "status": status,
        "sources": sources or [],
        "metrics": metrics or {},
        "evidence": evidence or [],
        "notes": notes,
        "updated_at": _now(),
    })
    if old_status and old_status != status:
        existing.setdefault("decision_log", []).append({"ts": _now(), "from": old_status, "to": status})
    _write(data)
    return existing


def update_test(test_id: str, *, status: str | None = None, note: str = "") -> dict[str, Any]:
    data = _read()
    rows = data.setdefault("experiments", [])
    allowed = {"new", "running", "testing", "approval_candidate", "approved", "killed", "paused"}
    for row in rows:
        if not isinstance(row, dict) or row.get("id") != test_id:
            continue
        if status:
            clean = status if status in allowed else "running"
            old = row.get("status")
            row["status"] = clean
            row.setdefault("decision_log", []).append({"ts": _now(), "from": old, "to": clean, "note": note})
        if note:
            row["operator_note"] = note
        row["updated_at"] = _now()
        _write(data)
        return row
    raise KeyError(f"test not found: {test_id}")


def request_promotion(test_id: str, *, amount_usd: float, note: str = "") -> dict[str, Any]:
    """Queue a test for real-money/revenue promotion.

    This is intentionally not a funding action. It creates the exact promotion
    packet the operator can approve: amount, lane, gates, and execution steps.
    """
    data = _read()
    rows = data.setdefault("experiments", [])
    amount = max(0.0, min(float(amount_usd or 0.0), 10_000.0))
    for row in rows:
        if not isinstance(row, dict) or row.get("id") != test_id:
            continue
        lane = str(row.get("lane") or "")
        key = str(row.get("key") or "")
        gates = [
            "operator confirms this exact experiment is approved for real money",
            "operator confirms funding amount and source account",
            "runtime verifies login/session/API credential health",
            "runtime verifies kill switch, max loss, and rollback/stop instructions",
        ]
        if "kalshi" in key or "prediction" in lane:
            gates.extend([
                "Kalshi live trading remains disabled until KALSHI_ALLOW_LIVE=1 and kalshi_live_armed.json exist",
                "Bovada remains read-only; no automated Bovada funding or wagering",
            ])
        packet = {
            "requested_at": _now(),
            "amount_usd": round(amount, 2),
            "status": "promotion_requested",
            "operator_note": note,
            "gates": gates,
            "execution_plan": [
                "open/verify the target account session or API credentials",
                "confirm balance and funding source",
                "fund only the approved amount",
                "switch the bot from paper to live with the same risk caps",
                "run one live micro-cycle, then stop and report exact result",
            ],
            "automatic_actions_allowed": [
                "prepare funding/account checklist",
                "verify credential/session health",
                "open/login to approved provider using existing stored credentials/session where available",
                "create account/application draft when allowed by provider terms",
                "queue funding workflow for the approved amount",
                "prepare live config and kill switch",
                "start paper-to-live promotion report",
            ],
            "automatic_actions_blocked_until_final_approval": [
                "submitting bank/card transfer or deposit",
                "placing real-money orders",
                "withdrawing or transferring funds",
                "accepting legal terms on the user's behalf",
                "bypassing MFA or identity checks",
                "Bovada wagering automation",
            ],
        }
        row["promotion"] = packet
        old = row.get("status")
        row["status"] = "promotion_requested"
        row.setdefault("decision_log", []).append({"ts": _now(), "from": old, "to": "promotion_requested", "note": note})
        row["updated_at"] = _now()
        _write(data)
        return row
    raise KeyError(f"test not found: {test_id}")


def run_promotion(test_id: str) -> dict[str, Any]:
    """Execute the non-money setup part of a promotion packet.

    This is the autonomous piece behind the button. It creates concrete prep
    tasks and live-arm instructions. It still blocks actual deposits/orders
    until the final gate is present.
    """
    data = _read()
    rows = data.setdefault("experiments", [])
    for row in rows:
        if not isinstance(row, dict) or row.get("id") != test_id:
            continue
        promo = row.get("promotion") or {}
        amount = float(promo.get("amount_usd") or 0.0)
        lane = str(row.get("lane") or "")
        key = str(row.get("key") or "")
        steps: list[dict[str, Any]] = []
        blockers: list[str] = []

        if "kalshi" in key or "prediction" in lane:
            steps.extend([
                {"status": "ready", "action": "verify Kalshi account/session/API credentials", "automation": "credential_check"},
                {"status": "ready", "action": f"prepare Kalshi paper-to-live config capped at ${amount:.2f}", "automation": "config_prepare"},
                {"status": "ready", "action": "create kalshi_live_armed.json only after final operator approval", "automation": "arm_file_prepare"},
            ])
            blockers.extend([
                "Kalshi account creation/login/MFA may require operator interaction if no valid session/API key exists.",
                "Funding must be submitted/confirmed by operator or by a separately approved funding credential workflow.",
            ])
        elif "forex" in key or "forex" in lane:
            steps.extend([
                {"status": "ready", "action": "verify OANDA/practice account credentials", "automation": "credential_check"},
                {"status": "ready", "action": f"prepare Forex live config capped at ${amount:.2f}", "automation": "config_prepare"},
                {"status": "ready", "action": "create forex_live_armed.json only after final operator approval", "automation": "arm_file_prepare"},
            ])
            blockers.extend([
                "Broker account creation/KYC/MFA may require operator interaction.",
                "Live FX funding/order placement remains blocked until final approval and broker credentials are healthy.",
            ])
        elif "micro-trader" in key or "market_simulation" in lane:
            steps.extend([
                {"status": "ready", "action": "verify broker paper/live API credentials", "automation": "credential_check"},
                {"status": "ready", "action": f"prepare live equity microtrader config capped at ${amount:.2f}", "automation": "config_prepare"},
                {"status": "ready", "action": "create micro_trader_live_armed.json only after final operator approval", "automation": "arm_file_prepare"},
            ])
            blockers.append("Broker login/KYC/funding may require operator interaction.")
        else:
            steps.append({"status": "ready", "action": "prepare generic revenue promotion checklist", "automation": "checklist"})

        promo["runner"] = {
            "started_at": _now(),
            "status": "prepared_needs_final_gate",
            "amount_usd": round(amount, 2),
            "steps": steps,
            "blockers": blockers,
            "next_operator_action": "approve final money movement/live arm only after account/session/funding details are verified",
        }
        row["promotion"] = promo
        row.setdefault("decision_log", []).append({"ts": _now(), "event": "promotion_runner", "status": promo["runner"]["status"]})
        row["updated_at"] = _now()
        _write(data)
        return row
    raise KeyError(f"test not found: {test_id}")


def record_kalshi_snapshot(status: dict[str, Any]) -> dict[str, Any]:
    signal = status.get("last_signal") or {}
    action = status.get("last_action") or {}
    bovada = status.get("bovada") or {}
    exposure = float(status.get("paper_exposure") or 0.0)
    positions = status.get("positions") or {}
    daily = status.get("daily") or {}
    status_name = "approval_candidate" if exposure > 0 and int(daily.get("orders") or 0) >= 5 else "running"
    return upsert_experiment(
        key="kalshi-paper-odds-bot",
        title="Kalshi paper odds bot",
        lane="prediction_markets",
        hypothesis="Use public Kalshi markets plus configured online odds/handicapper feeds to find paper-tradable event-contract edges before risking cash.",
        status=status_name,
        sources=[
            {"name": "Kalshi public market data", "mode": "read"},
            {"name": "Bovada odds bridge", "mode": "read_only", "configured": bool(bovada.get("enabled"))},
            {"name": "Research/handicapper feeds", "mode": "read_only", "configured": bool((status.get("research") or {}).get("sources"))},
        ],
        metrics={
            "paper_cash": status.get("cash"),
            "paper_exposure": exposure,
            "open_positions": len(positions),
            "daily_orders": daily.get("orders"),
            "last_signal": signal.get("ticker"),
            "last_edge_score": signal.get("edge_score"),
            "last_action": action.get("status"),
        },
        evidence=[
            f"status: {status.get('mode')} / {status.get('env')}",
            f"last market source: {status.get('last_market_source')}",
            f"last signal: {signal.get('ticker') or 'none'}",
            "Bovada automation is explicitly read-only; no Bovada wagers are placed.",
        ],
        notes="Promote only after sustained paper performance and clean source attribution.",
    )


def record_micro_trader_snapshot(status: dict[str, Any], backtest: dict[str, Any] | None = None) -> dict[str, Any]:
    decision = status.get("decision") or {}
    status_name = "approval_candidate" if backtest and float(backtest.get("net_pnl") or 0.0) > 0 else "running"
    return upsert_experiment(
        key="micro-trader-paper-equities",
        title="Micro trader paper equities simulator",
        lane="market_simulation",
        hypothesis="Run a tiny-capital paper strategy with hard loss gates before any real-money broker connection.",
        status=status_name,
        sources=[{"name": "synthetic/paper price stream", "mode": "paper"}],
        metrics={
            "equity": status.get("equity"),
            "cash": status.get("cash"),
            "total_pnl": status.get("total_pnl"),
            "daily_trades": (status.get("daily") or {}).get("trades"),
            "last_decision": decision.get("action"),
            "backtest_net_pnl": (backtest or {}).get("net_pnl"),
            "backtest_return_pct": (backtest or {}).get("return_pct"),
        },
        evidence=[
            f"mode: {status.get('mode')}",
            f"kill switch: {status.get('kill_switch')}",
            f"live trading possible: {status.get('live_trading_possible')}",
        ],
        notes="Synthetic backtests prove plumbing only; require forward paper logs before live money.",
    )


def record_forex_snapshot(status: dict[str, Any]) -> dict[str, Any]:
    signal = status.get("last_signal") or {}
    action = status.get("last_action") or {}
    return upsert_experiment(
        key="forex-paper-bot",
        title="Forex paper bot",
        lane="forex",
        hypothesis="Paper trade major FX pairs with tiny notional caps, online research feeds, and live broker gates before risking cash.",
        status="running",
        sources=[
            {"name": "major FX pair price stream", "mode": "paper/provider"},
            {"name": "Forex research feeds", "mode": "read_only", "configured": bool((status.get("risk") or {}).get("research_urls"))},
            {"name": "OANDA live adapter", "mode": "blocked_until_approved", "configured": bool((status.get("risk") or {}).get("oanda_token"))},
        ],
        metrics={
            "paper_cash": status.get("cash"),
            "equity": status.get("equity"),
            "realized_pnl": status.get("realized_pnl"),
            "open_positions": len(status.get("positions") or {}),
            "daily_trades": (status.get("daily") or {}).get("trades"),
            "last_signal": signal.get("pair"),
            "last_edge_score": signal.get("edge"),
            "last_action": action.get("status"),
        },
        evidence=[
            f"mode: {status.get('mode')}",
            f"live trading possible: {status.get('live_trading_possible')}",
            "Live Forex orders remain blocked until explicit gates are armed.",
        ],
        notes="Retail Forex can be leveraged and risky; promote only after forward paper evidence.",
    )
