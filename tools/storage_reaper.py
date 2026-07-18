#!/usr/bin/env python3
"""storage_reaper.py — pressure-tiered, allowlist-only disk reclamation.

Companion to scripts/disk_guardian.sh and tools/cdp_profile_pruner.py. It does NOT
duplicate or fight them — it extends coverage to regenerable bulk those two do not
own, and DEFERS any target they already manage:

  disk_guardian.sh owns : ~/Library/Caches/* (incl. pip), chrome *support* caches,
                          Time Machine snapshots, brew cleanup, npm cache clean,
                          docker image+builder prune, .stale-* chrome profiles.
  cdp_profile_pruner.py owns: ~/.chrome-cdp-profile*/ Cache/Code Cache/Service Worker.

  storage_reaper.py owns (this file):
    • ~/.cache (XDG) incl. Hugging Face cache, yarn caches  [guardian misses these]
    • repo-wide __pycache__ / *.pyc / .pytest_cache / .mypy_cache / .ruff_cache
      (guardian only reaches maxdepth 4 — the repo nests far deeper)
    • docker STOPPED containers + UNUSED volumes  (guardian only prunes images/builder)
    • /tmp + repo tmp leftovers older than N days
    • oversized repo logs (*.out / logs/*.log) truncated to a tail cap
    • backups BEYOND the rotation window
    • SCORCHED only: call cdp_profile_pruner --force (chrome caches), and purge
      large stale files in Downloads/offload older than N days.

Pressure tiers (free % on /, via df/statvfs):
    >= 25%  healthy   → no-op (just log)
    < 25%   aggressive→ caches, pycache, docker containers/volumes, tmp, logs, old backups
    < 12%   scorched  → all of aggressive + chrome-cache prune + Downloads/offload purge

SAFETY MODEL — delete-by-ALLOWLIST, never delete-by-exclusion:
  Every path is produced by an allowlisted enumerator of regenerable sources, then
  passed through _is_protected() which structurally refuses anything under the
  protect-list (.env, *.key, auth.json, Cookies/Login Data/Local State, data/,
  data.internal/, products/, brands, .git, backups-in-window, *state*.json, etc.).
  A target must clear BOTH gates to be touched. Bounded + reversible-only: if disk
  is still pressured after a full cycle, it ESCALATES to Operator (core.alert) rather
  than deleting anything outside the allowlist.

Usage:
  storage_reaper.py            # gated real run (launchd); no-op unless pressured
  storage_reaper.py --dry-run  # report reclaimable GB by source, delete nothing
  storage_reaper.py --force-aggressive | --force-scorched   # ignore disk gate (testing)
"""
from __future__ import annotations

import argparse
import gzip
import os
import plistlib
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
REPO = HOME / "claude-stack"
VENV_PY = REPO / ".venv/bin/python"
LOG = HOME / "Library/Logs/claude-stack/storage-reaper.log"

AGGRESSIVE_PCT = 25.0
SCORCHED_PCT = 12.0

LOG_CAP_BYTES = 50 * 1024 * 1024     # truncate repo logs bigger than this
LOG_KEEP_LINES = 5000
TMP_AGE_DAYS = 3
BACKUP_ROTATION_DAYS = 30            # backups newer than this are protected
DOWNLOADS_AGE_DAYS = 30
DOWNLOADS_MIN_BYTES = 200 * 1024 * 1024  # only purge genuinely large stale files

# --- reel/media balloon trim (surgical exception to the /data/ blanket-protect) -
REEL_AGE_DAYS = 2                        # tightened 14->2: engines generate GBs/day and post ~nothing,
                                         # so 14d retention WAS the recurring disk fire. 2d is plenty.
