"""interactive_preemption — when Operator is ACTIVELY interacting with the mini, background
work yields so the interactive path keeps full P-core power + a warm fast model.

The QoS governor (`scripts/remote_priority_governor.sh`) already demotes hogs to the
efficiency cores and protects the GUI/remote/stream/operator session continuously. What it
does NOT do: react to Operator *specifically pinging the box right now* — it has no notion of an
active interactive session, so a heavy agent that just spun up still gets a fair QoS slice in
the 30s before the next governor pass, which is exactly the window where Operator feels lag.

This module closes that gap:
  - is_operator_active() -> (active, seconds_since, source): cheap/deterministic detection of a
    recent inbound iMessage from Operator OR a recent REAL interactive work-GPT gateway request. The
    gateway is hit ~24/7 by synthetic __watchdog__/__probe__/canary traffic; we tail-parse the
    routes log for the newest entry whose `src` is a genuine interactive caller and ignore the rest.
  - preempt(active): on active, demote stack python background jobs to Background QoS + nice+15
    (reusing the governor's taskpolicy -b / renice pattern), NEVER touching the PROTECT set; on
    inactive, back off ONLY our extra nice delta (15 -> the governor's +10 baseline) on the pids WE
    demoted — QoS/base priority stay the governor's job. Idempotent, fail-soft.
  - ensure_warm_model(): keep the FAST interactive local model resident in Ollama so the first
    interactive reply has no cold-load lag. No reload if already warm.

Designed for a short launchd cadence (~30s) or to be called by the governor each pass.

Smoke test (safe): prints operator-active state from the real chat.db and DRY-prints which pids
preempt() WOULD demote — proving the PROTECT list is respected — without renicing anything.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.constants import FAST_LOCAL_MODEL, OLLAMA_URL  # noqa: E402

# --- config ---------------------------------------------------------------
ALEX_PHONE = "XPHONEX"
_ALEX_DIGITS = "".join(c for c in ALEX_PHONE if c.isdigit())[-10:]
APPLE_EPOCH = 978307200  # 2001-01-01 in unix seconds

ACTIVE_WINDOW_S = 90            # "actively interacting" if a signal is newer than this
GATEWAY_ROUTES_LOG = ROOT / "logs" / "work_action_routes.jsonl"
STATE_FILE = ROOT / "data" / "runtime" / ".interactive_preemption.json"

# How much tail of the routes log to scan for the newest REAL interactive request. The signal we
# care about is "within ACTIVE_WINDOW_S" — which lives at the very end of the file — so a few MB is
# always plenty; scanning newest-first lets us early-exit on the first interactive line.
ROUTES_TAIL_BYTES = 4 * 1024 * 1024

# A routes-log entry only counts as Operator actively interacting if its `src` is a REAL interactive
# caller. The gateway is hammered ~24/7 by synthetic traffic (__watchdog__/__probe__/
# frontdoor_deep_canary/diag/refresh_test = ~97% of /tools/run) — that is NOT Operator, and keying off
# it (as the old mtime-based signal did) reported active=True around the clock. Belt-and-suspenders:
# an explicit synthetic-src exclusion regex AND a positive allowlist of human-driven callers.
_INTERACTIVE_SRC_PREFIXES = ("corp_chatgpt", "codex", "webui", "imessage")
_SYNTHETIC_SRC_RE = re.compile(
    r"^__.*__$|^__probe__|frontdoor_deep_canary|_probe$|_test$|^diag$|^refresh_|canary|watchdog",
    re.IGNORECASE,
)

DEMOTE_NICE = 15               # background nice bump this module adds while Operator is active
GOVERNOR_BASELINE_NICE = 10    # remote_priority_governor.sh bg() steady-state (renice +10); on
#                                restore we back off ONLY our extra delta (15 -> 10) and leave QoS /
#                                base priority entirely to the governor — never fight its baseline.

# NEVER demote these — mirrors remote_priority_governor.sh PROTECT + the operator interactive
# session (shell/claude/node gateway/Terminal/sshd) + the live YouTube stream encoder + every
# process that SERVES the interactive path itself (the work-GPT gateways on 8078/8077, the local
# chat webui backend, the iMessage relay). Demoting an interactive server = the exact lag we're
# here to prevent, so these are hard-protected by name AND by listening port (INTERACTIVE_PORTS).
PROTECT = (
    "WindowServer|SkyLight|screensharingd|ScreensharingAgent|SSMenuAgent|ARDAgent|"
    "RemoteManagement|RemoteDesktop|edovia|Screens 5|Screens Connect|BetterDisplay|"
    "WindowManager|/Dock|loginwindow|coreaudiod|tailscaled|Tailscale|cloudflared|"
    "remote_priority_governor|interactive_preemption|"
    "chatgpt_redacted_action|chatgpt_sf_admin_action|webui|open.?webui|WEBUI_PORT|"
    "imessage_relay|imsg_bridge|qchat_relay|work_approval_imessage_reader|"
    # trailing ([[:space:]]|$) so these match a real cmdline that carries args after the binary
    # (e.g. "/usr/local/bin/node /path/server.js", "/opt/.../claude -p ...") — a bare "$" anchor
    # only matched an argv-less invocation and silently let the gateway/operator shell slip through.
    r"(^|/|-)(ba|z)?sh([[:space:]]|$)|/claude([[:space:]]|$)|/node([[:space:]]|$)|"
    r"Terminal|iTerm|tmux|sshd|ollama"
)

# Any pid LISTENing on one of these is part of the interactive path -> never demote it, even if
# its command line doesn't match PROTECT (defense-in-depth against the gateways slipping through).
INTERACTIVE_PORTS = (8078, 8077, 8101)
STREAM = (
    r"rtmp://a\.rtmp\.youtube\.com|rtmp\.youtube\.com/live|"
    r"dashboard_livestream|casestudy_stream"
)


# --- operator activity detection -----------------------------------------
def _last_inbound_imessage_age_s() -> float | None:
    """Seconds since the most recent INBOUND iMessage from Operator, or None if unavailable.
    Read-only against chat.db via the shared reader (mode=ro, retries, osascript fallback)."""
    try:
        from core.imessage_db import query_rows
    except Exception:
        return None
    sql = f"""
        SELECT MAX(m.date)
        FROM message m
        JOIN handle h ON m.handle_id = h.ROWID
        WHERE m.is_from_me = 0
          AND h.id LIKE '%{_ALEX_DIGITS}'
    """
    try:
        rows = query_rows(sql)
    except Exception:
        return None
    if not rows or not rows[0] or rows[0][0] is None:
        return None
    raw = float(rows[0][0])
    # modern chat.db stores date as nanoseconds since APPLE_EPOCH; legacy as seconds.
    secs = raw / 1e9 if raw > 1e12 else raw
    unix = secs + APPLE_EPOCH
    age = time.time() - unix
    return age if age >= 0 else 0.0


def _is_interactive_src(src: str) -> bool:
    """True only for a REAL human-driven gateway caller (corp_chatgpt / codex / webui / imessage).
    Every synthetic/automated src (__watchdog__, __probe__*, frontdoor_deep_canary, *_probe, *_test,
    diag, refresh_*) is rejected — those are NOT Operator and must never register as active."""
    if not src:
        return False
    low = src.strip().lower()
    if _SYNTHETIC_SRC_RE.search(low):
        return False
    return any(low == p or low.startswith(p) for p in _INTERACTIVE_SRC_PREFIXES)


def _tail_text(path: Path, max_bytes: int) -> str:
    """Last <=max_bytes of a file as text, dropping the leading partial line. Fail-soft to ''."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()  # discard the partial line we seeked into
            return f.read().decode("utf-8", "replace")
    except Exception:
        return ""


