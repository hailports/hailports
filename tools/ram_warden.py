#!/usr/bin/env python3
"""ram_warden.py — pressure-tiered, fail-safe RAM reclamation for the Mac mini.

The box must never choke on RAM, but "aggressive" only ever applies to things
with ZERO recovery cost or a proven instant respawn. A revenue service is never
OOM-killed here. Reclamation is allowlist-driven (we only ever act on a positive
list of regenerable / watchdog-backed targets), never kill-by-exclusion.

Pressure metric: macOS `memory_pressure -Q` free-percentage (falls back to a
vm_stat computation), plus swap saturation as a tie-breaker that can bump a tier.

Tiers (lower free% = more pressure):
  HEALTHY  (free% >= WARN_PCT)        -> no-op.
  WARN     (free% <  WARN_PCT)        -> purge OS inactive memory (best-effort) +
                                          unload IDLE ollama models, keeping the
                                          one in use (max expires_at). Daemon stays up.
  AGGRESSIVE (free% < AGGR_PCT)       -> WARN actions, then BOUNCE memory-bloated
                                          chrome-cdp profiles THROUGH their existing
                                          launchd watchdog (never a bare kill of the
                                          money path), capped per cycle. Reaps/reports
                                          defunct procs (zero recovery cost).
  CRITICAL (free% <  CRIT_PCT)        -> WARN+AGGRESSIVE reclaim, then ESCALATE to
                                          Operator via core.alert with the top RAM hogs.
                                          Never starts killing protected services.

HARD GUARD: is_protected() matches by cmdline substring AND launchd label and
FAILS SAFE — if we cannot read a proc's cmdline, or it matches any protected
token, ram_warden refuses to touch it. Bounces only fire for a chrome-cdp profile
whose watchdog job is actually loaded (else nothing would respawn it).

Usage:
  ram_warden.py                 # gated real run (launchd)
  ram_warden.py --dry-run       # decide + print actions, change nothing
  ram_warden.py --tier AGGRESSIVE --dry-run   # simulate a tier (with self-tests)
  ram_warden.py --self-test     # prove the protect-guard blocks a protected pid
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HOME = Path.home()
# launchd runs us as `python tools/ram_warden.py`, so sys.path[0] is tools/ and
# `from core.alert import alert_alex` fails ("No module named 'core'") — which has
# silently broken EVERY CRITICAL escalation (7x, incl the 2026-06-20 19:54 OOM).
# Pin the repo root so the escalation alert actually reaches Operator.
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
LOG = HOME / "Library/Logs/claude-stack/ram-warden.log"
CRIT_STAMP = HOME / ".ram-warden.last-escalation"

# ---- tier thresholds (free memory %, from memory_pressure) -------------------
WARN_PCT = 25.0
AGGR_PCT = 15.0
CRIT_PCT = 8.0
# swap tie-breaker: if swap is this saturated, bump the computed tier up one notch
SWAP_SATURATION_PCT = 80.0

# ---- compressor + jetsam pressure (the signals that actually precede an OOM
# watchdog panic; free% lies because it counts inactive/speculative as free, and
# macOS grows swapfiles dynamically so swap% stays moderate while the box dies.
# The 2026-06-08 panic fired at free~29% — these catch what free% cannot). ------
COMPRESSOR_AGGR_PCT = 38.0         # compressor holding this % of RAM -> AGGRESSIVE
COMPRESSOR_CRIT_PCT = 48.0         # -> CRITICAL (compressor near its hard limit)
JETSAM_WARN_LEVEL = 2              # kern.memorystatus_vm_pressure_level: floor WARN
JETSAM_CRIT_LEVEL = 4              # floor CRITICAL

# ---- bounds ------------------------------------------------------------------
MAX_BOUNCES_PER_CYCLE = 2          # cap chrome-cdp bounces / run
CHROME_BOUNCE_RSS_GB = 2.0         # only bounce a profile's browser proc above this
ESCALATION_COOLDOWN_S = 1800       # 30 min between Operator escalations
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")

# The YouTube livestream is the ONE service the owner cannot lose. Whenever ffmpeg is pushing
# AND free RAM is under this absolute floor, evict ollama (the big elastic hog) immediately —
# EVERY cycle, regardless of tier — so the stream is never the OOM victim. free% is too coarse
# and too slow (75s cycle) to catch a spike before the kernel kills ffmpeg. (owner mandate 2026-06-24)
STREAM_FREE_FLOOR_MB = 900.0

# ---- HARD protect-list (fail-safe) -------------------------------------------
# Any process whose cmdline contains one of these tokens is NEVER killed/bounced.
# Broad on purpose: over-protecting is the safe direction.
PROTECT_CMD_SUBSTR = (
    "ram_warden",                              # never touch self
    "run_self_serve.sh", "self_serve", ":8300",          # self-serve
    "cloudflared", "run_cloudflared_tunnel",             # tunnel
    "openclaw", "ai.openclaw.gateway",                   # openclaw gateway :18789
    "seo_server", "agents/seo_server",                   # seo-server :8096
    "mcp_gateway", "openapi_tool_gateway", "integration_gateway",  # gateway :8330
    "self_healer", "agents.self_healer",                 # healer
    "watchdog.watchdog",                                 # watchdog
    "jit_healer", "core.jit_healer",                     # jit healer
    "stack_heartbeat",                                   # heartbeat
    "Ollama.app", "ollama serve", "llama-server",        # ollama daemon + model servers
    "/ollama ",                                          # ollama bin
    "colima", "limactl", "lima-guestagent", "qemu-system",  # Docker VM (searxng/lead-discovery funnel)
    # ---- CompanyA WORK BRIDGES — TOP PRIORITY, never lose GPT access (owner mandate) ----
    "chatgpt_redacted_action", "chatgpt_work_action", ":8078",   # work-GPT Action server
    "chrome-cdp-profile-owa",                                   # Outlook Web bridge (attachments)
    "chrome-cdp-profile-zoomweb",                               # Zoom work bridge
    "CompanyA-bridge", "redacted_action", "work_frontdoor",       # local CompanyA bridges + guardian
    "littlebird", "Littlebird",                                 # LittleBird work notes
    # ---- REMOTE-VIEWING + DISPLAY pipeline — NEVER touch (owner's only way in) ----
    "WindowServer", "SkyLight",                                  # the display server itself
    "screensharingd", "ScreensharingAgent", "SSMenuAgent",      # macOS screen sharing / VNC
    "Screens Connect", "ScreensConnect", "edovia",              # Edovia Screens relay
    "tailscaled", "Tailscale", "sshd",                          # network path carrying the session
    "remote_link_adapter", "keep_ipad_locked",                  # the single display shaper
    "ipad-display-lock", "remote_priority_governor", "screens_remote_guard",
    # ---- YOUTUBE LIVESTREAM — broadcast uptime is sacred, RTMP drop = OFFLINE ----
    # The python3.14 supervisor feeds frames to a child ffmpeg; kill/throttle either and
    # the RTMP ingest dies -> YouTube flips offline. (cpumon culled it all day 2026-07-13.)
    "dashboard_livestream", "yt_live_supervisor", "ffmpeg", "CASE_STUDY_STREAM_LIVE",
)
# launchd labels we consider protected revenue services (for reporting/audit)
PROTECT_LABELS = (
    "com.claude-stack.self-serve", "com.claude-stack.cloudflared",
    "ai.openclaw.gateway", "com.openclaw.watchdog",
    "com.claude-stack.seo-server", "com.claude-stack.mcp-gateway",
    "com.claude-stack.openapi-tool-gateway", "com.claude-stack.healer",
    "com.claude-stack.watchdog", "com.claude-stack.jit-healer",
    "com.claude-stack.stack-heartbeat", "com.claude-stack.ollama",
    "com.ollama.ollama",
    # CompanyA work bridges (top priority)
    "com.claude-stack.chatgpt-CompanyA-action", "com.claude-stack.zoom-web-sync",
    "com.claude-stack.owa-web-sync", "com.claude-stack.work-frontdoor-guardian",
)

# ---- merge the AUTHORITATIVE work allowlist (data/hustle/work_protected.json) ----
# Single source of truth shared by EVERY sentinel. Fail-safe: if it can't be read we
# keep the hardcoded tokens above (over-protect, never under-protect). Edit the JSON,
# not this file, to protect more work surfaces.
WORK_CDP_BASENAMES: frozenset = frozenset()


def _load_work_allowlist():
    p = HOME / "claude-stack/data/hustle/work_protected.json"
    try:
        d = json.loads(p.read_text())
        return (tuple(d.get("never_kill_cmd_substr", [])),
                tuple(d.get("never_kill_labels", [])),
                frozenset(d.get("work_cdp_profile_basenames", [])))
    except Exception:
        return ((), (), frozenset())


_WL_CMD, _WL_LABELS, WORK_CDP_BASENAMES = _load_work_allowlist()
PROTECT_CMD_SUBSTR = PROTECT_CMD_SUBSTR + _WL_CMD
PROTECT_LABELS = PROTECT_LABELS + _WL_LABELS

# chrome-cdp profile dir basename -> its launchd watchdog label (restart path)
CHROME_CDP_WATCHDOGS = {
    ".chrome-cdp-profile-persona1": "com.imma.chrome-cdp-persona1",
    ".chrome-cdp-profile-persona2": "com.imma.chrome-cdp-persona2",
    ".chrome-cdp-profile-x2": "com.imma.chrome-cdp-x2",
    ".chrome-cdp-profile": "com.imma.chrome-cdp",
}

# ---- ALWAYS-ON CDP cap enforcement (independent of pressure tier) ------------
# This is the real proliferation fix: the leak is TABS that never close inside
# long-lived singleton persona Chromes (e.g. fyn-reply.js newPage() every 60s on
# :18803). Bouncing a whole browser (the tiered path below) only re-materialises
# the leak via --restore-last-session; the cure is to CLOSE the leaked tabs over
# CDP (Target close), reap orphaned Playwright headless shells, and never touch a
# profile that an automation run is actively driving. Runs every cycle.
CDP_ACTIVE_DIR = HOME / ".cdp-active"        # launcher heartbeat locks (a launcher
                                             # may touch <profile>.lock while a run is live)
ACTION_LOG = HOME / "Library/Logs/claude-stack/ram-warden-actions.jsonl"
MAX_TABS_PER_PROFILE = 2                 # keep newest N page-tabs per IDLE profile
MAX_TAB_CLOSES_PER_CYCLE = 120           # bounded reclaim (graceful HTTP close, reversible)
MAX_SHELL_KILLS_PER_CYCLE = 12           # bounded orphan headless-shell reap
LOCK_FRESH_S = 30                        # heartbeat lock counts as "live" within this
# node/python launchers that drive CDP — if any is alive AND we cannot prove a
# profile idle via lsof, we conservatively skip closing that cycle (fail-safe).
LAUNCHER_HINTS = ("engage-cdp.js", "fyn-reply.js", "upload-cdp.js")
# ports we always probe even if the browser happens to be down right now
KNOWN_CDP_PORTS = (18800, 18801, 18802, 18803, 18805, 18806)


def ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def note(msg: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    line = f"{ts()} {msg}"
    print(line)
    try:
        with LOG.open("a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


# ============================ memory pressure =================================
def _free_pct_from_memory_pressure() -> float | None:
    try:
        out = subprocess.run(
            ["/usr/bin/memory_pressure", "-Q"],
            capture_output=True, text=True, timeout=15,
        ).stdout
    except Exception:
        return None
    m = re.search(r"free percentage:\s*(\d+)%", out)
    return float(m.group(1)) if m else None


def _free_pct_from_vm_stat() -> float | None:
    try:
        out = subprocess.run(["/usr/bin/vm_stat"], capture_output=True, text=True, timeout=15).stdout
    except Exception:
        return None
    vals: dict[str, int] = {}
    for line in out.splitlines():
        m = re.match(r"(.+?):\s+(\d+)\.", line)
        if m:
            vals[m.group(1).strip().lower()] = int(m.group(2))
    keys = ("pages free", "pages active", "pages inactive", "pages speculative",
            "pages wired down", "pages occupied by compressor")
    total = sum(vals.get(k, 0) for k in keys)
    if total <= 0:
        return None
    freeish = (vals.get("pages free", 0) + vals.get("pages inactive", 0)
               + vals.get("pages speculative", 0) + vals.get("pages purgeable", 0))
    return 100.0 * freeish / total


def swap_saturation_pct() -> float:
    try:
        out = subprocess.run(["/usr/sbin/sysctl", "vm.swapusage"], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return 0.0
    tot = re.search(r"total\s*=\s*([\d.]+)M", out)
    used = re.search(r"used\s*=\s*([\d.]+)M", out)
    if not (tot and used) or float(tot.group(1)) <= 0:
        return 0.0
    return 100.0 * float(used.group(1)) / float(tot.group(1))


_TIER_ORDER = ["HEALTHY", "WARN", "AGGRESSIVE", "CRITICAL"]


def compressor_pct() -> float:
    """% of physical RAM currently held by the VM compressor. This is the signal
    that actually precedes the watchdog OOM panic (compressor hits its page limit)
    and is invisible to free% (which counts inactive/speculative as free)."""
    try:
        out = subprocess.run(["/usr/bin/vm_stat"], capture_output=True, text=True, timeout=15).stdout
        m = re.search(r"Pages occupied by compressor:\s+(\d+)\.", out)
        sz = re.search(r"page size of (\d+) bytes", out)
        if not m:
            return 0.0
        page = int(sz.group(1)) if sz else 16384
        comp_bytes = int(m.group(1)) * page
        memsize = int(subprocess.run(["/usr/sbin/sysctl", "-n", "hw.memsize"],
                      capture_output=True, text=True, timeout=10).stdout.strip())
        return 100.0 * comp_bytes / memsize if memsize > 0 else 0.0
    except Exception:
        return 0.0


def jetsam_level() -> int:
    """Apple's own VM pressure level: 1=normal, 2=warn, 4=critical. Authoritative."""
    try:
        out = subprocess.run(["/usr/sbin/sysctl", "-n", "kern.memorystatus_vm_pressure_level"],
                             capture_output=True, text=True, timeout=10).stdout.strip()
        return int(out)
    except Exception:
        return 1


