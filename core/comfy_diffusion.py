"""Small ComfyUI client for local realistic image and motion generation."""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(os.environ.get("CLAUDE_STACK_ROOT", Path.home() / "claude-stack"))
COMFY_URL = os.environ.get("COMFY_URL", "http://127.0.0.1:8188").rstrip("/")

SDXL_CHECKPOINT = os.environ.get("COMFY_SDXL_CHECKPOINT", "epicrealismXL_pureFix.safetensors")
IMAGE_OUTPUT_DIR = ROOT / "data" / "hustle" / "generated_images"


class ComfyError(RuntimeError):
    pass


def _slug(text: str, limit: int = 64) -> str:
    import re

    value = re.sub(r"[^a-zA-Z0-9]+", "-", str(text or "").strip().lower()).strip("-")
    return value[:limit] or "local-diffusion"


def comfy_root() -> Path | None:
    candidates: list[Path] = []
    if os.environ.get("COMFY_ROOT"):
        candidates.append(Path(os.environ["COMFY_ROOT"]).expanduser())
    if os.environ.get("AI_VIDEO_ROOT"):
        root = Path(os.environ["AI_VIDEO_ROOT"]).expanduser()
        candidates.extend([root / "ComfyUI", root])
    candidates.extend([Path.home() / "ComfyUI", Path.home() / "ai-video" / "ComfyUI"])

    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        if (candidate / "main.py").exists():
            return candidate
    return None


def _comfy_python(root: Path) -> Path:
    for rel in ("venv/bin/python", ".venv/bin/python"):
        candidate = root / rel
        if candidate.exists():
            return candidate
    return Path(shutil.which("python3") or "python3")


def _http_json(url: str, timeout: float = 5.0) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        parsed = json.loads(response.read().decode("utf-8", errors="replace"))
    return parsed if isinstance(parsed, dict) else {"raw": parsed}


def _http_post_json(url: str, payload: dict[str, Any], timeout: float = 10.0) -> dict[str, Any]:
    data = json.dumps(payload).encode()
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            parsed = json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise ComfyError(f"ComfyUI rejected prompt: {detail}") from exc
    return parsed if isinstance(parsed, dict) else {"raw": parsed}


def comfy_ready() -> bool:
    try:
        _http_json(f"{COMFY_URL}/system_stats", timeout=2.0)
        return True
    except Exception:
        return False


