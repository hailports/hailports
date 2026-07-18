#!/usr/bin/env python3
"""Plist integrity guard — make LaunchAgent corruption un-survivable across reboots.

Background: on 2026-07-08 a reboot surfaced ~7 com.claude-stack.* LaunchAgents whose
bodies had been overwritten with a raw JSON ProgramArguments array (escaped-slash, no-space
-> a Node/plutil-style serializer, NOT stack Python, which has no raw .plist writers). launchd
silently rejects an invalid plist, so enabled jobs just never come back. The existing
diagnostician.check_plist_drift only sha1-diffs deploy-vs-installed and never lints, so
corruption read as harmless "info" drift.

This guard lints EVERY installed com.claude-stack.* / com.Operator.* plist and, on any invalid
one, auto-restores from the canonical deploy/launchagents/ template (rendering the same
placeholders install.sh does) then re-bootstraps. If no clean source exists it quarantines
the corrupt file and alerts (can't silently pretend it's fixed). Runs at boot (RunAtLoad) and
hourly. Idempotent; a clean run touches nothing.
"""
from __future__ import annotations
import os, subprocess, shutil, datetime, sys, plistlib
from pathlib import Path

HOME = Path.home()
STACK_DIR = HOME / "claude-stack"
LA = HOME / "Library" / "LaunchAgents"
DEPLOY = STACK_DIR / "deploy" / "launchagents"
QUARANTINE = HOME / f".launchagents-retired/corrupt-{datetime.date.today():%Y%m%d}"
LOG = STACK_DIR / "logs.internal" / "plist-integrity-guard.log"
UID = os.getuid()
LOCAL_MODEL = os.environ.get("CLAUDE_STACK_LOCAL_MODEL") or os.environ.get("LOCAL_MODEL") or "qwen2.5:7b"
PREFIXES = ("com.claude-stack.", "com.Operator.")

# FAIL-CLOSED: reconcile_loaded() re-arms ONLY jobs on this explicit allowlist of
# pure infra / guards / routing daemons with NO outbound-to-human, send, post, trade,
# scrape, content-gen, or autonomous-code side effect. A denylist can't be trusted
# here — the launchd disable-store is corrupted (space-joined blob keys), so
# is_disabled() silently misses labels, and new senders land in LaunchAgents faster
# than a denylist can track. Anything NOT on this list is surfaced (logged) and left
# dark for a human to arm. Short names (label minus the com.claude-stack. prefix).
RECONCILE_ALLOWLIST = {
    # guards / watchdogs (supervision only — who-watches-the-watchmen)
    "plist-integrity-guard", "eternal-guardian", "work-frontdoor-guardian",
    "session-liveness-guard", "vpn-lane-guard", "runaway-guard",
    "stream-health-watchdog", "cdp-foreground-warden", "disk-guardian",
    "burn-rate-guard", "webui-guardian", "public-surface-watchdog",
    "remote-priority-governor",
    # core infra / model-routing daemons
    "vpn-socks", "ollama-guard", "ollama-queue", "mcp-unified", "mcp-gateway",
    "openapi-tool-gateway", "llm-api", "anthropic-proxy",
    # internal sync / backup / read-only (no external send)
    "continuous-repo-sync", "critical-mirror", "github-anon-sync",
    "git-onedrive-mirror",
    "ops-dashboard", "pii-guard", "smart-money-monitor",
    # OneDrive work publisher is surface-guarded + send-fenced. Its integrated
    # health guard only restarts a wedged local File Provider after repeat proof.
    "onedrive-work-sync",
    # work-brain ingest — read-only inbound pulls into the local FTS index,
    # no outbound send/post/trade/content-gen. Safe to auto-rearm if dark.
    "littlebird-web-sync", "zoom-web-sync", "work-context-index",
    "work-rag-refresh",
    "email-context-ingest",
    # onboarding surface (Operator2-action send-fenced; wizard only replies to enrolled handles)
    "Operator2-action", "onboarding-wizard",
}


