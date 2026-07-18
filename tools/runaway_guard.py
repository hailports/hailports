#!/usr/bin/env python3
"""runaway_guard.py — kills stack processes stuck in a runaway busy-loop.

The hole this closes: on 2026-06-20 a hailports_engagement pass crashed and then
busy-looped at ~99% CPU for 42 MINUTES with nothing killing it. The existing
guards do NOT cover this case:
  - ram_warden        -> RAM/OOM only, allowlist-driven; never looks at CPU time.
  - redacted_sentinel  -> acts on SYSTEM load1 vs core count (stage1 = load>cores*1.5).
                         On a 10-core mini ONE process pegging a single core = ~1.0
                         load, far below the 15.0 stage-1 trigger -> never fires.
                         (and even when it does it SIGSTOP/SIGCONTs, never kills.)
  - infra_pressure    -> reports a pressure score; never kills anything (os.kill(pid,0)
                         is only a liveness probe).
So a lone runaway is invisible to every one of them. This guard fills that gap.

2026-06-28 extension — runaway *system daemons*: on this date the box froze (load
68 on 10 cores, UI unresponsive). Root cause was NOT a stack job: the per-user
keychain daemons secd + ctkd entered a respawn/spin loop (>100% CPU from birth).
secd is the global serialization point for keychain access, so once it pegs,
every process doing an auth/cert/keychain op blocks behind it -> the runnable
queue explodes -> freeze. Neither guard above could touch it: the priority
governor only scans `-u $(id -u)` user procs, and this guard's STACK_PATTERNS
never match `/usr/libexec/secd`. So a runaway Apple daemon was invisible to the
ENTIRE stack. SYSTEM_DAEMON_TARGETS closes that: a tiny allowlist of stateless,
on-demand, launchd-managed daemons that are SAFE to SIGKILL on a sustained spin
(launchd relaunches them instantly; clients re-request). Kept deliberately small
and separate from the user-automation path — no STACK_PATTERN/protect/allow
gating, a tighter sustain window, immediate SIGKILL. Root-owned daemons
(PerfPowerServices) raise EPERM here and are logged for a future root deployment.

Mechanism (true CPU-time-over-walltime detector, NOT lifetime %cpu):
  Each cycle we read cumulative CPU time per process. Between cycles we compute
  recent utilization = d(cputime) / d(walltime). A process burning >= CPU_FRAC of
  a core *sustained* for >= SUSTAIN_SECONDS of consecutive samples, that positively
  matches a stack pattern, is not protected, and is not allowlisted, gets
  SIGTERM -> (5s) -> SIGKILL.

Fail-safe doctrine (HARD), mirrors redacted_sentinel:
  - KILLABLE only by POSITIVE match (stack python / ffmpeg / playwright headless).
    Anything unmatched is never touched.
  - Protected tokens (guards, healers, work/SF/ollama, the Claude harness, the
    persistent CDP Chrome sessions, this guard itself) are NEVER killed.
  - Allowlisted long legit high-CPU jobs (the YouTube livestream ffmpeg, manual
    builds, sf retrieves, model jobs) are NEVER killed.
  - Need >= MIN_SAMPLES real deltas before any kill (no kill on a single reading).
  - Never touches the work MacBook; pure local ps + os.kill.
  - --dry-run mutates nothing.

Run:
  python tools/runaway_guard.py --once                 # one cycle (launchd uses this)
  python tools/runaway_guard.py --once --dry-run       # show, kill nothing
  python tools/runaway_guard.py --daemon               # internal loop
  python tools/runaway_guard.py --status               # print state json
  python tools/runaway_guard.py --selftest             # classifier sanity check
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
STACK = HOME / "claude-stack"
# Pin repo root so `from tools import gen_runaway_allowlist` / `from core.alert_gateway`
# resolve no matter the launch cwd (launchd runs us as `tools/runaway_guard.py`).
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
STATE_FILE = HOME / ".runaway-guard.state.json"
ALLOW_FILE = HOME / ".runaway-guard.allow"          # user-extensible, one substr per line
LOG_DIR = STACK / "data" / "logs"
LOG_FILE = LOG_DIR / "runaway_guard.log"
INCIDENT_FILE = LOG_DIR / "runaway_guard.incidents.jsonl"

# --- tunables (env-overridable for tuning + tests) ------------------------
def _envf(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


CPU_FRAC = _envf("RUNAWAY_CPU_FRAC", 0.85)          # >= 85% of one core (cputime/walltime delta)
# RUNAWAY vs RESOURCE-INTENSIVE — the discriminator (no allowlist babysitting): a real workload's
# memory MOVES (it loads pages, allocates, frees, makes progress); a runaway busy-loop pegs a core
# with DEAD-FLAT RSS and goes nowhere. So a flat-RSS spinner gets a SHORT fuse, while a moving-RSS
# heavy task (render, inference, an actively-paginating scrape) is left alone up to a long ceiling.
SUSTAIN_SECONDS = _envf("RUNAWAY_SUSTAIN_S", 180)        # flat-RSS spinner: kill after this (was 600)
HARD_CEILING_SECONDS = _envf("RUNAWAY_HARD_CEILING_S", 1800)  # even a "busy" (moving-RSS) job is a runaway past this
RSS_FLAT_KB = _envf("RUNAWAY_RSS_FLAT_KB", 6144)        # RSS span < ~6MB across the window = "not making progress"
MIN_SAMPLES = int(_envf("RUNAWAY_MIN_SAMPLES", 3))  # min computed deltas first (no single-read kills)
INTERVAL = int(_envf("RUNAWAY_INTERVAL_S", 30))     # daemon loop cadence (was 60; tighter = faster catch)
STALE_PID_RESET = 12     # if a pid's start time shifts > this many s, treat as new proc
MAX_KILLS_PER_CYCLE = 6  # bounded; a storm shouldn't let us mass-kill

# STACKING — a scheduled singleton agent should NEVER have two live instances. When a launchd slot
# re-fires while the prior run is still alive (the exact pile-up that spiked the box), the OLDER
# instance has overstayed its slot -> reap it, keep the freshest. Only ever fires when a NEWER
# instance of the SAME script exists (proof it's a restartable scheduled job, not a unique long-runner).
STACK_GRACE_S = _envf("RUNAWAY_STACK_GRACE_S", 150)     # overlap tolerated before reaping the stale one

# QUARANTINE — self-heal escalation. Whacking the same chronic offender every 10min forever (see the
# salesintel_scraper history) is not self-healing. After N kills of the same script in the window, the
# guard disables that job's launchd plist (reversible) + alerts, so it stops relaunching-and-hanging.
QUARANTINE_KILLS = int(_envf("RUNAWAY_QUARANTINE_KILLS", 3))
QUARANTINE_WINDOW_S = _envf("RUNAWAY_QUARANTINE_WINDOW_S", 86400)  # 24h

# A process is KILLABLE only if its full command matches one of these.
#
# 2026-07-13 cascade fix: the stack-script patterns are matched on the full command
# line, which for an autonomous BUILD AGENT (`claude -p "...run tools/x.py..."`,
# `codex exec ... "...update agents/y.py..."`) contains stack .py/-m PATH SUBSTRINGS
# *inside the prompt text*. That made unrelated claude+codex agents classify as the
# SAME "script", so the stacking pre-pass reaped the older one as a "duplicate
# overstayer" (see runaway_guard.incidents.jsonl: dozens of claude/codex reaps
# mislabelled tools/hailports_signals.py, agents/docsapp_copy_variants.py, ...) and
# the CPU path could SIGKILL a mid-cycle build agent. The interpreter is the tell: a
# real stack job is EXECUTED by a python interpreter; a path merely NAMED in an agent
# prompt is not. So the .py/-m patterns now only count when a python interpreter is the
# actual executable. Direct-exec binaries (headless chrome / ffmpeg) are unaffected.
_DIRECT_EXEC_PATTERNS = (
    re.compile(r"chrome-headless-shell"),       # playwright headless (engagement agents)
    re.compile(r"ms-playwright"),
    re.compile(r"(?:^|/)ffmpeg\b"),
)
_STACK_SCRIPT_PATTERNS = (
    re.compile(r"/claude-stack/[^ ]*\.py"),                     # absolute stack script path
    re.compile(r"(?:^|/| )(?:agents|tools|core|apps)/[\w./-]+\.py"),  # relative stack script
    re.compile(r"-m\s+(?:agents|tools|core|apps)\."),           # module form (-m agents.x)
)
# Executables that just WRAP the real command; skip them to find argv[0].
_WRAPPER_EXES = frozenset(
    ("timeout", "gtimeout", "nice", "ionice", "stdbuf", "caffeinate", "env")
)
_PY_EXE_RE = re.compile(r"^(?:python[0-9.]*|Python)$")
# Never killed even if matched (guards/healers/work/harness/persistent infra).
PROTECT_TOKENS = (
    "runaway_guard", "redacted_sentinel", "ram_warden", "jit_healer",
    "invariants_guard", "eternal_guardian", "outcome_heal", "crash_supervisor",
    "alert_gateway", "chief_of_staff", "coordinator",
    "CompanyA", "salesforce", "sfdx", " sf ", "force-app",
    "ollama", "Google Chrome.app", "Screens", "WindowServer", "launchd",
    "hailports_guides_server", "public_case_study_dashboard",  # public HTTP surfaces: SIGSTOP'ing them = a live outage (this alert)
    "case_study_channel", "hailports_engagement", "hailports_short_distributor", "agent_run.sh",  # revenue engines: freezing = lost posts/follows
)
# Legit long-running high-CPU jobs (extend via ~/.runaway-guard.allow).
ALLOW_TOKENS = (
    "rtmp", "a.rtmp.youtube", "youtube.com/live", "dashboard_livestream",
    "livestream", "_stream_", "casestudy_stream", "hailports_stream",
    "build_manuals", "manuals.sh", "sf project retrieve", "sf data",
    "whisper", "ggml", "llama", "model retrieve",
)

# Runaway *system daemon* path (SEPARATE from the user-automation path above).
# These are stateless, on-demand, launchd-managed Apple daemons that are documented-
# safe to SIGKILL: launchd relaunches them instantly and clients transparently
# re-request. A sustained >85%-of-a-core spin in any of these is ALWAYS a bug (a
# keychain/cert op is sub-second), never legitimate work. This is the ONLY path that
# may signal a non-stack process, and it is gated by exact basename match against this
# frozen allowlist — it can never touch one of Operator's jobs. Matched on argv[0]'s
# basename (secd -> "/usr/libexec/secd", ctkd -> ".../CryptoTokenKit.framework/.../ctkd").
SYSTEM_DAEMON_TARGETS = frozenset(
    t.strip() for t in os.environ.get("RUNAWAY_SYSDAEMON_TARGETS", "secd,ctkd").split(",") if t.strip()
)
# A spinning keychain daemon is an emergency (it serializes the whole machine), so it
# gets a much tighter sustain window than the 600s user-job default.
SYSDAEMON_SUSTAIN_SECONDS = _envf("RUNAWAY_SYSDAEMON_SUSTAIN_S", 90)


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _parse_clock(s: str) -> float:
    """Parse ps cputime ('MMM:SS.ss', 'H:MM:SS') or etime ('DD-HH:MM:SS') -> seconds."""
    s = s.strip()
    if not s:
        return 0.0
    days = 0.0
    if "-" in s:
        d, s = s.split("-", 1)
        try:
            days = float(d)
        except ValueError:
            days = 0.0
    parts = s.split(":")
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return 0.0
    secs = 0.0
    for n in nums:
        secs = secs * 60 + n
    return days * 86400 + secs


def snapshot() -> list[dict]:
    try:
        out = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,rss=,time=,etime=,command="],
            capture_output=True, text=True, timeout=8,
        ).stdout
    except Exception as e:
        log(f"ps failed: {e}")
        return []
    rows = []
    for line in out.splitlines():
        parts = line.split(None, 5)
        if len(parts) != 6:
            continue
        pid, ppid, rss, cputime, etime, command = parts
        try:
            pid_i, ppid_i = int(pid), int(ppid)
            rss_kb = int(rss)
        except ValueError:
            continue
        rows.append({
            "pid": pid_i, "ppid": ppid_i, "rss_kb": rss_kb,
            "cputime_s": _parse_clock(cputime),
            "etime_s": _parse_clock(etime),
            "command": command,
        })
    return rows


_ALLOW_REFRESH_S = 3600  # re-derive the launchd allowlist at most hourly


def _maybe_refresh_allowlist() -> None:
    """Keep ~/.runaway-guard.allow auto-derived from the current launchd job set so a
    newly-added scheduled job is exempt without hand-editing. Throttled + fail-open:
    any error leaves the existing file untouched (the guard still reads it)."""
    try:
        stamp = HOME / ".runaway-guard.allow.refresh"
        if stamp.exists() and (time.time() - stamp.stat().st_mtime) < _ALLOW_REFRESH_S:
            return
        from tools import gen_runaway_allowlist as gen
        gen.write_allowlist(gen.derive_tokens())
        stamp.write_text(str(time.time()))
    except Exception as e:
        log(f"allowlist refresh skipped: {e}")


def _load_allow_tokens() -> tuple[str, ...]:
    extra = []
    try:
        if ALLOW_FILE.exists():
            for ln in ALLOW_FILE.read_text().splitlines():
                ln = ln.strip()
                if ln and not ln.startswith("#"):
                    extra.append(ln)
    except Exception:
        pass
    return ALLOW_TOKENS + tuple(extra)


def _executed_program(command: str) -> str:
    """Basename of argv[0], skipping leading wrappers (timeout N, nice, env VAR=…).
    This is the process actually running — NOT a path named later in a prompt."""
    toks = command.split()
    i = 0
    while i < len(toks):
        base = toks[i].rsplit("/", 1)[-1]
        if base in ("timeout", "gtimeout"):
            i += 2                      # skip 'timeout' and its duration arg
            continue
        if base in _WRAPPER_EXES:
            i += 1
            while i < len(toks) and (toks[i].startswith("-") or ("=" in toks[i] and not toks[i].startswith("/"))):
                i += 1
            continue
        break
    return toks[i].rsplit("/", 1)[-1] if i < len(toks) else ""


def _is_python_exec(command: str) -> bool:
    return bool(_PY_EXE_RE.match(_executed_program(command)))


def is_candidate(command: str) -> bool:
    if any(p.search(command) for p in _DIRECT_EXEC_PATTERNS):
        return True
    # a stack .py/-m counts ONLY when python is the real executable — never when the
    # path is merely a substring of an agent's prompt (claude -p / codex exec).
    return _is_python_exec(command) and any(p.search(command) for p in _STACK_SCRIPT_PATTERNS)


def _argv0_basename(command: str) -> str:
    first = command.split(None, 1)[0] if command else ""
    return first.rsplit("/", 1)[-1]


def is_system_daemon(command: str) -> bool:
    return _argv0_basename(command) in SYSTEM_DAEMON_TARGETS


def is_protected(command: str) -> bool:
    return any(tok in command for tok in PROTECT_TOKENS)


def is_allowlisted(command: str, allow: tuple[str, ...]) -> bool:
    return any(tok in command for tok in allow)


def killable(command: str, allow: tuple[str, ...]) -> bool:
    return is_candidate(command) and not is_protected(command) and not is_allowlisted(command, allow)


def load_state(state_file: Path = STATE_FILE) -> dict:
    try:
        return json.loads(state_file.read_text())
    except Exception:
        return {}


def save_state(state: dict, state_file: Path = STATE_FILE) -> None:
    try:
        tmp = state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(state))
        tmp.replace(state_file)
    except Exception as e:
        log(f"state save failed: {e}")


def _kill_now(pid: int, dry_run: bool) -> str:
    """Immediate SIGKILL — for stateless system daemons that launchd respawns.
    No SIGTERM grace: the point is to break the spin loop instantly. EPERM (a
    root-owned daemon, signalled from this user-context guard) is reported, not
    raised, so a future root LaunchDaemon deployment can act where this can't."""
    if dry_run:
        return "dry-run"
    try:
        os.kill(pid, signal.SIGKILL)
        return "kill"
    except ProcessLookupError:
        return "already-gone"
    except PermissionError:
        return "eperm-needs-root"
    except Exception as e:
        return f"sigkill-err:{e}"


