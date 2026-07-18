#!/usr/bin/env python3
"""posted_media_reaper.py — post-once-then-dead for the faceless REEL pipeline ($0/local).

After a CONFIRMED successful reel/TikTok post, the generated video is dead weight: the
cross-account dedup ledger already holds its fingerprint, so it can never be reposted.
Left alone, data/hustle/*_reels balloons (hundreds of MB of stale .mp4). This reaper
removes a *successfully-posted* reel REVERSIBLY: it moves the video (or its whole per-run
output dir) into data/hustle/.posted-trash/<stamp>/ and purges trash older than a short
TTL (default 24h ≈ same-day). Nothing is hard-deleted on the spot, so a misfire is
recoverable until the TTL sweep.

HARD SAFETY (never deletes the wrong thing):
  * Only operates on paths strictly under data/hustle/. Anything else -> skip-unsafe-path.
  * Moves the WHOLE per-run dir only when it is a dedicated run dir: exactly
    data/hustle/<category>_reels|_queue/<run>/. Flat layouts (e.g. data/hustle/persona1/studio/
    <ts>_<i>.mp4, where many posts share one dir) reap the single video FILE only — never
    a shared sibling/asset dir.
  * Caller is responsible for only invoking this on a verified post; this module does not
    decide success.
  * Disabled entirely with env REEL_KEEP_MEDIA=1 (mirrors the YT uploader's YTUP_KEEP_MEDIA).

Use from Python:   from tools.posted_media_reaper import reap; reap(video_path)
Use from shell:    python3 -m tools.posted_media_reaper --video /abs/reel.mp4
                   python3 -m tools.posted_media_reaper --purge        # TTL sweep only
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(os.path.expanduser("~/claude-stack"))
HUSTLE = (ROOT / "data" / "hustle").resolve()
TRASH = HUSTLE / ".posted-trash"
TRASH_TTL_HOURS = float(os.environ.get("REEL_TRASH_TTL_HOURS", "24"))
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm"}


def _disabled() -> bool:
    return os.environ.get("REEL_KEEP_MEDIA", "0") == "1"


def purge_trash(max_age_hours: float = TRASH_TTL_HOURS) -> int:
    """Hard-delete trash entries older than max_age_hours. Returns count removed."""
    if not TRASH.exists():
        return 0
    cutoff = time.time() - max_age_hours * 3600
    removed = 0
    for entry in TRASH.iterdir():
        try:
            if entry.stat().st_mtime < cutoff:
                if entry.is_dir():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    entry.unlink(missing_ok=True)
                removed += 1
        except Exception:
            pass
    return removed


def _reap_target(video: Path) -> Path:
    """Whole per-run dir for dedicated reel/queue run dirs; else the video file alone."""
    run_dir = video.parent
    cat = run_dir.parent
    if cat.parent == HUSTLE and cat.name.endswith(("_reels", "_queue")):
        return run_dir
    return video


def reap(video_path: str | os.PathLike) -> str:
    """Move a CONFIRMED-posted reel video (or its run dir) into .posted-trash, reversibly,
    then sweep expired trash. Best-effort; never raises. Returns a human-readable status."""
    if _disabled():
        return "kept (REEL_KEEP_MEDIA=1)"
    try:
        video = Path(video_path).resolve()
    except Exception as e:
        return f"err:badpath:{str(e)[:60]}"
    if video.suffix.lower() not in VIDEO_EXTS:
        return f"skip-not-video:{video.name}"
    if HUSTLE not in video.parents:
        return f"skip-unsafe-path:{video}"
    if not video.exists():
        return "already-gone"

    target = _reap_target(video)
    try:
        freed = sum(f.stat().st_size for f in target.rglob("*") if f.is_file()) \
            if target.is_dir() else target.stat().st_size
        TRASH.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        dest = TRASH / f"{target.parent.name}__{target.name}__{stamp}"
        shutil.move(str(target), str(dest))
        swept = purge_trash()
        return f"reaped {target} -> {dest.name} (freed {freed}B; swept {swept} expired)"
    except Exception as e:
        return f"err:{str(e)[:80]}"


def main() -> int:
    ap = argparse.ArgumentParser(description="Post-once-then-dead reaper for posted reels.")
    ap.add_argument("--video", help="absolute path to a CONFIRMED-posted reel video")
    ap.add_argument("--purge", action="store_true", help="only sweep expired trash, then exit")
    args = ap.parse_args()
    if args.purge:
        print(f"swept {purge_trash()} expired trash entries")
        return 0
    if not args.video:
        ap.error("--video PATH or --purge required")
    print(reap(args.video))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
