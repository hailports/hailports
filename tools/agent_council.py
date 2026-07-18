#!/usr/bin/env python3
"""agent_council — headless, nonstop driver for the master-grade startup-team agents.

Runs the revenue-intelligence cluster continuously via `claude -p --agent <name>`
(flat-rate Max subscription, NOT metered API — no OpenRouter-style bleed). Each
agent refreshes its own digest on its own cadence; findings land in the stack
digests dir and feed the existing python execution loops.

GUARDS (this stack has been bled/runaway'd before — these are not optional):
  * kill switch:  ~/.agent-council.stop present -> pause, never run
  * burn governor: hard daily_run_cap + paced spacing between runs
  * turbo window:  work harder overnight when Operator isn't on the machine
  * single instance lockfile
  * two-consecutive-failure skip per agent (Wells R11) + rate-limit backoff
Run: `python3 tools/agent_council.py --once` (cron) or `--loop` (daemon, default).
"""
from __future__ import annotations
import json, os, subprocess, sys, time, datetime, fcntl, pathlib

HOME = pathlib.Path.home()
STACK = HOME / "claude-stack"
CONFIG = STACK / "tools" / "council_config.json"
STATE = HOME / ".agent-council.state.json"
KILL = HOME / ".agent-council.stop"
LOCK = HOME / ".agent-council.lock"
DIGEST_DIR = HOME / ".openclaw/workspace/CompanyA-local/digests/council"
LOG = STACK / "logs.internal" / "agent-council.log"
AGENTS_DIR = HOME / ".claude/agents/startup-team"      # the 29 master-grade role .md files
RUNNER_SH = STACK / "scripts" / "agent_run.sh"          # unified engine: claude→codex→cheap, survives Max end 7/8


def agent_system_prompt(name: str) -> str:
    """The role .md body (master-grade system prompt) with YAML frontmatter stripped —
    fed to whatever engine agent_run.sh picks, so it's not tied to Claude Code's --agent."""
    p = AGENTS_DIR / f"{name}.md"
    if not p.exists():
        return ""
    t = p.read_text()
    if t.startswith("---"):
        parts = t.split("---", 2)
        if len(parts) == 3:
            return parts[2].strip()
    return t.strip()


def now() -> float: return time.time()
def today() -> str: return datetime.date.today().isoformat()