def _iso_age_s(ts: str | None, now: float | None = None) -> float | None:
    """Seconds since an ISO-8601 timestamp (tz-aware or assumed UTC). None if unparseable."""
    if not ts:
        return None
    now = time.time() if now is None else now
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age = now - dt.timestamp()
        return age if age >= 0 else 0.0
    except Exception:
        return None


def _last_real_gateway_request(max_bytes: int = ROUTES_TAIL_BYTES) -> tuple[float, str] | None:
    """(age_seconds, src) of the NEWEST real interactive gateway request in the routes log, or None.
    Tail-parses logs/work_action_routes.jsonl newest-first and stops at the first entry whose src is
    a real interactive caller — synthetic watchdog/probe/canary traffic is skipped entirely. This
    replaces the old routes-log MTIME signal, which every synthetic request bumped (active ~24/7)."""
    text = _tail_text(GATEWAY_ROUTES_LOG, max_bytes)
    if not text:
        return None
    now = time.time()
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if not _is_interactive_src(rec.get("src", "")):
            continue
        age = _iso_age_s(rec.get("ts"), now)
        if age is None:
            continue
        return (age, str(rec.get("src", "")))
    return None


def is_operator_active(window_s: int = ACTIVE_WINDOW_S) -> tuple[bool, float, str]:
    """Return (active, seconds_since, source). Active iff the NEWEST real interactive signal is
    within window_s: an inbound iMessage from Operator OR a real interactive work-GPT gateway request
    (corp_chatgpt/codex/webui/imessage) — synthetic __watchdog__/__probe__/canary traffic is
    excluded. seconds_since is the freshest signal; source names it. Cheap, deterministic, never
    raises."""
    signals: list[tuple[float, str]] = []
    im = _last_inbound_imessage_age_s()
    if im is not None:
        signals.append((im, "imessage"))
    gw = _last_real_gateway_request()
    if gw is not None:
        signals.append((gw[0], f"gateway:{gw[1]}"))
    if not signals:
        return (False, float("inf"), "none")
    age, source = min(signals, key=lambda s: s[0])
    return (age <= window_s, age, source)