def _kill(pid: int, dry_run: bool) -> str:
    if dry_run:
        return "dry-run"
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return "already-gone"
    except Exception as e:
        return f"sigterm-err:{e}"
    for _ in range(10):                      # up to ~5s for graceful exit
        time.sleep(0.5)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return "term"
        except Exception:
            return "term"
    try:
        os.kill(pid, signal.SIGKILL)
        return "kill"
    except ProcessLookupError:
        return "term"
    except Exception as e:
        return f"sigkill-err:{e}"


_SCRIPT_PATH_RE = re.compile(r"(?:^|/| )((?:agents|tools|core|apps)/[\w./-]+\.py)")
_MODULE_RE = re.compile(r"-m\s+((?:agents|tools|core|apps)\.[\w.]+)")


def script_identity(command: str) -> str | None:
    """Stable key for a stack script (stacking + quarantine): the -m module or the
    agents/tools/core/apps relative .py path. None if it isn't a stack script.
    Gated on python-exec so an agent prompt that merely NAMES a stack path (claude -p
    / codex exec) never mints an identity — that was the 2026-07-13 reap cascade."""
    if not _is_python_exec(command):
        return None
    m = _MODULE_RE.search(command)
    if m:
        return m.group(1)
    m = _SCRIPT_PATH_RE.search(command)
    if m:
        return m.group(1)
    return None