def log(msg: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    line = f"{datetime.datetime.now().isoformat(timespec='seconds')} {msg}"
    with open(LOG, "a") as f: f.write(line + "\n")
    print(line, flush=True)

def load_json(p: pathlib.Path, default):
    try: return json.loads(p.read_text())
    except Exception: return default

def save_state(s: dict) -> None:
    STATE.write_text(json.dumps(s, indent=2))

def cfg() -> dict:
    return load_json(CONFIG, {})

def in_turbo(c: dict) -> bool:
    return datetime.datetime.now().hour in set(c.get("turbo_hours", []))

def pace(c: dict) -> int:
    return int(c.get("turbo_pace_seconds", 600) if in_turbo(c) else c.get("pace_seconds", 1800))

def fresh_state(c: dict) -> dict:
    return {"date": today(), "runs_today": 0,
            "agents": {a["name"]: {"last_run": 0, "fails": 0} for a in c.get("agents", [])}}

def get_state(c: dict) -> dict:
    s = load_json(STATE, None)
    if not s or s.get("date") != today():
        s = fresh_state(c)  # daily reset of the burn counter
    for a in c.get("agents", []):
        s.setdefault("agents", {}).setdefault(a["name"], {"last_run": 0, "fails": 0})
    return s

def due(agent: dict, st: dict) -> bool:
    rec = st["agents"][agent["name"]]
    if rec["fails"] >= 2:  # Wells R11: two strikes -> parked until a manual reset/next day
        return False
    age_min = (now() - rec["last_run"]) / 60.0
    return age_min >= agent.get("min_interval_min", 180)

def run_agent(agent: dict, c: dict) -> bool:
    name = agent["name"]
    out = DIGEST_DIR / f"{name}.latest.md"
    DIGEST_DIR.mkdir(parents=True, exist_ok=True)
    prompt = (agent["task"] +
              "\n\nBe concise and decision-grade. Make reasonable assumptions; do not ask questions. "
              "Lead with the 3 highest-signal findings. Every number traces to a source.")
    sys_prompt = agent_system_prompt(name)  # the master-grade role .md, engine-agnostic
    tmo = int(agent.get("timeout_sec", 600))
    runner = c.get("runner", "codex")       # codex = ChatGPT-Plus flat-rate; NOT Claude Max (ends 7/8)
    # route through the stack's unified runner so the council survives Max ending + never touches
    # a metered API key. env: pin the engine + timeout; strip Anthropic keys as belt-and-suspenders.
    env = {k: v for k, v in os.environ.items()
           if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    env["AGENT_RUNNER"] = runner
    env["AGENT_RUN_TIMEOUT"] = str(tmo)
    cmd = ["bash", str(RUNNER_SH), prompt, str(agent.get("max_turns", 40)), str(STACK), sys_prompt]
    log(f"RUN {name} runner={runner} (turbo={in_turbo(c)})")
    try:
        r = subprocess.run(cmd, cwd=str(STACK), capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, env=env, timeout=tmo + 120)
    except subprocess.TimeoutExpired:
        log(f"TIMEOUT {name}"); return False
    body = (r.stdout or "").strip()
    low = body.lower()
    if r.returncode != 0 or not body or "usage limit" in low or "rate limit" in low:
        log(f"FAIL {name} rc={r.returncode} err={(r.stderr or '')[:180]!r}")
        return False
    stamp = datetime.datetime.now().isoformat(timespec="minutes")
    out.write_text(f"# {name} — {stamp}\n\n{body}\n")
    with open(DIGEST_DIR / "COUNCIL_LOG.md", "a") as f:
        f.write(f"\n---\n## {name} @ {stamp}\n\n{body}\n")
    log(f"OK {name} ({len(body)} chars) -> {out.name}")
    return True

def rebuild_digest(c: dict) -> None:
    lines = [f"# revenue-intelligence council — rolling digest",
             f"_updated {datetime.datetime.now().isoformat(timespec='minutes')}_\n"]
    for a in c.get("agents", []):
        f = DIGEST_DIR / f"{a['name']}.latest.md"
        if f.exists():
            head = f.read_text().split("\n\n", 1)
            lines.append(head[0])
            lines.append((head[1][:600] + " …") if len(head) > 1 and len(head[1]) > 600 else (head[1] if len(head) > 1 else ""))
            lines.append(f"\n> full: `{f}`\n")
    (DIGEST_DIR / "COUNCIL_DIGEST.md").write_text("\n".join(lines))

def tick(c: dict) -> None:
    if KILL.exists():
        log("KILL switch present — paused"); return
    st = get_state(c)
    if st["runs_today"] >= int(c.get("daily_run_cap", 60)):
        log(f"daily_run_cap {c.get('daily_run_cap')} hit — holding"); return
    # oldest-due-first so every agent gets refreshed fairly
    duelist = sorted([a for a in c["agents"] if due(a, st)],
                     key=lambda a: st["agents"][a["name"]]["last_run"])
    if not duelist:
        return
    agent = duelist[0]
    ok = run_agent(agent, c)
    rec = st["agents"][agent["name"]]
    rec["last_run"] = now()
    rec["fails"] = 0 if ok else rec["fails"] + 1
    st["runs_today"] += 1
    if not ok and rec["fails"] >= 2:
        log(f"PARK {agent['name']} — 2 consecutive fails, skipping until reset")
    save_state(st)
    rebuild_digest(c)

def main() -> None:
    mode = "--once" if "--once" in sys.argv else "--loop"
    lock = open(LOCK, "w")
    try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("another council instance holds the lock — exiting"); return
    c = cfg()
    if not c.get("agents"):
        log("no council_config.json / no agents — exiting"); return
    log(f"council start mode={mode} agents={len(c['agents'])} pace={c.get('pace_seconds')}s cap={c.get('daily_run_cap')}")
    if mode == "--once":
        tick(c); return
    backoff = 0
    while True:
        if KILL.exists():
            time.sleep(120); continue
        c = cfg()  # hot-reload config each cycle
        before = get_state(c)["runs_today"]
        tick(c)
        after = get_state(c)["runs_today"]
        # if the last run failed hard (no run recorded but attempted), back off; else normal pace
        if after == before and any(get_state(c)["agents"][a["name"]]["fails"] for a in c["agents"]):
            backoff = min(3600, (backoff or 300) * 2)
            log(f"backoff {backoff}s (a recent run failed — likely rate/usage limit)")
            time.sleep(backoff)
        else:
            backoff = 0
            time.sleep(pace(c))

if __name__ == "__main__":
    main()
