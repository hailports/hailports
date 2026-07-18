"""Shared corporate GPT draft lane for CompanyA inbox and Zoom work.

Draft replies via the CompanyA Enterprise Codex subscription instead of local,
personal, or metered model lanes. Why this is the right architecture:
  - QUALITY: corp Codex >> the local qwen 7B.
  - $0 MARGINAL: flat-rate corporate subscription, not a metered API.
  - BOX RELIEF: inference runs on OpenAI's compute, NOT the local box — so it removes the root cause of
    both the 7B slop (box too starved to generate cleanly) and the 14B deep-QA hang (cold-load under load).

Protections:
  - Own daily cap (GPT_DRAFT_DAILY_CAP, default 120) via its own counter file.
  - Bypasses agent_run.sh's shared cap for the call (our counter is the real limit).
  - FAIL-CLOSED: any miss returns '' so callers use deterministic output or skip;
    CompanyA content never spills onto local, personal, or free providers.
Kill: DRAFT_GPT=0 (global).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime

from core import BASE_DIR

DAILY_CAP = int(os.environ.get("GPT_DRAFT_DAILY_CAP", "120"))


def enabled() -> bool:
    return os.environ.get("DRAFT_GPT", "1").strip().lower() not in {"0", "false", "no", "off"}


def clean(out: str) -> str:
    """Strip codex CLI noise + markdown so only the reply prose survives; normalize curly quotes/em-dash
    to the plain forms Operator pastes."""
    lines = []
    for ln in (out or "").splitlines():
        s = ln.strip()
        if not s:
            continue
        low = s.lower()
        if low in {"codex", "ok"} or low.startswith("tokens used") or re.fullmatch(r"[\d,]+", s):
            continue
        if s.startswith("```"):
            continue
        lines.append(s)
    txt = "\n".join(lines).strip().strip('"').strip()
    return (txt.replace("’", "'").replace("‘", "'")
               .replace("“", '"').replace("”", '"').replace("—", "--"))


def _count_file() -> "os.PathLike":
    day = datetime.now().strftime("%Y%m%d")
    return BASE_DIR / "data" / "hustle" / f"gpt_draft_{day}.count"


_LOG = BASE_DIR / "data" / "hustle" / "logs" / "gpt_draft_lane.jsonl"


def _log(outcome: str, detail: str = "", src: str = "") -> None:
    """Append one outcome line so a monitor can watch health WITHOUT alerting per-event. Outcomes:
    ok | fallback:<why> (disabled/capped/empty/error). The monitor alerts ONLY when the lane is failing
    AND callers report their local fallback ALSO failed — last resort, per the self-heal doctrine."""
    try:
        _LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(_LOG, "a") as fh:
            fh.write(json.dumps({"at": datetime.now().isoformat(timespec="seconds"),
                                 "outcome": outcome, "detail": detail[:120], "src": src}) + "\n")
    except Exception:
        pass


def draft(prompt: str, timeout: int = 90, src: str = "") -> str:
    """Return a corporate Codex draft for `prompt`, or ''. Never raises."""
    if not enabled():
        _log("fallback:disabled", src=src)
        return ""
    cf = _count_file()
    try:
        n = int((open(cf).read().strip() or "0"))
    except Exception:
        n = 0
    if n >= DAILY_CAP:
        _log("fallback:capped", f"{n}/{DAILY_CAP}", src)
        return ""
    try:
        env = dict(os.environ, AGENT_RUNNER="codex", AGENT_BRAND="CompanyA",
                   AGENT_TENANT="corp", AGENT_RUN_TIMEOUT=str(timeout),
                   AGENT_RUN_DAILY_CAP="1000000")  # our counter is the real limit, not the shared cap
        r = subprocess.run(["bash", str(BASE_DIR / "scripts" / "agent_run.sh"), prompt, "1", str(BASE_DIR)],
                           capture_output=True, text=True, timeout=timeout + 25, env=env)
        out = (r.stdout or "").strip()
    except Exception as e:
        _log("fallback:error", str(e), src)
        return ""
    if not out:
        _log("fallback:empty", src=src)
        return ""
    try:
        with open(cf, "w") as fh:
            fh.write(str(n + 1))
    except Exception:
        pass
    _log("ok", f"chars={len(out)}", src)
    return clean(out)


def health(window: int = 40) -> dict:
    """Read the last `window` outcomes -> {ok, fallback, total, ok_rate, capped, degraded}. `degraded`
    True when the lane is producing NOTHING but non-cap fallbacks (codex genuinely down) — the signal a
    monitor escalates on, and ONLY as a last resort (callers still self-heal to local first)."""
    import json as _j
    rows = []
    try:
        with open(_LOG) as fh:
            rows = [_j.loads(x) for x in fh.read().splitlines() if x.strip()][-window:]
    except Exception:
        return {"ok": 0, "fallback": 0, "total": 0, "ok_rate": None, "capped": 0, "degraded": False}
    ok = sum(1 for r in rows if r.get("outcome") == "ok")
    capped = sum(1 for r in rows if r.get("outcome") == "fallback:capped")
    errs = sum(1 for r in rows if str(r.get("outcome", "")).startswith("fallback:") and r.get("outcome") != "fallback:capped")
    total = len(rows)
    # degraded = enough recent attempts, none succeeded, and it's NOT just the daily cap (cap = healthy,
    # expected). i.e. codex is actually failing.
    degraded = total >= 8 and ok == 0 and errs >= 6
    return {"ok": ok, "fallback": errs + capped, "total": total,
            "ok_rate": (ok / total) if total else None, "capped": capped, "degraded": degraded}