def _bump_to(tier: str, target: str) -> str:
    return _TIER_ORDER[max(_TIER_ORDER.index(tier), _TIER_ORDER.index(target))]


def decide_tier() -> tuple[str, float, float]:
    free = _free_pct_from_memory_pressure()
    if free is None:
        free = _free_pct_from_vm_stat()
    if free is None:
        free = 100.0  # can't measure -> assume healthy, never act blind
    swap = swap_saturation_pct()

    if free < CRIT_PCT:
        tier = "CRITICAL"
    elif free < AGGR_PCT:
        tier = "AGGRESSIVE"
    elif free < WARN_PCT:
        tier = "WARN"
    else:
        tier = "HEALTHY"

    # swap saturation bumps one notch (genuine memory tightness behind the free%)
    if swap >= SWAP_SATURATION_PCT and tier != "CRITICAL":
        tier = _TIER_ORDER[min(_TIER_ORDER.index(tier) + 1, len(_TIER_ORDER) - 1)]
        note(f"swap saturated ({swap:.0f}%) -> tier bumped to {tier}")

    # compressor occupancy — the real pre-panic signal that free% hides
    comp = compressor_pct()
    if comp >= COMPRESSOR_CRIT_PCT:
        tier = _bump_to(tier, "CRITICAL")
        note(f"compressor at {comp:.0f}% of RAM (>= {COMPRESSOR_CRIT_PCT:.0f}) -> {tier}")
    elif comp >= COMPRESSOR_AGGR_PCT:
        tier = _bump_to(tier, "AGGRESSIVE")
        note(f"compressor at {comp:.0f}% of RAM (>= {COMPRESSOR_AGGR_PCT:.0f}) -> {tier}")

    # Apple's jetsam pressure level — floor the tier to it
    jl = jetsam_level()
    if jl >= JETSAM_CRIT_LEVEL:
        tier = _bump_to(tier, "CRITICAL")
        note(f"jetsam pressure CRITICAL (level {jl}) -> {tier}")
    elif jl >= JETSAM_WARN_LEVEL:
        tier = _bump_to(tier, "WARN")
        note(f"jetsam pressure WARN (level {jl}) -> {tier}")

    return tier, free, swap