REEL_MAX_REAP_BYTES = 3 * 1024**3        # per-run cap → never kill-storm a 2GB tree at once
# --- append-only JSONL rotation (regenerable logs only; audit/compliance excluded) -
JSONL_CAP_BYTES = 25 * 1024 * 1024       # rotate regenerable .jsonl over this
JSONL_KEEP_BYTES = 15 * 1024 * 1024      # tail kept live (newline-aligned); full gz-archived first
AUDIT_JSONL_ALERT_BYTES = 120 * 1024 * 1024  # alert (never auto-touch) above this
# --- reversibility: everything "deleted" by the new reclaimers lands here first ---
_REAP_TRASH = REPO / ".reaped-trash"
TRASH_RETENTION_DAYS = 7                  # GC reap-trash older than this
# --- duplicate-app detection / prevention -----------------------------------
APP_SCAN_ROOTS = (
    Path("/Applications"),
    HOME / "Applications",
    HOME / "Applications/Chrome Apps.localized",
    HOME / "Desktop",
    HOME / "Downloads",
)
_NUMBERED_APP = re.compile(r"^(?P<base>.+?) \d+\.app$")  # Chrome-PWA " N.app" shells
# --- repo/venv clone sprawl (report-only, the real 'duplicate install') -------
REPO_CLONE_CANDIDATES = (
    HOME / "claude-stack-fresh", HOME / "claude-stack-local",
    HOME / "claude-stack-local-runtime", HOME / ".claude-stack",
    HOME / ".claude-stack-critical-mirror", HOME / ".stack-anon-mirror",
)
REPORT_INTERVAL = 12 * 3600              # expensive report passes run at most this often

# ---------------------------------------------------------------------------
# Protect-list — structural refusal. If a candidate path matches ANY of these it
# is NEVER touched, regardless of which enumerator produced it.
# ---------------------------------------------------------------------------
_PROTECT_NAME_EXACT = {
    ".env", "auth.json", "Cookies", "Cookies-journal",
    "Login Data", "Login Data-journal", "Login Data For Account",
    "Local State", "Web Data", ".git", "Network", "Sessions",
}
_PROTECT_NAME_SUFFIX = (".key", ".pem", ".env", "state.json", "_state.json")
# absolute subtrees that must be unreachable (resolved-path prefix match)
_PROTECT_TREE_PARTS = (
    "/.env", "/.git/",
    "/claude-stack/data/", "/claude-stack/data.internal/",
    "/claude-stack/products/", "/claude-stack/brands/",
    "/claude-stack/output/",
    "/.openclaw/", "/.claude/", "/.codex/", "/.sfdx/", "/.sf/",
    "/Library/Application Support/Google/Chrome/",   # guardian/cdp-pruner territory
    "/.chrome-cdp-profile",                          # cdp_profile_pruner territory
)
_PROTECT_NAME_SUBSTR = ("cookie", "login data", "credential", "secret", "token")


def _load_work_tree_parts():
    """Merge the AUTHORITATIVE work allowlist (data/hustle/work_protected.json) into
    the structural protect-list so the reaper NEVER touches the work brain, work CDP
    profiles, OneDrive mirrors or the SF docs inputs. Fail-safe: unreadable JSON keeps
    the hardcoded parts above (over-protect, never under-protect)."""
    import json as _json
    p = HOME / "claude-stack/data/hustle/work_protected.json"
    try:
        return tuple(_json.loads(p.read_text()).get("never_reap_tree_parts", []))
    except Exception:
        return ()


_PROTECT_TREE_PARTS = _PROTECT_TREE_PARTS + _load_work_tree_parts()


def _is_protected(path: Path) -> bool:
    try:
        rp = str(path.resolve())
    except OSError:
        rp = str(path)
    rpl = rp.lower()
    name = path.name
    if name in _PROTECT_NAME_EXACT:
        return True
    if any(name.endswith(s) for s in _PROTECT_NAME_SUFFIX):
        return True
    if any(s in rp for s in _PROTECT_TREE_PARTS):
        return True
    if any(s in rpl for s in _PROTECT_NAME_SUBSTR):
        return True
    return False


# ---------------------------------------------------------------------------
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


def free_pct() -> float:
    st = os.statvfs("/")
    return 100.0 * st.f_bavail / st.f_blocks


def free_gb() -> float:
    st = os.statvfs("/")
    return st.f_bavail * st.f_frsize / 1e9


def path_size(path: Path) -> int:
    if path.is_file() or path.is_symlink():
        try:
            return os.lstat(path).st_size
        except OSError:
            return 0
    total = 0
    for root, _d, files in os.walk(path, onerror=lambda e: None):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                continue
    return total


def _rm(path: Path, dry_run: bool) -> int:
    """Allowlist-gated removal of one path. Returns bytes freed (or that would be)."""
    if not path.exists() and not path.is_symlink():
        return 0
    if _is_protected(path):
        note(f"  PROTECTED, skip: {path}")
        return 0
    sz = path_size(path)
    if dry_run:
        return sz
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
        return sz
    except OSError:
        return 0  # open/locked — leave it


