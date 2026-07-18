#!/usr/bin/env python3
"""Daily warm-up ramp for the relentless outbound cap.

Climbs RELENTLESS_DAILY_CAP in .env aggressively (+50%/day) as long as yesterday's sends were
healthy (low immediate-failure rate); HOLDS/CUTS the moment trouble shows. This is how we scale
WAY past 20/day without torching scannerapp.dev's reputation — a fresh domain that suddenly blasts
hundreds reads as a spammer and gets blacklisted, which would silently kill inbox placement.

  50 (floor) -> 75 -> 112 -> 168 -> 252 -> 300 (ceiling), one step per healthy day.

Run daily via launchd, before the send window opens.
"""
import json
import os
import re
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(os.environ.get("CLAUDE_STACK_DIR", Path(__file__).resolve().parents[1]))
ENV = ROOT / ".env"
LOG = ROOT / "data" / "hustle" / "relentless_campaign.jsonl"
RAMP_LOG = ROOT / "data" / "hustle" / "relentless_ramp.jsonl"

FLOOR, CEIL = 50, 300          # raise CEIL once a dedicated IP / multi-domain is in place
GROWTH = 1.5                   # +50% per healthy day
FAIL_LIMIT = 0.15              # >15% immediate send failures => cut, don't climb
RECENT_HRS = 36


def _get_cap() -> int:
    if ENV.exists():
        for line in ENV.read_text(errors="ignore").splitlines():
            if line.startswith("RELENTLESS_DAILY_CAP="):
                try:
                    return int(line.split("=", 1)[1].strip().strip("'\""))
                except Exception:
                    pass
    return FLOOR


def _set_cap(n: int) -> None:
    txt = ENV.read_text() if ENV.exists() else ""
    repl = f"RELENTLESS_DAILY_CAP={n}"
    if re.search(r"(?m)^RELENTLESS_DAILY_CAP=", txt):
        txt = re.sub(r"(?m)^RELENTLESS_DAILY_CAP=.*$", repl, txt)
    else:
        txt += ("\n" if txt and not txt.endswith("\n") else "") + repl + "\n"
    os.chmod(ENV, 0o600)
    ENV.write_text(txt)
    os.chmod(ENV, 0o400)


def _recent_health():
    """Immediate send health from the campaign log over the last RECENT_HRS. None if no send data."""
    if not LOG.exists():
        return None
    cutoff = datetime.now(timezone.utc).timestamp() - RECENT_HRS * 3600
    sent = fail = 0
    for line in LOG.read_text(errors="ignore").splitlines():
        try:
            d = json.loads(line)
        except Exception:
            continue
        try:
            t = datetime.fromisoformat(str(d.get("ts", "")).replace("Z", "+00:00")).timestamp()
        except Exception:
            t = 0
        if t < cutoff:
            continue
        action = str(d.get("action") or "").lower()
        is_send = action in ("sent", "send", "failed", "send_failed") or "track" in d or "sent" in d
        if not is_send:
            continue
        ok = bool(d.get("track")) or d.get("sent") is True or action == "sent"
        if ok:
            sent += 1
            continue
        # Pre-send FILTER refusals (enterprise-block / suppression / bounce-guard /
        # dedup) are NOT deliverability failures — they mean we correctly skipped a
        # bad recipient BEFORE any provider send, so they say nothing about the
        # sending domain's reputation. Counting them as fails crashed the warm-up to
        # the floor on 2026-06-18 (2960 enterprise-block refusals read as 100% fail,
        # 168 -> 50). Only real transport/provider failures should cut the cap.
        reason = str(d.get("reason") or d.get("error") or "").lower()
        if any(k in reason for k in (
            "engine refused", "gate", "suppress", "bounce", "blocked", "dedup", "already")):
            continue
        if action in ("failed", "send_failed") or d.get("sent") is False:
            fail += 1
    total = sent + fail
    if total == 0:
        return None
    return {"sent": sent, "fail": fail, "fail_rate": fail / total}


def main() -> None:
    cur = _get_cap()
    h = _recent_health()
    if h is None:
        new, reason = max(cur, FLOOR), "no send data yet -> hold at floor"
    elif h["fail_rate"] > FAIL_LIMIT:
        new, reason = max(FLOOR, int(cur * 0.6)), f"unhealthy fail_rate={h['fail_rate']:.0%} -> cut"
    else:
        new, reason = min(CEIL, int(cur * GROWTH)), f"healthy (sent={h['sent']}, fail={h['fail']}) -> +50%"
    new = max(FLOOR, min(CEIL, new))
    if new != cur:
        _set_cap(new)
    rec = {"ts": datetime.now(timezone.utc).isoformat(), "old_cap": cur, "new_cap": new,
           "reason": reason, "health": h}
    RAMP_LOG.parent.mkdir(parents=True, exist_ok=True)
    with RAMP_LOG.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    print(json.dumps(rec, indent=2))


if __name__ == "__main__":
    main()
