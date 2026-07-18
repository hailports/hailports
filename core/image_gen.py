#!/usr/bin/env python3
from __future__ import annotations

"""AI image generation with local ComfyUI first and remote fallback."""

import os
import logging
import time
import urllib.error
import urllib.request
import urllib.parse
from datetime import datetime
from pathlib import Path

try:
    from core.comfy_diffusion import ComfyError, render_realistic_image
except Exception:  # pragma: no cover - import fallback for standalone use
    ComfyError = RuntimeError
    render_realistic_image = None

BASE_DIR = Path.home() / "claude-stack"
OUTPUT_DIR = BASE_DIR / "data" / "hustle" / "generated_images"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

log = logging.getLogger(__name__)

# Ordered by remote fallback quality.
MODELS = ["flux", "flux-realism", "flux-anime", "turbo"]

POLLINATIONS_BASE = "https://image.pollinations.ai/prompt"

LOCAL_MODEL_ALIASES = {"flux", "flux-realism", "sdxl", "realistic", "local", "comfy", "epicrealism"}
LOCAL_ONLY_MODELS = {"sdxl", "realistic", "local", "comfy", "epicrealism"}


def _truthy_env(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip().lower() not in {"0", "false", "no", "off"}


def _try_local_realistic(
    prompt: str,
    model: str,
    width: int,
    height: int,
    seed,
    timeout: int,
    *,
    strict: bool,
    negative: str | None = None,
    checkpoint: str | None = None,
) -> Path | None:
    if render_realistic_image is None:
        return None
    if not _truthy_env("LOCAL_COMFY_IMAGE", "1"):
        return None
    if str(model or "").strip().lower() not in LOCAL_MODEL_ALIASES:
        return None
    try:
        return render_realistic_image(
            prompt,
            output_dir=OUTPUT_DIR,
            width=width,
            height=height,
            seed=seed,
            negative=negative,
            timeout=max(timeout, 300),
            checkpoint=checkpoint,
        )
    except Exception as exc:
        if strict:
            raise
        log.warning("local Comfy image failed, falling back to remote image model: %s", exc)
        return None


def generate_image(
    prompt: str,
    model: str = "flux",
    width: int = 1080,
    height: int = 1920,
    seed=None,
    negative: str | None = None,
    timeout: int = 180,
    checkpoint: str | None = None,
) -> Path:
    """Generate an image from a text prompt. Returns saved file path or None."""
    model_key = str(model or "").strip().lower()
    local = _try_local_realistic(
        prompt,
        model,
        width,
        height,
        seed,
        timeout,
        strict=model_key in LOCAL_ONLY_MODELS,
        negative=negative,
        checkpoint=checkpoint,
    )
    if local:
        return local

    # fal.ai (cheap flux) — preferred remote backend now that Pollinations is paid (402).
    try:
        from core import fal_image
        if fal_image.configured():
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            out_path = OUTPUT_DIR / f"{ts}_fal_{urllib.parse.quote(prompt[:30], safe='')}.jpg"
            size = "portrait_16_9" if height >= width else "landscape_16_9"
            fal_model = "dev" if model_key in ("flux-realism", "realistic", "dev", "epicrealism", "sdxl") else "schnell"
            p = fal_image.generate(prompt, out=str(out_path), model=fal_model, image_size=size, seed=seed)
            if p:
                log.info("fal image generated -> %s", p)
                return Path(p)
    except Exception as e:
        log.warning("fal image backend failed: %s", e)

    models_to_try = [model] + [m for m in MODELS if m != model]

    for m in models_to_try:
        try:
            encoded = urllib.parse.quote(prompt)
            params = f"?width={width}&height={height}&model={m}&nologo=true&enhance=true"
            if seed is not None:
                params += f"&seed={seed}"
            url = f"{POLLINATIONS_BASE}/{encoded}{params}"

            log.info("generating image model=%s prompt=%.60s...", m, prompt)
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout) as response:
                content = response.read()
                status_code = response.status
                ct = response.headers.get("content-type", "")

            if status_code != 200:
                log.warning("model %s returned %d, trying next", m, status_code)
                continue

            if "image" not in ct:
                log.warning("model %s returned non-image content-type: %s", m, ct)
                continue

            if len(content) < 1000:
                log.warning("model %s returned tiny response (%d bytes)", m, len(content))
                continue

            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            ext = "jpg" if "jpeg" in ct else "png"
            safe_prompt = urllib.parse.quote(prompt[:40], safe="")
            filename = f"{ts}_{m}_{safe_prompt}.{ext}"
            out_path = OUTPUT_DIR / filename

            out_path.write_bytes(content)
            log.info("saved %s (%d KB)", out_path.name, len(content) // 1024)
            return out_path

        except (TimeoutError, urllib.error.URLError):
            log.warning("model %s timed out", m)
            continue
        except Exception as e:
            log.warning("model %s failed: %s", m, e)
            continue

    log.error("all models failed for prompt: %.80s", prompt)
    return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    import sys
    prompt = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "aesthetic productivity planner flatlay"
    path = generate_image(prompt)
    if path:
        print(f"OK {path}")
    else:
        print("FAIL")
        sys.exit(1)
