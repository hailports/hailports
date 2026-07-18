#!/usr/bin/env python3
from __future__ import annotations

"""Free IMAGE generation pool — ordered round-robin across $0 backends.

Mirrors core.free_llm_pool for images. Ordered chain, each backend fails to the
next and NEVER raises:

    Cloudflare flux-1-schnell  (free, best photoreal, ~10k neurons/day)
      -> Gemini 2.5-flash-image (nano-banana; best text-in-image / brand graphics)
      -> Pollinations           (free, KEYLESS — no account, unlimited-ish)
      -> local mflux            (FLUX.1-schnell 4-bit on MLX; unlimited OFFLINE)
      -> fal.ai                 (PAID — only if IMAGE_GEN_ALLOW_REMOTE_PAID)

The CF + Gemini implementations are the canonical ones in core.image_gen (imported
lazily to avoid a circular import). A per-backend simple daily counter spreads load
so no single free quota gets hammered.

    from core.free_image_pool import generate
    path = generate("photoreal beach lifestyle, golden hour")   # Path or None
"""

import json
import logging
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

BASE_DIR = Path.home() / "claude-stack"
OUTPUT_DIR = BASE_DIR / "data" / "hustle" / "generated_images"
_COUNTS_PATH = BASE_DIR / "data" / "hustle" / "free_image_pool_counts.json"

log = logging.getLogger(__name__)

# Graphic/brand/text work reads better on Gemini (nano-banana) — flip it ahead of
# CF flux for those prompt classes. Matches image_gen.GEMINI_MODEL_KEYS.
GEMINI_FIRST_KEYS = {"graphic", "logo", "text", "brand", "edit", "banner", "poster"}


