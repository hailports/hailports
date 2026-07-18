#!/usr/bin/env python3
"""Synthetic end-to-end capability canary.

Process-up watchdogs miss SILENT capability degradation: a job is "running" but
its output is broken. This probes the actual capabilities end-to-end and pages
only on a NEW failure (recovery routes a healed alert). Three failure classes it
was born to catch, all invisible to process-up checks:

  - a missing dep (litellm gone from .venv => free LLM pool silently local-only)
  - an unrendered plist template (__STACK_DIR__ => exit-78 every run)
  - a memory-killed daemon that never relaunched (OneDrive)

Run under the stack venv:
    ~/claude-stack/.venv/bin/python tools/capability_canary.py --once
    ~/claude-stack/.venv/bin/python tools/capability_canary.py --loop [--interval 1200]
    ...add --heavy to force the quota-gated probes (image/voice) to run this tick.

Every probe is fail-soft: a probe that raises is recorded FAIL, never crashes the
canary. Exit code is ALWAYS 0 — a monitor must never look like the broken thing.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Run under any other interpreter and the dep/import probes report the WRONG env's
# gaps as critical stack failures (and alert on them). Re-exec into the venv instead.
_VENV_PY = ROOT / ".venv" / "bin" / "python"
if Path(sys.executable).resolve() != _VENV_PY.resolve() and _VENV_PY.exists():
    os.execv(str(_VENV_PY), [str(_VENV_PY), *sys.argv])

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

HOME = Path(os.path.expanduser("~"))
STATUS_PATH = ROOT / "data" / "runtime" / "capability_canary.json"
LAUNCHAGENTS = HOME / "Library" / "LaunchAgents"
ONEDRIVE_WORKREF = (
    HOME / "Library" / "CloudStorage"
    / "OneDrive-redactedIndustries,Inc" / "Work Reference"
)

# Quota/model-load-heavy probes run at most once an hour (unless --heavy).
HEAVY_MIN_INTERVAL = 3600


def _probe(name, ok, detail, severity):
    return {"name": name, "ok": bool(ok), "detail": str(detail)[:500], "severity": severity}


# Consecutive-fail debounce across --once ticks: a single transient (a job that exits nonzero
# once then recovers next tick) shouldn't page; N-in-a-row means it's real. State is tiny +
# stdlib so it survives even when the rest of the stack is broken.
_DEBOUNCE_FILE = Path.home() / ".capability_canary_debounce.json"


def _consec_fail(key: str, failing: bool) -> int:
    try:
        d = json.loads(_DEBOUNCE_FILE.read_text())
    except Exception:
        d = {}
    n = (int(d.get(key, 0)) + 1) if failing else 0
    d[key] = n
    try:
        _DEBOUNCE_FILE.write_text(json.dumps(d))
    except Exception:
        pass
    return n


# Curated ALWAYS-ON critical labels — guards/watchdogs that must never be absent or disabled.
# Deliberately tiny + hand-picked: a "every plist must be loaded" check would false-alarm on the
# many intentionally on-demand/gated jobs (persona3, reddit-warmer, BrandA-gated). Being absent
# from `launchctl list` OR present in print-disabled is itself the alert (catches a runaway_guard
# / bootout cascade that the exit-code probe is blind to).
_CRITICAL_ALWAYS_ON = (
    "com.claude-stack.ops-health",
    "com.claude-stack.capability-canary",
    "com.claude-stack.plist-integrity-guard",
    "com.claude-stack.runaway-guard",
    "com.claude-stack.work-frontdoor-guardian",
    "com.claude-stack.healer",
    "com.claude-stack.alert-morning-flush",
    "com.claude-stack.mini-pulse-ping",
)


def _http_get(url, timeout=8):
    req = urllib.request.Request(url, headers={"User-Agent": "capability-canary"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.getcode(), r.read()


# --------------------------------------------------------------------------- #
# probes
# --------------------------------------------------------------------------- #

def probe_venv_deps():
    # (import_name, pip_name) — import names differ from module labels.
    critical = [
        "litellm", "openai", "anthropic", "httpx", "pydantic",
        "PIL", "fastapi", "uvicorn", "dotenv", "numpy", "bs4",
    ]
    missing = []
    for mod in critical:
        try:
            importlib.import_module(mod)
        except Exception as e:  # noqa: BLE001
            missing.append(f"{mod} ({type(e).__name__})")
    if missing:
        return _probe("venv_deps", False, "missing: " + ", ".join(missing), "critical")
    return _probe("venv_deps", True, f"{len(critical)} critical deps importable", "critical")


# Free-tier 429 rate-limits self-heal within minutes. A single empty tick is NOT an
# outage — the router falls through to local Ollama / paid so drafting never dies. Only
# a SUSTAINED empty streak (this many consecutive ticks) is a real degradation worth a
# transition alert. At the 1200s canary cadence, 3 ticks ≈ 1h of total free-pool outage.
_STRONG_POOL_SUSTAINED_TICKS = 3


def probe_free_llm_pool_strong():
    from core.free_llm_pool import try_free_providers

    async def _go():
        return await asyncio.wait_for(
            try_free_providers("return the number 7 and nothing else", tier="strong", max_tokens=16),
            timeout=90,
        )
    try:
        text, provider = asyncio.run(_go())
    except Exception as e:  # noqa: BLE001 — a raised pool call is a degraded tick, never a canary crash
        text, provider = None, f"error: {type(e).__name__}"
    answered = bool(text and str(text).strip())
    n = _consec_fail("free_llm_pool_strong", not answered)
    if answered:
        return _probe("free_llm_pool_strong", True,
                      f"answered via {provider}: {str(text).strip()[:40]!r}", "warn")
    # Confirm the local fallback is alive so an empty free pool is genuinely harmless.
    local_ok = True
    try:
        code, _ = _http_get("http://127.0.0.1:11434/api/tags", timeout=5)
        local_ok = (code == 200)
    except Exception:  # noqa: BLE001
        local_ok = False
    if n < _STRONG_POOL_SUSTAINED_TICKS and local_ok:
        # Momentary free-tier 429/empty. Degraded-not-failed: report OK so no page fires;
        # local Ollama fallback is UP and covering drafting. Self-heals on the next tick.
        return _probe("free_llm_pool_strong", True,
                      f"degraded: free strong pool empty this tick (x{n}); local Ollama fallback UP", "warn")
    detail = f"SUSTAINED: free strong pool empty x{n} consecutive ticks"
    if not local_ok:
        detail += " AND local Ollama fallback DOWN (drafting at risk)"
    return _probe("free_llm_pool_strong", False, detail, "warn")


def probe_free_image_pool():
    from core.free_image_pool import generate
    out = generate("a single small red circle on white, minimal test glyph")
    if not out or not Path(out).exists():
        return _probe("free_image_pool", False, "generate() returned no file", "warn")
    size = Path(out).stat().st_size
    try:
        Path(out).unlink()
    except OSError:
        pass
    if size < 10 * 1024:
        return _probe("free_image_pool", False, f"image too small ({size}B <10KB)", "warn")
    return _probe("free_image_pool", True, f"generated {size}B image", "warn")


def probe_local_ollama():
    code, body = _http_get("http://127.0.0.1:11434/api/tags", timeout=6)
    if code != 200:
        return _probe("local_ollama", False, f"HTTP {code}", "warn")
    tags = {m.get("name", "") for m in json.loads(body).get("models", [])}
    want = ["qwen2.5-coder:14b", "qwen3:14b"]
    absent = [w for w in want if not any(t == w or t.startswith(w) for t in tags)]
    if absent:
        return _probe("local_ollama", False, f"models missing: {', '.join(absent)}", "warn")
    return _probe("local_ollama", True, f"{len(tags)} models, coder+quality present", "warn")


def probe_voice_kokoro():
    from core.ima_voice import speak
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
        wav = Path(tf.name)
    try:
        ok = speak("test", wav)
        if not ok or not wav.exists():
            return _probe("voice_kokoro", False, "speak() failed / no wav", "warn")
        size = wav.stat().st_size
        if size < 1024:
            return _probe("voice_kokoro", False, f"wav too small ({size}B <1KB)", "warn")
        return _probe("voice_kokoro", True, f"synthesized {size}B wav", "warn")
    finally:
        wav.unlink(missing_ok=True)


def probe_work_gateway():
    code, _ = _http_get("http://127.0.0.1:8078/", timeout=8)
    if code != 200:
        return _probe("work_gateway", False, f"HTTP {code} (expected 200)", "critical")
    return _probe("work_gateway", True, "HTTP 200", "critical")


def probe_launchd_health():
    out = subprocess.run(
        ["launchctl", "list"], capture_output=True, text=True, timeout=20
    ).stdout
    flagged = []
    for line in out.splitlines():
        if "com.claude-stack" not in line:
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        _pid, status, label = parts[0], parts[1], parts[2].strip()
        try:
            code = int(status)
        except ValueError:
            continue
        # 0 = clean; negative = killed by a signal (SIGTERM/-15, SIGKILL/-9) which
        # is normal for our on-demand jobs. Positive nonzero (78, 1, 2...) = a real
        # crash/exit error — the exit-78 template class.
        if code > 0:
            flagged.append(f"{label}={code}")
    if flagged:
        # Debounce a single transient nonzero exit (many jobs exit 1 once then recover). Warn on
        # the first bad tick; page critical only when it persists across ticks (a real stuck job).
        n = _consec_fail("launchd_health", True)
        sev = "critical" if n >= 2 else "warn"
        return _probe("launchd_health", False, f"bad exits (x{n} tick): " + ", ".join(flagged), sev)
    _consec_fail("launchd_health", False)  # recovered -> reset the streak
    return _probe("launchd_health", True, "no nonzero-error exits", "critical")


def probe_critical_jobs_loaded():
    """Catch a critical guard that got booted-out / disabled (the 2026-07-12 runaway_guard cascade
    class) — invisible to the exit-code probe because a disabled job vanishes from `launchctl list`.
    FAIL critical if any always-on label is absent from the list OR present in print-disabled."""
    listed = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=20).stdout
    disabled = subprocess.run(
        ["launchctl", "print-disabled", f"gui/{os.getuid()}"],
        capture_output=True, text=True, timeout=20).stdout
    missing, off = [], []
    for lab in _CRITICAL_ALWAYS_ON:
        if lab not in listed:
            missing.append(lab.replace("com.claude-stack.", ""))
        elif f'"{lab}" => disabled' in disabled or f'"{lab}" => true' in disabled:
            off.append(lab.replace("com.claude-stack.", ""))
    if missing or off:
        parts = []
        if missing:
            parts.append("ABSENT: " + ", ".join(missing))
        if off:
            parts.append("DISABLED: " + ", ".join(off))
        return _probe("critical_jobs_loaded", False, " | ".join(parts), "critical")
    return _probe("critical_jobs_loaded", True, f"all {len(_CRITICAL_ALWAYS_ON)} always-on guards loaded", "critical")


def probe_plist_placeholders():
    token = re.compile(r"__[A-Z][A-Z0-9_]*__|\{\{")
    hits = []
    for plist in sorted(LAUNCHAGENTS.glob("com.claude-stack.*.plist")):
        try:
            text = plist.read_text(errors="replace")
        except OSError as e:
            hits.append(f"{plist.name} (unreadable: {e})")
            continue
        found = set(token.findall(text))
        if found:
            hits.append(f"{plist.name} {sorted(found)}")
    if hits:
        return _probe("plist_placeholders", False, "unrendered: " + "; ".join(hits), "critical")
    return _probe("plist_placeholders", True, "all plists fully rendered", "critical")


def probe_onedrive():
    running = subprocess.run(["pgrep", "-x", "OneDrive"], capture_output=True).returncode == 0
    if not running:
        return _probe("onedrive", False, "OneDrive process not running (memory-killed?)", "warn")
    if not ONEDRIVE_WORKREF.exists():
        return _probe("onedrive", False, f"work drive missing: {ONEDRIVE_WORKREF}", "warn")
    cutoff = time.time() - 6 * 3600
    newest = 0.0
    for p in ONEDRIVE_WORKREF.rglob("*"):
        try:
            if p.is_file():
                newest = max(newest, p.stat().st_mtime)
        except OSError:
            continue
    if newest < cutoff:
        age_h = (time.time() - newest) / 3600 if newest else -1
        return _probe("onedrive", False, f"no file modified in last 6h (newest ~{age_h:.1f}h)", "warn")
    return _probe("onedrive", True, "running + fresh work-drive activity", "warn")


def probe_outlook_bridge():
    # Work-lane mail bridge. Reuse outlook_app's serialized, timeout-safe osascript
    # runner so a hung Outlook is KILLED by the timeout (returns error tuple) instead
    # of piling up zombies. OK = non-empty default-account name within ~12s.
    from tools.outlook_app import _osa
    rc, out, err = _osa(
        'tell application "Microsoft Outlook" to return name of default account',
        timeout=12,
    )
    if rc != 0 or not out.strip():
        reason = err or "empty account (hung or dead Outlook)"
        return _probe("outlook_bridge", False, f"no account: {reason}", "critical")
    return _probe("outlook_bridge", True, f"default account: {out.strip()[:60]}", "critical")


def probe_headroom():
    disk_gib = round(shutil.disk_usage("/").free / (1024 ** 3), 1)
    ram_free = None
    try:
        page = int(subprocess.run(["sysctl", "-n", "hw.pagesize"], capture_output=True, text=True).stdout.strip())
        vm = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=10).stdout
        # "available" ~= reclaimable RAM the OS can hand out without swapping:
        # free + speculative + inactive + purgeable. Free-alone drastically
        # undercounts on macOS (most RAM sits as reclaimable file cache).
        want = {"Pages free:": 0, "Pages speculative:": 0,
                "Pages inactive:": 0, "Pages purgeable:": 0}
        for ln in vm.splitlines():
            for label in want:
                if ln.startswith(label):
                    want[label] = int(ln.split(":")[1].strip().rstrip("."))
        ram_free = sum(want.values()) * page / (1024 ** 3)
    except Exception:  # noqa: BLE001
        ram_free = None

    problems = []
    if disk_gib is not None and disk_gib < 35:
        problems.append(f"disk {disk_gib}GiB<35")
    if ram_free is not None and ram_free < 3:
        problems.append(f"ram {ram_free:.1f}GiB<3")
    detail = f"disk={disk_gib}GiB ram_free={ram_free:.1f}GiB" if ram_free is not None else f"disk={disk_gib}GiB ram_free=?"
    if problems:
        return _probe("headroom", False, "low: " + ", ".join(problems) + f" ({detail})", "warn")
    return _probe("headroom", True, detail, "warn")


# probe registry: (fn, heavy?) — heavy probes are hourly-gated / --heavy-only.
PROBES = [
    (probe_venv_deps, False),
    (probe_free_llm_pool_strong, False),
    (probe_free_image_pool, True),
    (probe_local_ollama, False),
    (probe_voice_kokoro, True),
    (probe_work_gateway, False),
    (probe_launchd_health, False),
    (probe_critical_jobs_loaded, False),
    (probe_plist_placeholders, False),
    (probe_onedrive, False),
    (probe_outlook_bridge, False),
    (probe_headroom, False),
]


# --------------------------------------------------------------------------- #
# runner
# --------------------------------------------------------------------------- #

def _load_status():
    try:
        return json.loads(STATUS_PATH.read_text())
    except Exception:  # noqa: BLE001
        return {}


def run_once(heavy: bool = False) -> dict:
    prev = _load_status()
    prev_probes = {p["name"]: p for p in prev.get("probes", [])}
    prev_heavy_run = {k: v for k, v in prev.get("heavy_last_run", {}).items()}
    now = time.time()

    results = []
    for fn, is_heavy in PROBES:
        name = fn.__name__.replace("probe_", "")
        if is_heavy and not heavy:
            last = prev_heavy_run.get(name, 0)
            if now - last < HEAVY_MIN_INTERVAL:
                prior = prev_probes.get(name)
                if prior:
                    prior = dict(prior)
                    base = prior["detail"]
                    while True:
                        stripped = re.sub(r"^\(skipped this tick; (.*)\)$", r"\1", base)
                        if stripped == base:
                            break
                        base = stripped
                    prior["detail"] = f"(skipped this tick; {base})"
                    results.append(prior)
                else:
                    results.append(_probe(name, True, "hourly-gated; not yet run", "warn"))
                continue
        try:
            r = fn()
        except Exception as e:  # noqa: BLE001 — fail-soft: a probe must never crash the canary
            sev = "warn" if is_heavy else "critical"
            r = _probe(name, False, f"probe raised: {type(e).__name__}: {e}", sev)
        if is_heavy:
            prev_heavy_run[name] = now
        results.append(r)

    overall_ok = all(p["ok"] for p in results)

    # page only on transitions
    for p in results:
        name = p["name"]
        was_ok = prev_probes.get(name, {}).get("ok", True)  # first-ever run: no page on green
        issue_key = f"capability_canary:{name}"
        if not p["ok"] and was_ok:
            _route_alert(p["severity"], name, p["detail"], issue_key, healed=False)
        elif p["ok"] and not was_ok:
            _route_alert("info", name, "recovered", issue_key, healed=True)

    status = {
        "ts": now,
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now)),
        "overall_ok": overall_ok,
        "probes": results,
        "heavy_last_run": prev_heavy_run,
    }
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATUS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(status, indent=2))
    tmp.replace(STATUS_PATH)
    return status


def _route_alert(severity, name, detail, issue_key, healed):
    try:
        from core import alert_gateway
        subject = (f"capability recovered: {name}" if healed
                   else f"capability FAIL: {name}")
        alert_gateway.route(
            severity, "capability_canary", subject,
            body=detail, issue_key=issue_key, healed=healed,
        )
    except Exception as e:  # noqa: BLE001 — never let paging crash the monitor
        print(f"[canary] alert routing failed for {name}: {e}", file=sys.stderr)


def _print_summary(status):
    width = shutil.get_terminal_size((100, 20)).columns
    rows = status["probes"]
    nlen = max((len(p["name"]) for p in rows), default=12)
    print(f"\ncapability canary  {status['generated']}  overall={'OK' if status['overall_ok'] else 'FAIL'}")
    print("-" * min(width, 100))
    for p in rows:
        flag = "OK  " if p["ok"] else "FAIL"
        dlen = max(20, min(width, 100) - nlen - 10)
        print(f"{p['name']:<{nlen}}  {flag}  {p['detail'][:dlen]}")
    print("-" * min(width, 100))


def main():
    ap = argparse.ArgumentParser(description="synthetic capability canary")
    ap.add_argument("--once", action="store_true", help="run one pass and exit")
    ap.add_argument("--loop", action="store_true", help="run forever on --interval")
    ap.add_argument("--interval", type=int, default=1200, help="loop interval seconds")
    ap.add_argument("--heavy", action="store_true", help="force quota-gated probes this run")
    args = ap.parse_args()

    if args.loop:
        while True:
            try:
                st = run_once(heavy=args.heavy)
                _print_summary(st)
            except Exception as e:  # noqa: BLE001
                print(f"[canary] run_once crashed: {e}", file=sys.stderr)
            time.sleep(max(60, args.interval))
        return

    st = run_once(heavy=args.heavy)
    _print_summary(st)


if __name__ == "__main__":
    sys.exit(main() or 0)