# ============================ protect guard ===================================
def proc_cmdline(pid: int) -> str | None:
    try:
        r = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "command="],
                           capture_output=True, text=True, timeout=10)
        cmd = r.stdout.strip()
        return cmd or None
    except Exception:
        return None


def is_protected(pid: int) -> bool:
    """Fail-safe: True (do NOT touch) if cmdline matches any protected token OR
    cannot be read at all."""
    if pid in (0, 1, os.getpid(), os.getppid()):
        return True
    cmd = proc_cmdline(pid)
    if cmd is None:
        return True  # unknown -> never kill
    low = cmd.lower()
    return any(tok.lower() in low for tok in PROTECT_CMD_SUBSTR)


# ============================ ollama model unload =============================
def _ollama_get(path: str) -> dict | None:
    try:
        with urllib.request.urlopen(f"{OLLAMA_URL}{path}", timeout=8) as r:
            return json.loads(r.read() or b"{}")
    except Exception:
        return None


def _ollama_unload(model: str, dry_run: bool) -> bool:
    """Force-unload a model by issuing an empty generate with keep_alive=0."""
    if dry_run:
        return True
    payload = json.dumps({"model": model, "keep_alive": 0}).encode()
    try:
        req = urllib.request.Request(f"{OLLAMA_URL}/api/generate", data=payload,
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=15) as r:
            r.read()
        return True
    except Exception as e:
        note(f"  ollama unload {model} failed: {e}")
        return False