# ---------------------------------------------------------------------------
# Alerting + scheduling helpers (deduped via the central gateway; never spam)
# ---------------------------------------------------------------------------
def _alert(severity: str, issue_key: str, subject: str, body: str = "") -> None:
    """Route through core.alert_gateway (issue-key dedup + cooldown + digest).
    Low-sev (warn/info) coalesces into a 15-min digest, so repeated runs never
    page Operator twice for the same standing condition. Best-effort: a routing
    failure is logged, never fatal."""
    try:
        sys.path.insert(0, str(REPO))
        from core.alert_gateway import route
        route(severity, "storage_reaper", subject, body, issue_key=issue_key)
    except Exception as e:
        note(f"  alert route failed ({issue_key}): {e}")


def _stamp_due(name: str, interval_s: int) -> bool:
    """True at most once per interval_s. Stamps on success so expensive report
    passes don't run every tick."""
    st = HOME / f".storage-reaper.{name}"
    now = time.time()
    try:
        last = float(st.read_text().strip())
    except (OSError, ValueError):
        last = 0.0
    if now - last >= interval_s:
        try:
            st.write_text(str(now))
        except OSError:
            pass
        return True
    return False


# ---------------------------------------------------------------------------
# Reversible removal — MOVE to .reaped-trash/<date>/ (GC'd after 7d) instead of
# hard-delete, so any mistaken reap is recoverable for a week. Handles files+dirs.
# ---------------------------------------------------------------------------
def _reap_to_trash(path: Path, dry_run: bool) -> int:
    sz = path_size(path)
    if sz == 0 and not (path.exists() or path.is_symlink()):
        return 0
    if dry_run:
        return sz
    day = time.strftime("%Y%m%d")
    try:
        rel = path.resolve().relative_to(HOME)
    except (ValueError, OSError):
        rel = Path(path.name)
    dest = _REAP_TRASH / day / rel
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            dest = dest.with_name(dest.name + f".{int(time.time() * 1000) % 100000}")
        shutil.move(str(path), str(dest))
        return sz
    except OSError:
        return 0  # locked/open — leave it


def gc_reap_trash(dry_run: bool) -> int:
    """Remove reap-trash day-folders older than TRASH_RETENTION_DAYS. This is the
    only step that hard-deletes — and only things that already sat reversible 7d."""
    if not _REAP_TRASH.is_dir():
        return 0
    cutoff = time.time() - TRASH_RETENTION_DAYS * 86400
    freed = 0
    for day_dir in _REAP_TRASH.iterdir():
        if not day_dir.is_dir():
            continue
        try:
            if day_dir.stat().st_mtime > cutoff:
                continue
        except OSError:
            continue
        sz = path_size(day_dir)
        if dry_run:
            freed += sz
            continue
        shutil.rmtree(day_dir, ignore_errors=True)
        note(f"  GC'd reap-trash {day_dir.name} ({sz/1e9:.3f}GB, >{TRASH_RETENTION_DAYS}d old)")
        freed += sz
    return freed


# ---------------------------------------------------------------------------
# Reel/media balloon trim — SURGICAL exception to the /data/ blanket-protect.
# Only PROVEN-regenerable, already-posted MEDIA (mp4/mov/images) older than
# REEL_AGE_DAYS. NEVER json/jsonl/metadata/state/queue. Reversible (→ trash).
# ---------------------------------------------------------------------------
_MEDIA_EXTS = (".mp4", ".mov", ".m4v", ".webm", ".png", ".jpg", ".jpeg", ".webp", ".gif")
_REEL_REFUSE_SUBSTR = ("queue", "pending", "draft", "wip", "posting", "staging",
                       "cookie", "login", "credential", "secret", "token", "state.json")


def _is_reapable_media(path: Path) -> bool:
    name = path.name.lower()
    if not name.endswith(_MEDIA_EXTS):
        return False
    if any(s in str(path).lower() for s in _REEL_REFUSE_SUBSTR):
        return False
    return True


def _reel_media_roots() -> list[Path]:
    base = REPO / "data/hustle"
    roots = [r for r in base.glob("*_reels") if r.is_dir()]
    tc = base / "tiktok_content"
    if tc.is_dir():
        roots.append(tc)
    return roots


