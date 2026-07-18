#!/usr/bin/env python3
"""cold_data_tiering.py — SAFE recurring auto-tiering of COLD data to the slow External.

Purpose: keep the internal SSD healthy (low free space trips the work-protection
sentinel) by relocating genuinely-cold, speed-insensitive, regenerable/archival
data — old logs — off the internal disk onto /Volumes/External. This is a MOVER,
not a deleter (nothing is destroyed; everything lands under an archive root and is
recorded in a reversible manifest).

Scope note — it deliberately COMPLEMENTS the existing movers, it does not fight them:
  • scripts/disk_guardian.sh   — internal-only reclaim (truncate/delete regenerable).
  • tools/storage_reaper.py     — delete-by-allowlist reclamation.
  • scripts/cold_storage_janitor.sh — moves $ROOT/output, $ROOT/logs, ~/Downloads,
        data/hustle MEDIA → /Volumes/External/{output,logs,...}.
  This tool owns ONLY the cold sets the janitor does NOT: data/logs, ~/Library/Logs
  stack dirs, and (future) data/videos / data/outputs. Its destination root is a
  SEPARATE tree (/Volumes/External/claude-stack-archive/<mirrored-abs-path>) so it
  can never collide with the janitor.

SAFETY MODEL (allowlist, never blocklist):
  • Only files matching an explicit per-rule pattern, older than MIN_AGE_DAYS.
  • HARD runtime protection: data/runtime, .git, live *.db/*.sqlite/*.state.json,
    CDP profiles, Ollama, ~/Library/Mail|Metadata, vm_bundles, OneDrive/CloudStorage
    are refused even if a pattern somehow matched.
  • Open-file guard: a file currently held open by any process is skipped (never
    redirect a live writer onto the slow disk).
  • Single file > CAP_GB → reported, NOT moved (ambiguous-big deferral).
  • Log rules relocate with NO symlink (pure archive); a rule may request a symlink
    only where something might still read the path.

Usage:
  cold_data_tiering.py --dry-run   # report exactly what WOULD move + total GB, move nothing
  cold_data_tiering.py             # real run: move eligible cold set, write manifest
  cold_data_tiering.py --json      # machine-readable summary
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

HOME = Path.home()
ROOT = HOME / "claude-stack"
ARCHIVE_ROOT = Path("/Volumes/External/claude-stack-archive")
LOG = HOME / "Library/Logs/claude-stack/cold-data-tiering.log"
MANIFEST = ROOT / "data/logs/cold_data_tiering.tsv"

MIN_AGE_DAYS = int(os.environ.get("COLD_TIER_MIN_AGE_DAYS", "7"))
CAP_GB = float(os.environ.get("COLD_TIER_CAP_GB", "2"))  # single LOG/MEDIA file bigger than this -> report only
# Provably-safe categories (whole-dir snapshots, media outputs) are NOT held to the
# small per-file CAP_GB report-only ceiling — that timidity left space rotting. They are
# gated instead by the External free-space FLOOR below (the real safety constraint).
EXTERNAL_MIN_FREE_GB = float(os.environ.get("COLD_TIER_EXT_MIN_FREE_GB", "15"))
LAUNCHAGENTS = HOME / "Library/LaunchAgents"

# ---- HARD runtime protection (refused even if a rule pattern matched) ----------
# Substring match against the absolute path. These are live/hot or lane-separated.
PROTECTED_SUBSTR = (
    "/data/runtime/",
    "/.git/",
    "/.chrome-cdp-profile",
    "/.ollama/",
    "/Library/Mail/",
    "/Library/Metadata/",
    "/Library/Application Support/Claude/vm_bundles",
    "/Library/CloudStorage/",
    "/OneDrive",
    "/CompanyA-local",
    "/.venv/",
    "/node_modules/",
)
# Filename patterns that indicate live state — never relocate regardless of age.
PROTECTED_NAME_GLOBS = (
    "*.db", "*.db-wal", "*.db-shm", "*.sqlite", "*.sqlite3", "*.sqlite-*",
    "*.state.json", "*.pid", "*.lock", "*.sock",
    "*healer_state*", "*sentinel*", "work_context*", "redacted_context_brain*",
)

# Absolute-path prefixes that are RUNTIME and never move as whole directories, even if a
# dir glob matched. (The .colima docker VM, the ollama inference tier, secrets/runtime,
# any live .venv, the live repo working tree.)
RUNTIME_DIR_PREFIXES = (
    str(HOME / ".colima"),
    str(HOME / ".ollama"),
    str(HOME / ".docker"),
    str(ROOT / "data/runtime"),
    str(ROOT / "data/secrets"),
    str(ROOT / ".git"),
)

# ---- ALLOWLIST: (label, src_root, name_globs, symlink) -------------------------
# symlink=False => pure archive relocate (correct for rotated/stdout logs: leaving a
# symlink would redirect a resumed launchd job's writes onto the slow External).
RULES = [
    ("stack-data-logs", ROOT / "data/logs",
     ("*.log", "*.err", "*.out", "*.log.*", "*.err.*", "*.out.*", "*.gz"), False),
    ("lib-logs-claude-stack", HOME / "Library/Logs/claude-stack",
     ("*.log", "*.err", "*.out", "*.log.*", "*.err.*", "*.out.*", "*.gz"), False),
    ("lib-logs-ClaudeStack", HOME / "Library/Logs/ClaudeStack",
     ("*.log", "*.err", "*.out", "*.log.*", "*.err.*", "*.out.*", "*.gz"), False),
    # Cold render artifacts (empty today; future-proof). Media only — never state.
    ("stack-videos", ROOT / "data/videos",
     ("*.mp4", "*.mov", "*.webm", "*.mkv", "*.m4v", "*.gif"), False),
    ("stack-outputs", ROOT / "data/outputs",
     ("*.mp4", "*.mov", "*.png", "*.jpg", "*.jpeg", "*.webp", "*.pdf", "*.zip", "*.csv"), False),
]

# ---- WHOLE-DIRECTORY tiering (broadened) ---------------------------------------
# The rails permit relocating PROVABLY non-runtime dirs: retired/*-fresh/*.bak/.KILLED
# snapshots, INACTIVE (dated backup) CDP profiles, and Downloads.internal. We DISCOVER
# candidates by glob under a small set of scan roots, then every candidate must survive
# dir_move_blockers() (launchd-referenced? open REG file? runtime prefix? too fresh?).
# Nothing is hardcoded by date — a candidate qualifies only by being provably cold.
DIR_SCAN_ROOTS = (HOME,)
MOVABLE_DIR_GLOBS = (
    "*-retired", "*-fresh", "*.bak", "*.bak-*", "*.bak.*", "*.KILLED", "*.diag",
    ".chrome-cdp-*-backup-*", ".chrome-cdp-*-login-backup-*",
    "Downloads.internal",
)
# Whole-dir moves leave a symlink back (transparent + reversible). SAFE here precisely
# because dir_move_blockers() refuses any dir that a launchd job WRITES to.
DIR_SYMLINK_BACK = True

CAP_BYTES = int(CAP_GB * 1024 ** 3)
AGE_CUTOFF = time.time() - MIN_AGE_DAYS * 86400


def ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def note(msg: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a") as fh:
        fh.write(f"{ts()} {msg}\n")


def external_healthy() -> bool:
    if not os.path.ismount("/Volumes/External"):
        return False
    try:
        ARCHIVE_ROOT.mkdir(parents=True, exist_ok=True)
        probe = ARCHIVE_ROOT / ".writeprobe"
        probe.write_text("ok")
        probe.unlink()
        return True
    except Exception:
        return False


def external_free_bytes() -> int:
    try:
        st = os.statvfs("/Volumes/External")
        return st.f_bavail * st.f_frsize
    except Exception:
        return 0


def _launchd_text() -> str:
    """Concatenated text of every LaunchAgent plist (best-effort, cached per run)."""
    if not hasattr(_launchd_text, "_cache"):
        buf = []
        try:
            for pl in LAUNCHAGENTS.glob("*.plist*"):
                try:
                    buf.append(pl.read_text(errors="ignore"))
                except Exception:
                    continue
        except Exception:
            pass
        _launchd_text._cache = "\n".join(buf)
    return _launchd_text._cache


def launchd_references(p: Path) -> bool:
    """True if any LaunchAgent plist mentions this dir's absolute path (it or a child).
    Catches runtime dirs whose 'retired' look is a lie (e.g. ~/.claude-stack →
    webui-backend-a's provider-homes)."""
    return str(p) in _launchd_text()


def dir_has_open_regfile(p: Path) -> bool:
    """True if any *regular file* under p is held open by a live process. A bare
    directory read handle (Dock/Finder browsing) does NOT count — only real data I/O."""
    if not p.exists():
        return False
    try:
        r = subprocess.run(
            ["/usr/sbin/lsof", "-F", "ptn", "-w", "+D", str(p)],
            capture_output=True, text=True, timeout=45,
        )
    except Exception as e:
        note(f"lsof dir-guard unavailable for {p} ({e}); refusing move to stay safe")
        return True  # fail-safe: unknown = do not move
    cur_type = None
    for line in r.stdout.splitlines():
        if line.startswith("t"):
            cur_type = line[1:]
        elif line.startswith("n") and cur_type == "REG":
            return True
    return False


def newest_mtime(p: Path) -> float:
    newest = 0.0
    for dp, dirs, files in os.walk(p):
        for fn in files:
            try:
                m = (Path(dp) / fn).lstat().st_mtime
            except OSError:
                continue
            if m > newest:
                newest = m
    return newest


def dir_move_blockers(p: Path) -> str | None:
    """Return a human reason the dir must NOT move, or None if provably safe to tier."""
    s = str(p)
    if p.is_symlink():
        return "is a symlink"
    if any(s == pre or s.startswith(pre + "/") or pre.startswith(s + "/")
           for pre in RUNTIME_DIR_PREFIXES):
        return "runtime prefix"
    if any(sub in s for sub in PROTECTED_SUBSTR):
        # NB: PROTECTED_SUBSTR blocks LIVE cdp profiles ("/.chrome-cdp-profile"); dated
        # *-backup-*/*-login-backup-* dirs don't contain that substring, so they pass.
        return "protected substring"
    if launchd_references(p):
        return "referenced by a launchd job"
    nm = newest_mtime(p)
    if nm == 0.0:
        return "empty / unreadable"
    if nm >= AGE_CUTOFF:
        return f"has a file modified <{MIN_AGE_DAYS}d ago (possibly hot)"
    if dir_has_open_regfile(p):
        return "a live process holds a file open inside it"
    return None


def dir_size_and_count(p: Path) -> tuple[int, int]:
    total = n = 0
    for dp, dirs, files in os.walk(p):
        for fn in files:
            fp = Path(dp) / fn
            try:
                if fp.is_symlink():
                    n += 1
                    continue
                total += fp.stat().st_size
                n += 1
            except OSError:
                continue
    return total, n


def is_protected(p: Path) -> bool:
    s = str(p)
    if any(sub in s for sub in PROTECTED_SUBSTR):
        return True
    name = p.name
    return any(fnmatch.fnmatch(name, g) for g in PROTECTED_NAME_GLOBS)


def matches(name: str, globs) -> bool:
    return any(fnmatch.fnmatch(name, g) for g in globs)


def open_files(roots) -> set[str]:
    """Absolute paths currently held open under the given roots (best-effort lsof)."""
    existing = [str(r) for r in roots if r.exists()]
    if not existing:
        return set()
    out = set()
    try:
        r = subprocess.run(
            ["/usr/sbin/lsof", "-Fn", "-w", "+D", *existing],
            capture_output=True, text=True, timeout=45,
        )
        for line in r.stdout.splitlines():
            if line.startswith("n/"):
                out.add(line[1:])
    except Exception as e:
        note(f"lsof open-file guard unavailable ({e}); relying on age gate only")
    return out


def dest_for(src: Path) -> Path:
    # Mirror the absolute path (minus leading '/') under the archive root.
    return ARCHIVE_ROOT / str(src).lstrip("/")


def scan():
    """Return (moves, deferred). moves=[(rule,label,src,dest,size,symlink)], deferred=[(src,size,reason)]."""
    opens = open_files([r[1] for r in RULES])
    moves, deferred = [], []
    for label, root, globs, symlink in RULES:
        if not root.exists():
            continue
        for dp, dirs, files in os.walk(root):
            # never descend into a protected subtree
            dirs[:] = [d for d in dirs if not is_protected(Path(dp) / d)]
            for fn in files:
                src = Path(dp) / fn
                if src.is_symlink() or not matches(fn, globs):
                    continue
                if is_protected(src):
                    continue
                try:
                    st = src.stat()
                except OSError:
                    continue
                if st.st_mtime >= AGE_CUTOFF:
                    continue  # too fresh = potentially hot
                if str(src) in opens:
                    continue  # held open by a live writer
                if st.st_size > CAP_BYTES:
                    deferred.append((src, st.st_size, f">{CAP_GB}GB single item"))
                    continue
                moves.append((label, src, dest_for(src), st.st_size, symlink))
    return moves, deferred


def scan_dirs():
    """Discover provably-cold whole directories to tier.
    Return (dir_moves, dir_blocked). dir_moves=[(label,src,dest,size,count)],
    dir_blocked=[(src,size,reason)]."""
    seen, dir_moves, dir_blocked = set(), [], []
    for root in DIR_SCAN_ROOTS:
        for glob in MOVABLE_DIR_GLOBS:
            for cand in sorted(root.glob(glob)):
                if not cand.is_dir() or cand in seen:
                    continue
                seen.add(cand)
                reason = dir_move_blockers(cand)
                size, count = dir_size_and_count(cand)
                if reason:
                    dir_blocked.append((cand, size, reason))
                    continue
                dir_moves.append((glob_label(glob), cand, dest_for(cand), size, count))
    return dir_moves, dir_blocked


def glob_label(glob: str) -> str:
    return "dir:" + glob.strip("*.").replace("/", "_")[:20] or "dir:snapshot"


def do_move_dir(src: Path, dest: Path, symlink: bool) -> tuple[bool, int]:
    """Mirror-then-VERIFY-then-remove a whole directory. Returns (ok, bytes_freed).
    Never deletes the original until the copy is verified (file count + total bytes)."""
    src_bytes, src_count = dir_size_and_count(src)
    if dest.exists():
        dest = dest.with_name(dest.name + "." + time.strftime("%Y%m%d%H%M%S"))
    try:
        shutil.copytree(src, dest, symlinks=True, dirs_exist_ok=False)
    except Exception as e:
        note(f"dir copy FAILED {src} -> {dest}: {e}")
        try:
            if dest.exists():
                shutil.rmtree(dest)
        except Exception:
            pass
        return False, 0
    dst_bytes, dst_count = dir_size_and_count(dest)
    if dst_count != src_count or dst_bytes != src_bytes:
        note(f"dir VERIFY MISMATCH {src}: src={src_count}f/{src_bytes}b "
             f"dst={dst_count}f/{dst_bytes}b — leaving original, removing partial copy")
        try:
            shutil.rmtree(dest)
        except Exception:
            pass
        return False, 0
    try:
        shutil.rmtree(src)
    except Exception as e:
        note(f"dir rm-original FAILED {src} (copy verified at {dest}): {e}")
        return False, 0
    if symlink:
        try:
            os.symlink(dest, src)
        except Exception as e:
            note(f"dir symlink-back FAILED {src} -> {dest}: {e}")
    note(f"dir tiered {src} -> {dest} ({human(src_bytes)}, {src_count} files)")
    return True, src_bytes


def do_move(src: Path, dest: Path, symlink: bool) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest = dest.with_name(dest.name + "." + time.strftime("%Y%m%d%H%M%S"))
    try:
        shutil.move(str(src), str(dest))
    except Exception as e:
        note(f"move FAILED {src} -> {dest}: {e}")
        return False
    if symlink:
        try:
            os.symlink(dest, src)
        except Exception as e:
            note(f"symlink FAILED {src} -> {dest}: {e}")
    return True


def human(n: int) -> str:
    return f"{n / 1024**3:.2f} GB" if n >= 1024**3 else f"{n / 1024**2:.1f} MB"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="report only, move nothing")
    ap.add_argument("--json", action="store_true", help="machine-readable summary")
    args = ap.parse_args()

    if not external_healthy():
        msg = "External absent/unwritable — no-op"
        note(msg)
        print(json.dumps({"skipped": msg}) if args.json else f"[cold-tier] {msg}")
        return 0

    moves, deferred = scan()
    dir_moves, dir_blocked = scan_dirs()
    file_total = sum(m[3] for m in moves)
    dir_total = sum(d[3] for d in dir_moves)
    total = file_total + dir_total
    floor_bytes = int(EXTERNAL_MIN_FREE_GB * 1024 ** 3)
    ext_free = external_free_bytes()

    if args.dry_run or (not moves and not dir_moves):
        if not args.json:
            print(f"[cold-tier] DRY-RUN — cutoff={MIN_AGE_DAYS}d  file-cap={CAP_GB}GB  "
                  f"ext-floor={EXTERNAL_MIN_FREE_GB}GB (free now {human(ext_free)})  dest={ARCHIVE_ROOT}")
            by_label = {}
            for label, src, dest, size, _ in moves:
                by_label.setdefault(label, [0, 0])
                by_label[label][0] += 1
                by_label[label][1] += size
            for label, (n, sz) in sorted(by_label.items()):
                print(f"  {label:24} {n:5} files  {human(sz)}")
            if dir_moves:
                print("  WHOLE-DIR moves (mirror+verify+symlink-back):")
                for label, src, dest, size, count in dir_moves:
                    print(f"    {human(size):>10}  {count:5}f  {src}")
            print(f"  {'TOTAL WOULD FREE':24}        {human(total)}")
            if deferred:
                print("  DEFERRED files (report, NOT moved):")
                for src, size, reason in deferred:
                    print(f"    {human(size):>10}  {src}  [{reason}]")
            if dir_blocked:
                print("  BLOCKED dirs (runtime/hot/referenced — correctly NOT moved):")
                for src, size, reason in dir_blocked:
                    print(f"    {human(size):>10}  {src}  [{reason}]")
        else:
            print(json.dumps({
                "dry_run": True, "would_move_files": len(moves),
                "would_move_dirs": len(dir_moves), "would_free_bytes": total,
                "dir_moves": [{"path": str(s), "bytes": z} for _, s, _, z, _ in dir_moves],
                "deferred": [{"path": str(s), "bytes": z, "reason": r} for s, z, r in deferred],
                "dir_blocked": [{"path": str(s), "bytes": z, "reason": r} for s, z, r in dir_blocked],
            }))
        if not args.dry_run and not moves and not dir_moves:
            note("real run: nothing eligible")
        return 0

    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    budget = ext_free - floor_bytes  # bytes we may still consume on External
    moved_n = moved_b = 0
    skipped_floor = []
    with open(MANIFEST, "a") as mf:
        for label, src, dest, size, symlink in moves:
            if size > budget:
                skipped_floor.append((src, size))
                continue
            if do_move(src, dest, symlink):
                mf.write(f"{ts()}\t{label}\t{size}\t{src}\t{dest}\t{int(symlink)}\n")
                moved_n += 1
                moved_b += size
                budget -= size
        for label, src, dest, size, count in dir_moves:
            if size > budget:
                skipped_floor.append((src, size))
                continue
            ok, freed = do_move_dir(src, dest, DIR_SYMLINK_BACK)
            if ok:
                mf.write(f"{ts()}\t{label}\t{freed}\t{src}\t{dest}\t{int(DIR_SYMLINK_BACK)}\n")
                moved_n += 1
                moved_b += freed
                budget -= freed
    # prune now-empty dirs left behind (roots themselves preserved)
    for _, root, _, _ in RULES:
        if root.exists():
            subprocess.run(["/usr/bin/find", str(root), "-mindepth", "1", "-depth",
                            "-type", "d", "-empty", "-delete"],
                           capture_output=True)
    # mirror manifest to external for durability
    try:
        (ARCHIVE_ROOT / "manifests").mkdir(parents=True, exist_ok=True)
        shutil.copy2(MANIFEST, ARCHIVE_ROOT / "manifests" / MANIFEST.name)
    except Exception:
        pass

    if skipped_floor:
        note(f"External-floor held back {len(skipped_floor)} item(s) to keep "
             f">={EXTERNAL_MIN_FREE_GB}GB free on External")
    note(f"moved {moved_n} items, {human(moved_b)}; deferred {len(deferred)}; "
         f"blocked-dirs {len(dir_blocked)}")
    summary = {"moved": moved_n, "freed_bytes": moved_b,
               "skipped_external_floor": [{"path": str(s), "bytes": z} for s, z in skipped_floor],
               "deferred": [{"path": str(s), "bytes": z, "reason": r} for s, z, r in deferred],
               "dir_blocked": [{"path": str(s), "bytes": z, "reason": r} for s, z, r in dir_blocked]}
    print(json.dumps(summary) if args.json else
          f"[cold-tier] moved {moved_n} items, freed {human(moved_b)}; "
          f"deferred {len(deferred)}; blocked-dirs {len(dir_blocked)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
