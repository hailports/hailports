"""Measured-autonomy ledger + truth-gate.

Single source of truth for what the hailports stack can HONESTLY claim about its
autonomy. Public claims like "0 humans / zero-touch / 99% automated / 60+ days
unattended" are FALSE by measured reality (humans are gated on money/sends/
strategy and the owner queue is non-empty). This module:

  1. records every human vs autonomous action to an append-only ledger,
  2. derives REAL signals from on-disk artifacts (never fabricated),
  3. emits TRUTHFUL public tiles, and
  4. fail-closed-gates any text that overclaims zero-human autonomy.

Honest framing: an autonomous inner loop finds->builds->ships->self-heals
reversible work, unattended, 24/7; humans stay gated on money, sends & strategy.
"""
from __future__ import annotations

import json
import re
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HUSTLE = ROOT / "data" / "hustle"
LEDGER = HUSTLE / "autonomy_ledger.jsonl"
QUEUE = HUSTLE / "ALEX_ACTION_QUEUE.md"
START_DATE = "2026-04-15"  # first continuous-runtime day (matches dashboard __START__)

_DAY = 86400


# --------------------------------------------------------------------------- #
# recording
# --------------------------------------------------------------------------- #
def record(kind: str, action: str, detail: str = "", actor: str = "") -> dict:
    """Append one event to the append-only ledger. kind in {human, autonomous}."""
    kind = (kind or "").strip().lower()
    if kind not in ("human", "autonomous"):
        kind = "autonomous" if kind else "human"
    row = {
        "ts": time.time(),
        "kind": kind,
        "action": str(action or ""),
        "detail": str(detail or ""),
        "actor": str(actor or ""),
    }
    try:
        HUSTLE.mkdir(parents=True, exist_ok=True)
        with LEDGER.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass
    return row


def _read_ledger() -> list[dict]:
    rows: list[dict] = []
    try:
        with LEDGER.open("r", encoding="utf-8") as fh:
            for ln in fh:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    rows.append(json.loads(ln))
                except Exception:
                    continue
    except FileNotFoundError:
        return []
    except Exception:
        return []
    return rows


# --------------------------------------------------------------------------- #
# real signals (defensive: omit what's missing, never fabricate)
# --------------------------------------------------------------------------- #
def _autonomous_cycles_24h() -> int | None:
    """Real count of autonomous claude autoflow runs today.

    Prefer the per-day counter the autoflow writes; else count successful
    DONE/rc=0 build/improve log entries dated today.
    """
    today = datetime.now().strftime("%Y%m%d")
    cf = HUSTLE / f"autoflow_claude_runs_{today}.count"
    try:
        n = int(cf.read_text().strip())
        if n >= 0:
            return n
    except Exception:
        pass

    # fallback: scan today's build/improve logs for successful completions
    today_iso = date.today().isoformat()
    cnt = 0
    found_any = False
    pats = ("*IMPROVE*", "*BUILD*")
    seen: set[Path] = set()
    for pat in pats:
        for p in HUSTLE.glob(pat):
            if p in seen or not p.is_file():
                continue
            seen.add(p)
            try:
                txt = p.read_text(errors="ignore")
            except Exception:
                continue
            for ln in txt.splitlines():
                if today_iso not in ln:
                    continue
                low = ln.lower()
                if "done" in low and ("rc=0" in low or "rc0" in low or "success" in low):
                    cnt += 1
                    found_any = True
    return cnt if found_any else None


def _days_continuous() -> int | None:
    """Continuous UPTIME in days (self-healing) — NOT 'unattended'.

    Try the public dashboard's real days_active; else count days since START_DATE.
    """
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        import tools.public_case_study_dashboard as dash  # type: ignore

        for attr in ("days_active", "DAYS_ACTIVE", "days_continuous"):
            v = getattr(dash, attr, None)
            if isinstance(v, (int, float)) and v > 0:
                return int(v)
        sav = getattr(dash, "_savings", None)
        if callable(sav):
            try:
                d = sav() or {}
                da = (d.get("alltime") or {}).get("days_active")
                if isinstance(da, (int, float)) and da > 0:
                    return int(da)
            except Exception:
                pass
        st = getattr(dash, "START", None)
        if isinstance(st, str) and st:
            try:
                start = datetime.fromisoformat(st.replace("Z", "+00:00"))
                delta = datetime.now(timezone.utc) - start
                if delta.days >= 0:
                    return int(delta.days)
            except Exception:
                pass
    except Exception:
        pass

    try:
        start = date.fromisoformat(START_DATE)
        d = (date.today() - start).days
        return d if d >= 0 else None
    except Exception:
        return None


