#!/usr/bin/env python3
"""perf_guard — autonomous performance-meltdown guard.

Catches the felt-lag causes the RAM/disk-space/CPU guardians MISS and AUTO-REMEDIATES
them, THEN alerts — so a meltdown never needs a human to notice again.

Built 2026-07-09 after a Colima VM misconfigured with `--inotify-dir=$HOME` saturated
disk to 122 MB/s (2450 tps) while CPU sat 60% idle and RAM was healthy — every app
blocked on disk, the machine crawled, and it required manual intervention. The existing
guardians never saw it because none of them watch disk *throughput*.

Each tick (~3 min):
  1. DISK I/O — two consecutive iostat samples; if BOTH exceed the saturation floor,
     remediate the known offenders (below), because the box is thrashing on disk.
  2. RUNAWAY LOGS — any tracked *.log over the cap gets tail-truncated (prevents the
     append-storm that both wastes disk and feeds the I/O), whether or not saturated.
  3. COLIMA HOME-INOTIFY MISCONFIG — if colima is watching $HOME (the known bug) AND
     disk is saturated, stop it + its brew keeper (search can be restarted correctly).
  4. ORPHAN HEADLESS — chrome-headless-shell reparented to launchd (ppid 1) = a dead
     automation still spinning; reap it.
Remediation happens FIRST; then it pages (critical) with exactly what it did. Fail-soft.

    python3 tools/perf_guard.py --once
"""
from __future__ import annotations

import glob
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

STATUS = ROOT / "data" / "runtime" / "perf_guard.json"
HOME = os.path.expanduser("~")

# CALIBRATION (measured 2026-07-09): this box's disk is inherently BURSTY — routine churn
# hits 3072 tps / 93 MB/s for a sample or two (58 chrome + ~110 procs + interval jobs).
# The meltdown was 122 MB/s SUSTAINED. So tps is a USELESS discriminator (normal bursts
# exceed the meltdown's tps) — trigger on sustained MB/s only, confirmed across 3 samples.
# tps is still recorded for context. Getting this wrong = paging Operator on normal bursts.
IO_MBS_SAT = float(os.environ.get("PERF_IO_MBS_SAT", "110"))
IO_CONFIRM_SAMPLES = int(os.environ.get("PERF_IO_CONFIRM_SAMPLES", "3"))
# CPU sweet-spot guard: background non-critical hogs ONLY when 1-min load exceeds this ratio ×
# core-count (this box: 10 cores → fires above load 16). Baseline here is ~1.3×/core, harmful
# spikes were 1.8-2.5×, so 1.6× throttles the spikes and never touches normal operation.
CPU_SAT_RATIO = float(os.environ.get("PERF_CPU_SAT_RATIO", "1.6"))
CPU_HOG_MIN_PCT = float(os.environ.get("PERF_CPU_HOG_MIN_PCT", "25"))  # catch mid hogs, not just >40
CPU_HOG_LIMIT = int(os.environ.get("PERF_CPU_HOG_LIMIT", "8"))
LOG_CAP_MB = float(os.environ.get("PERF_LOG_CAP_MB", "80"))
LOG_KEEP_KB = 256


def _sh(cmd, t=25) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=t).stdout
    except Exception:
        return ""


def _page(subject: str, body: str) -> None:
    try:
        from core import alert_gateway
        alert_gateway.route("critical", "perf_guard", subject, body, issue_key="perf:meltdown")
    except Exception:
        pass


def _selfheal_log(subject: str, body: str) -> None:
    """SELF-CORRECT, DON'T TELL (Operator 2026-07-11): a meltdown perf_guard AUTO-FIXED is logged
    silently, never paged. Only an UNFIXED saturation still escalates. The old code paged every
    auto-fix — pure noise for a problem it already solved."""
    try:
        import os, json, time
        d = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "learning")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "perf_guard_selfheal.jsonl"), "a") as f:
            f.write(json.dumps({"ts": time.time(), "subject": subject, "body": body[:300]}) + "\n")
    except Exception:
        pass


def _io_sample() -> tuple[float, float]:
    """One 2s iostat rate sample (tps, MB/s). iostat's first row is cumulative-since-boot,
    so we take the LAST data row."""
    out = _sh(["iostat", "-d", "-w", "2", "-c", "2", "disk0"])
    rows = [l for l in out.splitlines() if re.match(r"\s*[\d.]+\s+\d+\s+[\d.]+", l)]
    if not rows:
        return (0.0, 0.0)
    p = rows[-1].split()
    try:
        return (float(p[1]), float(p[2]))  # tps, MB/s
    except Exception:
        return (0.0, 0.0)


def _tracked_logs() -> list[str]:
    out = []
    out += glob.glob(str(ROOT / "data" / "**" / "*.log"), recursive=True)
    out += glob.glob(os.path.join(HOME, "Library", "Logs", "claude-stack", "*.log"))
    out += glob.glob(os.path.join(HOME, ".ollama", "logs", "*.log"))
    return out