# --- pid enumeration + QoS control ---------------------------------------
def _grep_match(text: str, pattern: str) -> bool:
    try:
        return subprocess.run(
            ["grep", "-Eqi", pattern],
            input=text, text=True, timeout=3,
        ).returncode == 0
    except Exception:
        return True  # fail-safe: treat as protected on any error


def _cmdline(pid: int) -> str:
    try:
        return subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)],
            capture_output=True, text=True, timeout=3,
        ).stdout.strip()
    except Exception:
        return ""


def _interactive_port_pids() -> set[int]:
    """Pids LISTENing on an interactive port (gateways/webui). Cached per call via lru is
    overkill for a 30s cadence; compute fresh + fail-soft."""
    pids: set[int] = set()
    args = ["lsof", "-nP", "-sTCP:LISTEN", "-t"]
    for p in INTERACTIVE_PORTS:
        args.append(f"-iTCP:{p}")
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=6).stdout
        for line in out.split():
            if line.isdigit():
                pids.add(int(line))
    except Exception:
        pass
    return pids


_PROTECTED_PORT_PIDS: set[int] | None = None


def _is_protected(pid: int, cmd: str) -> bool:
    global _PROTECTED_PORT_PIDS
    if not cmd:
        return True
    if _PROTECTED_PORT_PIDS is None:
        _PROTECTED_PORT_PIDS = _interactive_port_pids()
    if pid in _PROTECTED_PORT_PIDS:
        return True
    if _grep_match(cmd, PROTECT):
        return True
    if _grep_match(cmd, STREAM):
        return True
    return False