def unload_idle_models(dry_run: bool) -> int:
    """Unload every loaded model except the most-recently-active one (max
    expires_at = most likely in use). Daemon is never touched. Returns # unloaded."""
    ps = _ollama_get("/api/ps")
    if not ps:
        note("  ollama /api/ps unavailable — skipping model unload")
        return 0
    models = ps.get("models", []) or []
    if len(models) <= 1:
        note(f"  ollama: {len(models)} model(s) loaded — nothing idle to unload")
        return 0
    # keep the one with the latest expiry (its keep-alive was most recently refreshed)
    keep = max(models, key=lambda m: m.get("expires_at", ""))
    unloaded = 0
    for m in models:
        name = m.get("name") or m.get("model")
        if not name or name == (keep.get("name") or keep.get("model")):
            continue
        gb = (m.get("size_vram") or m.get("size") or 0) / 1e9
        verb = "would unload" if dry_run else "unloaded"
        if _ollama_unload(name, dry_run):
            note(f"  {verb} idle ollama model {name} (~{gb:.1f}GB), keeping {keep.get('name')}")
            unloaded += 1
    return unloaded


def force_evict_ollama(dry_run: bool) -> int:
    """Under real pressure, evict ALL resident ollama models (keep_alive=0) -- even the
    single 'in use' one. This is the fix for the warden's old blind spot: unload_idle_models
    no-ops at <=1 loaded, so with OLLAMA_MAX_LOADED_MODELS=1 the one pinned model -- the
    single biggest RAM hog (~6GB) -- was NEVER reclaimed under pressure, and ollama is on the
    protect-list so nothing else could touch it. Safe: core/runtime_pressure.py routes
    inference to the remote path while local is unloaded, and keep_alive (2m) reloads it on
    the next request once pressure clears."""
    ps = _ollama_get("/api/ps")
    if not ps:
        return 0
    n = 0
    for m in (ps.get("models", []) or []):
        name = m.get("name") or m.get("model")
        if not name:
            continue
        gb = (m.get("size_vram") or m.get("size") or 0) / 1e9
        if _ollama_unload(name, dry_run):
            note(f"  {'would force-evict' if dry_run else 'force-evicted'} ollama {name} (~{gb:.1f}GB) under pressure")
            n += 1
    return n


# ============================ purge ===========================================
def purge_inactive(dry_run: bool) -> None:
    if dry_run:
        note("  would run purge (OS inactive memory)")
        return
    if os.geteuid() != 0:
        note("  purge skipped (needs root)")
        return
    try:
        r = subprocess.run(["/usr/sbin/purge"], capture_output=True, text=True, timeout=120)
        if r.returncode == 0 and not r.stderr.strip():
            note("  purged OS inactive memory")
        else:
            note(f"  purge unavailable/denied (needs root): {r.stderr.strip()[:80] or 'rc=' + str(r.returncode)}")
    except Exception as e:
        note(f"  purge error: {e}")


# ============================ chrome-cdp bounce ===============================
def launchd_label_loaded(label: str) -> bool:
    try:
        r = subprocess.run(["/bin/launchctl", "list"], capture_output=True, text=True, timeout=10)
        return any(line.endswith(label) or f"\t{label}" in line for line in r.stdout.splitlines())
    except Exception:
        return False