def start_comfy(timeout: float = 150.0) -> subprocess.Popen[str] | None:
    if comfy_ready():
        return None
    root = comfy_root()
    if not root:
        raise ComfyError("ComfyUI is not installed under ~/ComfyUI or ~/ai-video/ComfyUI")

    log_dir = ROOT / "data" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log = (log_dir / "comfyui-local-diffusion.log").open("a")
    proc = subprocess.Popen(
        [
            str(_comfy_python(root)),
            str(root / "main.py"),
            "--listen",
            "127.0.0.1",
            "--port",
            "8188",
            "--disable-auto-launch",
        ],
        cwd=str(root),
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    deadline = time.time() + timeout
    while time.time() < deadline:
        if comfy_ready():
            return proc
        if proc.poll() is not None:
            raise ComfyError("ComfyUI exited before becoming ready")
        time.sleep(2)
    raise ComfyError(f"ComfyUI did not become ready on {COMFY_URL}")


def _ensure_ready(start: bool) -> None:
    if comfy_ready():
        return
    if not start:
        raise ComfyError("ComfyUI is not running")
    start_comfy()


def _fit_size(width: int, height: int, *, max_dim: int, max_pixels: int, min_dim: int = 256) -> tuple[int, int]:
    width = max(64, int(width or 512))
    height = max(64, int(height or 512))
    scale = min(1.0, max_dim / max(width, height), math.sqrt(max_pixels / max(1, width * height)))
    out_w = max(min_dim, int(round((width * scale) / 8) * 8))
    out_h = max(min_dim, int(round((height * scale) / 8) * 8))
    return out_w, out_h


def _seed(seed: int | None, prompt: str) -> int:
    if seed is not None:
        return int(seed) % (2**63 - 1)
    digest = hashlib.sha1(prompt.encode("utf-8", errors="ignore")).hexdigest()
    return int(digest[:12], 16) % 2_000_000_000 + random.randint(0, 9999)


def _wait(prompt_id: str, timeout: int) -> dict[str, Any]:
    deadline = time.time() + timeout
    last_error = ""
    while time.time() < deadline:
        try:
            history = _http_json(f"{COMFY_URL}/history/{prompt_id}", timeout=15.0)
        except (TimeoutError, socket.timeout, urllib.error.URLError, ConnectionError) as exc:
            last_error = str(exc)
            time.sleep(2)
            continue
        if prompt_id in history:
            record = history[prompt_id]
            status = record.get("status") or {}
            if status.get("status_str") != "success":
                raise ComfyError(f"ComfyUI prompt failed: {status}")
            return record
        time.sleep(2)
    detail = f"; last polling error: {last_error}" if last_error else ""
    raise ComfyError(f"ComfyUI prompt timed out after {timeout}s{detail}")


def _submit(prompt: dict[str, Any], timeout: int) -> dict[str, Any]:
    result = _http_post_json(
        f"{COMFY_URL}/prompt",
        {"prompt": prompt, "client_id": str(uuid.uuid4())},
        timeout=10.0,
    )
    prompt_id = result.get("prompt_id")
    if not prompt_id:
        raise ComfyError(f"ComfyUI did not return a prompt_id: {result}")
    return _wait(str(prompt_id), timeout=timeout)


def _first_output(record: dict[str, Any], key: str) -> dict[str, Any]:
    outputs = record.get("outputs") or {}
    for value in outputs.values():
        items = value.get(key) or []
        if items:
            return items[0]
    raise ComfyError(f"ComfyUI completed without a {key} output")


def _copy_output(item: dict[str, Any], target_dir: Path, prefix: str) -> Path:
    root = comfy_root()
    if not root:
        raise ComfyError("ComfyUI root is unavailable")
    filename = str(item.get("filename") or "").strip()
    subfolder = str(item.get("subfolder") or "").strip()
    if not filename:
        raise ComfyError(f"ComfyUI output has no filename: {item}")
    source = root / "output" / subfolder / filename
    if str(item.get("fullpath") or "").strip():
        source = Path(str(item["fullpath"]))
    if not source.exists():
        raise ComfyError(f"ComfyUI output not found: {source}")
    target_dir.mkdir(parents=True, exist_ok=True)
    suffix = source.suffix or ".png"
    target = target_dir / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{_slug(prefix)}{suffix}"
    shutil.copy2(source, target)
    return target


def _maybe_resize(path: Path, width: int, height: int) -> None:
    try:
        from PIL import Image

        with Image.open(path) as image:
            if image.size == (width, height):
                return
            resized = image.resize((width, height), Image.Resampling.LANCZOS)
            resized.save(path)
    except Exception:
        return


def _write_metadata(path: Path, payload: dict[str, Any]) -> None:
    metadata = dict(payload)
    metadata.setdefault("created_at", datetime.now().isoformat())
    metadata.setdefault("file", str(path))
    try:
        path.with_suffix(".json").write_text(json.dumps(metadata, indent=2, default=str))
    except Exception:
        pass


def _image_negative(prompt: str, negative: str | None) -> str:
    # Always prepend hard clown/makeup blockers regardless of caller-supplied negative
    hard_block = (
        "face paint, cheek paint, clown makeup, clown face, mime makeup, white face paint, "
        "white makeup patches, powdery white makeup, pale painted face, costume makeup, "
        "theatrical makeup, carnival makeup, stage makeup, kabuki makeup, corpse paint, "
        "sports fan face paint, war paint, tribal markings, painted cheeks, cheek stripes, "
        "colored streaks on face, neon color bands on skin, painted skin, skin markings, "
        "blown highlights on face, overexposed forehead, overexposed cheeks"
    )
    base = negative or (
        "cartoon, anime, illustration, painting, CGI, 3d render, plastic skin, waxy skin, "
        "airbrushed skin, porcelain skin, bad hands, distorted face, deformed, blurry, "
        "low quality, watermark, text, logo, mask, harsh color cast, bruised face, smudged makeup, "
        "uncanny smile, distorted teeth, bad teeth, extra teeth"
    )
    if hard_block not in base:
        base = f"{hard_block}, {base}"
    lower = prompt.lower()
    if "no people" in lower or "no faces" in lower or "no bodies" in lower:
        base += ", person, people, face, body, human, woman, man, hands"
    return base


def render_realistic_image(
    prompt: str,
    *,
    output_dir: Path | None = None,
    width: int = 1080,
    height: int = 1920,
    seed: int | None = None,
    steps: int | None = None,
    cfg: float | None = None,
    negative: str | None = None,
    timeout: int = 900,
    start: bool = True,
    title: str = "",
    checkpoint: str | None = None,
) -> Path:
    _ensure_ready(start=start)
    prompt = str(prompt or "").strip()
    if not prompt:
        raise ComfyError("image prompt is empty")

    ckpt = str(checkpoint or "").strip() or SDXL_CHECKPOINT
    max_dim = int(os.environ.get("COMFY_SDXL_MAX_DIM", "1344"))
    max_pixels = int(os.environ.get("COMFY_SDXL_MAX_PIXELS", str(1152 * 1152)))
    latent_w, latent_h = _fit_size(width, height, max_dim=max_dim, max_pixels=max_pixels, min_dim=384)
    prefix = _slug(title or prompt)
    image_cfg = float(cfg if cfg is not None else os.environ.get("COMFY_SDXL_CFG", "4.2"))
    negative_prompt = _image_negative(prompt, negative)
    positive = (
        prompt
        + ", photorealistic, authentic candid photograph, natural ambient light, "
        "realistic skin texture, even skin tone, sharp subject, high detail"
    )
    api_prompt = {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ckpt}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["1", 1], "text": positive}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["1", 1], "text": negative_prompt}},
        "4": {"class_type": "EmptyLatentImage", "inputs": {"width": latent_w, "height": latent_h, "batch_size": 1}},
        "5": {
            "class_type": "KSampler",
            "inputs": {
                "model": ["1", 0],
                "positive": ["2", 0],
                "negative": ["3", 0],
                "latent_image": ["4", 0],
                "seed": _seed(seed, prompt),
                "steps": int(steps or os.environ.get("COMFY_SDXL_STEPS", "18")),
                "cfg": image_cfg,
                "sampler_name": os.environ.get("COMFY_SDXL_SAMPLER", "dpmpp_2m"),
                "scheduler": os.environ.get("COMFY_SDXL_SCHEDULER", "karras"),
                "denoise": 1.0,
            },
        },
        "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
        "7": {"class_type": "SaveImage", "inputs": {"images": ["6", 0], "filename_prefix": f"stack/sdxl_{prefix}"}},
    }
    record = _submit(api_prompt, timeout=timeout)
    out = _copy_output(_first_output(record, "images"), output_dir or IMAGE_OUTPUT_DIR, prefix)
    _maybe_resize(out, int(width), int(height))
    _write_metadata(
        out,
        {
            "backend": "comfyui_sdxl_epicrealism",
            "prompt": prompt,
            "checkpoint": ckpt,
            "latent_width": latent_w,
            "latent_height": latent_h,
            "width": width,
            "height": height,
            "steps": int(steps or os.environ.get("COMFY_SDXL_STEPS", "18")),
            "cfg": image_cfg,
            "sampler": os.environ.get("COMFY_SDXL_SAMPLER", "dpmpp_2m"),
            "scheduler": os.environ.get("COMFY_SDXL_SCHEDULER", "karras"),
            "negative_prompt": negative_prompt,
            "paid_model_call": False,
            "local_ai": True,
        },
    )
    return out