def _candidate_pids() -> list[tuple[int, str]]:
    """Heavy stack python background jobs eligible for demotion when Operator is active:
    every com.claude-stack launchd top-level pid (children inherit bg QoS) plus any running
    stack workflow/agent/collector python — MINUS anything on the PROTECT set and self."""
    global _PROTECTED_PORT_PIDS
    _PROTECTED_PORT_PIDS = _interactive_port_pids()  # refresh once per enumeration pass
    me = os.getpid()
    parent = os.getppid()
    seen: set[int] = set()
    out: list[tuple[int, str]] = []

    def _consider(pid: int) -> None:
        if pid in seen or pid in (me, parent) or pid <= 1:
            return
        seen.add(pid)
        cmd = _cmdline(pid)
        if not cmd:
            return
        if _is_protected(pid, cmd):
            return
        out.append((pid, cmd))

    # 1) com.claude-stack launchd jobs
    try:
        listing = subprocess.run(
            ["launchctl", "list"], capture_output=True, text=True, timeout=8
        ).stdout
        for line in listing.splitlines():
            if "com.claude-stack" not in line:
                continue
            first = line.split("\t", 1)[0].strip()
            if first.isdigit():
                _consider(int(first))
    except Exception:
        pass

    # 2) running stack python (heavy agents/collectors + any workflow python) under this repo
    for pat in (f"{ROOT}/agents/", f"{ROOT}/tools/", f"{ROOT}/core/", "workflow", "collector"):
        try:
            pids = subprocess.run(
                ["pgrep", "-f", pat], capture_output=True, text=True, timeout=5
            ).stdout.split()
            for p in pids:
                if p.isdigit():
                    _consider(int(p))
        except Exception:
            continue

    return out


def _taskpolicy(pid: int, background: bool) -> None:
    flag = "-b" if background else "-B"
    try:
        subprocess.run(["/usr/sbin/taskpolicy", flag, "-p", str(pid)],
                       capture_output=True, timeout=5)
    except Exception:
        pass


