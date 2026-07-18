from __future__ import annotations

import hashlib
import json
import os
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(os.environ.get("CLAUDE_STACK_DIR", Path(__file__).resolve().parents[1]))
APPROVAL_LEDGER_PATH = ROOT / "data" / "runtime" / "myos_approval_ledger.json"
APPROVAL_AUDIT_PATH = ROOT / "data" / "logs" / "myos_approval_ledger.jsonl"

TERMINAL_STATUSES = {"executed", "rejected", "failed"}
ACTIVE_STATUSES = {"pending", "approved", "policy_authorized", "blocked"}
IDENTITY_TIED_PIPELINES = {"Operator", "alex_personal", "branda", "CompanyA", "workos"}
SPEND_KINDS = {"spend", "purchase", "ad_spend", "booking_purchase", "contract_signature"}
MONEY_SPEND_KINDS = SPEND_KINDS - {"contract_signature"}
COMMUNICATION_KINDS = {
    "outbound_email",
    "email",
    "reply",
    "dm",
    "social_post",
    "proposal",
    "calendar_invite",
    "listing_publish",
    "communication",
}


class ApprovalLedgerError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _action_amount_usd(action: dict[str, Any]) -> float | None:
    candidates = [action]
    tool_input = action.get("tool_input")
    if isinstance(tool_input, dict):
        candidates.append(tool_input)
    for source in candidates:
        for key in ("amount_usd", "cost_usd", "budget_usd", "amount", "price_usd"):
            if key not in source:
                continue
            try:
                amount = float(source.get(key))
            except (TypeError, ValueError):
                continue
            if amount >= 0:
                return amount
    return None


def stable_action_id(
    *,
    kind: str,
    pipeline: str = "",
    destination: str = "",
    subject: str = "",
    body: str = "",
    tool_name: str = "",
    tool_input: dict[str, Any] | None = None,
) -> str:
    seed = {
        "kind": kind,
        "pipeline": pipeline.lower().strip(),
        "destination": destination.lower().strip(),
        "subject": subject.strip(),
        "body": body.strip(),
        "tool_name": tool_name.strip(),
        "tool_input": tool_input or {},
    }
    return hashlib.sha256(_canonical_json(seed).encode("utf-8")).hexdigest()[:20]


def _empty_ledger() -> dict[str, Any]:
    return {"version": 1, "actions": {}}


def load_ledger() -> dict[str, Any]:
    try:
        if APPROVAL_LEDGER_PATH.exists():
            data = json.loads(APPROVAL_LEDGER_PATH.read_text())
            if isinstance(data, dict):
                data.setdefault("version", 1)
                data.setdefault("actions", {})
                if isinstance(data["actions"], dict):
                    return data
    except Exception:
        pass
    return _empty_ledger()


def save_ledger(ledger: dict[str, Any]) -> None:
    APPROVAL_LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    APPROVAL_LEDGER_PATH.write_text(json.dumps(ledger, indent=2, sort_keys=True) + "\n")


def audit_event(event: str, action: dict[str, Any], *, actor: str = "system", detail: dict[str, Any] | None = None) -> None:
    APPROVAL_AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": _now(),
        "event": event,
        "actor": actor,
        "action_id": action.get("id"),
        "status": action.get("status"),
        "kind": action.get("kind"),
        "pipeline": action.get("pipeline"),
        "tool_name": action.get("tool_name"),
        "detail": detail or {},
    }
    with APPROVAL_AUDIT_PATH.open("a") as handle:
        handle.write(json.dumps(row, sort_keys=True, default=str) + "\n")


def classify_action_policy(action: dict[str, Any]) -> dict[str, Any]:
    kind = str(action.get("kind") or "").lower().strip()
    pipeline = str(action.get("pipeline") or "").lower().strip()
    tool_name = str(action.get("tool_name") or "").strip()
    identity_tied = bool(
        action.get("identity_tied")
        or action.get("sender_identity_tied")
        or action.get("could_tie_back_to_alex")
        or pipeline in IDENTITY_TIED_PIPELINES
    )
    approval_required = bool(action.get("approval_required")) if "approval_required" in action else False
    reason = str(action.get("approval_reason") or "").strip()

    blocked_reason = ""
    if kind == "contract_signature":
        approval_required = True
        reason = reason or "contracts always require express approval"
    elif kind in MONEY_SPEND_KINDS:
        try:
            from core.revenue_autonomy import autonomous_revenue_enabled, spend_decision

            amount_usd = _action_amount_usd(action)
            if not autonomous_revenue_enabled(ROOT):
                approval_required = True
                reason = reason or "spend requires express approval unless bounded revenue autonomy is enabled"
            elif amount_usd is None:
                approval_required = True
                blocked_reason = "spend blocked: missing amount_usd for hard weekly budget gate"
                reason = blocked_reason
            else:
                decision = spend_decision(amount_usd, base=ROOT)
                if decision.get("allowed"):
                    approval_required = False
                    reason = (
                        reason
                        or f"spend is within hard weekly cap: ${float(amount_usd):.2f} "
                        f"requested, ${float(decision.get('cap_week_usd') or 0):.2f} cap"
                    )
                else:
                    approval_required = True
                    blocked_reason = f"spend blocked: {decision.get('reason')}"
                    reason = blocked_reason
        except Exception as exc:
            approval_required = True
            blocked_reason = f"spend blocked: budget gate unavailable: {exc}"
            reason = blocked_reason
    elif identity_tied and (kind in COMMUNICATION_KINDS or tool_name):
        approval_required = True
        reason = reason or "identity-tied communication or workspace write requires express approval"
    elif action.get("human_confirm_required"):
        approval_required = True
        reason = reason or "tool requested human confirmation"

    return {
        "approval_required": approval_required,
        "approval_reason": reason or ("policy allows autonomous action" if not approval_required else "approval required"),
        "identity_tied": identity_tied,
        "blocked_reason": blocked_reason,
    }