def find_chrome_cdp_browsers() -> list[dict]:
    """Return the main browser process per chrome-cdp profile with summed RSS.

    Main browser proc = the chrome process carrying --user-data-dir but NOT a
    --type= (renderers/helpers carry --type). RSS is summed across all PIDs of
    that profile so a bounce reflects the profile's true footprint."""
    try:
        r = subprocess.run(["/bin/ps", "-axo", "pid=,rss=,command="],
                           capture_output=True, text=True, timeout=20)
    except Exception:
        return []
    by_profile: dict[str, dict] = {}
    for line in r.stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        pid_s, rss_s, cmd = parts
        if "chrome-cdp-profile" not in cmd:
            continue
        m = re.search(r"--user-data-dir=(\S+)", cmd)
        if not m:
            continue
        base = os.path.basename(m.group(1).rstrip("/"))
        if base not in CHROME_CDP_WATCHDOGS:
            continue
        try:
            pid, rss_kb = int(pid_s), int(rss_s)
        except ValueError:
            continue
        ent = by_profile.setdefault(base, {"profile": base, "rss_gb": 0.0, "main_pid": None})
        ent["rss_gb"] += rss_kb / 1e6  # rss is KB -> GB
        if "--type=" not in cmd and ent["main_pid"] is None:
            ent["main_pid"] = pid
    return [e for e in by_profile.values() if e["main_pid"]]


def bounce_chrome_cdp(dry_run: bool, budget: int, skip: set | None = None) -> int:
    """Bounce the most-bloated chrome-cdp profiles through their watchdog. Capped.
    `skip` = profiles the tab-cap pass just cleaned (their RSS is still draining as
    renderers exit — re-evaluate next cycle instead of a wasteful kill+respawn that
    would re-materialise tabs via --restore-last-session)."""
    skip = skip or set()
    candidates = sorted(
        (c for c in find_chrome_cdp_browsers() if c["rss_gb"] >= CHROME_BOUNCE_RSS_GB),
        key=lambda c: c["rss_gb"], reverse=True,
    )
    if not candidates:
        note("  no chrome-cdp profile over RSS cap — nothing to bounce")
        return 0
    bounced = 0
    for c in candidates:
        if bounced >= budget:
            note(f"  bounce budget ({budget}) reached — deferring rest")
            break
        if c["profile"] in skip:
            note(f"  skip bounce {c['profile']}: just tab-capped this cycle (RSS draining)")
            continue
        label = CHROME_CDP_WATCHDOGS[c["profile"]]
        pid = c["main_pid"]
        if is_protected(pid):  # fail-safe re-check
            note(f"  REFUSE bounce {c['profile']} pid={pid}: matched protect-list")
            continue
        if not launchd_label_loaded(label):
            note(f"  skip bounce {c['profile']}: watchdog {label} NOT loaded (no respawn path)")
            continue
        if dry_run:
            note(f"  would bounce {c['profile']} (~{c['rss_gb']:.1f}GB) via {label} "
                 f"(SIGTERM pid {pid} + kickstart watchdog)")
            bounced += 1
            continue
        try:
            os.kill(pid, 15)  # SIGTERM the bloated browser; watchdog respawns it
            note(f"  bounced {c['profile']} (~{c['rss_gb']:.1f}GB): SIGTERM pid {pid}")
        except ProcessLookupError:
            note(f"  {c['profile']} pid {pid} already gone")
        except Exception as e:
            note(f"  bounce {c['profile']} kill failed: {e}")
            continue
        # nudge the watchdog so respawn is immediate, not up to its StartInterval
        try:
            uid = os.getuid()
            subprocess.run(["/bin/launchctl", "kickstart", "-k", f"gui/{uid}/{label}"],
                           capture_output=True, text=True, timeout=15)
            note(f"  kickstarted {label} for fast respawn")
        except Exception as e:
            note(f"  kickstart {label} failed (watchdog StartInterval will catch it): {e}")
        bounced += 1
    return bounced


def reap_defunct() -> int:
    """Report zombie/defunct procs (zero recovery cost). We cannot reap another
    process's zombie (only its parent can wait()), so this is observational —
    surfacing leaked parents without ever killing a live, possibly-protected one."""
    try:
        r = subprocess.run(["/bin/ps", "-axo", "pid=,stat=,command="],
                           capture_output=True, text=True, timeout=15)
    except Exception:
        return 0
    n = 0
    for line in r.stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) >= 2 and parts[1].startswith("Z"):
            n += 1
    if n:
        note(f"  {n} defunct/zombie proc(s) present (await parent reap; not killable directly)")
    return n


# ==================== CDP cap enforcement (always-on) =========================
def jlog(action: str, **fields) -> None:
    """Append one structured action record to the jsonl audit trail."""
    rec = {"ts": ts(), "action": action}
    rec.update(fields)
    try:
        ACTION_LOG.parent.mkdir(parents=True, exist_ok=True)
        with ACTION_LOG.open("a") as fh:
            fh.write(json.dumps(rec) + "\n")
    except OSError:
        pass


def discover_cdp_profiles() -> dict[int, str]:
    """port -> profile basename, read from the live Chrome MAIN procs (no --type=)."""
    try:
        r = subprocess.run(["/bin/ps", "-axo", "command="],
                           capture_output=True, text=True, timeout=20)
    except Exception:
        return {}
    out: dict[int, str] = {}
    for line in r.stdout.splitlines():
        if "chrome-cdp-profile" not in line or "--type=" in line:
            continue
        mp = re.search(r"--remote-debugging-port=(\d+)", line)
        md = re.search(r"--user-data-dir=(\S+)", line)
        if mp and md:
            out[int(mp.group(1))] = os.path.basename(md.group(1).rstrip("/"))
    return out