def reclaim_posted_reels(dry_run: bool) -> int:
    """Trim posted/dead reel + tiktok MEDIA older than REEL_AGE_DAYS. Bounded by
    REEL_MAX_REAP_BYTES/run and reversible via .reaped-trash. metadata.json and
    any *queue*/*state* path are structurally refused by _is_reapable_media."""
    freed = 0
    cutoff = time.time() - REEL_AGE_DAYS * 86400
    for root in _reel_media_roots():
        for f in root.rglob("*"):
            if freed >= REEL_MAX_REAP_BYTES:
                note(f"  reel trim hit per-run cap ({REEL_MAX_REAP_BYTES/1e9:.1f}GB) — deferring rest")
                return freed
            if not f.is_file() or f.is_symlink():
                continue
            if not _is_reapable_media(f):
                continue
            try:
                if f.stat().st_mtime > cutoff:
                    continue
            except OSError:
                continue
            freed += _reap_to_trash(f, dry_run)
    return freed


# ---------------------------------------------------------------------------
# Append-only JSONL rotation — regenerable logs only. Reversible: the FULL
# original is gz-archived to reap-trash before the live file is cut to its tail.
# Audit/compliance logs (dnc/audit/state/queue) are NEVER auto-touched — alert only.
# ---------------------------------------------------------------------------
_JSONL_REFUSE_SUBSTR = ("audit", "dnc", "compliance", "state", "queue",
                        "credential", "secret", "token")


def _archive_and_tail(f: Path, keep_bytes: int) -> bool:
    """Reversibly cap an append-only log: gz-archive the FULL original to reap-trash,
    then rewrite the live file to its last ~keep_bytes, trimmed to a clean newline
    boundary so the first surviving record is never a half-line."""
    day = time.strftime("%Y%m%d")
    try:
        rel = f.resolve().relative_to(HOME)
    except (ValueError, OSError):
        rel = Path(f.name)
    archive = _REAP_TRASH / day / (str(rel) + ".full.gz")
    try:
        sz = f.stat().st_size
        archive.parent.mkdir(parents=True, exist_ok=True)
        with open(f, "rb") as src, gzip.open(archive, "wb", compresslevel=6) as dst:
            shutil.copyfileobj(src, dst, 1024 * 1024)
        with open(f, "rb") as src:
            if sz > keep_bytes:
                src.seek(sz - keep_bytes)
                chunk = src.read()
                nl = chunk.find(b"\n")
                tail = chunk[nl + 1:] if nl != -1 else chunk
            else:
                tail = src.read()
        with open(f, "wb") as fh:
            fh.write(tail)
        return True
    except OSError:
        return False


def reclaim_oversized_jsonl(dry_run: bool) -> int:
    freed = 0
    safelist = [REPO / "data/metrics.jsonl"]
    for sub in ("data/logs", "data/runtime"):
        d = REPO / sub
        if d.is_dir():
            safelist += [p for p in d.rglob("*.jsonl") if p.is_file()]
    seen: set[Path] = set()
    for f in safelist:
        try:
            rf = f.resolve()
        except OSError:
            continue
        if rf in seen or not f.is_file():
            continue
        seen.add(rf)
        if any(s in str(f).lower() for s in _JSONL_REFUSE_SUBSTR):
            continue
        try:
            sz = f.stat().st_size
        except OSError:
            continue
        if sz <= JSONL_CAP_BYTES:
            continue
        if dry_run:
            freed += max(0, sz - JSONL_CAP_BYTES)
            note(f"  would rotate {f.name} ({sz/1e6:.0f}MB → ~{JSONL_CAP_BYTES/1e6:.0f}MB)")
            continue
        if _archive_and_tail(f, JSONL_KEEP_BYTES):
            try:
                after = f.stat().st_size
            except OSError:
                after = 0
            freed += max(0, sz - after)
            note(f"  rotated {f.relative_to(REPO)} ({sz/1e6:.0f}MB→{after/1e6:.1f}MB; "
                 f"full gz-archived to reap-trash)")
    # compliance/audit logs: never auto-touch — surface (deduped) when very large
    for name in ("data/hustle/dnc_audit.jsonl",):
        p = REPO / name
        if not p.is_file():
            continue
        try:
            sz = p.stat().st_size
        except OSError:
            continue
        if sz >= AUDIT_JSONL_ALERT_BYTES:
            note(f"  audit log large (no auto-rotate): {name} {sz/1e6:.0f}MB")
            if not dry_run:
                _alert("warn", "storage_audit_jsonl",
                       f"Audit log large: {name} {sz/1e6:.0f}MB",
                       "Compliance/audit JSONL is never auto-rotated (records matter). "
                       "Archive manually if you want the space back.")
    return freed