def log(msg: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{stamp}  {msg}"
    with LOG.open("a") as fh:
        fh.write(line + "\n")
    print(line)


def is_valid(p: Path) -> bool:
    return subprocess.run(["plutil", "-lint", str(p)],
                          capture_output=True).returncode == 0


def render(src: Path) -> bytes:
    t = src.read_text()
    t = t.replace("__STACK_DIR__", str(STACK_DIR))
    t = t.replace("__HOME_DIR__", str(HOME))
    t = t.replace("__LOCAL_MODEL__", LOCAL_MODEL)
    return t.encode()


def is_disabled(label: str) -> bool:
    # A held job (persona3 lanes, reddit-warmer, outreach-cron, ...) must be repaired on
    # disk but NEVER re-armed. launchd stores disable overrides separately from the plist.
    out = subprocess.run(["launchctl", "print-disabled", f"gui/{UID}"],
                         capture_output=True, text=True).stdout
    return f'"{label}" => disabled' in out


def bootstrap(p: Path) -> None:
    if is_disabled(p.stem):
        log(f"  {p.stem} is disabled/held — repaired file, skipping bootstrap (not re-arming)")
        return
    subprocess.run(["launchctl", "bootout", f"gui/{UID}/{p.stem}"], capture_output=True)
    subprocess.run(["launchctl", "bootstrap", f"gui/{UID}", str(p)], capture_output=True)


def alert(subject: str, body: str = "") -> None:
    try:
        sys.path.insert(0, str(STACK_DIR))
        from core import alert_gateway
        alert_gateway.route("critical", source="plist_integrity_guard",
                            subject=subject, body=body, issue_key="plist_integrity")
    except Exception as e:
        log(f"alert route failed: {e}")


def alert_info(body: str) -> None:
    # A pure auto-restore is fully self-healed — nothing for Operator to do. Route as
    # info so it digests silently instead of paging. Only a QUARANTINE (no clean
    # source -> needs rebuild) is a real owner action and still pages critical.
    try:
        sys.path.insert(0, str(STACK_DIR))
        from core import alert_gateway
        alert_gateway.route("info", source="plist_integrity_guard",
                            subject="plist auto-restored", body=body,
                            issue_key="plist_integrity")
    except Exception as e:
        log(f"alert route failed: {e}")


def _loaded_labels() -> set[str]:
    """Labels currently known to launchd (last column of `launchctl list`)."""
    out = subprocess.run(["launchctl", "list"], capture_output=True, text=True).stdout
    labels = set()
    for line in out.splitlines():
        parts = line.split()
        if parts:
            labels.add(parts[-1])
    return labels


def reconcile_loaded() -> None:
    """Re-arm every enabled, auto-run com.claude-stack.* LaunchAgent whose Label is
    absent from `launchctl list`.

    FAIL-CLOSED: re-arms ONLY jobs on RECONCILE_ALLOWLIST (pure infra/guards/routing,
    no send/post/trade/autonomous side effect). Additionally requires an unattended
    trigger (KeepAlive OR StartInterval OR StartCalendarInterval) and not-disabled.
    Any dark job NOT on the allowlist is logged and left alone — arming a sender/poster
    is always a human decision. Idempotent + safe every cycle: a loaded, disabled, or
    one-shot job is a no-op. Corrupt plists are skipped — main()'s pass owns those."""
    if not LA.is_dir():
        return
    loaded = _loaded_labels()
    for p in sorted(LA.glob("com.claude-stack.*.plist")):
        if not is_valid(p):
            continue
        try:
            data = plistlib.loads(p.read_bytes())
        except Exception as e:
            log(f"reconcile: cannot parse {p.name}: {e}")
            continue
        label = data.get("Label") or p.stem
        if label in loaded:
            continue
        has_trigger = (bool(data.get("KeepAlive"))
                       or "StartInterval" in data
                       or "StartCalendarInterval" in data)
        if not has_trigger:
            continue
        if data.get("Disabled") is True or is_disabled(label):
            continue
        short = label[len("com.claude-stack."):] if label.startswith("com.claude-stack.") else label
        if short not in RECONCILE_ALLOWLIST:
            log(f"reconcile: {label} is DARK but NOT on the safe allowlist — "
                f"surfacing only, NOT auto-arming (may send/post/trade; human decision)")
            continue
        bootstrap(p)
        log(f"reconcile: re-armed dark SAFE job {label} via bootstrap")


def main() -> int:
    if not LA.is_dir():
        return 0
    repaired, quarantined = [], []
    for p in sorted(LA.glob("*.plist")):
        if not p.name.startswith(PREFIXES):
            continue
        if is_valid(p):
            continue
        # corrupt installed plist
        src = DEPLOY / p.name
        if src.exists() and is_valid(src):
            p.write_bytes(render(src))
            if is_valid(p):
                bootstrap(p)
                repaired.append(p.name)
                log(f"REPAIRED {p.name} from deploy/ + re-bootstrapped")
                continue
        # no clean source — quarantine so it stops confusing launchd, and alert a human
        QUARANTINE.mkdir(parents=True, exist_ok=True)
        shutil.move(str(p), str(QUARANTINE / p.name))
        quarantined.append(p.name)
        log(f"QUARANTINED {p.name} (no clean deploy/ source) -> {QUARANTINE}")

    if quarantined:
        # Real owner action — a plist couldn't be auto-restored. Page it.
        parts = [f"NEEDS REBUILD {len(quarantined)}: {', '.join(quarantined)}"]
        if repaired:
            parts.append(f"(also auto-restored {len(repaired)}: {', '.join(repaired)})")
        alert("plist integrity guard: NEEDS REBUILD", " | ".join(parts))
    elif repaired:
        # Corruption caught AND fully fixed on its own — no page, just a digest note.
        alert_info(f"auto-restored {len(repaired)}: {', '.join(repaired)}")
    else:
        log("clean — all com.claude-stack./com.Operator. plists valid")

    # Second pass: re-arm any enabled auto-run job that has silently gone dark
    # (dropped from launchd without being disabled). Isolated so a reconcile hiccup
    # can never break the lint/restore guard above.
    try:
        reconcile_loaded()
    except Exception as e:
        log(f"reconcile_loaded failed: {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
