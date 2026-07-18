#!/usr/bin/env python3
"""
Talking-avatar / lip-sync engine — local, $0, load-on-demand.

Animates a still face (photo or cartoon) in sync with an audio track, so the
"cartoons with nice voice dubbing" content can ship without any cloud avatar API.
Pairs with core.ima_voice.speak(): synth a wav there, feed it in here.

Engine: Wav2Lip (Rudrabha), staged on External at WAV2LIP_DIR. Runs CPU-first on
Apple Silicon (the conv stack is rock-solid on CPU; MPS is opt-in via
WAV2LIP_DEVICE=mps with a full fallback). No resident service — each call shells
the tool once and exits.

Usage:
    from core.talking_avatar import lipsync
    out = lipsync(Path("face.jpg"), Path("voice.wav"), Path("clip.mp4"))
    if out:  # Path on success, None on any failure
        ...
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger("talking-avatar")

FFMPEG = "/opt/homebrew/bin/ffmpeg"
FFPROBE = "/opt/homebrew/bin/ffprobe"

# Wav2Lip lives on External (disk-tight main volume); its own py3.12 venv carries
# the old ML stack (librosa/numba/torch) that won't build on the stack's py3.14.
WAV2LIP_DIR = Path(os.environ.get("WAV2LIP_DIR", "/Volumes/External/tools/Wav2Lip"))
WAV2LIP_PY = WAV2LIP_DIR / ".venv312" / "bin" / "python"
WAV2LIP_CKPT = WAV2LIP_DIR / "checkpoints" / "wav2lip_gan.pth"
WAV2LIP_S3FD = WAV2LIP_DIR / "face_detection" / "detection" / "sfd" / "s3fd.pth"

# cpu = most robust on Apple Silicon; mps = opt-in, fallback covers unsupported ops
DEVICE = os.environ.get("WAV2LIP_DEVICE", "cpu").strip().lower() or "cpu"

_ENV = dict(os.environ)
_ENV["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:" + _ENV.get("PATH", "")
_ENV["PYTORCH_ENABLE_MPS_FALLBACK"] = "1"  # MPS lacks some conv ops; fall back to CPU
_ENV.setdefault("HF_HOME", "/Volumes/External/models/hf")


def available() -> bool:
    """True if the engine + checkpoints are staged and runnable."""
    return (
        WAV2LIP_PY.exists()
        and WAV2LIP_CKPT.exists()
        and WAV2LIP_S3FD.exists()
        and shutil.which(FFMPEG) is not None
    )


def _to_wav16k(src: Path, dst: Path) -> bool:
    """Normalize any audio to 16kHz mono WAV (Wav2Lip's expected input)."""
    try:
        r = subprocess.run(
            [FFMPEG, "-y", "-i", str(src), "-ar", "16000", "-ac", "1",
             "-c:a", "pcm_s16le", str(dst)],
            capture_output=True, timeout=60, env=_ENV,
        )
        return r.returncode == 0 and dst.exists() and dst.stat().st_size > 0
    except Exception as e:
        log.warning("audio normalize failed: %s", e)
        return False


def _has_video_and_audio(mp4: Path) -> bool:
    """Confirm the output actually carries both a video and an audio stream."""
    try:
        r = subprocess.run(
            [FFPROBE, "-v", "error", "-show_entries", "stream=codec_type",
             "-of", "default=nw=1:nk=1", str(mp4)],
            capture_output=True, timeout=20, env=_ENV,
        )
        kinds = r.stdout.decode().split()
        return "video" in kinds and "audio" in kinds
    except Exception:
        return False


def lipsync(
    source_image: Path,
    driven_audio_wav: Path,
    out_mp4: Path,
    *,
    device: str | None = None,
    pads: tuple[int, int, int, int] = (0, 10, 0, 0),
    resize_factor: int = 1,
    wav2lip_batch_size: int = 16,
    face_det_batch_size: int = 4,
    nosmooth: bool = True,
    timeout: int = 900,
) -> Path | None:
    """
    Lip-sync a still face image to an audio track → mp4 (video + audio).

    Args:
        source_image: still face/character (jpg/png). A detectable face is required;
            heavily stylized cartoons may not be detected → returns None.
        driven_audio_wav: audio track (wav preferred; other formats auto-converted).
        out_mp4: destination mp4 path.
        pads: (top, bottom, left, right) chin/face padding; bump `bottom` if the
            jaw gets clipped.
        resize_factor: downscale large frames (>1) to speed up / dodge OOM.
        nosmooth: skip inter-frame face-box smoothing (better for a single still).

    Returns:
        out_mp4 Path on success, else None (fails gracefully — never raises).
    """
    source_image = Path(source_image)
    driven_audio_wav = Path(driven_audio_wav)
    out_mp4 = Path(out_mp4)

    if not available():
        log.warning(
            "Wav2Lip not staged (py=%s ckpt=%s s3fd=%s). "
            "Stage it under %s before calling lipsync().",
            WAV2LIP_PY.exists(), WAV2LIP_CKPT.exists(), WAV2LIP_S3FD.exists(), WAV2LIP_DIR,
        )
        return None
    if not source_image.exists():
        log.warning("source_image missing: %s", source_image)
        return None
    if not driven_audio_wav.exists():
        log.warning("driven_audio_wav missing: %s", driven_audio_wav)
        return None

    out_mp4.parent.mkdir(parents=True, exist_ok=True)

    # Wav2Lip's audio path is happiest with 16k mono wav; normalize anything else.
    audio_in = driven_audio_wav
    tmp_wav: Path | None = None
    if driven_audio_wav.suffix.lower() != ".wav":
        tmp_wav = out_mp4.with_name(out_mp4.stem + "_ta16k.wav")
        if not _to_wav16k(driven_audio_wav, tmp_wav):
            return None
        audio_in = tmp_wav

    env = dict(_ENV)
    env["WAV2LIP_DEVICE"] = (device or DEVICE)

    cmd = [
        str(WAV2LIP_PY), "inference.py",
        "--checkpoint_path", str(WAV2LIP_CKPT),
        "--face", str(source_image.resolve()),
        "--audio", str(audio_in.resolve()),
        "--outfile", str(out_mp4.resolve()),
        "--pads", *map(str, pads),
        "--resize_factor", str(resize_factor),
        "--wav2lip_batch_size", str(wav2lip_batch_size),
        "--face_det_batch_size", str(face_det_batch_size),
    ]
    if nosmooth:
        cmd.append("--nosmooth")

    try:
        r = subprocess.run(
            cmd, cwd=str(WAV2LIP_DIR), capture_output=True, timeout=timeout, env=env,
        )
    except subprocess.TimeoutExpired:
        log.warning("lipsync timed out after %ss", timeout)
        return None
    except Exception as e:
        log.warning("lipsync launch error: %s", e)
        return None
    finally:
        if tmp_wav is not None:
            tmp_wav.unlink(missing_ok=True)

    if r.returncode != 0:
        tail = r.stderr.decode(errors="replace")[-500:]
        if "Face not detected" in tail or "Face not detected" in r.stdout.decode(errors="replace"):
            log.warning("no face detected in %s (try a clearer/less stylized face)", source_image)
        else:
            log.warning("Wav2Lip failed (rc=%d): %s", r.returncode, tail)
        return None

    if not out_mp4.exists() or out_mp4.stat().st_size == 0:
        log.warning("Wav2Lip returned 0 but produced no output: %s", out_mp4)
        return None
    if not _has_video_and_audio(out_mp4):
        log.warning("output missing a video or audio stream: %s", out_mp4)
        return None

    log.info("lip-synced clip: %s", out_mp4)
    return out_mp4


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="Local lip-sync: still face + audio -> mp4")
    ap.add_argument("--check", action="store_true", help="report engine availability and exit")
    ap.add_argument("--face")
    ap.add_argument("--audio")
    ap.add_argument("--out")
    ap.add_argument("--device", default=None)
    a = ap.parse_args()

    if a.check or not (a.face and a.audio and a.out):
        print(f"available: {available()}")
        print(f"  python:  {WAV2LIP_PY} ({WAV2LIP_PY.exists()})")
        print(f"  ckpt:    {WAV2LIP_CKPT} ({WAV2LIP_CKPT.exists()})")
        print(f"  s3fd:    {WAV2LIP_S3FD} ({WAV2LIP_S3FD.exists()})")
        print(f"  device:  {a.device or DEVICE}")
        raise SystemExit(0)

    res = lipsync(Path(a.face), Path(a.audio), Path(a.out), device=a.device)
    print(res if res else "FAILED")
    raise SystemExit(0 if res else 1)