def _human_actions(rows: list[dict], window_s: float) -> int:
    now = time.time()
    n = 0
    for r in rows:
        if r.get("kind") != "human":
            continue
        try:
            ts = float(r.get("ts", 0))
        except Exception:
            continue
        if now - ts <= window_s:
            n += 1
    return n


def _owner_queue() -> int | None:
    try:
        txt = QUEUE.read_text(errors="ignore")
    except Exception:
        return None
    return sum(1 for ln in txt.splitlines() if re.match(r"^- \[ \]", ln))


def _last_human_action(rows: list[dict]) -> float | None:
    ts_vals = []
    for r in rows:
        if r.get("kind") != "human":
            continue
        try:
            ts_vals.append(float(r.get("ts", 0)))
        except Exception:
            continue
    return max(ts_vals) if ts_vals else None


def _signals() -> dict:
    rows = _read_ledger()
    sig: dict = {}
    c = _autonomous_cycles_24h()
    if c is not None:
        sig["autonomous_cycles_24h"] = c
    d = _days_continuous()
    if d is not None:
        sig["days_continuous"] = d  # LABEL: "continuous uptime", never "unattended"
    sig["human_actions_7d"] = _human_actions(rows, 7 * _DAY)
    sig["human_actions_30d"] = _human_actions(rows, 30 * _DAY)
    q = _owner_queue()
    if q is not None:
        sig["owner_queue"] = q
    sig["last_human_action"] = _last_human_action(rows)
    return sig


def stats() -> dict:
    return _signals()


# --------------------------------------------------------------------------- #
# honest public tiles
# --------------------------------------------------------------------------- #
def honest_tiles() -> list[dict]:
    s = _signals()
    tiles: list[dict] = []
    if "autonomous_cycles_24h" in s:
        tiles.append({
            "label": "Autonomous cycles · 24h",
            "value": str(s["autonomous_cycles_24h"]),
            "sub": "build / deploy / self-heal, hands-off",
        })
    if "days_continuous" in s:
        tiles.append({
            "label": "Runtime",
            "value": f"{s['days_continuous']}d",
            "sub": "continuous uptime, self-healing",
        })
    tiles.append({
        "label": "Human-supervised",
        "value": "owner-gated",
        "sub": "money, sends & strategy stay human",
    })
    if "owner_queue" in s:
        tiles.append({
            "label": "Owner queue",
            "value": str(s["owner_queue"]),
            "sub": "items staged for the human, by design",
        })
    return tiles


# --------------------------------------------------------------------------- #
# fail-closed truth gate
# --------------------------------------------------------------------------- #
_BANNED = [
    r"\b0 humans?\b",
    r"no humans?\b.*loop",
    r"0 human interventions",
    r"zero[- ]?touch",
    r"\b99% automated\b",
    r"\b100% automated\b",
    r"nobody touches",
    r"runs without me",
    r"60\+ days unattended",
]
_BANNED_RE = [re.compile(p, re.IGNORECASE | re.DOTALL) for p in _BANNED]


def assert_truthful(text: str) -> tuple[bool, str]:
    """FAIL-CLOSED. Measured reality (human_actions>0, owner_queue>0) never
    supports an absolute zero-human autonomy claim, so any such phrasing fails."""
    t = text or ""
    for rx in _BANNED_RE:
        m = rx.search(t)
        if m:
            return (False, f"overclaims zero-human autonomy: matched {rx.pattern!r} -> {m.group(0)!r}")
    return (True, "ok")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _main(argv=None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="measured-autonomy ledger + truth-gate")
    ap.add_argument("--stats", action="store_true", help="print json stats()")
    ap.add_argument("--tiles", action="store_true", help="print honest_tiles()")
    ap.add_argument("--check", metavar="TEXT", help="run assert_truthful on TEXT")
    ap.add_argument("--record", nargs=2, metavar=("KIND", "ACTION"),
                    help="append a human/autonomous event")
    ap.add_argument("--detail", default="")
    ap.add_argument("--actor", default="")
    args = ap.parse_args(argv)

    did = False
    if args.record:
        did = True
        row = record(args.record[0], args.record[1], detail=args.detail, actor=args.actor)
        print(json.dumps(row, ensure_ascii=False))
    if args.stats:
        did = True
        print(json.dumps(stats(), ensure_ascii=False, indent=2))
    if args.tiles:
        did = True
        print(json.dumps(honest_tiles(), ensure_ascii=False, indent=2))
    if args.check is not None:
        did = True
        ok, reason = assert_truthful(args.check)
        print(json.dumps({"ok": ok, "reason": reason}, ensure_ascii=False))
    if not did:
        ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