QUARANTINE_FILE = HOME / ".runaway-guard.quarantine.json"


def _load_quar() -> dict:
    try:
        return json.loads(QUARANTINE_FILE.read_text())
    except Exception:
        return {}


def _save_quar(q: dict) -> None:
    try:
        tmp = QUARANTINE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(q))
        tmp.replace(QUARANTINE_FILE)
    except Exception:
        pass


def _alert(title: str, body: str) -> None:
    try:
        from core.alert_gateway import route
        route("warn", "runaway_guard", title, body)
    except Exception:
        pass


def _disable_plist_for(script_id: str, dry_run: bool) -> list[str]:
    """Find launchd plists whose ProgramArguments reference this script and bootout+disable them
    (reversible: launchctl enable + bootstrap). Never disables a guard/sentinel. Returns labels."""
    import plistlib
    acted: list[str] = []
    la = HOME / "Library" / "LaunchAgents"
    try:
        uid = os.getuid()
        for pl in la.glob("*.plist"):
            try:
                d = plistlib.load(open(pl, "rb"))
            except Exception:
                continue
            argv = " ".join(str(a) for a in d.get("ProgramArguments", []))
            # Boundary-anchored: a bare substring test lets `agents.lead` also match — and then
            # bootout+disable — a legit sibling like `agents.lead_harvester`, the exact cascade-kill
            # class of the 2026-07-12 incident. Anchor to a token boundary so it can only ever hit
            # the precise offender. (Purely narrows what gets disabled; never widens it.)
            if not re.search(rf'(?:^|[\s/]){re.escape(script_id)}(?:$|\s)', argv):
                continue
            label = d.get("Label") or pl.stem
            if any(t in label for t in ("runaway", "guard", "sentinel", "warden")):
                continue
            if dry_run:
                acted.append(f"{label}(dry)")
                continue
            try:
                subprocess.run(["launchctl", "bootout", f"gui/{uid}/{label}"], capture_output=True, timeout=10)
                subprocess.run(["launchctl", "disable", f"gui/{uid}/{label}"], capture_output=True, timeout=10)
                acted.append(label)
            except Exception:
                pass
    except Exception:
        pass
    return acted