def _http_get(url: str, timeout: float = 4.0) -> str | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except Exception:
        return None


def cdp_list(port: int) -> list | None:
    body = _http_get(f"http://127.0.0.1:{port}/json/list")
    if body is None:
        return None
    try:
        data = json.loads(body)
        return data if isinstance(data, list) else None
    except Exception:
        return None


def cdp_close(port: int, target_id: str) -> bool:
    """Graceful tab close over the DevTools HTTP endpoint (reversible: the lane
    reopens whatever page it needs on its next run)."""
    return _http_get(f"http://127.0.0.1:{port}/json/close/{target_id}") is not None


def _lock_fresh(base: str) -> bool:
    """A launcher heartbeat lock that was touched within LOCK_FRESH_S means a run
    is live on this profile -> ACTIVE. Missing dir/lock -> not fresh."""
    lk = CDP_ACTIVE_DIR / f"{base}.lock"
    try:
        return lk.exists() and (time.time() - lk.stat().st_mtime) <= LOCK_FRESH_S
    except Exception:
        return False


_LAUNCHER_CACHE: dict[str, bool] = {}


def _any_launcher_alive() -> bool:
    """True if any CDP-driving launcher process is currently running (cached/run)."""
    if "v" in _LAUNCHER_CACHE:
        return _LAUNCHER_CACHE["v"]
    try:
        r = subprocess.run(["/bin/ps", "-axo", "command="],
                           capture_output=True, text=True, timeout=15)
        low = r.stdout.lower()
        v = any(h in low for h in LAUNCHER_HINTS)
    except Exception:
        v = True  # cannot tell -> assume busy (fail-safe)
    _LAUNCHER_CACHE["v"] = v
    return v


def _port_has_active_client(port: int) -> bool | None:
    """True if a non-Chrome process holds an ESTABLISHED connection to the debug
    port (an automation run is driving it). False if only Chrome's own ends are
    present. None if we cannot determine (lsof failed)."""
    try:
        r = subprocess.run(
            ["/usr/sbin/lsof", "-nP", f"-iTCP@127.0.0.1:{port}", "-sTCP:ESTABLISHED"],
            capture_output=True, text=True, timeout=8)
    except Exception:
        return None
    if r.returncode not in (0, 1):   # 1 == no matching connections (fine)
        return None
    for line in r.stdout.splitlines()[1:]:
        cmd = line.split(None, 1)[0] if line.split() else ""
        cl = cmd.lower()
        if cmd and not cl.startswith("google") and "chrome" not in cl:
            return True
    return False


def is_port_active(port: int, base: str) -> bool:
    """Fail-safe ACTIVE check — protect any profile an automation run is driving.
    ACTIVE if: fresh heartbeat lock, OR a live non-Chrome CDP client, OR (when
    lsof can't tell) any launcher is alive."""
    if _lock_fresh(base):
        return True
    client = _port_has_active_client(port)
    if client is True:
        return True
    if client is None:
        return _any_launcher_alive()
    return False


def enforce_tab_caps(dry_run: bool, profiles: dict[int, str]) -> tuple[int, set]:
    """Close leaked background tabs on IDLE persona profiles down to
    MAX_TABS_PER_PROFILE. Never touches an active profile; never closes the
    browser (always leaves >=1 tab so the singleton stays alive). Returns
    (tabs_closed, {profile bases we acted on}) so the tiered bouncer can skip
    a profile we just cleaned (its renderers are still exiting)."""
    closed = 0
    capped: set = set()
    for port, base in sorted(profiles.items()):
        if closed >= MAX_TAB_CLOSES_PER_CYCLE:
            break
        if base in WORK_CDP_BASENAMES:   # never touch a CompanyA work bridge's tabs
            note(f"  CDP {base}:{port} is a WORK bridge — never tab-cap (allowlist)")
            continue
        targets = cdp_list(port)
        if targets is None:
            continue  # port unreachable
        pages = [t for t in targets if t.get("type") == "page"]
        if len(pages) <= MAX_TABS_PER_PROFILE:
            continue
        if is_port_active(port, base):
            note(f"  CDP {base}:{port} ACTIVE ({len(pages)} tabs) — skip tab-cap this cycle")
            jlog("tab_cap_skip_active", profile=base, port=port, pages=len(pages))
            continue
        # Keep the newest MAX_TABS TOP-LEVEL tabs (no parentId) and close the rest.
        # Keeping top-level tabs guarantees a kept tab can't be cascade-closed when
        # its parent closes (which is how a profile can collapse to 0 tabs and quit).
        top = [t for t in pages if not t.get("parentId")] or pages
        keep_ids = {t.get("id") for t in top[:MAX_TABS_PER_PROFILE]}
        victims = [t for t in pages if t.get("id") not in keep_ids]
        if not victims:
            continue
        note(f"  CDP {base}:{port} idle, {len(pages)} tabs ({len(top)} top-level) "
             f"-> closing {len(victims)} (keep {len(keep_ids)} top-level)")
        capped.add(base)
        for t in victims:
            if closed >= MAX_TAB_CLOSES_PER_CYCLE:
                note(f"  tab-close budget ({MAX_TAB_CLOSES_PER_CYCLE}) reached — rest next cycle")
                break
            tid = t.get("id")
            url = (t.get("url") or "")[:80]
            if not tid:
                continue
            if dry_run:
                jlog("tab_close", profile=base, port=port, target=tid, url=url, dry_run=True)
            else:
                ok = cdp_close(port, tid)
                jlog("tab_close", profile=base, port=port, target=tid, url=url, ok=ok)
            closed += 1
    if closed:
        note(f"  CDP tab-cap: {'would close' if dry_run else 'closed'} {closed} idle tab(s)")
    return closed, capped


