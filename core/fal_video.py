#!/usr/bin/env python3
"""fal.ai video backend — cheap image->video for the persona1 reels engine.

Cost trick: animate a flux STILL (consistent persona1 face) rather than text->video. Far cheaper
than text->video AND preserves the persona's face. Uses cheap open i2v models (LTX / Wan)
by default; same FAL_KEY as core/fal_image.

  from core.fal_video import image_to_video
  mp4 = image_to_video(image_url=ima_still_url, prompt="she smiles and the breeze moves her hair",
                       out="/tmp/clip.mp4")
"""
from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path

# cheapest-first i2v endpoints (override via FAL_VIDEO_MODEL)
_I2V = {
    "ltx": "https://fal.run/fal-ai/ltx-video/image-to-video",      # cheapest, fast
    "wan": "https://fal.run/fal-ai/wan-i2v",                        # cheap, good motion
    "kling": "https://fal.run/fal-ai/kling-video/v1/standard/image-to-video",  # pricier, best
}
_T2V = {
    "ltx": "https://fal.run/fal-ai/ltx-video",
    "wan": "https://fal.run/fal-ai/wan-t2v",
}


def _key() -> str:
    return (os.environ.get("FAL_KEY") or os.environ.get("FAL_API_KEY") or "").strip()


def configured() -> bool:
    return bool(_key())


def _post(url: str, payload: dict, timeout: int = 300) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Authorization": f"Key {_key()}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _save_video(res: dict, out: str) -> str | None:
    v = res.get("video") or {}
    url = v.get("url") if isinstance(v, dict) else (res.get("video_url") or "")
    if not url and isinstance(res.get("videos"), list) and res["videos"]:
        url = res["videos"][0].get("url", "")
    if not url:
        return None
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=120) as r:
        Path(out).write_bytes(r.read())
    return out


def image_to_video(image_url: str, prompt: str, out: str, model: str = None) -> str | None:
    """Animate a still (the consistent persona1 image) into a short clip. Returns mp4 path or None."""
    if not configured():
        return None
    model = (model or os.environ.get("FAL_VIDEO_MODEL", "ltx")).lower()
    try:
        res = _post(_I2V.get(model, _I2V["ltx"]),
                    {"image_url": image_url, "prompt": prompt})
        return _save_video(res, out)
    except Exception as e:
        print(f"[fal-video] i2v failed ({type(e).__name__}): {str(e)[:140]}")
    return None


def text_to_video(prompt: str, out: str, model: str = "ltx") -> str | None:
    if not configured():
        return None
    try:
        res = _post(_T2V.get(model, _T2V["ltx"]), {"prompt": prompt})
        return _save_video(res, out)
    except Exception as e:
        print(f"[fal-video] t2v failed ({type(e).__name__}): {str(e)[:140]}")
    return None


if __name__ == "__main__":
    print("fal video configured:", configured(), "| set FAL_KEY in .env to use")