def record_kill_and_maybe_quarantine(script_id: str | None, now: float, dry_run: bool) -> None:
    """Self-heal escalation: count kills per script in a rolling window; once a script is killed
    QUARANTINE_KILLS times it's a chronic offender (relaunch-and-hang) — disable its job + alert,
    so the guard stops whacking the same thing forever."""
    if not script_id:
        return
    q = _load_quar()
    rec = q.get(script_id, {})
    hits = [t for t in rec.get("kills", []) if now - t < QUARANTINE_WINDOW_S]
    hits.append(now)
    rec["kills"] = hits
    if len(hits) >= QUARANTINE_KILLS and not rec.get("quarantined"):
        labels = _disable_plist_for(script_id, dry_run)
        rec["quarantined"] = now
        rec["disabled_labels"] = labels
        if labels:
            _alert(f"runaway_guard QUARANTINED {script_id}",
                   f"killed {len(hits)}x/24h — disabled launchd job(s): {', '.join(labels)}. "
                   f"reversible (launchctl enable + bootstrap); fix the hang before re-enabling.")
            log(f"[QUARANTINE] {script_id} -> disabled {labels}")
        else:
            _alert(f"runaway_guard repeat offender {script_id}",
                   f"killed {len(hits)}x/24h but no launchd plist references it — something else keeps "
                   f"launching it (demon/coordinator). can't auto-disable; needs a look.")
            log(f"[QUARANTINE] {script_id} -> no plist; alerted")
    q[script_id] = rec
    _save_quar(q)