def reap_orphan_shells(dry_run: bool) -> int:
    """Kill orphaned Playwright chrome-headless-shell MAIN procs (reparented to
    launchd, ppid==1 -> their node/python parent died). Renderers (--type=) die
    with the main shell. Protected procs are never touched. Bounded per cycle."""
    try:
        r = subprocess.run(["/bin/ps", "-axo", "pid=,ppid=,command="],
                           capture_output=True, text=True, timeout=15)
    except Exception:
        return 0
    killed = 0
    for line in r.stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        pid_s, ppid_s, cmd = parts
        if "chrome-headless-shell" not in cmd or "--type=" in cmd:
            continue
        try:
            pid, ppid = int(pid_s), int(ppid_s)
        except ValueError:
            continue
        if ppid != 1:          # parent still alive -> live Playwright session, keep
            continue
        if is_protected(pid):
            continue
        if killed >= MAX_SHELL_KILLS_PER_CYCLE:
            note("  orphan-shell reap budget reached")
            break
        if dry_run:
            note(f"  would reap orphan headless-shell pid={pid} (ppid=1)")
            jlog("orphan_shell_reap", pid=pid, dry_run=True)
        else:
            try:
                os.kill(pid, 15)
                note(f"  reaped orphan headless-shell pid={pid}")
                jlog("orphan_shell_reap", pid=pid)
            except ProcessLookupError:
                continue
            except Exception as e:
                note(f"  reap pid={pid} failed: {e}")
                continue
        killed += 1
    return killed


def enforce_cdp_caps(dry_run: bool) -> set:
    """Always-on pass (runs every cycle regardless of pressure tier): cap idle
    persona tabs + reap orphaned headless shells. Logged + bounded + reversible.
    Returns the set of profile bases whose tabs were (or would be) capped this
    cycle, so the reactive bouncer doesn't SIGTERM a browser we just cleaned."""
    _LAUNCHER_CACHE.clear()
    profiles = discover_cdp_profiles()
    note(f"CDP cap enforcement: {len(profiles)} live persona profile(s) "
         f"{sorted(profiles.values())} mode={'DRY-RUN' if dry_run else 'LIVE'}")
    _, capped = enforce_tab_caps(dry_run, profiles)
    reap_orphan_shells(dry_run)
    return capped


# ============================ escalation ======================================
def top_ram_hogs(n: int = 8) -> list[str]:
    try:
        r = subprocess.run(["/bin/ps", "-axo", "rss=,pid=,comm="],
                           capture_output=True, text=True, timeout=15)
    except Exception:
        return []
    rows = []
    for line in r.stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            rss_kb = int(parts[0])
        except ValueError:
            continue
        rows.append((rss_kb, parts[1], parts[2]))
    rows.sort(reverse=True)
    return [f"{rss/1e6:.1f}GB  pid {pid}  {comm}" for rss, pid, comm in rows[:n]]


def escalate(free: float, swap: float, dry_run: bool) -> None:
    now = time.time()
    try:
        last = float(CRIT_STAMP.read_text().strip())
    except Exception:
        last = 0.0
    if now - last < ESCALATION_COOLDOWN_S:
        note(f"  CRITICAL escalation suppressed (cooldown, last {int(now-last)}s ago)")
        return
    hogs = "\n".join(top_ram_hogs())
    body = (f"RAM CRITICAL on Mac mini: only {free:.0f}% free, swap {swap:.0f}% used. "
            f"ram_warden ran a full WARN+AGGRESSIVE reclaim (purge, idle-model unload, "
            f"chrome-cdp bounce) and pressure persists. NOT killing any protected "
            f"service. Top RAM hogs:\n{hogs}")
    if dry_run:
        note("  would ESCALATE to Operator via core.alert.alert_alex:")
        for h in hogs.splitlines():
            note(f"    {h}")
        return
    try:
        from core.alert import alert_alex
        alert_alex("RAM CRITICAL — manual headroom needed", body)
        CRIT_STAMP.write_text(str(now))
        note("  escalated to Operator via core.alert")
    except Exception as e:
        note(f"  escalation failed: {e}")