def _truthy_env(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() not in {"0", "false", "no", "off"}


# ---- per-backend daily counter (best-effort, never fatal) ------------------
def _bump_count(name: str) -> None:
    try:
        today = datetime.now().strftime("%Y%m%d")
        data = {}
        if _COUNTS_PATH.exists():
            data = json.loads(_COUNTS_PATH.read_text() or "{}")
        if data.get("_date") != today:
            data = {"_date": today}
        data[name] = int(data.get(name, 0)) + 1
        _COUNTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        _COUNTS_PATH.write_text(json.dumps(data))
    except Exception:
        pass


def daily_counts() -> dict:
    try:
        if _COUNTS_PATH.exists():
            return json.loads(_COUNTS_PATH.read_text() or "{}")
    except Exception:
        pass
    return {}


# ---- backends --------------------------------------------------------------
def _cf_flux(prompt: str, timeout: int, **_) -> Path | None:
    from core.image_gen import _try_cloudflare_flux
    return _try_cloudflare_flux(prompt, timeout)


def _gemini(prompt: str, timeout: int, **_) -> Path | None:
    from core.image_gen import _try_gemini_image
    return _try_gemini_image(prompt, timeout)


def _pollinations(prompt: str, timeout: int, *, width: int | None = None,
                  height: int | None = None, seed=None, **_) -> Path | None:
    """Pollinations keyless free endpoint. Still $0 with no account/key via the
    GET image URL (the removed path was the newer authed API). Never raises."""
    from core.image_gen import _save_image_bytes
    try:
        q = urllib.parse.quote(prompt[:900], safe="")
        params = {"nologo": "true", "model": "flux"}
        if width:
            params["width"] = int(width)
        if height:
            params["height"] = int(height)
        if seed is not None:
            params["seed"] = int(seed)
        url = f"https://image.pollinations.ai/prompt/{q}?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            ctype = r.headers.get("Content-Type", "")
            data = r.read()
        if "image" not in ctype and not data[:3] == b"\xff\xd8\xff":
            log.warning("pollinations returned non-image (%s)", ctype)
            return None
        ext = "png" if "png" in ctype else "jpg"
        return _save_image_bytes(data, "pollinations", prompt, ext=ext)
    except Exception as e:
        log.warning("pollinations backend failed: %s", e)
        return None


def _mflux(prompt: str, timeout: int, *, width: int | None = None,
           height: int | None = None, seed=None, **_) -> Path | None:
    """Local FLUX.1-schnell 4-bit via MLX (mflux). Unlimited OFFLINE photoreal.
    LOAD-ON-DEMAND: builds the model, renders, then drops the reference so RAM is
    freed (24GB box). No resident server. Never raises. None if mflux not installed."""
    try:
        import mflux  # noqa: F401
    except Exception:
        return None
    out = _out_path("mflux", prompt, "png")
    try:
        if _mflux_gen(prompt, str(out), width=width, height=height, seed=seed):
            return _validate(out)
        return None
    except Exception as e:
        log.warning("mflux backend failed: %s", e)
        return None


def _load_flux1():
    """Return the txt2img Flux1 class across mflux layouts (0.18 moved it under
    models.flux.variants.txt2img; older builds exported it at top level)."""
    try:
        from mflux.models.flux.variants.txt2img.flux import Flux1  # mflux >=0.16 layout
        return Flux1
    except Exception:
        from mflux import Flux1  # legacy top-level export
        return Flux1


def _mflux_gen(prompt: str, out: str, *, width: int | None = None,
               height: int | None = None, seed=None) -> bool:
    """Render one FLUX.1-schnell 4-bit image to `out`. Returns True on success.
    Model weights land under HF_HOME (External). Reference dropped + GC'd after."""
    Flux1 = _load_flux1()
    w = int(width or 1024)
    h = int(height or 1024)
    # FLUX latents want multiples of 16; clamp to keep the box light.
    w = max(256, min(1024, (w // 16) * 16))
    h = max(256, min(1024, (h // 16) * 16))
    # The canonical BFL "schnell" repo is now HF-gated (401 without a licensed
    # token). Default to an UNGATED pre-quantized 4-bit mirror so it runs $0 with
    # no HF login; override with MFLUX_MODEL (+ MFLUX_BASE). A repo path ("org/name")
    # is treated as already-quantized (quantize=None); the bare "schnell" alias
    # re-quantizes to 4-bit on the fly (needs a licensed HF token).
    model_name = os.environ.get("MFLUX_MODEL", "madroid/flux.1-schnell-mflux-4bit").strip()
    flux = None
    try:
        if "/" in model_name:  # pre-quantized mirror; base inferred from "schnell" in the name
            flux = Flux1.from_name(model_name=model_name)
        else:
            flux = Flux1.from_name(model_name=model_name, quantize=4)
        image = flux.generate_image(
            seed=int(seed) if seed is not None else int(time.time()) % 100000,
            prompt=prompt,
            num_inference_steps=4,
            width=w,
            height=h,
        )
        image.save(path=out)
        return Path(out).exists() and Path(out).stat().st_size > 10_000
    finally:
        # drop the ~7GB model + reclaim MLX buffers on the tight 24GB box
        del flux
        try:
            import gc
            gc.collect()
            import mlx.core as mx  # type: ignore
            mx.clear_cache()
        except Exception:
            pass


def _fal(prompt: str, timeout: int, *, width: int | None = None,
         height: int | None = None, seed=None, **_) -> Path | None:
    """fal.ai PAID — true last resort, gated by IMAGE_GEN_ALLOW_REMOTE_PAID."""
    if not _truthy_env("IMAGE_GEN_ALLOW_REMOTE_PAID", "0"):
        return None
    try:
        from core import fal_image
        if not fal_image.configured():
            return None
        out = _out_path("fal", prompt, "jpg")
        size = "portrait_16_9" if (height or 0) >= (width or 1) else "landscape_16_9"
        log.warning("free_image_pool: falling to PAID fal.ai (all free backends failed)")
        p = fal_image.generate(prompt, out=str(out), model="schnell", image_size=size, seed=seed)
        return Path(p) if p else None
    except Exception as e:
        log.warning("fal backend failed: %s", e)
        return None


# ---- helpers ---------------------------------------------------------------
def _out_path(tag: str, prompt: str, ext: str) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe = urllib.parse.quote(prompt[:40], safe="")
    return OUTPUT_DIR / f"{ts}_{tag}_{safe}.{ext}"


def _validate(path: Path) -> Path | None:
    """Real image, not a stub. Bytes first, then Pillow if present."""
    try:
        if not path.exists() or path.stat().st_size < 10_000:
            return None
        try:
            from PIL import Image
            with Image.open(path) as im:
                im.verify()
        except ImportError:
            pass
    except Exception:
        return None
    return path


# ---- ordered pool ----------------------------------------------------------
_FREE_ORDER = [
    ("cf_flux", _cf_flux),
    ("gemini", _gemini),
    ("pollinations", _pollinations),
    ("mflux", _mflux),
]


def generate(prompt: str, *, model: str = "flux", timeout: int = 180,
             width: int | None = None, height: int | None = None,
             seed=None) -> Path | None:
    """Round-robin the free image backends in order; return the first real image
    (Path) or None. fal.ai (paid) is appended only if IMAGE_GEN_ALLOW_REMOTE_PAID.
    Never raises."""
    model_key = str(model or "").strip().lower()
    order = list(_FREE_ORDER)
    if model_key in GEMINI_FIRST_KEYS:
        order.sort(key=lambda kv: kv[0] != "gemini")  # gemini to the front
    order.append(("fal", _fal))

    for name, backend in order:
        try:
            got = backend(prompt, timeout, width=width, height=height, seed=seed)
        except Exception as e:  # backends never raise, but belt-and-suspenders
            log.warning("free_image_pool: %s raised: %s", name, e)
            got = None
        if got:
            _bump_count(name)
            if name != "fal":  # fal is the paid backend — no savings when we pay
                try:
                    from core.llm_router import log_media_savings
                    log_media_savings("image", 1, source="free_image_pool", engine=name)
                except Exception:
                    pass
            log.info("free_image_pool: %s produced %s", name, Path(got).name)
            return Path(got)
    log.error("free_image_pool: all backends failed for: %.80s", prompt)
    return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    import sys
    p = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "photorealistic candid beach lifestyle photo, golden hour"
    out = generate(p)
    print(f"OK {out}" if out else "FAIL")
    if not out:
        sys.exit(1)