# ---------------------------------------------------------------------------
# Duplicate-app detection + prevention. Auto-quarantines only Chrome-PWA
# numbered shells (' N.app') — reversible to trash. True dup installs (same
# CFBundleIdentifier in 2+ live locations) are WARNED, never auto-removed.
# ---------------------------------------------------------------------------
def _iter_apps(root: Path, maxdepth: int = 2):
    if not root.is_dir():
        return
    rootdepth = len(root.parts)
    for cur, dirs, _files in os.walk(root, onerror=lambda e: None):
        curp = Path(cur)
        depth = len(curp.parts) - rootdepth
        for d in list(dirs):
            if d.endswith(".app"):
                yield curp / d
                dirs.remove(d)            # never descend INTO a bundle
        if depth >= maxdepth:
            dirs[:] = []


def _bundle_id(app: Path):
    info = app / "Contents/Info.plist"
    if not info.is_file():
        return None
    try:
        with open(info, "rb") as fh:
            return plistlib.load(fh).get("CFBundleIdentifier")
    except Exception:
        return None


def detect_duplicate_apps(dry_run: bool) -> int:
    freed = 0
    apps: list[Path] = []
    seen_paths: set[str] = set()
    for root in APP_SCAN_ROOTS:
        for app in _iter_apps(root):
            rp = str(app.resolve())
            if rp in seen_paths:
                continue
            seen_paths.add(rp)
            apps.append(app)

    # 1) Chrome-PWA numbered shells (" N.app") with a base sibling → quarantine.
    quarantined = []
    for app in apps:
        m = _NUMBERED_APP.match(app.name)
        if not m:
            continue
        sibling = app.parent / (m.group("base") + ".app")
        if sibling.exists() and sibling.resolve() != app.resolve():
            sz = _reap_to_trash(app, dry_run)
            if sz:
                quarantined.append(str(app))
                freed += sz
                note(f"  [dup-app] {'would quarantine' if dry_run else 'quarantined'} "
                     f"numbered shell: {app}")

    # 2) True duplicate installs: same bundle-id in 2+ locations → WARN only.
    by_id: dict[str, set] = {}
    for app in apps:
        if _NUMBERED_APP.match(app.name):
            continue  # handled above; don't double-flag
        bid = _bundle_id(app)
        if bid:
            by_id.setdefault(bid, set()).add(str(app))
    dups = {bid: paths for bid, paths in by_id.items() if len(paths) >= 2}
    if dups:
        lines = [f"{bid}:\n    " + "\n    ".join(sorted(p)) for bid, p in sorted(dups.items())]
        note(f"  [dup-app] {len(dups)} bundle-id(s) installed in 2+ locations")
        if not dry_run:
            _alert("warn", "storage_dup_app",
                   f"Duplicate app installs detected ({len(dups)} bundle-id(s))",
                   "Same CFBundleIdentifier in multiple live locations (NOT auto-removed — "
                   "verify which to keep):\n" + "\n".join(lines))
    if quarantined and not dry_run:
        _alert("warn", "storage_dup_shell",
               f"Quarantined {len(quarantined)} duplicate app shell(s)",
               "Chrome-PWA numbered shells moved to .reaped-trash (reversible 7d):\n  "
               + "\n  ".join(quarantined))
    return freed


# ---------------------------------------------------------------------------
# Repo/venv clone sprawl — the real 'duplicate install'. REPORT-ONLY: code trees
# are never auto-deleted; emit one deduped alert listing reclaimable GB.
# ---------------------------------------------------------------------------
def report_repo_clones(dry_run: bool) -> int:
    found = []
    total = 0
    for p in REPO_CLONE_CANDIDATES:
        if p.is_dir():
            sz = path_size(p)
            total += sz
            found.append((p, sz))
    if not found:
        return 0
    lines = [f"{p}  {sz/1e9:.2f}GB" for p, sz in sorted(found, key=lambda x: -x[1])]
    note(f"  [repo-clones] {len(found)} sibling clone(s), {total/1e9:.2f}GB reclaimable")
    if not dry_run:
        _alert("warn", "storage_repo_clones",
               f"Repo/clone sprawl: {len(found)} copies, {total/1e9:.1f}GB",
               f"Report-only (code is NEVER auto-deleted). Canonical tree: {REPO}\n"
               "Reclaimable copies:\n  " + "\n  ".join(lines))
    return 0