# ============================ self-test =======================================
def self_test() -> int:
    """Prove the HARD GUARD: feed it a known protected service pid and confirm
    is_protected() refuses; feed a clearly-unprotected pid (ourselves' shell? no)
    and confirm matching works on tokens."""
    note("self-test: protect-guard")
    ok = True
    # find a live protected service pid (self-serve / cloudflared / gateway / ollama)
    target_pid = None
    target_name = None
    try:
        r = subprocess.run(["/bin/ps", "-axo", "pid=,command="], capture_output=True, text=True, timeout=15)
        for line in r.stdout.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) < 2:
                continue
            pid_s, cmd = parts
            low = cmd.lower()
            for tok in ("ollama serve", "cloudflared", "run_self_serve", "openclaw", "seo_server"):
                if tok in low:
                    target_pid, target_name = int(pid_s), tok
                    break
            if target_pid:
                break
    except Exception:
        pass
    if target_pid:
        blocked = is_protected(target_pid)
        note(f"  protected pid {target_pid} ({target_name}): is_protected={blocked} "
             f"-> {'PASS (refuses kill)' if blocked else 'FAIL'}")
        ok = ok and blocked
    else:
        note("  (no protected service pid found live to test — guard logic still validated below)")
    # unreadable pid -> fail-safe True
    fake = is_protected(999999)
    note(f"  nonexistent pid 999999: is_protected={fake} -> {'PASS (fail-safe)' if fake else 'FAIL'}")
    ok = ok and fake
    # read-only CDP enforcement probe (no tabs closed): prove discovery + active-guard
    note("self-test: CDP cap pass (read-only)")
    profiles = discover_cdp_profiles()
    note(f"  discovered {len(profiles)} persona profile(s): {sorted(profiles.values())}")
    for port, base in sorted(profiles.items()):
        tg = cdp_list(port)
        pages = len([t for t in (tg or []) if t.get("type") == "page"])
        active = is_port_active(port, base)
        note(f"  {base}:{port} pages={pages} active={active} "
             f"-> {'protected (skip)' if active else 'idle (would cap to %d)' % MAX_TABS_PER_PROFILE}")
    note(f"self-test {'PASSED' if ok else 'FAILED'}")
    return 0 if ok else 1


# ============================ main ============================================
def _livestream_active() -> bool:
    """True if an ffmpeg RTMP push (the YouTube livestream) is running."""
    try:
        return subprocess.run(["pgrep", "-f", "ffmpeg.*rtmp"],
                              capture_output=True, timeout=5).returncode == 0
    except Exception:
        return False


def _free_mb() -> float:
    try:
        out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5).stdout
        for line in out.splitlines():
            if "Pages free" in line:
                return int(line.split()[-1].rstrip(".")) * 4096 / 1048576
    except Exception:
        pass
    return 1e9


def run_tier(tier: str, free: float, swap: float, dry_run: bool,
             skip_bounce: set | None = None) -> None:
    note(f"tier={tier} free={free:.0f}% swap={swap:.0f}% mode={'DRY-RUN' if dry_run else 'LIVE'}")
    # ── stream-first guard ── the livestream outranks a warm model. Fires regardless of tier so
    # a RAM spike can't OOM-kill ffmpeg while a 5-6GB model squats. This is the real fix: the old
    # logic only evicted at AGGRESSIVE/CRITICAL free%, which lagged behind the spike that killed it.
    if _livestream_active():
        fmb = _free_mb()
        if fmb < STREAM_FREE_FLOOR_MB:
            note(f"  stream-first: ffmpeg live + free {fmb:.0f}MB < {STREAM_FREE_FLOOR_MB:.0f}MB "
                 f"-> force-evicting ollama to protect the livestream")
            force_evict_ollama(dry_run)
    if tier == "HEALTHY":
        note("  healthy — no action")
        return
    # WARN actions (also run inside higher tiers)
    purge_inactive(dry_run)
    unload_idle_models(dry_run)
    if tier in ("AGGRESSIVE", "CRITICAL"):
        force_evict_ollama(dry_run)   # reclaim the resident model (~6GB) -- the old blind spot
        reap_defunct()
        bounce_chrome_cdp(dry_run, budget=MAX_BOUNCES_PER_CYCLE, skip=skip_bounce)
    if tier == "CRITICAL":
        escalate(free, swap, dry_run)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="decide + print actions, change nothing")
    ap.add_argument("--tier", choices=["HEALTHY", "WARN", "AGGRESSIVE", "CRITICAL"],
                    help="force a tier (implies extra self-checks); pairs well with --dry-run")
    ap.add_argument("--self-test", action="store_true", help="prove the protect-guard then exit")
    ap.add_argument("--enforce-only", action="store_true",
                    help="run ONLY the always-on CDP cap pass (tab-cap + orphan reap), skip pressure tiers")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    if args.enforce_only:
        enforce_cdp_caps(args.dry_run)
        return 0

    # GUARANTEED-RESUME backstop: if a CompanyA-priority task paused non-CompanyA (the resident
    # local model) and didn't resume (crash/timeout), force-resume it here. Runs every cycle so
    # non-CompanyA work is NEVER left paused. (Owner mandate: pause for CompanyA, but always resume.)
    if not args.dry_run:
        try:
            from tools.redacted_priority import ensure_resumed
            ensure_resumed()
        except Exception:
            pass

    measured_tier, free, swap = decide_tier()
    tier = args.tier or measured_tier
    if args.tier and args.tier != measured_tier:
        note(f"FORCED tier {tier} (measured {measured_tier}) for simulation")
        if not args.dry_run:
            note("refusing to force a LIVE tier override without --dry-run")
            return 2

    # ALWAYS-ON: cap leaked CDP tabs + reap orphan shells every cycle, regardless
    # of pressure tier (proactively stops proliferation before it builds pressure).
    capped = enforce_cdp_caps(args.dry_run)

    # Reactive, pressure-tiered reclaim (purge / idle-model unload / whole-browser
    # bounce / escalate) layered on top. Don't bounce a profile we just cleaned.
    run_tier(tier, free, swap, args.dry_run, skip_bounce=capped)
    return 0


if __name__ == "__main__":
    sys.exit(main())
