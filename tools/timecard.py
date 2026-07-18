"""QuickBooks Time (TSheets) timecard tool for the work-GPT gateway.

Read / fill / submit the weekly timecard. All browser work goes through the
existing CDP driver (tools/qbo_timecard.js) against the dedicated, already
signed-in Chrome profile on :18823 (~/.chrome-cdp-profile-timeclock) — there is
no QBO API token; the live Intuit MFA session in that profile IS the auth.

Submit is a two-stage trust ramp: until one real submit has been verified
(data/work/timecard_verified.json), a submit REQUIRES explicit approval
(preview -> approve -> submit). After the flag is set, later submits may run on
prompt. We NEVER blind-submit before the flag, and we NEVER self-heal past
Intuit MFA — a down session surfaces a clear pointer at the login script.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from datetime import date, datetime, timedelta
from pathlib import Path

from tools.base import BaseTool, make_tool_def

ROOT = Path(__file__).resolve().parent.parent
DRIVER = ROOT / "tools" / "qbo_timecard.js"
STATE_PATH = ROOT / "data" / "focus" / "qbo_timecard.json"
VERIFIED_FLAG = ROOT / "data" / "work" / "timecard_verified.json"
LOGIN_SCRIPT = "scripts/login_capture_timeclock.sh"

CDP_PORT = 18823
CDP_URL = f"http://127.0.0.1:{CDP_PORT}"
_NODE_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday"]
_ABBR = {"monday": "Mon", "tuesday": "Tue", "wednesday": "Wed", "thursday": "Thu", "friday": "Fri"}

_MFA_DOWN = (
    "MFA session down: the QuickBooks Time Chrome profile on :{port} isn't reachable "
    "or is logged out. This needs a one-time human sign-in with Operator's Intuit MFA — "
    "run `bash {script}`, sign in (QuickBooks TIME, company 'TGPL SOLUTIONS INC'), land "
    "on the timesheet, and leave the window open. I can't self-heal past Intuit MFA."
).format(port=CDP_PORT, script=LOGIN_SCRIPT)


def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {}


def _verified() -> bool:
    try:
        return bool(json.loads(VERIFIED_FLAG.read_text()).get("verified"))
    except Exception:
        return False


def _set_verified(detail: str, period: str) -> None:
    try:
        VERIFIED_FLAG.parent.mkdir(parents=True, exist_ok=True)
        VERIFIED_FLAG.write_text(
            json.dumps(
                {"verified": True, "at": datetime.now().isoformat(timespec="seconds"),
                 "period": period, "detail": detail},
                indent=2,
            )
            + "\n"
        )
    except Exception:
        pass


def _week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())  # Monday


def _target_week(week_arg: str | None) -> date:
    if week_arg:
        try:
            return _week_start(datetime.strptime(str(week_arg).strip(), "%Y-%m-%d").date())
        except Exception:
            pass
    return _week_start(date.today())


def _plan_lines(state: dict, ws: date) -> tuple[list[str], float, str]:
    sched = state.get("standard_schedule") or {}
    lines = []
    total = 0.0
    for i, wd in enumerate(_WEEKDAYS):
        hrs = float(sched.get(wd) or 0)
        if hrs <= 0:
            continue
        d = ws + timedelta(days=i)
        total += hrs
        lines.append(f"  {_ABBR[wd]} {d.isoformat()}: {hrs:g}h")
    period = f"{ws.isoformat()}_{(ws + timedelta(days=6)).isoformat()}"
    return lines, total, period


def _cdp_up() -> bool:
    try:
        import httpx

        r = httpx.get(f"{CDP_URL}/json/version", timeout=3.0)
        return r.status_code == 200
    except Exception:
        pass
    try:
        import urllib.request

        with urllib.request.urlopen(f"{CDP_URL}/json/version", timeout=3.0) as resp:
            return resp.status == 200
    except Exception:
        return False


def _looks_mfa_down(output: str) -> bool:
    low = (output or "").lower()
    return (
        "login required" in low
        or "login/mfa" in low
        or "cannot connect" in low
        or "no_cdp" in low
        or "sign in" in low
    )


async def _run_driver(flags: list[str], week_arg: str | None) -> tuple[int, str]:
    args = ["node", str(DRIVER), *flags]
    if week_arg:
        args.append(str(week_arg).strip())
    env = dict(os.environ)
    env["QBO_CDP"] = CDP_URL
    env["PATH"] = _NODE_PATH + ":" + env.get("PATH", "")

    def _run() -> tuple[int, str]:
        p = subprocess.run(
            args, cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=200
        )
        return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()

    try:
        return await asyncio.to_thread(_run)
    except subprocess.TimeoutExpired:
        return 1, "error: timecard driver timed out (>200s)"
    except Exception as e:
        return 1, f"error: timecard driver failed to launch: {e}"


class TimecardTool(BaseTool):
    name = "timecard"
    description = "QuickBooks Time (TSheets) weekly timecard: read state, fill standard hours, submit (gated)."

    def get_definitions(self) -> list[dict]:
        return [
            make_tool_def(
                "timecard",
                "QuickBooks Time (TSheets) weekly timecard for Operator's CompanyA hours. "
                "action='read' shows the current week's plan, live entries, submit-readiness, and "
                "whether the first real submit has been verified yet. action='fill' stages the "
                "standard 8h/weekday (worked-only, no PTO) onto the timesheet WITHOUT submitting. "
                "action='submit' submits: until one real submit has been verified it REQUIRES "
                "explicit approval (call once to get the preview, then re-call with "
                "explicit_approval=true to actually submit); after that it may submit on prompt. "
                "Depends on Operator's live Intuit MFA session in the dedicated Chrome profile on :18823 — "
                "if that's down the tool says so and points at the login script (never self-heals MFA).",
                {
                    "action": {
                        "type": "string",
                        "enum": ["read", "fill", "submit"],
                        "description": "read = state + live entries; fill = stage standard hours (no submit); submit = submit (gated).",
                    },
                    "week": {
                        "type": "string",
                        "description": "Optional target week as YYYY-MM-DD (any day in it); defaults to the current week.",
                    },
                    "live": {
                        "type": "boolean",
                        "description": "read only: also open the live timesheet for a readback (default true). false = cached state only.",
                    },
                    "explicit_approval": {
                        "type": "boolean",
                        "description": "submit only: Operator's explicit go-ahead. Required for the FIRST real submit (before it's verified).",
                    },
                    "format": {
                        "type": "string",
                        "enum": ["text", "json"],
                        "description": "Output format (default text).",
                    },
                },
                ["action"],
            )
        ]

    async def handle(self, tool_name: str, tool_input: dict) -> str:
        ti = tool_input or {}
        action = str(ti.get("action") or "").strip().lower()
        week = ti.get("week")
        fmt = str(ti.get("format") or "text").strip().lower()
        if action == "read":
            return await self._read(week, ti.get("live"), fmt)
        if action == "fill":
            return await self._fill(week)
        if action == "submit":
            approved = bool(ti.get("explicit_approval") or ti.get("approve"))
            return await self._submit(week, approved)
        return "Error: action must be one of read|fill|submit."

    async def _read(self, week, live, fmt: str) -> str:
        state = _load_state()
        ws = _target_week(week)
        lines, total, period = _plan_lines(state, ws)
        verified = _verified()
        ready = (
            state.get("real_submit_enabled")
            and state.get("dry_run_approved")
            and state.get("status") == "ready_for_real_submit"
        )
        submitted = (state.get("submitted_periods") or {}).get(period, {})

        do_live = live is None or bool(live)
        cdp_ok = _cdp_up() if do_live else None
        live_block = ""
        mfa_down = False
        if do_live:
            if not cdp_ok:
                mfa_down = True
                live_block = _MFA_DOWN
            else:
                rc, out = await _run_driver(["--dry", "--no-imessage", "--no-wait"], week)
                if _looks_mfa_down(out):
                    mfa_down = True
                    live_block = _MFA_DOWN + "\n\n--- driver ---\n" + out
                else:
                    live_block = out

        if fmt == "json":
            return json.dumps(
                {
                    "action": "read",
                    "week": period,
                    "status": state.get("status"),
                    "standard_schedule": state.get("standard_schedule"),
                    "plan_total_hours": total,
                    "real_submit_ready": bool(ready),
                    "first_submit_verified": verified,
                    "this_period_state": submitted.get("status"),
                    "defaults": state.get("defaults"),
                    "mfa_session_down": mfa_down,
                    "live_readback": live_block or None,
                },
                indent=2,
                default=str,
            )

        parts = [
            f"QBO Time timecard — week {ws.isoformat()} to {(ws + timedelta(days=6)).isoformat()}",
            f"driver status: {state.get('status')}   this-week state: {submitted.get('status') or 'not staged'}",
            f"standard plan ({total:g}h total, worked-only, no PTO):",
            *(lines or ["  (no weekday standard hours configured)"]),
            f"real-submit ready (driver gate): {'yes' if ready else 'no'}",
            f"first real submit verified: {'YES — submits may run on prompt' if verified else 'NO — first submit needs explicit approval'}",
        ]
        if live_block:
            parts.append("\nlive readback:\n" + live_block)
        return "\n".join(parts)

    async def _fill(self, week) -> str:
        if not _cdp_up():
            return _MFA_DOWN
        rc, out = await _run_driver(["--fill-only", "--no-wait", "--no-imessage"], week)
        if _looks_mfa_down(out):
            return _MFA_DOWN + "\n\n--- driver ---\n" + out
        return "timecard fill (standard hours staged, NOT submitted):\n" + (out or "(no output)")

    async def _submit(self, week, approved: bool) -> str:
        verified = _verified()
        ws = _target_week(week)
        lines, total, period = _plan_lines(_load_state(), ws)

        if not _cdp_up():
            return _MFA_DOWN

        # Two-stage trust ramp: before the verified flag exists, a submit is
        # preview-only unless Operator has explicitly approved this call.
        if not verified and not approved:
            rc, out = await _run_driver(["--dry", "--no-imessage", "--no-wait"], week)
            if _looks_mfa_down(out):
                return _MFA_DOWN + "\n\n--- driver ---\n" + out
            return (
                "APPROVAL REQUIRED — first real submit is NOT yet verified, so this is a PREVIEW only "
                "(nothing was submitted). Review the plan below, then re-call timecard action=submit "
                "with explicit_approval=true to actually submit.\n\n"
                f"week {period} — standard plan {total:g}h:\n"
                + "\n".join(lines or ["  (no weekday standard hours)"])
                + "\n\n--- live dry-run readback ---\n"
                + (out or "(no output)")
            )

        # verified OR explicitly approved -> real submit. --no-wait skips the
        # driver's 30-min iMessage override loop (approval already happened here);
        # --no-imessage keeps the notification to the GPT's report to Operator.
        rc, out = await _run_driver(["--submit", "--no-wait", "--no-imessage"], week)
        if _looks_mfa_down(out):
            return _MFA_DOWN + "\n\n--- driver ---\n" + out

        low = (out or "").lower()
        submitted_ok = "submitted and readback matched" in low
        if submitted_ok:
            if not verified:
                _set_verified(f"submit ok for {period}", period)
            tag = (
                "SUBMITTED (verified — future submits may run on prompt)."
                if not verified
                else "SUBMITTED."
            )
            return f"timecard {tag}\n" + out
        if "blocked" in low:
            return "timecard submit BLOCKED by the driver's own gate (not submitted):\n" + out
        # e.g. "verify_needed", "already submitted", "held", conflicts
        return "timecard submit — driver output (review; NOT confirmed as a clean submit):\n" + (out or "(no output)")
