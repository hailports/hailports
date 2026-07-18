#!/usr/bin/env python3
"""$0 Remotion (React -> MP4) video backend — watermark-free, unlimited, commercial-safe.

A React/Remotion composition (title/hook -> image sequence -> CTA) is rendered to
an MP4 by shelling `npx remotion render`. No credits, no per-clip vendor cost, no
watermark, full commercial rights. This is the branded/text-driven $0 default video
path (ken-burns is the b-roll-only path; fal video is the explicit paid opt-in).

Project scaffold lives on External (large node_modules):
    /Volumes/External/tools/remotion_stack

  from core.remotion_video import render
  mp4 = render(
      {"hook": "The system that runs the business without you.",
       "cta": "link below", "brand": "Ops Library",
       "images": ["/path/a.png", "/path/b.png"],   # from the image engine
       "audio": "/path/vo.wav",                      # optional
       "bg": "#060b14", "accent": "#6aa1ff"},
      "/tmp/brand.mp4",
  )

First render needs `ensure_installed()` (npm install under the shared pkg lock).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

PROJECT = Path(os.environ.get("REMOTION_STACK_DIR", "/Volumes/External/tools/remotion_stack"))
PUBLIC = PROJECT / "public"
ENTRY = "src/index.ts"
COMPOSITION = "BrandVideo"
PKG_LOCK = Path("/tmp/stack_pkg_install.lock")

# defaults mirror BrandVideo.defaultProps / the premium_short navy+accent palette
_DEFAULTS = {
    "brand": "Studio",
    "cta": "link below",
    "bg": "#060b14",
    "accent": "#6aa1ff",
    "ink": "#f4f8ff",
    "muted": "#9eb0cc",
    "fps": 30,
    "width": 1080,
    "height": 1920,
    "hookSeconds": 2.4,
    "ctaSeconds": 2.4,
}


def _npx() -> str | None:
    return shutil.which("npx")


def _node() -> str | None:
    return shutil.which("node")


def installed() -> bool:
    return (PROJECT / "node_modules" / "remotion").exists()


def configured() -> bool:
    return bool(_npx() and _node() and (PROJECT / "package.json").exists())


def _probe_audio_dur(path: str) -> float:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=30)
        return float(json.loads(out.stdout)["format"]["duration"])
    except Exception:
        return 0.0


def ensure_installed(timeout: int = 1800) -> bool:
    """npm install remotion deps under the shared atomic pkg lock. Idempotent."""
    if installed():
        return True
    if not configured():
        print("[remotion] node/npx/project missing")
        return False
    # serialize with sibling agents (mkdir = atomic)
    waited = 0
    while True:
        try:
            PKG_LOCK.mkdir()
            break
        except FileExistsError:
            time.sleep(5)
            waited += 5
            if waited > 900:
                print("[remotion] gave up waiting for pkg lock")
                return False
    try:
        r = subprocess.run(
            ["npm", "install", "--no-audit", "--no-fund"],
            cwd=str(PROJECT), capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            print(f"[remotion] npm install failed: {r.stderr[-800:]}")
            return False
    finally:
        try:
            PKG_LOCK.rmdir()
        except Exception:
            pass
    return installed()


def _stage_asset(src: str, tag: str) -> str:
    """Copy an input asset into public/ under a unique name; return the basename."""
    p = Path(src)
    PUBLIC.mkdir(parents=True, exist_ok=True)
    name = f"{tag}_{abs(hash((str(p), p.stat().st_mtime))) % 10**10}{p.suffix.lower()}"
    dst = PUBLIC / name
    if not dst.exists():
        shutil.copy2(p, dst)
    return name


def render(props: dict | str, out_mp4: str, *, fps: int | None = None,
           duration_s: float | None = None, cleanup: bool = True,
           timeout: int = 1800) -> str | None:
    """Render the BrandVideo composition to out_mp4 with brand props.

    props: dict (or JSON path/string) with keys the composition understands:
      hook, cta, brand, images[list of abs paths], audio(abs path|None),
      bg, accent, ink, muted, hookSeconds, ctaSeconds.
    duration_s: total length; if omitted, derived from audio, else 10s.

    Returns the mp4 path on success, else None.
    """
    if isinstance(props, str):
        props = json.loads(Path(props).read_text()) if Path(props).exists() else json.loads(props)
    props = {**_DEFAULTS, **dict(props)}

    if not configured():
        print("[remotion] not configured (need node + npx + project)")
        return None
    if not installed() and not ensure_installed():
        return None

    fps = int(fps or props.get("fps") or 30)
    props["fps"] = fps

    # stage images + audio into public/
    staged_imgs = []
    for i, img in enumerate(props.get("images") or []):
        if img and Path(img).exists():
            staged_imgs.append(_stage_asset(img, f"img{i}"))
    props["images"] = staged_imgs

    audio_name = None
    audio_dur = 0.0
    if props.get("audio") and Path(props["audio"]).exists():
        audio_dur = _probe_audio_dur(props["audio"])
        audio_name = _stage_asset(props["audio"], "aud")
    props["audio"] = audio_name

    total = duration_s or (audio_dur + 0.6 if audio_dur else 10.0)
    props["durationInFrames"] = max(fps, int(round(total * fps)))

    out_mp4 = str(out_mp4)
    Path(out_mp4).parent.mkdir(parents=True, exist_ok=True)

    npx = _npx()
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as pf:
        json.dump(props, pf)
        props_path = pf.name

    try:
        cmd = [npx, "remotion", "render", ENTRY, COMPOSITION, out_mp4,
               f"--props={props_path}", "--log=error"]
        r = subprocess.run(cmd, cwd=str(PROJECT), capture_output=True, text=True,
                           timeout=timeout)
        if r.returncode != 0 or not Path(out_mp4).exists():
            print(f"[remotion] render failed: {(r.stderr or r.stdout)[-1000:]}")
            return None
        return out_mp4
    finally:
        try:
            os.unlink(props_path)
        except Exception:
            pass
        if cleanup:
            for nm in staged_imgs + ([audio_name] if audio_name else []):
                try:
                    (PUBLIC / nm).unlink()
                except Exception:
                    pass


if __name__ == "__main__":
    import sys
    print("remotion configured:", configured(), "| installed:", installed())
    if len(sys.argv) > 1 and sys.argv[1] == "install":
        print("install:", ensure_installed())