def run_once(dry_run: bool = False, system_only: bool = False) -> dict:
    now = time.time()
    if not system_only:
        _maybe_refresh_allowlist()
    allow = _load_allow_tokens()
    self_pid = os.getpid()
    procs = snapshot()
    # system-only runs (the root LaunchDaemon) keep a SEPARATE state file so they
    # never collide with the user guard's per-pid tracking.
    state_file = STATE_FILE.with_suffix(".sys.json") if system_only else STATE_FILE
    old = load_state(state_file)
    new: dict = {}
    actions: list[dict] = []
    kills = 0

    # STACKING pre-pass (skipped for system-daemon-only root runs): a scheduled singleton with >1
    # live instance is a pile-up. Keep the freshest; mark older siblings that overstayed the grace
    # window for reaping. Only ever fires when a NEWER instance of the SAME script exists.
    stack_reap: dict[int, str] = {}
    if not system_only:
        by_script: dict[str, list] = {}
        for p in procs:
            if p["pid"] in (self_pid, os.getppid(), 1):
                continue
            cmd = p["command"]
            sid = script_identity(cmd)
            if not sid or is_protected(cmd) or is_allowlisted(cmd, allow):
                continue
            by_script.setdefault(sid, []).append(p)
        for sid, group in by_script.items():
            if len(group) < 2:
                continue
            group.sort(key=lambda q: q["etime_s"])   # youngest (least elapsed) first
            for q in group[1:]:                       # keep the youngest, consider older siblings
                if q["etime_s"] > STACK_GRACE_S:
                    stack_reap[q["pid"]] = sid

    for p in procs:
        pid, cmd = p["pid"], p["command"]
        if pid in (self_pid, os.getppid(), 1):
            continue
        sysdaemon = is_system_daemon(cmd)
        # --system-only (root daemon): ONLY ever consider the daemon allowlist.
        # It is then structurally incapable of touching one of Operator's jobs.
        if system_only and not sysdaemon:
            continue
        if not is_candidate(cmd) and not sysdaemon:
            continue

        # STACKING reap: an older duplicate that overstayed its slot — kill regardless of CPU.
        if pid in stack_reap and kills < MAX_KILLS_PER_CYCLE:
            result = _kill(pid, dry_run)
            kills += 1
            act = {"pid": pid, "kind": "stacking", "script": stack_reap[pid],
                   "etime_s": round(p["etime_s"]), "result": result, "cmd": cmd[:200]}
            actions.append(act)
            log(f"[STACK-KILL] pid={pid} ({stack_reap[pid]}) duplicate overstayer result={result}")
            try:
                INCIDENT_FILE.parent.mkdir(parents=True, exist_ok=True)
                with INCIDENT_FILE.open("a") as f:
                    f.write(json.dumps({"ts": now, **act, "dry_run": dry_run}) + "\n")
            except Exception:
                pass
            # NB: stacking reaps do NOT feed quarantine. A slow-but-legit singleton that
            # overstays its slot is not a runaway; counting these toward the quarantine
            # threshold disabled ~13 legit jobs (work_ledger, fastctl, mockup-regen, ...).
            # Only genuine sustained-CPU runaways (below, line ~587) may escalate to disable.
            continue
        start_epoch = now - p["etime_s"]
        key = str(pid)
        prev = old.get(key)
        # PID-reuse / restart guard
        if prev and abs(prev.get("start_epoch", start_epoch) - start_epoch) > STALE_PID_RESET:
            prev = None

        entry = {
            "pid": pid, "start_epoch": start_epoch,
            "last_cputime": p["cputime_s"], "last_ts": now,
            "cmd": cmd[:200],
            "samples": (prev.get("samples", 0) if prev else 0),
            "high_since": (prev.get("high_since") if prev else None),
            "rss_lo": (prev.get("rss_lo") if prev else None),
            "rss_hi": (prev.get("rss_hi") if prev else None),
        }

        frac = None
        if prev:
            dwall = now - prev.get("last_ts", now)
            dcpu = p["cputime_s"] - prev.get("last_cputime", p["cputime_s"])
            if dwall >= 1 and dcpu >= 0:
                frac = dcpu / dwall
                entry["samples"] = prev.get("samples", 0) + 1

        rss = p.get("rss_kb", 0)
        if frac is not None and frac >= CPU_FRAC:
            if not entry["high_since"]:
                entry["high_since"] = now            # open a fresh high-CPU progress window
                entry["rss_lo"] = entry["rss_hi"] = rss
            else:
                lo = entry.get("rss_lo"); hi = entry.get("rss_hi")
                entry["rss_lo"] = min(rss if lo is None else lo, rss)
                entry["rss_hi"] = max(rss if hi is None else hi, rss)
        else:
            entry["high_since"] = None
            entry["rss_lo"] = entry["rss_hi"] = None

        sustained = (now - entry["high_since"]) if entry["high_since"] else 0.0
        # PROGRESS DISCRIMINATOR (runaway vs resource-intensive, no allowlist needed): memory span
        # across the high-CPU window. Flat RSS = a busy-loop making no progress (runaway); moving RSS
        # = real work (render / inference / an active scrape paging through data).
        rss_span = ((entry["rss_hi"] or 0) - (entry["rss_lo"] or 0)) if entry["high_since"] else 0
        rss_flat = entry["high_since"] is not None and rss_span < RSS_FLAT_KB

        if sysdaemon:
            runaway = (
                entry["high_since"] is not None
                and sustained >= SYSDAEMON_SUSTAIN_SECONDS
                and entry["samples"] >= MIN_SAMPLES
            )
        else:
            # flat RSS (no progress) -> short fuse; a job whose memory keeps moving (real work) is
            # left alone until the absolute HARD_CEILING. Either way it's bounded, never indefinite.
            runaway = (
                entry["high_since"] is not None
                and entry["samples"] >= MIN_SAMPLES
                and ((rss_flat and sustained >= SUSTAIN_SECONDS) or sustained >= HARD_CEILING_SECONDS)
            )

        # System-daemon path: frozen-allowlist basename only (never a stack job),
        # immediate SIGKILL, tighter window. Kept entirely separate from the
        # user-automation kill below so it can never touch one of Operator's jobs.
        if sysdaemon:
            if runaway and kills < MAX_KILLS_PER_CYCLE:
                result = _kill_now(pid, dry_run)
                kills += 1
                act = {
                    "pid": pid, "kind": "sysdaemon", "frac": round(frac or 0, 2),
                    "sustained_s": round(sustained), "cputime_s": round(p["cputime_s"]),
                    "result": result, "cmd": cmd[:200],
                }
                actions.append(act)
                log(f"[SYSDAEMON-KILL] pid={pid} ({_argv0_basename(cmd)}) frac={act['frac']} "
                    f"sustained={act['sustained_s']}s result={result}")
                try:
                    INCIDENT_FILE.parent.mkdir(parents=True, exist_ok=True)
                    with INCIDENT_FILE.open("a") as f:
                        f.write(json.dumps({"ts": now, **act, "dry_run": dry_run}) + "\n")
                except Exception:
                    pass
                continue  # drop killed pid from state
            new[key] = entry
            continue

        if runaway and killable(cmd, allow):
            if kills >= MAX_KILLS_PER_CYCLE:
                log(f"[hold] kill cap hit, deferring pid={pid}")
            else:
                result = _kill(pid, dry_run)
                kills += 1
                act = {
                    "pid": pid, "frac": round(frac or 0, 2),
                    "sustained_s": round(sustained), "cputime_s": round(p["cputime_s"]),
                    "rss_span_kb": rss_span, "rss_flat": rss_flat,
                    "result": result, "cmd": cmd[:200],
                }
                actions.append(act)
                log(f"[KILL] pid={pid} frac={act['frac']} sustained={act['sustained_s']}s "
                    f"rss_flat={rss_flat} cputime={act['cputime_s']}s result={result} :: {cmd[:140]}")
                try:
                    INCIDENT_FILE.parent.mkdir(parents=True, exist_ok=True)
                    with INCIDENT_FILE.open("a") as f:
                        f.write(json.dumps({"ts": now, **act, "dry_run": dry_run}) + "\n")
                except Exception:
                    pass
                record_kill_and_maybe_quarantine(script_identity(cmd), now, dry_run)
                # drop killed pid from state
                continue
        elif runaway and not killable(cmd, allow):
            reason = "protected" if is_protected(cmd) else ("allowlisted" if is_allowlisted(cmd, allow) else "unmatched")
            log(f"[skip:{reason}] runaway pid={pid} sustained={round(sustained)}s :: {cmd[:120]}")

        new[key] = entry

    save_state(new, state_file)
    return {"tracked": len(new), "kills": kills, "actions": actions, "dry_run": dry_run}


