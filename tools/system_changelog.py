#!/usr/bin/env python3
"""Master system changelog — snapshot/diff logger for changes that live OUTSIDE
the git repo (launchd jobs, ~/AGENTS.md, ~/.openclaw config, key dotfiles,
selected macOS defaults) plus new commits in ~/claude-stack.

Runs on a schedule. Each run diffs every watched path against a shadow snapshot
and appends human-readable entries (with a capped unified diff) to a single
master changelog. Catches changes no matter who made them — me, the brain, or
you by hand. Append-only; the file lives in-repo so stack-keeper versions it too.
"""
from __future__ import annotations

import difflib
import glob
import hashlib
import json
import os
import socket
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

HOME = Path.home()
LOCAL_TZ = ZoneInfo("America/Chicago")
MASTER = HOME / "claude-stack/SYSTEM_CHANGELOG.md"
SNAP_DIR = HOME / ".system-changelog/snapshots"
MANIFEST = HOME / ".system-changelog/manifest.json"
REPO = HOME / "claude-stack"
MAX_DIFF_LINES = 40

# (label, list of file paths / globs) — boundary-spanning, NOT already in repo.
WATCH = [
    ("launchd", [str(HOME / "Library/LaunchAgents/com.claude-stack.*.plist"),
                 str(HOME / "Library/LaunchAgents/com.openclaw.*.plist"),
                 str(HOME / "Library/LaunchAgents/com.imma.*.plist")]),
    ("self-model", [str(HOME / "AGENTS.md"), str(HOME / "SOUL.md"), str(HOME / "USER.md")]),
    ("openclaw-config", [str(HOME / ".openclaw/pin-stable-config.py"),
                         str(HOME / ".openclaw/openclaw.json")]),
    ("claude-config", [str(HOME / ".claude/settings.json"),
                       str(HOME / ".claude/CLAUDE.md")]),
    ("dotfiles", [str(HOME / ".zshrc"), str(HOME / ".zprofile"), str(HOME / ".gitconfig")]),
]
# macOS defaults worth tracking: (domain, key). Empty domain = global (-g).
DEFAULTS = [("", "KeyRepeat"), ("", "InitialKeyRepeat"), ("", "ApplePressAndHold")]


def _now():
    return datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")


def _actor() -> str:
    """Who/where made the change — distinguishes local vs SSH sessions."""
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or "?"
    host = socket.gethostname().split(".")[0]
    ssh = os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_CLIENT")
    if ssh:
        return f"{user}@{host} via ssh {ssh.split()[0]}"
    return f"{user}@{host}"


def note(text: str) -> None:
    """Append a single attributed entry (used by git/shell hooks)."""
    MASTER.parent.mkdir(parents=True, exist_ok=True)
    header = "" if MASTER.exists() else "# 🗒️ Master System Changelog\n\n"
    with MASTER.open("a") as fh:
        if header:
            fh.write(header)
        fh.write(f"### {_now()} — [{_actor()}] {text}\n\n")


def _snap_path(real_path: str) -> Path:
    key = hashlib.sha1(real_path.encode()).hexdigest()[:16]
    return SNAP_DIR / f"{key}__{Path(real_path).name}"


def _read(p: str) -> str | None:
    try:
        return Path(p).read_text(errors="replace")
    except Exception:
        return None


def _expand() -> list[tuple[str, str]]:
    out = []
    for label, patterns in WATCH:
        for pat in patterns:
            for f in (sorted(glob.glob(pat)) if any(c in pat for c in "*?[") else [pat]):
                out.append((label, f))
    return out


def _diff(old: str, new: str, path: str) -> str:
    d = list(difflib.unified_diff(
        old.splitlines(), new.splitlines(),
        fromfile=f"a{path}", tofile=f"b{path}", lineterm="", n=1))
    body = [l for l in d if l.startswith(("+", "-")) and not l.startswith(("+++", "---"))]
    capped = body[:MAX_DIFF_LINES]
    extra = f"\n  … (+{len(body) - len(capped)} more changed lines)" if len(body) > len(capped) else ""
    return "\n".join("  " + l for l in capped) + extra


def _load_manifest() -> dict:
    try:
        return json.loads(MANIFEST.read_text())
    except Exception:
        return {"git_head": None}


def run(baseline: bool = False) -> int:
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    MASTER.parent.mkdir(parents=True, exist_ok=True)
    manifest = _load_manifest()
    entries = []

    seen_snaps = set()
    for label, path in _expand():
        snap = _snap_path(path)
        seen_snaps.add(snap.name)
        cur = _read(path)
        prev = _read(str(snap))
        short = path.replace(str(HOME), "~")
        if cur is None:
            continue
        if prev is None:
            if not baseline:
                entries.append(f"### {_now()} — ADDED [{label}] `{short}`")
            snap.write_text(cur)
        elif cur != prev:
            if not baseline:
                entries.append(f"### {_now()} — MODIFIED [{label}] `{short}`\n```diff\n{_diff(prev, cur, short)}\n```")
            snap.write_text(cur)

    # removed files (snapshot exists, source gone)
    for snap in SNAP_DIR.glob("*"):
        if snap.name not in seen_snaps and "__defaults__" not in snap.name:
            if not baseline:
                entries.append(f"### {_now()} — REMOVED `{snap.name.split('__', 1)[-1]}`")
            snap.unlink()

    # macOS defaults
    for domain, key in DEFAULTS:
        snap = SNAP_DIR / f"__defaults__{domain or 'global'}__{key}"
        seen_snaps.add(snap.name)
        cmd = ["defaults", "read"] + (["-g"] if not domain else [domain]) + [key]
        r = subprocess.run(cmd, capture_output=True, text=True)
        cur = r.stdout.strip() if r.returncode == 0 else "(unset)"
        prev = snap.read_text().strip() if snap.exists() else None
        if prev is None:
            snap.write_text(cur)
        elif cur != prev:
            if not baseline:
                dom = domain or "-g (global)"
                entries.append(f"### {_now()} — MODIFIED [defaults] `{dom} {key}`: {prev} → {cur}")
            snap.write_text(cur)

    # git commits are logged in real time by the post-commit hook (attributed +
    # auto-keeper-filtered); the poller just tracks HEAD as a backstop marker.
    try:
        head = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
        manifest["git_head"] = head or manifest.get("git_head")
    except Exception:
        pass

    MANIFEST.write_text(json.dumps(manifest, indent=2))

    if entries:
        header = "" if MASTER.exists() else "# 🗒️ Master System Changelog\n\nAuto-captured changes to launchd jobs, self-model, configs, dotfiles, macOS defaults, and the stack repo. Append-only.\n\n"
        with MASTER.open("a") as fh:
            if header:
                fh.write(header)
            fh.write("\n".join(entries) + "\n\n")
    return len(entries)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", action="store_true", help="snapshot silently, don't log (first run)")
    ap.add_argument("--note", help="append a single attributed entry (for git/shell hooks)")
    args = ap.parse_args()
    if args.note:
        note(args.note)
        print("noted")
    else:
        n = run(baseline=args.baseline)
        print(f"{'baseline snapshot taken' if args.baseline else f'{n} change(s) logged'} → {MASTER}")
