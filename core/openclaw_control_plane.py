"""OpenClaw control-plane ingress for every chat surface.

OpenClaw owns routing, policy visibility, deterministic short-circuit actions,
and audit. The existing MyOS engine remains the tool/runtime executor behind
this control plane.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from core import BASE_DIR


ADMIN_URL = "http://127.0.0.1:8310"
INGRESS_LOG = BASE_DIR / "data" / "logs" / "openclaw_ingress.jsonl"
_IDENTITY_CACHE: dict[str, Any] = {"ts": 0.0, "data": {}}
_CONTROL_PREFIX_RE = re.compile(
    r"^\s*!(?:fast|quick|haiku|local|free|api|sonnet|opus|chatgpt|gpt|corp)\b[:\s-]*",
    re.I,
)


def _now() -> float:
    return time.time()


def _preview(text: str, limit: int = 220) -> str:
    clean = " ".join(str(text or "").split())
    return clean[:limit]


_MISSED_ITEMS_RE = re.compile(
    r"\b(?:what(?:'d| did)? i miss|what have i missed|catch me up|catch up|missed this week|missed today|what's? up|what(?:'s| is)?\s+happening\s+this\s+week|what(?:'s| is)?\s+on\s+my\s+week)\b",
    re.I,
)


def _is_missed_items_request(text: str) -> bool:
    return bool(_MISSED_ITEMS_RE.search(str(text or "")))


def _build_missed_items_reply(user_id: str) -> str:
    parts: list[str] = []
    try:
        from core.context_brain import get_now_snapshot

        snap = get_now_snapshot(user_id) or {}
        cal = (snap.get("calendar") or [{}])[0] if isinstance(snap.get("calendar"), list) else {}
        today = str(cal.get("today") or "").strip()
        upcoming = str(snap.get("calendar_upcoming") or cal.get("upcoming") or "").strip()
        workos = snap.get("workos") or {}
        top_items = workos.get("top_items") if isinstance(workos, dict) else []
        if today and today != "unavailable":
            parts.append(f"Today looked busy: {today[:220]}.")
        if upcoming and upcoming != "unavailable" and upcoming != today:
            parts.append(f"This week also has: {upcoming[:260]}.")
        if isinstance(top_items, list) and top_items:
            names = [
                str(item.get("subject") or item.get("title") or item.get("name") or "").strip()
                for item in top_items[:4]
                if isinstance(item, dict)
            ]
            names = [name for name in names if name]
            if names:
                parts.append(f"WorkOS is pointing at: {', '.join(names[:4])}.")
    except Exception:
        pass
    try:
        from tools.littlebird_local import _reconcile_meetings_with_calendar

        meeting_blob = _reconcile_meetings_with_calendar(user_id)
        meeting_lines = [line.strip() for line in str(meeting_blob).splitlines() if line.strip() and "|" in line]
        if meeting_lines:
            notable: list[str] = []
            for line in meeting_lines[:3]:
                cols = [part.strip() for part in line.split("|")]
                if len(cols) >= 2:
                    subject = cols[1]
                    org = cols[2] if len(cols) > 2 else ""
                    item = " ".join(piece for piece in (subject, org) if piece).strip()
                    if item:
                        notable.append(item[:100])
            if notable:
                parts.append(f"LittleBird has notes for {', '.join(notable)}.")
                if len(meeting_lines) > len(notable):
                    parts.append(f"There are {len(meeting_lines) - len(notable)} more meetings I can summarize if you want the call notes.")
    except Exception:
        pass
    if not parts:
        return "I’m checking the live work context now and can give you the week briefing from calendar, notes, and work systems."
    lead = "You missed a real week of work."
    tail = " Tell me if you want the call notes or a deeper pass on any of those meetings."
    return lead + " " + " ".join(parts) + tail


def _route_text(text: str) -> str:
    clean = str(text or "").strip()
    previous = None
    while clean and clean != previous:
        previous = clean
        clean = _CONTROL_PREFIX_RE.sub("", clean).strip()
    return clean


def _append_log(row: dict[str, Any]) -> None:
    try:
        INGRESS_LOG.parent.mkdir(parents=True, exist_ok=True)
        with INGRESS_LOG.open("a") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def _request_json(url: str, timeout: float = 0.45) -> dict[str, Any]:
    req = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as response:
        text = response.read().decode("utf-8", errors="replace")
        return json.loads(text) if text else {}


def _local_llm_status() -> dict[str, Any]:
    try:
        data = _request_json("http://127.0.0.1:11434/api/version", timeout=0.45)
        return {
            "ok": True,
            "status": "online",
            "provider": "ollama",
            "version": data.get("version"),
            "probe": "GET /api/version",
        }
    except Exception as exc:
        return {
            "ok": False,
            "status": "offline",
            "provider": "ollama",
            "error": str(exc)[:180],
            "probe": "GET /api/version",
        }


def identity_snapshot(force: bool = False) -> dict[str, Any]:
    age = _now() - float(_IDENTITY_CACHE.get("ts") or 0.0)
    if not force and age < 30 and isinstance(_IDENTITY_CACHE.get("data"), dict):
        return dict(_IDENTITY_CACHE.get("data") or {})
    try:
        data = _request_json(f"{ADMIN_URL}/openclaw/identity")
        if isinstance(data, dict):
            _IDENTITY_CACHE["ts"] = _now()
            _IDENTITY_CACHE["data"] = data
            return dict(data)
    except Exception as exc:
        data = {"ok": False, "enabled": False, "error": str(exc)}
        _IDENTITY_CACHE["ts"] = _now()
        _IDENTITY_CACHE["data"] = data
        return data
    return {}


def record_ingress(
    *,
    user_id: str,
    text: str,
    frontend: str = "",
    thread_id: str | None = None,
    attachments=None,
) -> dict[str, Any]:
    identity = identity_snapshot()
    entry = {
        "id": f"oc-{int(_now())}-{uuid.uuid4().hex[:10]}",
        "ts": _now(),
        "event": "ingress",
        "control_plane": "openclaw",
        "openclaw_enabled": bool(identity.get("enabled", True)),
        "runtime_actor": identity.get("runtime_actor") or "openclaw",
        "approval_actor": identity.get("approval_actor") or "Operator",
        "user_id": user_id,
        "frontend": frontend,
        "thread_id": thread_id,
        "has_attachments": bool(attachments),
        "text_preview": _preview(text),
    }
    _append_log(entry)
    return entry


def record_outcome(ingress: dict[str, Any] | None, *, route: str, status: str, detail: str = "") -> None:
    if not ingress:
        return
    _append_log({
        "id": ingress.get("id"),
        "ts": _now(),
        "event": "outcome",
        "control_plane": "openclaw",
        "route": route,
        "status": status,
        "detail": _preview(detail, 500),
    })


def _safe_route_error(route: str, prompt: str, exc: Exception, *, frontend: str = "") -> dict[str, str]:
    from core.response_contract import finalize_chat_reply

    return {
        "route": route,
        "reply": finalize_chat_reply(
            prompt=prompt,
            reply=f"{route} failed before completion: {exc}",
            frontend=frontend,
            route=route,
        ),
    }


async def try_fast_reply(
    *,
    user_id: str,
    text: str,
    clean_text: str,
    frontend: str = "",
    thread_id: str | None = None,
    attachments=None,
) -> dict[str, str] | None:
    """Return a deterministic OpenClaw-routed reply or None to delegate."""
    if attachments:
        return None
    routed_text = _route_text(clean_text or text)

    # "Yes" needs thread context before generic intent detection.
    try:
        from core import conversation
        from core.revenue_followups import maybe_handle_revenue_yes

        history = conversation.get_thread_history(user_id, thread_id, limit=12) if thread_id else []
        reply = maybe_handle_revenue_yes(routed_text, history)
        if reply:
            return {"route": "openclaw:revenue_followup", "reply": reply}
    except Exception:
        pass

    try:
        from core.revenue_rundown import (
            build_revenue_action_text,
            build_revenue_discovery_text,
            build_revenue_rundown_text,
            is_revenue_action_request,
            is_revenue_discovery_request,
            is_revenue_rundown_request,
        )

        if is_revenue_discovery_request(routed_text):
            reply = await asyncio.to_thread(build_revenue_discovery_text, BASE_DIR, routed_text, True)
            return {"route": "openclaw:revenue_discovery", "reply": reply}
        if is_revenue_action_request(routed_text):
            reply = await asyncio.to_thread(build_revenue_action_text, BASE_DIR, routed_text, None)
            return {"route": "openclaw:revenue_action", "reply": reply}
        if is_revenue_rundown_request(routed_text):
            reply = await asyncio.to_thread(build_revenue_rundown_text, BASE_DIR, routed_text)
            return {"route": "openclaw:revenue_rundown", "reply": reply}
    except Exception as exc:
        return _safe_route_error("openclaw:revenue_error", routed_text, exc, frontend=frontend)

    try:
        from core import mailbox_intelligence

        if mailbox_intelligence.is_mailbox_question(routed_text):
            reply = await mailbox_intelligence.answer_mailbox_question(routed_text)
            if reply:
                return {"route": "openclaw:mailbox", "reply": reply}
    except Exception as exc:
        return _safe_route_error("openclaw:mailbox_error", routed_text, exc, frontend=frontend)

    try:
        from core.activity_rundown import build_activity_rundown_text, is_activity_rundown_request

        if is_activity_rundown_request(routed_text):
            reply = await asyncio.to_thread(build_activity_rundown_text, BASE_DIR, routed_text)
            return {"route": "openclaw:activity", "reply": reply}
    except Exception as exc:
        return _safe_route_error("openclaw:activity_error", routed_text, exc, frontend=frontend)

    try:
        from core.basic_questions import answer_basic_question, is_basic_current_question

        if is_basic_current_question(routed_text):
            reply = await answer_basic_question(routed_text)
            if reply:
                return {"route": "openclaw:basic_question", "reply": reply}
    except Exception as exc:
        return _safe_route_error("openclaw:basic_question_error", routed_text, exc, frontend=frontend)

    try:
        from core.local_compound import _fast_day_snapshot

        reply = await asyncio.to_thread(_fast_day_snapshot, routed_text)
        if reply:
            return {"route": "openclaw:day_snapshot", "reply": reply}
    except Exception as exc:
        return _safe_route_error("openclaw:day_snapshot_error", routed_text, exc, frontend=frontend)

    lowered = routed_text.lower()
    if any(token in lowered for token in ("micro trader", "microtrader", "paper trader", "day trader", "trading bot")):
        try:
            from core.micro_trader import backtest_text, run_once, status_text

            if any(token in lowered for token in ("backtest", "simulate", "simulation", "prove", "handful of days")):
                days = 5
                import re

                match = re.search(r"(\d+)\s*(?:day|days)", lowered)
                if match:
                    days = max(1, min(int(match.group(1)), 30))
                reply = await asyncio.to_thread(backtest_text, days)
                return {"route": "openclaw:micro_trader_backtest", "reply": reply}
            if any(token in lowered for token in ("run", "cycle", "tick", "fire")):
                await asyncio.to_thread(run_once)
            reply = await asyncio.to_thread(status_text)
            return {"route": "openclaw:micro_trader", "reply": reply}
        except Exception as exc:
            return _safe_route_error("openclaw:micro_trader_error", clean_text, exc, frontend=frontend)

    if any(token in lowered for token in ("kalshi", "bovada", "event bot", "event trader", "prediction market")):
        try:
            from core.kalshi_bot import run_once as kalshi_run_once
            from core.kalshi_bot import status_text as kalshi_status_text

            if any(token in lowered for token in ("run", "scan", "cycle", "fire", "check odds")):
                await asyncio.to_thread(kalshi_run_once)
            reply = await asyncio.to_thread(kalshi_status_text)
            return {"route": "openclaw:kalshi_bot", "reply": reply}
        except Exception as exc:
            return _safe_route_error("openclaw:kalshi_bot_error", clean_text, exc, frontend=frontend)

    if any(token in lowered for token in ("forex", "fx bot", "currency trading", "currency bot", "oanda")):
        try:
            from core.forex_bot import run_once as forex_run_once
            from core.forex_bot import status_text as forex_status_text

            if any(token in lowered for token in ("run", "scan", "cycle", "fire")):
                await asyncio.to_thread(forex_run_once)
            reply = await asyncio.to_thread(forex_status_text)
            return {"route": "openclaw:forex_bot", "reply": reply}
        except Exception as exc:
            return _safe_route_error("openclaw:forex_bot_error", clean_text, exc, frontend=frontend)

    return None


def control_status() -> dict[str, Any]:
    identity = identity_snapshot(force=True)
    provider_lanes: dict[str, Any] = {}
    try:
        from core.codex_cli_bridge import provider_lane_status

        provider_lanes = provider_lane_status()
    except Exception as exc:
        provider_lanes = {"error": str(exc)}
    recent = []
    try:
        if INGRESS_LOG.exists():
            lines = INGRESS_LOG.read_text(errors="replace").splitlines()[-20:]
            for line in lines:
                try:
                    recent.append(json.loads(line))
                except Exception:
                    pass
    except Exception:
        recent = []
    openclaw_status = "fallback" if identity.get("enabled") is False else "online"
    return {
        "ok": True,
        "control_plane": "openclaw",
        "openclaw": {
            "ok": True,
            "status": openclaw_status,
            "runtime_actor": identity.get("runtime_actor") or "openclaw",
            "approval_actor": identity.get("approval_actor") or "Operator",
        },
        "identity": identity,
        "provider_lanes": provider_lanes,
        "local_llm": _local_llm_status(),
        "channel_policy": {
            "main_path": "channel -> Engine.handle_message -> OpenClaw control plane -> local/deterministic/provider route",
            "direct_channel_bypass_env": "CLAUDE_STACK_ALLOW_DIRECT_AGENT_CHANNEL_BYPASS",
            "direct_channel_bypass_default": False,
        },
        "ingress_log": str(INGRESS_LOG),
        "recent": recent[-12:],
    }