def selftest() -> int:
    allow = _load_allow_tokens()
    cases = [
        # (command, expect_killable)
        ("/opt/homebrew/bin/python3.14 -m agents.hailports_engagement", False),  # now a PROTECT revenue engine
        ("/Users/x/claude-stack/agents/hailports_engagement.py", False),         # now a PROTECT revenue engine
        ("/opt/homebrew/bin/python3.14 -m agents.aeo_agency_leads_harvester", True),
        (".../chrome-headless-shell --disable-dev-shm-usage --headless", True),
        ("ffmpeg -re -i x.png -f flv rtmp://a.rtmp.youtube.com/live2/abc", False),  # livestream
        ("/opt/homebrew/bin/ffmpeg -i in.mp4 out.mp4", True),                       # non-stream ffmpeg
        ("python -m tools.redacted_sentinel --daemon", False),                       # protected
        ("python -m tools.runaway_guard --once", False),                            # protected (self)
        ("claude", False),                                                          # not a candidate
        ("/Applications/Google Chrome.app/.../Helper --user-data-dir=.chrome-cdp", False),  # protected
        ("sf project retrieve start -m Flow", False),                               # not a candidate anyway
        ("/System/.../WindowServer -daemon", False),
        ("python -m core.jit_healer", False),                                       # protected
        # 2026-07-13 cascade: an autonomous build agent that NAMES a stack path in its
        # prompt is NOT that script and must never be killable as it.
        ("/Users/x/.npm-global/bin/claude -p AUTONOMOUS CYCLE work in claude-stack run tools/hailports_signals.py", False),
        ("timeout 5400 /opt/homebrew/bin/codex exec -m gpt-5.5 -C /x/claude-stack update agents/docsapp_copy_variants.py", False),
        ("node /opt/homebrew/bin/codex exec --sandbox -C /x/claude-stack edit core/pii_guard.py", False),
        # but a genuinely python-executed stack script (unregistered) IS killable.
        ("timeout 600 /x/.venv/bin/python3 -m agents.some_unregistered_loop", True),
        ("/opt/homebrew/bin/python3 /x/claude-stack/agents/some_unregistered_loop.py", True),
    ]
    ok = True
    for cmd, expect in cases:
        got = killable(cmd, allow)
        flag = "PASS" if got == expect else "FAIL"
        if got != expect:
            ok = False
        print(f"  [{flag}] killable={got} expect={expect} :: {cmd[:70]}")

    # System-daemon path: ONLY the frozen allowlist matches; a stack job never does.
    sys_cases = [
        ("/usr/libexec/secd", True),
        ("/System/Library/Frameworks/CryptoTokenKit.framework/Versions/A/XPCServices/"
         "ctkd.xpc/Contents/MacOS/ctkd", True),
        ("/usr/libexec/PerfPowerServices", False),   # root daemon, intentionally out of scope
        ("/Users/x/claude-stack/agents/hailports_engagement.py", False),  # a real job — never
        ("python -m tools.runaway_guard --once", False),
        ("ffmpeg -re -i x.png -f flv rtmp://a.rtmp.youtube.com/live2/abc", False),  # the stream
    ]
    for cmd, expect in sys_cases:
        got = is_system_daemon(cmd)
        flag = "PASS" if got == expect else "FAIL"
        if got != expect:
            ok = False
        print(f"  [{flag}] sysdaemon={got} expect={expect} :: {cmd[:70]}")

    # script identity — the stacking + quarantine key (must be stable + None for non-stack procs).
    id_cases = [
        ("/opt/homebrew/bin/python3.14 agents/aeo_agency_leads_harvester.py --limit 40",
         "agents/aeo_agency_leads_harvester.py"),
        ("/x/.venv/bin/python -m agents.revenue_demon", "agents.revenue_demon"),
        ("/usr/libexec/secd", None),
        ("claude", None),
        # agent-prompt path substrings must NOT mint an identity (reap-cascade guard).
        ("/x/.npm-global/bin/claude -p run tools/hailports_signals.py now", None),
        ("node /opt/homebrew/bin/codex exec -C /x edit agents/docsapp_copy_variants.py", None),
    ]
    for cmd, expect in id_cases:
        got = script_identity(cmd)
        flag = "PASS" if got == expect else "FAIL"
        if got != expect:
            ok = False
        print(f"  [{flag}] script_id={got} expect={expect} :: {cmd[:60]}")

    print("selftest:", "ALL PASS" if ok else "FAILURES")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--daemon", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--system-only", action="store_true",
                    help="only police the system-daemon allowlist (for the root LaunchDaemon)")
    args = ap.parse_args()

    if args.selftest:
        return selftest()
    if args.status:
        sf = STATE_FILE.with_suffix(".sys.json") if args.system_only else STATE_FILE
        print(json.dumps(load_state(sf), indent=2))
        return 0
    if args.daemon:
        log("runaway_guard daemon start")
        while True:
            try:
                run_once(dry_run=args.dry_run, system_only=args.system_only)
            except Exception as e:
                log(f"cycle error: {e}")
            time.sleep(INTERVAL)
    # default: one cycle
    res = run_once(dry_run=args.dry_run, system_only=args.system_only)
    print(json.dumps(res))
    return 0


if __name__ == "__main__":
    sys.exit(main())