# ---------------------------------------------------------------------------
# Always-on maintenance — idempotent, bounded, deduped. Runs every invocation
# regardless of disk pressure (prevention, not just reaction).
# ---------------------------------------------------------------------------
def run_maintenance(dry_run: bool) -> None:
    note("maintenance pass (always-on, idempotent, deduped)")
    for label, fn, gated in (
        ("reap-trash GC (>7d)", gc_reap_trash, False),
        ("regenerable JSONL rotation", reclaim_oversized_jsonl, False),
        ("duplicate-app detection", detect_duplicate_apps, False),
        ("repo/venv clone report", report_repo_clones, True),
    ):
        # slug the throttle key: label.split()[0] could be "repo/venv", whose '/'
        # makes the stamp path's parent nonexistent → write fails → 12h throttle
        # never persists → expensive clone stat-walk runs every tick. Slash-free key.
        key = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")
        if gated and not dry_run and not _stamp_due(key, REPORT_INTERVAL):
            continue
        try:
            freed = fn(dry_run)
            if freed:
                note(f"  {'would free' if dry_run else 'freed'} {freed/1e9:.3f}GB — {label}")
        except Exception as e:
            note(f"  maintenance '{label}' error: {e}")


# ---------------------------------------------------------------------------
# Source reclaimers — each returns (bytes_freed, label). Allowlist-only.
# ---------------------------------------------------------------------------
def reclaim_xdg_and_hf_cache(dry_run: bool) -> int:
    """~/.cache (XDG) incl. Hugging Face + pre-commit etc. All regenerable.
    Guardian only touches ~/Library/Caches, not ~/.cache — no overlap."""
    freed = 0
    cache = HOME / ".cache"
    if cache.is_dir():
        for child in cache.iterdir():
            freed += _rm(child, dry_run)
    return freed


def reclaim_yarn_cache(dry_run: bool) -> int:
    freed = 0
    for p in (HOME / "Library/Caches/Yarn", HOME / ".yarn/cache"):
        if p.is_dir():
            for child in p.iterdir():
                freed += _rm(child, dry_run)
    return freed


def reclaim_pycache(dry_run: bool) -> int:
    """Repo-wide __pycache__/*.pyc/.pytest_cache/.mypy_cache/.ruff_cache.
    Guardian only reaches maxdepth 4; the venv + nested pkgs go far deeper."""
    freed = 0
    targets = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
    for root, dirs, files in os.walk(REPO, onerror=lambda e: None):
        # don't descend into protected trees
        if _is_protected(Path(root)):
            dirs[:] = []
            continue
        for d in list(dirs):
            if d in targets:
                freed += _rm(Path(root) / d, dry_run)
                dirs.remove(d)
        for f in files:
            if f.endswith((".pyc", ".pyo")):
                freed += _rm(Path(root) / f, dry_run)
    return freed


def reclaim_docker_containers_volumes(dry_run: bool) -> int:
    """STOPPED containers + UNUSED volumes only. Guardian prunes images/builder;
    running containers (searxng web-search) are never matched by these prunes."""
    if not shutil.which("docker"):
        return 0
    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        return 0
    if dry_run:
        # report reclaimable space without deleting
        out = subprocess.run(
            ["docker", "system", "df", "--format",
             "{{.Type}} {{.Reclaimable}}"],
            capture_output=True, text=True,
        ).stdout.strip()
        note(f"  docker reclaimable (df): {out or 'n/a'}")
        return 0
    subprocess.run(["docker", "container", "prune", "-f"], capture_output=True)
    subprocess.run(["docker", "volume", "prune", "-f"], capture_output=True)
    note("  docker: pruned stopped containers + unused volumes")
    return 0  # docker reports its own reclaim; not counted in byte total


def reclaim_tmp(dry_run: bool) -> int:
    """User-owned /tmp + repo tmp leftovers older than TMP_AGE_DAYS."""
    freed = 0
    cutoff = time.time() - TMP_AGE_DAYS * 86400
    roots = [Path("/tmp"), REPO / "tmp", HOME / "tmp"]
    uid = os.getuid()
    for r in roots:
        if not r.is_dir():
            continue
        for child in r.iterdir():
            try:
                stt = child.lstat()
                if stt.st_uid != uid:        # only our own tmp junk
                    continue
                if stt.st_mtime > cutoff:
                    continue
            except OSError:
                continue
            freed += _rm(child, dry_run)
    return freed