def remediate_logs() -> list[str]:
    """Tail-truncate any tracked log over the cap. Keeps the last LOG_KEEP_KB so recent
    context survives. Returns basenames acted on."""
    acted = []
    for f in _tracked_logs():
        try:
            if os.path.getsize(f) <= LOG_CAP_MB * 1e6:
                continue
            with open(f, "rb") as fh:
                fh.seek(-int(LOG_KEEP_KB * 1024), 2)
                tail = fh.read()
            with open(f, "wb") as fh:
                fh.write(f"[perf_guard truncated {time.strftime('%F %T')}]\n".encode())
                fh.write(tail)
            acted.append(os.path.basename(f))
        except Exception:
            pass
    return acted


def _colima_watches_home() -> bool:
    ps = _sh(["ps", "axo", "command"])
    for line in ps.splitlines():
        if "colima" in line and "--inotify-dir" in line:
            m = re.search(r"--inotify-dir\s+(\S+)", line)
            if m and os.path.realpath(m.group(1).rstrip("/")) == os.path.realpath(HOME):
                return True
    return False


def remediate_colima() -> bool:
    if not _colima_watches_home():
        return False
    _sh(["/opt/homebrew/bin/colima", "stop"], t=60)
    _sh(["brew", "services", "stop", "colima"], t=45)
    return True


# NEVER throttle these. CLAUDE.md: the YouTube ffmpeg encoder must stay full-priority or the
# broadcast flips OFFLINE, and the casestudy Chrome is that broadcast's surface.
# WindowServer/Screensharing = Operator's live remote session. ollama serves every lane. The
# guardians must never be slowed. And never throttle ourselves.
#
# Match binaries by BASENAME, not substring: a naive r"/claude\b" also matches every process
# under /claude-stack/ (i.e. the whole stack, including the hogs we exist to throttle).
_PROTECT_BASE = {"ffmpeg", "WindowServer", "ScreensharingAgent", "avconferenced",
                 "ollama", "claude", "Terminal", "launchd", "kernel_task"}
_PROTECT_CMD = re.compile(
    r"perf_guard|redacted_sentinel|eternal_guardian|capability_canary|self_healer|"
    r"ram_warden|jit_healer|chrome-cdp-profile-casestudy|"
    # the live work gateways (8078/8077) are Operator's interactive work-GPT surface --
    # backgrounding their QoS makes every ChatGPT-action call hang under load. Kept in
    # lockstep with remote_priority_governor.sh's NEVERTHROTTLE set (never fight it).
    r"chatgpt_redacted_action|chatgpt_sf_admin_action|"
    # livestream supervisor (python3.14) feeds frames to ffmpeg; taskpolicy -b it and the
    # frame pipe stalls -> RTMP drop -> YouTube OFFLINE. ffmpeg itself is in _PROTECT_BASE.
    r"dashboard_livestream|yt_live_supervisor", re.I)


def _protected(cmd: str) -> bool:
    try:
        base = os.path.basename(cmd.split()[0])
    except Exception:
        base = ""
    return base in _PROTECT_BASE or bool(_PROTECT_CMD.search(cmd))


def remediate_throttle_hogs(min_cpu: float = 40.0, limit: int = 6) -> list[str]:
    """Demote the heaviest UNPROTECTED hogs to macOS background QoS (`taskpolicy -b`), which
    throttles both CPU and disk I/O. The job keeps running — it just stops starving Operator's
    interactive session. This is the systemic fix: 196/270 launchd jobs carry no ProcessType,
    so we cannot rely on per-job plists to prevent the next starvation."""
    me = os.getpid()
    rows = []
    for line in _sh(["ps", "axo", "pid,%cpu,command"]).splitlines()[1:]:
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        pid, cpu, cmd = parts[0], parts[1], parts[2]
        try:
            cpu = float(cpu)
        except ValueError:
            continue
        if cpu < min_cpu or _protected(cmd) or pid == str(me):
            continue
        rows.append((cpu, pid, cmd))
    throttled = []
    for cpu, pid, cmd in sorted(rows, reverse=True)[:limit]:
        try:
            if subprocess.run(["taskpolicy", "-b", "-p", pid],
                              capture_output=True, timeout=10).returncode == 0:
                throttled.append(f"{os.path.basename(cmd.split()[0])}[{pid}]@{cpu:.0f}%cpu")
        except Exception:
            pass
    return throttled


def remediate_orphan_headless() -> int:
    """Reap chrome-headless-shell reparented to launchd (ppid 1) — a dead automation
    still burning CPU/disk. Live ones keep their real parent and are untouched."""
    n = 0
    out = _sh(["ps", "axo", "pid,ppid,comm"])
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and "chrome-headless-shell" in parts[2]:
            pid, ppid = parts[0], parts[1]
            if ppid == "1":
                try:
                    os.kill(int(pid), 9)
                    n += 1
                except Exception:
                    pass
    return n