def _normalize_action(action: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(action, dict):
        raise ApprovalLedgerError("approval action must be an object")
    normalized = deepcopy(action)
    kind = str(normalized.get("kind") or "tool_call").strip()
    pipeline = str(normalized.get("pipeline") or "").lower().strip()
    tool_name = str(normalized.get("tool_name") or "").strip()
    tool_input = normalized.get("tool_input") if isinstance(normalized.get("tool_input"), dict) else {}
    destination = str(normalized.get("destination") or tool_input.get("to") or tool_input.get("email") or "").strip()
    subject = str(normalized.get("subject") or tool_input.get("subject") or "").strip()
    body = str(normalized.get("body") or normalized.get("body_preview") or tool_input.get("body") or "").strip()
    policy = classify_action_policy(normalized)

    normalized["kind"] = kind
    normalized["pipeline"] = pipeline
    normalized["tool_name"] = tool_name
    normalized["tool_input"] = tool_input
    normalized["destination"] = destination
    normalized["subject"] = subject
    normalized["approval_required"] = bool(policy["approval_required"])
    normalized["approval_reason"] = policy["approval_reason"]
    normalized["identity_tied"] = bool(policy["identity_tied"])
    if policy.get("blocked_reason"):
        normalized.setdefault("blocked_reason", policy["blocked_reason"])
    normalized.setdefault("risk", "medium" if normalized["approval_required"] else "low")
    normalized.setdefault("idempotency_key", "")
    normalized.setdefault("created_at", _now())
    normalized["updated_at"] = _now()

    if not normalized.get("id"):
        normalized["id"] = stable_action_id(
            kind=kind,
            pipeline=pipeline,
            destination=destination,
            subject=subject,
            body=body,
            tool_name=tool_name,
            tool_input=tool_input,
        )

    if not normalized.get("body_preview") and body:
        normalized["body_preview"] = body[:500]

    if not normalized.get("status"):
        if normalized.get("blocked_reason"):
            normalized["status"] = "blocked"
        elif normalized["approval_required"]:
            normalized["status"] = "pending"
        else:
            normalized["status"] = "policy_authorized"

    if normalized["status"] not in ACTIVE_STATUSES | TERMINAL_STATUSES:
        raise ApprovalLedgerError(f"invalid approval status: {normalized['status']}")

    return normalized


def queue_action(action: dict[str, Any], *, actor: str = "system") -> dict[str, Any]:
    normalized = _normalize_action(action)
    ledger = load_ledger()
    actions = ledger.setdefault("actions", {})
    existing = actions.get(normalized["id"])
    if isinstance(existing, dict) and existing.get("status") in TERMINAL_STATUSES:
        return deepcopy(existing)
    if isinstance(existing, dict) and existing.get("status") == "approved":
        merged = {**normalized, **existing, "updated_at": _now()}
    elif isinstance(existing, dict):
        merged = {**existing, **normalized, "created_at": existing.get("created_at", normalized["created_at"])}
    else:
        merged = normalized
    merged["updated_at"] = _now()
    actions[merged["id"]] = merged
    save_ledger(ledger)
    audit_event("queued", merged, actor=actor)
    return deepcopy(merged)


def list_actions(status: str | None = None, pipeline: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    status_filter = str(status or "").strip().lower()
    pipeline_filter = str(pipeline or "").strip().lower()
    rows = []
    for action in load_ledger().get("actions", {}).values():
        if not isinstance(action, dict):
            continue
        if status_filter and str(action.get("status") or "").lower() != status_filter:
            continue
        if pipeline_filter and str(action.get("pipeline") or "").lower() != pipeline_filter:
            continue
        rows.append(deepcopy(action))
    rows.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return rows[: max(0, int(limit))]


def get_action(action_id: str) -> dict[str, Any] | None:
    action = load_ledger().get("actions", {}).get(action_id)
    return deepcopy(action) if isinstance(action, dict) else None


def _update_action(action_id: str, updater, *, actor: str, event: str, detail: dict[str, Any] | None = None) -> dict[str, Any]:
    ledger = load_ledger()
    action = ledger.setdefault("actions", {}).get(action_id)
    if not isinstance(action, dict):
        raise ApprovalLedgerError(f"unknown approval action: {action_id}")
    updater(action)
    action["updated_at"] = _now()
    save_ledger(ledger)
    audit_event(event, action, actor=actor, detail=detail)
    return deepcopy(action)


def approve_action(action_id: str, *, actor: str = "Operator", note: str = "") -> dict[str, Any]:
    def updater(action: dict[str, Any]) -> None:
        if action.get("status") in TERMINAL_STATUSES:
            raise ApprovalLedgerError(f"cannot approve terminal action: {action.get('status')}")
        action["status"] = "approved"
        action["approved_by"] = actor
        action["approval_note"] = note
        action["approved_at"] = _now()

    return _update_action(action_id, updater, actor=actor, event="approved")


def reject_action(action_id: str, *, actor: str = "Operator", note: str = "") -> dict[str, Any]:
    def updater(action: dict[str, Any]) -> None:
        if action.get("status") in TERMINAL_STATUSES:
            raise ApprovalLedgerError(f"cannot reject terminal action: {action.get('status')}")
        action["status"] = "rejected"
        action["rejected_by"] = actor
        action["rejection_note"] = note
        action["rejected_at"] = _now()

    return _update_action(action_id, updater, actor=actor, event="rejected")


def mark_executed(action_id: str, result: Any, *, actor: str = "system") -> dict[str, Any]:
    def updater(action: dict[str, Any]) -> None:
        action["status"] = "executed"
        action["executed_by"] = actor
        action["executed_at"] = _now()
        action["execution_result"] = result

    return _update_action(action_id, updater, actor=actor, event="executed")


def mark_failed(action_id: str, result: Any, *, actor: str = "system") -> dict[str, Any]:
    def updater(action: dict[str, Any]) -> None:
        action["status"] = "failed"
        action["failed_by"] = actor
        action["failed_at"] = _now()
        action["failure_result"] = result

    return _update_action(action_id, updater, actor=actor, event="failed")


def annotate_action(
    action_id: str,
    fields: dict[str, Any],
    *,
    actor: str = "system",
    event: str = "annotated",
) -> dict[str, Any]:
    safe_fields = fields if isinstance(fields, dict) else {}

    def updater(action: dict[str, Any]) -> None:
        for key, value in safe_fields.items():
            if key in {"id", "created_at"}:
                continue
            action[key] = value

    return _update_action(action_id, updater, actor=actor, event=event, detail={"fields": sorted(safe_fields)})


def authorize_gateway_action(tool_name: str, approval_id: str, idempotency_key: str = "") -> tuple[bool, dict[str, Any] | str]:
    ledger = load_ledger()
    action = ledger.get("actions", {}).get(approval_id)
    if not isinstance(action, dict):
        return False, "approval_id not found"
    if action.get("status") not in {"approved", "policy_authorized"}:
        return False, f"approval action status is {action.get('status')}"
    expected_tool = str(action.get("tool_name") or "").strip()
    if expected_tool and expected_tool != tool_name:
        return False, f"approval action is for {expected_tool}, not {tool_name}"
    expected_key = str(action.get("idempotency_key") or "").strip()
    if expected_key and idempotency_key and expected_key != idempotency_key:
        return False, "idempotency_key does not match approval action"
    if str(action.get("kind") or "").lower().strip() in MONEY_SPEND_KINDS:
        amount_usd = _action_amount_usd(action)
        if amount_usd is None:
            return False, "spend blocked: missing amount_usd for hard weekly budget gate"
        if not action.get("spend_reserved_at"):
            try:
                from core.revenue_autonomy import record_spend

                result = record_spend(
                    amount_usd,
                    purpose=str(action.get("subject") or action.get("kind") or "revenue spend"),
                    base=ROOT,
                    metadata={"approval_id": approval_id, "tool_name": tool_name, "pipeline": action.get("pipeline")},
                )
            except Exception as exc:
                return False, f"spend blocked: budget gate unavailable: {exc}"
            if not result.get("allowed"):
                return False, str(result.get("reason") or "weekly spend cap exceeded")
            action["spend_reserved_at"] = _now()
            action["spend_amount_usd"] = float(amount_usd)
            action["spend_budget_detail"] = result
            action["updated_at"] = _now()
            save_ledger(ledger)
    if idempotency_key and not expected_key:
        action["idempotency_key"] = idempotency_key
        action["updated_at"] = _now()
        save_ledger(ledger)
    return True, deepcopy(action)
