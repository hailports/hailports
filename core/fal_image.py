#!/usr/bin/env python3
"""fal.ai image backend — cheap, fast flux for the AI-persona (persona1) socials engine.

Why fal: flux/schnell ~ $0.003/img (fast volume), flux/dev better quality, and
flux-pulid gives FACE CONSISTENCY from a single reference image — the thing that makes
a persona look like the same person across every post (the #1 reason AI personas fail).

Key: set FAL_KEY (or FAL_API_KEY) in .env. Get one at fal.ai/dashboard/keys.

  from core.fal_image import generate, generate_consistent
  path = generate("a candid beach lifestyle photo of a woman, golden hour", out="/tmp/x.jpg")
  path = generate_consistent(prompt, ref_image_url=IMA_FACE_URL, out=...)  # same-face persona1
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from pathlib import Path

_ENDPOINTS = {
    "schnell": "https://fal.run/fal-ai/flux/schnell",       # cheapest/fastest
    "dev": "https://fal.run/fal-ai/flux/dev",               # better quality
    "pulid": "https://fal.run/fal-ai/flux-pulid",           # face-consistent (ref image)
}


def _key() -> str:
    return (os.environ.get("FAL_KEY") or os.environ.get("FAL_API_KEY") or "").strip()


def configured() -> bool:
    return bool(_key())


def _post(url: str, payload: dict, timeout: int = 120) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Authorization": f"Key {_key()}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _download(url: str, out: str) -> str:
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=60) as r:
        Path(out).write_bytes(r.read())
    return out


def generate(prompt: str, out: str, model: str = "schnell",
             image_size: str = "portrait_16_9", seed: int | None = None,
             safety: bool = True, return_url: bool = False):
    """Generate one image. Returns the saved path, or None on failure. Never raises.
    With return_url=True, returns (path, hosted_url) so callers can chain the SAME
    image into image-to-video without paying for (and risking a different/black) re-gen.
    safety=False disables fal's safety checker (it over-blocks legit swimwear/lifestyle —
    returns black frames); use only for intentional, legal lifestyle content."""
    if not configured():
        return (None, None) if return_url else None
    payload = {"prompt": prompt, "image_size": image_size, "num_images": 1,
               "enable_safety_checker": bool(safety)}
    if seed is not None:
        payload["seed"] = seed
    try:
        res = _post(_ENDPOINTS.get(model, _ENDPOINTS["schnell"]), payload)
        imgs = res.get("images") or []
        if imgs and imgs[0].get("url"):
            url = imgs[0]["url"]
            path = _download(url, out)
            return (path, url) if return_url else path
    except Exception as e:
        print(f"[fal] generate failed ({type(e).__name__}): {str(e)[:120]}")
    return (None, None) if return_url else None


def generate_consistent(prompt: str, ref_image_url: str, out: str,
                        seed: int | None = None) -> str | None:
    """Same-face persona image via flux-pulid + a reference face URL. The persona1-consistency path."""
    if not configured():
        return None
    payload = {"prompt": prompt, "reference_image_url": ref_image_url,
               "image_size": "portrait_16_9", "num_images": 1, "enable_safety_checker": True}
    if seed is not None:
        payload["seed"] = seed
    try:
        res = _post(_ENDPOINTS["pulid"], payload, timeout=180)
        imgs = res.get("images") or []
        if imgs and imgs[0].get("url"):
            return _download(imgs[0]["url"], out)
    except Exception as e:
        print(f"[fal] consistent generate failed ({type(e).__name__}): {str(e)[:120]}")
    return None


if __name__ == "__main__":
    import sys
    if not configured():
        print("FAL_KEY not set — get one at fal.ai/dashboard/keys, add to .env")
        raise SystemExit(1)
    p = generate(sys.argv[1] if len(sys.argv) > 1 else "a candid beach lifestyle photo of a woman, golden hour, photorealistic",
                 out="/tmp/fal_test.jpg")
    print("saved:", p)