def run_once() -> dict:
    # proactive: runaway logs get truncated every tick regardless of I/O
    logs_truncated = remediate_logs()

    # disk I/O: only SUSTAINED high MB/s counts. A single burst (routine on this box) is
    # not a meltdown; require IO_CONFIRM_SAMPLES consecutive samples over the bar.
    actions = []
    if logs_truncated:
        actions.append(f"truncated {len(logs_truncated)} runaway log(s): {', '.join(logs_truncated[:5])}")

    tps1, mbs1 = _io_sample()
    samples = [(tps1, mbs1)]
    saturated = False
    if mbs1 >= IO_MBS_SAT:
        for _ in range(max(0, IO_CONFIRM_SAMPLES - 1)):
            t, m = _io_sample()
            samples.append((t, m))
            if m < IO_MBS_SAT:
                break  # dropped below the bar -> it was a burst, not a meltdown
        saturated = len(samples) >= IO_CONFIRM_SAMPLES and all(m >= IO_MBS_SAT for _, m in samples)
    mbs2 = samples[-1][1]
    if saturated:
        if remediate_colima():
            actions.append("stopped Colima (home-wide inotify misconfig)")
        reaped = remediate_orphan_headless()
        if reaped:
            actions.append(f"reaped {reaped} orphan headless-chrome")
        # The real starvation class (2026-07-09): an unthrottled heavy producer (mockup-regen
        # + its chrome renderers) saturating disk at 161MB/s. Demote, don't kill.
        hogs = remediate_throttle_hogs()
        if hogs:
            actions.append("throttled to background QoS: " + ", ".join(hogs))

    # CPU sweet-spot guard (the disk path above only catches I/O meltdowns). The box also starves
    # when too many agents hit the cores at once — that's what times out the work-GPT deep flows
    # (ollama inference can't get a core). When 1-min load exceeds CPU_SAT_RATIO×ncpu, demote the
    # heaviest UNPROTECTED cpu hogs to background QoS: they keep running (on E-cores), while the
    # protected set (ollama, gateway, stream, guardians) keeps the P-cores. Progressive by design —
    # zero throttle below the ratio, so agents run full-speed whenever there's headroom.
    ncpu = os.cpu_count() or 8
    load1 = os.getloadavg()[0]
    cpu_saturated = load1 >= CPU_SAT_RATIO * ncpu
    if cpu_saturated:
        cpu_hogs = remediate_throttle_hogs(min_cpu=CPU_HOG_MIN_PCT, limit=CPU_HOG_LIMIT)
        if cpu_hogs:
            actions.append(f"cpu load {load1:.0f} on {ncpu}c -> backgrounded: " + ", ".join(cpu_hogs))
            _selfheal_log("perf_guard eased CPU starvation",
                          f"load1 {load1:.1f} on {ncpu} cores (>= {CPU_SAT_RATIO}x/core). "
                          "Backgrounded (still running, on E-cores): " + "; ".join(cpu_hogs))

    # NOISE RULE: routine log truncation is silent housekeeping — page ONLY on real
    # sustained saturation (fixed or not). Operator must never get a page for a log rotation.
    if saturated and actions:
        # SELF-CORRECT, DON'T TELL (Operator 2026-07-11): an auto-FIXED meltdown is logged silently,
        # never texted — it's noise for a problem already solved. (_selfheal_log existed but the
        # call site still paged.)
        _selfheal_log("perf_guard auto-fixed a disk/IO meltdown",
              f"disk was {mbs1:.0f}MB/s / {tps1:.0f}tps sustained. Actions: " + "; ".join(actions))
    elif saturated:
        # confirmed saturation with NO known offender to auto-fix — must NOT go silent
        # (the whole mandate). Page with the top recent writers as an investigation lead.
        hot = _sh(["bash", "-c",
                   "find ~/claude-stack/data ~/Library/Logs /tmp -type f -newermt '-90 seconds' "
                   "-size +5M 2>/dev/null -exec ls -lhS {} + 2>/dev/null | head -4"])
        _page("perf_guard: DISK SATURATED, cause unknown — INVESTIGATE",
              f"disk {mbs1:.0f}MB/s / {tps1:.0f}tps sustained; not colima-home-inotify / runaway-log / "
              f"orphan-headless. Recent big writers:\n{hot.strip() or '(none found — check colima/docker/spotlight/fileproviderd)'}")

    out = {
        "ts": time.time(),
        "io_mbs": round(mbs1, 1), "io_tps": round(tps1, 1),
        "io_mbs_confirm": round(mbs2, 1),
        "saturated": bool(saturated),
        "cpu_load1": round(load1, 1), "ncpu": ncpu,
        "cpu_saturated": bool(cpu_saturated),
        "actions": actions,
        "ok": not saturated and not cpu_saturated,
    }
    try:
        STATUS.parent.mkdir(parents=True, exist_ok=True)
        STATUS.write_text(json.dumps(out, indent=2))
    except Exception:
        pass
    return out


def main() -> int:
    out = run_once()
    print(f"disk: {out['io_mbs']}MB/s {out['io_tps']}tps  saturated={out['saturated']}")
    if out["actions"]:
        for a in out["actions"]:
            print(f"  ACTED: {a}")
    else:
        print("  no action (healthy)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
