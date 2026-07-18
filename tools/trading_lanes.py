#!/usr/bin/env python3
"""trading_lanes.py — one control plane for the 4 real-money trading lanes.

Lanes (each is a self-contained, paper-by-default, live-double-locked bot):
  kalshi  -> core.kalshi_bot     (event contracts)
  forex   -> core.forex_bot      (OANDA FX, real prices)
  kraken  -> core.kraken_bot     (crypto spot)
  alpaca  -> core.alpaca_bot     (US equities/ETF, swing)

Plus the research engine (paper-only, longer track record):
  markets -> agents.markets_paper_trader  (crypto+equities on real prices)

Doctrine enforced here:
  - Everything runs PAPER on real data at $0 by default.
  - `arm <lane>` is the ONLY way to enable real money, and it REFUSES unless
    (a) that lane's API keys are present AND (b) the lane's paper record clears
    the proof gate (core.trade_proof_gate). No proof, no real dollar.
  - Even after `arm`, the bot still needs MODE=live + ALLOW_LIVE=1 in the env to
    place a real order — arm only writes the proof-gated live-arm file.

$0 to operate. Stdlib only.

CLI:
  python3 tools/trading_lanes.py status              # all lanes: mode / equity / armed / proof
  python3 tools/trading_lanes.py paper-run           # run one paper cycle on every lane
  python3 tools/trading_lanes.py proof [lane]        # proof-gate verdict per lane
  python3 tools/trading_lanes.py arm <lane>          # proof-gated: enable real money for a lane
  python3 tools/trading_lanes.py disarm <lane>       # remove the live-arm file
  python3 tools/trading_lanes.py kill [lane|all]     # kill switch (halts entries)
  python3 tools/trading_lanes.py resume [lane|all]   # clear kill switch
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(os.environ.get("CLAUDE_STACK_DIR") or Path(__file__).resolve().parents[1])
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from core import trade_proof_gate as gate  # noqa: E402
try:
    from core import scaling_ladder  # noqa: E402
except Exception:
    scaling_ladder = None

RUNTIME = "data/runtime"
TRADING = "data/trading"

# lane -> everything the control plane needs to know about it
LANES: dict[str, dict] = {
    "kalshi": {
        "module": "core.kalshi_bot",
        "status": f"{RUNTIME}/kalshi_bot_status.json",
        "trades": f"{RUNTIME}/kalshi_bot_signals.jsonl",
        "live_arm": f"{RUNTIME}/kalshi_live_armed.json",
        "kill": f"{RUNTIME}/kalshi_bot.kill",
        "mode_env": "KALSHI_MODE", "allow_env": "KALSHI_ALLOW_LIVE",
        "key_envs": ["KALSHI_API_KEY_ID", "KALSHI_PRIVATE_KEY_PATH"],
        "settles_at_event": True,  # no round-trip close events yet -> proof can't pass until tracked
    },
    "forex": {
        "module": "core.forex_bot",
        "status": f"{RUNTIME}/forex_bot_status.json",
        "trades": f"{RUNTIME}/forex_bot_trades.jsonl",
        "live_arm": f"{RUNTIME}/forex_live_armed.json",
        "kill": f"{RUNTIME}/forex_bot.kill",
        "mode_env": "FOREX_MODE", "allow_env": "FOREX_ALLOW_LIVE",
        "key_envs": ["OANDA_ACCOUNT_ID", "OANDA_API_TOKEN"],
        "settles_at_event": False,
    },
    "kraken": {
        "module": "core.kraken_bot",
        "status": f"{TRADING}/kraken_bot_status.json",
        "trades": f"{TRADING}/kraken_bot_trades.jsonl",
        "live_arm": f"{RUNTIME}/kraken_live_armed.json",
        "kill": f"{RUNTIME}/kraken_bot.kill",
        "mode_env": "KRAKEN_MODE", "allow_env": "KRAKEN_ALLOW_LIVE",
        "key_envs": ["KRAKEN_API_KEY", "KRAKEN_API_SECRET"],
        "settles_at_event": False,
    },
    "alpaca": {
        "module": "core.alpaca_bot",
        "status": f"{TRADING}/alpaca_bot_status.json",
        "trades": f"{TRADING}/alpaca_bot_trades.jsonl",
        "live_arm": f"{RUNTIME}/alpaca_live_armed.json",
        "kill": f"{RUNTIME}/alpaca_bot.kill",
        "mode_env": "ALPACA_MODE", "allow_env": "ALPACA_ALLOW_LIVE",
        "key_envs": ["ALPACA_API_KEY_ID", "ALPACA_API_SECRET_KEY"],
        "settles_at_event": False,
    },
}
RESEARCH = {"markets": {"module": "agents.markets_paper_trader",
                        "status": f"{TRADING}/markets_paper_status.json",
                        "trades": f"{TRADING}/markets_paper_trades.jsonl"}}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_dotenv() -> None:
    """Make API keys visible to child bot processes. Loads, in order, claude-stack/.env
    and the gitignored credential vault data/secrets/trading_accounts.env (where the
    broker API keys live). First value wins; neither is ever committed."""
    for rel in (".env", "data/secrets/trading_accounts.env"):
        envf = BASE_DIR / rel
        if not envf.exists():
            continue
        try:
            from dotenv import load_dotenv
            load_dotenv(envf)
            continue
        except Exception:
            pass
        for line in envf.read_text(errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _read_json(rel: str) -> dict | None:
    p = BASE_DIR / rel
    try:
        if p.exists():
            return json.loads(p.read_text())
    except Exception:
        pass
    return None


def _run_module(module: str, *args: str, timeout: int = 180) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            [sys.executable, "-m", module, *args],
            cwd=str(BASE_DIR), env=os.environ.copy(),
            capture_output=True, text=True, timeout=timeout,
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except Exception as exc:
        return 1, f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

def cmd_status() -> int:
    _load_dotenv()
    print("TRADING LANES — paper on real data at $0 until proof-gated + armed\n")
    for lane, reg in LANES.items():
        st = _read_json(reg["status"]) or {}
        armed = (BASE_DIR / reg["live_arm"]).exists()
        killed = (BASE_DIR / reg["kill"]).exists()
        keys = "set" if all(os.environ.get(k) for k in reg["key_envs"]) else "MISSING"
        mode = st.get("mode", "—")
        equity = st.get("equity", st.get("cash", "—"))
        start = st.get("starting_cash", "—")
        live_possible = st.get("live_trading_possible", False)
        res = gate.evaluate(BASE_DIR / reg["trades"])
        proof = "PROVEN✓" if res["proven"] else f"not proven ({res['n_closed']} closed)"
        flags = []
        if killed:
            flags.append("KILLED")
        if armed:
            flags.append("ARMED")
        if live_possible:
            flags.append("LIVE-CAPABLE")
        tag = (" [" + ",".join(flags) + "]") if flags else ""
        print(f"  {lane:7} mode={mode:5} equity={equity} / start={start}  keys={keys}  proof={proof}{tag}")
    r = RESEARCH["markets"]
    st = _read_json(r["status"]) or {}
    res = gate.evaluate(BASE_DIR / r["trades"])
    print(f"\n  {'markets':7} (research, paper-only)  equity={st.get('equity','—')}  "
          f"closed_trades={res['n_closed']} net=${res['net_pnl']} pf={res['profit_factor']}")
    print("\n  arm a lane only after: keys=set AND proof=PROVEN✓  (python3 tools/trading_lanes.py arm <lane>)")
    return 0


def cmd_paper_run() -> int:
    _load_dotenv()
    for lane, reg in LANES.items():
        code, out = _run_module(reg["module"], "--once")
        last = out.strip().splitlines()[-1] if out.strip() else ""
        print(f"[{lane}] {'ok' if code == 0 else 'ERR'}  {last[:160]}")
    return 0


def cmd_proof(lane: str | None) -> int:
    lanes = [lane] if lane else list(LANES.keys())
    for ln in lanes:
        reg = LANES.get(ln)
        if not reg:
            print(f"unknown lane: {ln}")
            continue
        res = gate.evaluate(BASE_DIR / reg["trades"])
        print(gate.verdict_text(ln, res))
        if reg.get("settles_at_event") and res["n_closed"] == 0:
            print("  note: this lane settles at event resolution; round-trip P&L tracking "
                  "is a follow-up before it can ever prove out.")
    return 0


def cmd_arm(lane: str) -> int:
    _load_dotenv()
    reg = LANES.get(lane)
    if not reg:
        print(f"unknown lane: {lane}. choose from {list(LANES)}")
        return 2
    blocks: list[str] = []
    missing = [k for k in reg["key_envs"] if not os.environ.get(k)]
    if missing:
        blocks.append(f"API keys not set in .env: {', '.join(missing)}")
    if lane == "kalshi":
        pk = os.environ.get("KALSHI_PRIVATE_KEY_PATH")
        if pk and not Path(pk).exists():
            blocks.append(f"KALSHI_PRIVATE_KEY_PATH file not found: {pk}")
    res = gate.evaluate(BASE_DIR / reg["trades"])
    if not res["proven"]:
        blocks.append("paper record has not proven an edge yet: " + "; ".join(res["reasons"]))
    if blocks:
        print(f"REFUSED to arm '{lane}' — real money stays locked:")
        for b in blocks:
            print(f"  - {b}")
        return 1
    arm_path = BASE_DIR / reg["live_arm"]
    arm_path.parent.mkdir(parents=True, exist_ok=True)
    arm_path.write_text(json.dumps({"armed_at": _now(), "proof": res,
                                    "note": "proof-gated live arm"}, indent=2, default=str))
    print(f"ARMED '{lane}'. Live-arm file written: {reg['live_arm']}")
    print(f"  Final step to place real orders — set in the environment/.env:")
    print(f"    {reg['mode_env']}=live  and  {reg['allow_env']}=1")
    print(f"  Then run the lane loop. Kill any time: python3 tools/trading_lanes.py kill {lane}")
    return 0


def cmd_disarm(lane: str) -> int:
    reg = LANES.get(lane)
    if not reg:
        print(f"unknown lane: {lane}")
        return 2
    p = BASE_DIR / reg["live_arm"]
    if p.exists():
        p.unlink()
        print(f"disarmed '{lane}' (removed {reg['live_arm']})")
    else:
        print(f"'{lane}' was not armed")
    return 0


def cmd_kill(target: str) -> int:
    targets = list(LANES) if target in ("all", "") else [target]
    for ln in targets:
        reg = LANES.get(ln)
        if not reg:
            print(f"unknown lane: {ln}")
            continue
        p = BASE_DIR / reg["kill"]
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"activated_at": _now()}))
        print(f"KILLED '{ln}' ({reg['kill']})")
    return 0


def cmd_resume(target: str) -> int:
    targets = list(LANES) if target in ("all", "") else [target]
    for ln in targets:
        reg = LANES.get(ln)
        if not reg:
            continue
        p = BASE_DIR / reg["kill"]
        if p.exists():
            p.unlink()
        print(f"resumed '{ln}'")
    return 0


def cmd_scale(lane: str | None) -> int:
    """Scaling-ladder recommendation per lane. Recommend-only — actually moving real
    money to a higher rung stays a proof-gated, human-executed step."""
    if scaling_ladder is None:
        print("scaling_ladder module unavailable")
        return 1
    recs = scaling_ladder.recommend_all()
    if lane:
        recs = {lane: recs.get(lane, {"action": "unknown-lane"})}
    print("SCALING LADDER — recommend-only (real-money moves stay arm-gated)\n")
    for ln, r in recs.items():
        cur, tgt = r.get("current_stake"), r.get("target_stake")
        arrow = f"${cur} -> ${tgt}" if cur != tgt else f"${cur}"
        print(f"  {ln:7} {str(r.get('action','?')).upper():8} {arrow}")
        for reason in (r.get("reasons") or [])[:2]:
            print(f"          - {reason}")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "status"
    arg = argv[1] if len(argv) > 1 else ""
    if cmd == "status":
        return cmd_status()
    if cmd == "paper-run":
        return cmd_paper_run()
    if cmd == "proof":
        return cmd_proof(arg or None)
    if cmd == "arm":
        return cmd_arm(arg) if arg else (print("usage: arm <lane>") or 2)
    if cmd == "disarm":
        return cmd_disarm(arg) if arg else (print("usage: disarm <lane>") or 2)
    if cmd == "kill":
        return cmd_kill(arg or "all")
    if cmd == "resume":
        return cmd_resume(arg or "all")
    if cmd == "scale":
        return cmd_scale(arg or None)
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
