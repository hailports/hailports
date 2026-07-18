#!/usr/bin/env python3
"""hive.py — orchestrates autonomous revenue bee cycles.

Runs as: python3 -m core.hive
Managed by: launchd com.claude-stack.bee-hive
"""
import fcntl
import json
import os
import signal
import sys
import time
from pathlib import Path

ROOT = "/home/user"
STACK = f"{ROOT}/claude-stack"
LOG = f"{ROOT}/.bee-hive.log"
LOCK_FILE = f"{ROOT}/.bee-hive.lock"
# Honor either env name; HIVE_MAX_CYCLE_SECONDS is what the launchd plist sets.
MAX_CYCLE_SECONDS = int(
    os.environ.get("BEE_CYCLE_TIMEOUT")
    or os.environ.get("HIVE_MAX_CYCLE_SECONDS")
    or "1800"
)
# Per-bee ceiling so one hung bee aborts to the next instead of killing the cycle.
PER_BEE_SECONDS = int(os.environ.get("BEE_PER_TIMEOUT", "300"))
MAX_LOG_BYTES = 2 * 1024 * 1024  # 2MB

sys.path.insert(0, STACK)
from core.bee import forage, log  # noqa: E402

BEES = {
    "healer": (
        "Fast self-heal. In AT MOST 4 commands: run `launchctl list | grep claude-stack | "
        "awk '$1!~/^-?[0-9]+$/||$2!=0'` to list jobs with nonzero exit. If any, fix the single "
        "worst one if reversible (repoint a bad path, restart it) and verify. If all clean, "
        "RESULT: healthy. Do not scan large logs. Be fast."
    ),
    "harvester": (
        "Hive economics: harvest FREE/organic resources without hesitation. Inspect the stack for "
        "free AI tiers, free platforms, and organic traffic surfaces (read core/free_llm_pool.py and "
        "core/ai_arbitrage to see what's wired). Find ONE concrete free resource that is unused or "
        "underused and wire it in reversibly VIA CODE. "
        "NEVER edit/delete/'optimize' .env or any secrets file — keys there are owner-managed; if a key "
        "is missing, RECOMMEND it, do not touch the file. RESULT: what you tapped."
    ),
    "optimizer": (
        "underperforming, and make one small reversible improvement; verify it. RESULT: what you fixed."
    ),
    "prospector": (
        "Inspect the lead/prospect pipeline (data/hustle, prospect files). Find ONE concrete way "
        "to improve targeting or unstick a stalled step; apply it reversibly and verify. RESULT: change made."
    ),
    "scout": (
        "Scan the stack's revenue surfaces (docsapp senders, social engine, Hun Vault, products) and "
        "identify the single highest-leverage gap blocking real revenue. RESULT: gap + recommendation."
    ),
    "leadgen": (
        "WINNER #1 lane: build sellable, verified-deliverable lead/dataset INVENTORY from infra we "
        "already run — NO sends, NO spend. Pull a batch of fresh leads from an existing scraper/prospect "
        "file, dedupe, quality-gate to DELIVERABLE-ONLY using agents/resend_ground_truth.py. Append "
        "clean batch to data/hustle/leadgen_inventory.jsonl (create if absent). "
        "RESULT: how many deliverable leads added + niche."
    ),
}


def _acquire_singleton():
    f = open(LOCK_FILE, "w")
    try:
        fcntl.lockf(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("[hive] already running -- exit")
        sys.exit(0)
    return f


def _watchdog(signum, frame):
    log("[hive] WATCHDOG timeout -- cycle killed")
    sys.exit(1)


class _BeeTimeout(Exception):
    pass


def _bee_watchdog(signum, frame):
    raise _BeeTimeout()


def _trim_log():
    try:
        p = Path(LOG)
        if p.exists() and p.stat().st_size > MAX_LOG_BYTES:
            lines = p.read_text(errors="replace").splitlines()
            p.write_text("\n".join(lines[-2000:]) + "\n")
    except Exception:
        pass


def run_cycle():
    results = {}
    for name, goal in BEES.items():
        # Per-bee deadline: a single stuck bee aborts here, the cycle continues.
        signal.signal(signal.SIGALRM, _bee_watchdog)
        signal.alarm(PER_BEE_SECONDS)
        try:
            results[name] = forage(name, goal)
        except _BeeTimeout:
            log(f"[{name}] PER-BEE timeout ({PER_BEE_SECONDS}s) -- skipped")
            results[name] = f"SKIP: bee timeout {PER_BEE_SECONDS}s"
        finally:
            signal.alarm(0)
    log("[hive] cycle done")
    return results


if __name__ == "__main__":
    _trim_log()
    _lock = _acquire_singleton()
    log("[hive] cycle start")
    # Per-bee alarms (set inside run_cycle) bound the cycle to
    # len(BEES) * PER_BEE_SECONDS; a single hung bee no longer kills the run.
    _cycle_start = time.time()
    out = run_cycle()
    if time.time() - _cycle_start > MAX_CYCLE_SECONDS:
        log(f"[hive] cycle exceeded soft cap {MAX_CYCLE_SECONDS}s (informational)")
    for k, v in out.items():
        print(f"=== {k} ===\n{v}\n")
