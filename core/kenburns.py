#!/usr/bin/env python3
"""$0 ken-burns b-roll — turn photoreal STILLS into motion video with FFmpeg.

A slow push-in over a single-subject still reads like real b-roll. This is the
free, unlimited, watermark-free, full-commercial-rights default video path for
single-subject shots (vs. paying per-clip for fal i2v). Pure FFmpeg zoompan +
xfade crossfades + audio mux — no model, no network, no credits.

Generalized from scripts/build_premium_short.py's moviepy ken_burns/crossfade
block, but done natively in ffmpeg so it's fast and dependency-light.

  from core.kenburns import stills_to_clip
  mp4 = stills_to_clip(
      ["a.png", "b.png", "c.png"],   # ordered stills (from the image engine)
      "vo.wav",                       # narration / music bed (or None for silent)
      "/tmp/broll.mp4",
      fps=30,
  )

Motion: slow push-in  z='min(zoom+0.0012,1.12)'  over each still, LANCZOS
upscaled first so the zoom stays smooth (no integer-pixel jitter). Consecutive
stills are joined with a soft xfade so a photo sequence dissolves like footage.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"


def _probe_duration(path: str) -> float:
    try:
        out = subprocess.run(
            [FFPROBE, "-v", "error", "-show_entries", "format=duration",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=30)
        return float(json.loads(out.stdout)["format"]["duration"])
    except Exception:
        return 0.0


def _seg_filter(w: int, h: int, fps: int, total_frames: int,
                zoom_step: float, max_zoom: float, push_in: bool) -> str:
    """Single-still zoompan segment. Upscale ~2x first for smooth sub-pixel zoom."""
    sw, sh = w * 2, h * 2
    if push_in:
        z = f"min(zoom+{zoom_step},{max_zoom})"
    else:  # start zoomed, pull out
        z = f"if(eq(on,0),{max_zoom},max(zoom-{zoom_step},1.0))"
    return (
        f"scale={sw}:{sh}:force_original_aspect_ratio=increase,"
        f"crop={sw}:{sh},"
        f"zoompan=z='{z}':d={total_frames}:"
        f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
        f"fps={fps}:s={w}x{h},"
        f"format=yuv420p,setsar=1"
    )


def _render_segment(image: str, out: str, dur: float, w: int, h: int, fps: int,
                    zoom_step: float, max_zoom: float, push_in: bool) -> bool:
    total = max(2, int(round(dur * fps)))
    vf = _seg_filter(w, h, fps, total, zoom_step, max_zoom, push_in)
    cmd = [FFMPEG, "-y", "-loop", "1", "-i", str(image),
           "-vf", vf, "-frames:v", str(total),
           "-c:v", "libx264", "-preset", "medium", "-pix_fmt", "yuv420p",
           "-r", str(fps), str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if r.returncode != 0 or not Path(out).exists():
        print(f"[kenburns] segment render failed: {r.stderr[-400:]}")
        return False
    return True


def stills_to_clip(
    images: list[str],
    audio_wav: str | None,
    out: str,
    fps: int = 30,
    *,
    width: int = 1080,
    height: int = 1920,
    per_still: float = 4.0,
    xfade: float = 0.5,
    zoom_step: float = 0.0012,
    max_zoom: float = 1.12,
    match_audio: bool = True,
) -> str | None:
    """Render ordered STILLS into a ken-burns + crossfade clip, muxing audio.

    images      : ordered image paths (>=1). Each gets a slow push-in.
    audio_wav   : narration / music bed path, or None for a silent clip.
    out         : output .mp4 path.
    per_still   : seconds each still holds when there's no audio to match.
    xfade       : crossfade seconds between consecutive stills.
    match_audio : if audio given, stretch still durations so total == audio len.

    Returns the mp4 path on success, else None.
    """
    imgs = [str(i) for i in images if i and Path(i).exists()]
    if not imgs:
        print("[kenburns] no valid input images")
        return None
    n = len(imgs)
    out = str(out)
    Path(out).parent.mkdir(parents=True, exist_ok=True)

    audio_dur = _probe_duration(audio_wav) if (audio_wav and Path(audio_wav).exists()) else 0.0
    xf = xfade if n > 1 else 0.0

    # per-segment duration; when matching audio, solve seg so
    # total = n*seg - (n-1)*xf == audio_dur
    if audio_dur > 0 and match_audio:
        seg = (audio_dur + (n - 1) * xf) / n
    else:
        seg = per_still
    seg = max(seg, xf + 0.8)  # never shorter than a transition + breath
    video_dur = round(n * seg - (n - 1) * xf, 3)

    with tempfile.TemporaryDirectory() as td:
        segs: list[str] = []
        for i, img in enumerate(imgs):
            sp = str(Path(td) / f"seg_{i:03d}.mp4")
            if not _render_segment(img, sp, seg, width, height, fps,
                                   zoom_step, max_zoom, push_in=(i % 2 == 0)):
                return None
            segs.append(sp)

        # single still: just add audio (no xfade needed)
        if n == 1:
            return _mux(segs[0], audio_wav, out, video_dur, fps)

        # chain xfade transitions across all segments
        inputs: list[str] = []
        for s in segs:
            inputs += ["-i", s]
        # cumulative xfade offset: the i-th transition starts once i stills have
        # played minus the overlap already consumed → i*seg - i*xf.
        fc_parts: list[str] = []
        prev = "0:v"
        for i in range(1, n):
            off = round(i * seg - i * xf, 3)
            label = f"vx{i}"
            fc_parts.append(
                f"[{prev}][{i}:v]xfade=transition=fade:duration={xf}:offset={off}[{label}]"
            )
            prev = label
        filter_complex = ";".join(fc_parts)
        vlabel = prev

        video_only = str(Path(td) / "video.mp4")
        cmd = [FFMPEG, "-y", *inputs,
               "-filter_complex", filter_complex,
               "-map", f"[{vlabel}]",
               "-c:v", "libx264", "-preset", "medium", "-pix_fmt", "yuv420p",
               "-r", str(fps), video_only]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1200)
        if r.returncode != 0 or not Path(video_only).exists():
            print(f"[kenburns] xfade chain failed: {r.stderr[-600:]}")
            return None

        return _mux(video_only, audio_wav, out, video_dur, fps)


def _mux(video: str, audio_wav: str | None, out: str, video_dur: float, fps: int) -> str | None:
    if audio_wav and Path(audio_wav).exists():
        cmd = [FFMPEG, "-y", "-i", str(video), "-i", str(audio_wav),
               "-map", "0:v", "-map", "1:a",
               "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
               "-t", str(video_dur), str(out)]
    else:
        cmd = [FFMPEG, "-y", "-i", str(video),
               "-map", "0:v", "-c:v", "copy", "-t", str(video_dur), str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if r.returncode != 0 or not Path(out).exists():
        print(f"[kenburns] mux failed: {r.stderr[-400:]}")
        return None
    return out


def overlay_captions_music(
    base_video: str,
    captions: list[dict] | None,
    music: str | None,
    out: str,
    *,
    fps: int = 30,
    width: int = 1080,
    height: int = 1920,
    music_volume: float = 0.6,
) -> str | None:
    """Burn timed caption PNGs onto a silent base clip and lay a music bed under it.

    The AUTHENTIC path: real-photo ken-burns motion comes in as `base_video`, kinetic
    text is pre-rendered as transparent 1080x1920 PNGs, and a free/commercial-safe
    music track (looped + faded) rides underneath. No voice-over, no drawtext (this
    ffmpeg build has none) — captions are overlay filters with alpha fades so the text
    reads as animated. Fail-soft: returns None on any failure.

    captions : [{"png": abs_path, "start": s, "end": s}, ...] (fades baked per clip)
    music    : commercial-safe audio path, or None for a clean silent cut.
    """
    base_video, out = str(base_video), str(out)
    if not Path(base_video).exists():
        return None
    dur = _probe_duration(base_video)
    if dur <= 0:
        return None
    caps = [c for c in (captions or []) if c.get("png") and Path(c["png"]).exists()]
    Path(out).parent.mkdir(parents=True, exist_ok=True)

    cmd = [FFMPEG, "-y", "-i", base_video]
    for c in caps:
        cmd += ["-loop", "1", "-i", str(c["png"])]
    music_idx = None
    if music and Path(music).exists():
        music_idx = 1 + len(caps)
        cmd += ["-stream_loop", "-1", "-i", str(music)]

    fc: list[str] = []
    fade = 0.4
    prev = "0:v"
    for i, c in enumerate(caps):
        k = i + 1
        st = max(0.0, float(c["start"]))
        en = min(dur, float(c["end"]))
        out_st = max(st, en - fade)
        fc.append(
            f"[{k}:v]format=rgba,"
            f"fade=t=in:st={st:.2f}:d={fade}:alpha=1,"
            f"fade=t=out:st={out_st:.2f}:d={fade}:alpha=1[cap{k}]"
        )
        lbl = f"ov{k}"
        fc.append(
            f"[{prev}][cap{k}]overlay=x=(W-w)/2:y=0:"
            f"enable='between(t,{st:.2f},{en:.2f})'[{lbl}]"
        )
        prev = lbl
    vlabel = prev

    map_args = ["-map", f"[{vlabel}]"] if fc else ["-map", "0:v"]
    if music_idx is not None:
        a_out = max(0.0, dur - 1.2)
        fc.append(
            f"[{music_idx}:a]volume={music_volume},"
            f"afade=t=out:st={a_out:.2f}:d=1.2[aud]"
        )
        map_args += ["-map", "[aud]"]

    if fc:
        cmd += ["-filter_complex", ";".join(fc)]
    cmd += map_args
    cmd += ["-c:v", "libx264", "-preset", "medium", "-pix_fmt", "yuv420p", "-r", str(fps)]
    if music_idx is not None:
        cmd += ["-c:a", "aac", "-b:a", "192k"]
    cmd += ["-t", f"{dur:.3f}", out]

    r = subprocess.run(cmd, capture_output=True, text=True, timeout=1200)
    if r.returncode != 0 or not Path(out).exists():
        print(f"[kenburns] caption/music overlay failed: {r.stderr[-600:]}")
        return None
    return out


def configured() -> bool:
    import shutil
    return bool(shutil.which(FFMPEG) and shutil.which(FFPROBE))


if __name__ == "__main__":
    import sys
    print("kenburns configured (ffmpeg+ffprobe):", configured())
    if len(sys.argv) > 2:
        # kenburns.py out.mp4 img1 img2 ... [--audio path]
        args = sys.argv[1:]
        audio = None
        if "--audio" in args:
            ix = args.index("--audio")
            audio = args[ix + 1]
            args = args[:ix] + args[ix + 2:]
        outp, imgs = args[0], args[1:]
        print(stills_to_clip(imgs, audio, outp))