def _current_nice(pid: int) -> int | None:
    try:
        out = subprocess.run(["ps", "-o", "nice=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=3).stdout.strip()
        return int(out) if out.lstrip("-").isdigit() else None
    except Exception:
        return None


def _renice(pid: int, value: int) -> None:
    try:
        subprocess.run(["renice", str(value), "-p", str(pid)],
                       capture_output=True, timeout=5)
    except Exception:
        pass


def _demote(pid: int) -> None:
    _taskpolicy(pid, background=True)
    ni = _current_nice(pid)
    if ni is not None and ni < DEMOTE_NICE:
        _renice(pid, DEMOTE_NICE)


def _restore(pid: int) -> None:
    """Back off ONLY the extra nice delta this module added (15 -> the governor's +10 baseline).
    Deliberately does NOT touch QoS/base priority: applying taskpolicy -B (foreground QoS) + renice
    0 overshot the governor's steady state (background QoS + nice+10), briefly boosting heavy jobs
    onto the P-cores where they contend with the live stream. QoS and base priority belong to
    remote_priority_governor.sh; we never fight its baseline. Only lower a pid we pushed to 15 —
    if the governor has since put it at <=10 (e.g. promoted for active work), leave it alone."""
    ni = _current_nice(pid)
    if ni is not None and ni > GOVERNOR_BASELINE_NICE:
        _renice(pid, GOVERNOR_BASELINE_NICE)


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"demoted": [], "active": False}


def _save_state(d: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(d))
    except Exception:
        pass


def preempt(active: bool, *, dry_run: bool = False) -> dict:
    """When active, demote stack background python to Background QoS + nice+15. When inactive,
    restore ONLY the pids we previously demoted (so we never fight the governor's baseline).
    Idempotent + fail-soft. Returns a summary dict."""
    state = _load_state()
    prev_demoted = set(int(p) for p in state.get("demoted", []))

    if active:
        cands = _candidate_pids()
        demoted_now: list[int] = []
        for pid, _cmd in cands:
            if not dry_run:
                _demote(pid)
            demoted_now.append(pid)
        if not dry_run:
            _save_state({"demoted": demoted_now, "active": True, "ts": time.time()})
        return {"active": True, "demoted": demoted_now, "count": len(demoted_now),
                "dry_run": dry_run}

    # inactive: restore what we demoted, then clear
    restored: list[int] = []
    for pid in prev_demoted:
        if _cmdline(pid):  # still alive
            if not dry_run:
                _restore(pid)
            restored.append(pid)
    if not dry_run:
        _save_state({"demoted": [], "active": False, "ts": time.time()})
    return {"active": False, "restored": restored, "count": len(restored), "dry_run": dry_run}


# --- warm fast model ------------------------------------------------------
def _ollama_loaded_models() -> set[str]:
    try:
        req = urllib.request.Request(f"{OLLAMA_URL}/api/ps")
        with urllib.request.urlopen(req, timeout=4) as r:
            data = json.loads(r.read())
        return {m.get("name", "") for m in data.get("models", [])}
    except Exception:
        return set()


def ensure_warm_model(model: str | None = None, keep_alive: str = "30m") -> dict:
    """Keep the FAST interactive local model resident in Ollama so the first interactive reply
    has no cold-load lag. No-op (beyond a cheap /api/ps check) if it's already loaded."""
    model = model or FAST_LOCAL_MODEL
    loaded = _ollama_loaded_models()
    # ollama tags may append :latest; match on prefix
    if any(m == model or m.startswith(model.split(":")[0]) for m in loaded):
        return {"model": model, "warm": True, "action": "already-loaded"}
    # empty-prompt generate with keep_alive just loads + pins the model (no tokens generated)
    body = {"model": model, "prompt": "", "stream": False, "keep_alive": keep_alive}
    try:
        req = urllib.request.Request(
            f"{OLLAMA_URL}/api/generate",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=90) as r:
            json.loads(r.read())
        return {"model": model, "warm": True, "action": "loaded"}
    except Exception as e:
        return {"model": model, "warm": False, "action": "error", "error": str(e)}


def run_once(window_s: int = ACTIVE_WINDOW_S) -> dict:
    """One governor/launchd pass: detect activity, preempt background accordingly, and when
    active keep the fast model warm. Fail-soft end to end."""
    active, age, source = is_operator_active(window_s)
    result = {"active": active, "seconds_since": age, "source": source}
    try:
        result["preempt"] = preempt(active)
    except Exception as e:
        result["preempt"] = {"error": str(e)}
    if active:
        try:
            result["warm"] = ensure_warm_model()
        except Exception as e:
            result["warm"] = {"error": str(e)}
    return result


if __name__ == "__main__":
    if "--run-once" in sys.argv:
        # LIVE pass (launchd entrypoint): detect -> preempt -> warm. Prints one JSON line.
        print(json.dumps(run_once()))
        sys.exit(0)

    print("=== interactive_preemption smoke test (read-only, DRY) ===")
    active, age, source = is_operator_active()
    age_str = "inf" if age == float("inf") else f"{age:.1f}s"
    print(f"is_operator_active() -> active={active}  seconds_since={age_str}  source={source}")
    im = _last_inbound_imessage_age_s()
    gw = _last_real_gateway_request()
    print(f"  last inbound iMessage from Operator: "
          f"{'n/a' if im is None else f'{im:.1f}s ago'}")
    print(f"  last REAL interactive gateway req: "
          f"{'n/a (only synthetic traffic)' if gw is None else f'{gw[0]:.1f}s ago  src={gw[1]}'}")

    print("\n--- DRY: pids preempt() WOULD demote (PROTECT respected, nothing renined) ---")
    cands = _candidate_pids()
    if not cands:
        print("  (no eligible stack background pids right now)")
    for pid, cmd in cands:
        print(f"  demote pid={pid:<7} nice->{DEMOTE_NICE}  {cmd[:96]}")
    print(f"  total candidates: {len(cands)}")

    # prove PROTECT: show a couple protected pids we deliberately skip (self, governor if up)
    me = os.getpid()
    print(f"\n  self pid {me} explicitly excluded: "
          f"{me not in [p for p, _ in cands]}")

    print("\n--- fast model warmth (observe only, no forced reload here) ---")
    loaded = _ollama_loaded_models()
    warm = any(m == FAST_LOCAL_MODEL or m.startswith(FAST_LOCAL_MODEL.split(':')[0])
               for m in loaded)
    print(f"  FAST_LOCAL_MODEL={FAST_LOCAL_MODEL}  currently_loaded={warm}  "
          f"ollama_ps={sorted(loaded) or '(none/unreachable)'}")
    print("\nsmoke ok")