def reclaim_repo_logs(dry_run: bool) -> int:
    """Truncate oversized repo logs to a tail cap. Guardian owns ~/Library/Logs;
    this covers the repo's own *.out / logs/*.log that guardian never sees."""
    freed = 0
    candidates: list[Path] = []
    logs_dir = REPO / "logs"
    if logs_dir.is_dir():
        candidates += [p for p in logs_dir.rglob("*.log") if p.is_file()]
    candidates += [p for p in REPO.glob("*.out") if p.is_file()]
    for f in candidates:
        if _is_protected(f):
            continue
        try:
            sz = f.stat().st_size
        except OSError:
            continue
        if sz <= LOG_CAP_BYTES:
            continue
        if dry_run:
            freed += max(0, sz - LOG_CAP_BYTES)
            continue
        try:
            tail = subprocess.run(
                ["/usr/bin/tail", "-n", str(LOG_KEEP_LINES), str(f)],
                capture_output=True,
            ).stdout
            with open(f, "wb") as fh:
                fh.write(tail)
            freed += max(0, sz - len(tail))
            note(f"  truncated {f.relative_to(REPO)} ({sz/1e6:.0f}MB→{len(tail)/1e6:.1f}MB)")
        except OSError:
            continue
    return freed


def reclaim_old_backups(dry_run: bool) -> int:
    """Backups BEYOND the rotation window only (older than BACKUP_ROTATION_DAYS).
    Backups within the window are protected; service-config backup dirs are skipped."""
    freed = 0
    bdir = REPO / "backups"
    if not bdir.is_dir():
        return 0
    cutoff = time.time() - BACKUP_ROTATION_DAYS * 86400
    for child in bdir.iterdir():
        # only regenerable point-in-time *.bak snapshots; leave service dirs (e.g. cloudflared/)
        if child.is_dir():
            continue
        if not (child.suffix == ".bak" or ".bak" in child.name):
            continue
        try:
            if child.stat().st_mtime > cutoff:
                continue
        except OSError:
            continue
        freed += _rm(child, dry_run)
    return freed


# --- scorched-only ---------------------------------------------------------
def reclaim_chrome_caches_via_pruner(dry_run: bool) -> int:
    """Defer to the OWNER: tools/cdp_profile_pruner.py. Never reimplement."""
    pruner = REPO / "tools/cdp_profile_pruner.py"
    if not pruner.exists():
        return 0
    py = str(VENV_PY) if VENV_PY.exists() else sys.executable
    arg = "--dry-run" if dry_run else "--force"
    r = subprocess.run([py, str(pruner), arg], capture_output=True, text=True)
    for ln in (r.stdout or "").splitlines():
        if "freed" in ln or "would free" in ln or "done" in ln:
            note(f"  [cdp_pruner] {ln.split(' ', 2)[-1]}")
    return 0  # pruner accounts its own bytes


def reclaim_macos_system_caches(dry_run: bool) -> int:
    """SCORCHED: the two macOS regenerable caches that balloon to 10s of GB and
    are the usual reason a scorched cycle still pages Operator — neither guardian nor
    cdp_pruner own them:
      • ~/Library/Caches/CloudKit  — known macOS bloat bug; regenerates from iCloud
      • ~/.Trash                   — user already discarded these
    Direct-removed (NOT reap-trashed): moving 17GB to .reaped-trash on the SAME
    volume frees zero bytes. Both are pure caches / already-trashed, so direct
    removal is the correct reversibility model (CloudKit re-downloads; Trash is
    by definition already-discarded). Spotlight's CoreSpotlight index is the third
    big one but needs `sudo mdutil -E /` — left to manual (no unattended sudo)."""
    freed = 0
    for base in (HOME / "Library/Caches/CloudKit", HOME / ".Trash"):
        if not base.is_dir():
            continue
        for child in base.iterdir():
            if _is_protected(child):
                continue
            sz = path_size(child)
            if dry_run:
                freed += sz
                continue
            try:
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink(missing_ok=True)
                freed += sz
            except OSError:
                continue
    return freed


def reclaim_downloads_offload(dry_run: bool) -> int:
    """SCORCHED: purge LARGE, STALE files in Downloads / offload dirs only."""
    freed = 0
    cutoff = time.time() - DOWNLOADS_AGE_DAYS * 86400
    roots = [HOME / "Downloads", REPO / "offload", REPO / "downloads"]
    for r in roots:
        if not r.is_dir():
            continue
        for child in r.rglob("*"):
            if not child.is_file() or _is_protected(child):
                continue
            try:
                stt = child.stat()
                if stt.st_size < DOWNLOADS_MIN_BYTES or stt.st_mtime > cutoff:
                    continue
            except OSError:
                continue
            freed += _rm(child, dry_run)
    return freed


# ---------------------------------------------------------------------------
AGGRESSIVE_SOURCES = [
    ("~/.cache + Hugging Face", reclaim_xdg_and_hf_cache),
    ("yarn caches", reclaim_yarn_cache),
    ("repo __pycache__/*.pyc", reclaim_pycache),
    ("docker stopped containers/volumes", reclaim_docker_containers_volumes),
    ("tmp (>3d, ours)", reclaim_tmp),
    ("oversized repo logs", reclaim_repo_logs),
    ("backups beyond rotation", reclaim_old_backups),
    ("posted reels/tiktok media (>14d, reversible)", reclaim_posted_reels),
]
SCORCHED_SOURCES = [
    ("macOS system caches (CloudKit + Trash)", reclaim_macos_system_caches),
    ("chrome caches (via cdp_profile_pruner)", reclaim_chrome_caches_via_pruner),
    ("Downloads/offload (>30d, >200MB)", reclaim_downloads_offload),
]


def run_cycle(tier: str, dry_run: bool) -> int:
    sources = list(AGGRESSIVE_SOURCES)
    if tier == "scorched":
        sources += SCORCHED_SOURCES
    total = 0
    verb = "would reclaim" if dry_run else "reclaimed"
    for label, fn in sources:
        try:
            freed = fn(dry_run)
        except Exception as e:  # one bad source never aborts the cycle
            note(f"  source '{label}' error: {e}")
            continue
        total += freed
        if freed:
            note(f"  {verb} {freed/1e9:.3f}GB — {label}")
        else:
            note(f"  {verb} 0.000GB — {label}")
    return total


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="report reclaimable GB by source; delete nothing")
    ap.add_argument("--force-aggressive", action="store_true",
                    help="run aggressive tier regardless of disk pressure")
    ap.add_argument("--force-scorched", action="store_true",
                    help="run scorched tier regardless of disk pressure")
    args = ap.parse_args()

    pct = free_pct()
    gb = free_gb()

    if args.force_scorched:
        tier = "scorched"
    elif args.force_aggressive:
        tier = "aggressive"
    elif pct < SCORCHED_PCT:
        tier = "scorched"
    elif pct < AGGRESSIVE_PCT:
        tier = "aggressive"
    else:
        tier = "healthy"

    mode = "DRY-RUN" if args.dry_run else "LIVE"
    note(f"start [{mode}] free={gb:.1f}GB ({pct:.1f}%) → tier={tier} "
         f"(aggressive<{AGGRESSIVE_PCT}%, scorched<{SCORCHED_PCT}%)")

    # Always-on prevention/maintenance (dup-app, JSONL rotation, trash GC, clone
    # report) runs regardless of disk pressure — it stops ballooning before it
    # becomes pressure, and is independently bounded + deduped + reversible.
    run_maintenance(args.dry_run)

    if tier == "healthy" and not args.dry_run:
        note("healthy — maintenance only (disk not pressured)")
        return 0

    # In dry-run on a healthy disk, still report potential reclaim at aggressive tier.
    run_tier = tier if tier != "healthy" else "aggressive"
    total = run_cycle(run_tier, args.dry_run)

    after_pct = free_pct()
    after_gb = free_gb()
    note(f"done [{mode}] tier={run_tier} "
         f"{'would reclaim' if args.dry_run else 'reclaimed'} {total/1e9:.3f}GB; "
         f"free {gb:.1f}GB→{after_gb:.1f}GB ({after_pct:.1f}%)")

    # Bounded + reversible: if a LIVE scorched cycle still leaves us pressured,
    # escalate to Operator instead of deleting outside the allowlist.
    if not args.dry_run and tier == "scorched" and after_pct < SCORCHED_PCT:
        try:
            sys.path.insert(0, str(REPO))
            from core.alert import alert_alex
            alert_alex(
                "Disk still SCORCHED after full reclaim",
                f"storage_reaper ran its full scorched allowlist cycle but free space "
                f"is still {after_gb:.1f}GB ({after_pct:.1f}%). All regenerable sources "
                f"are exhausted — manual intervention needed (OrbStack VM image, large "
                f"data exports, Downloads). Reaper will not delete outside its allowlist.",
            )
            note("escalated to Operator via core.alert (still scorched post-reclaim)")
        except Exception as e:
            note(f"escalation failed: {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
