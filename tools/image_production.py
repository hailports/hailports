"""Image production tool for shared WebUI, Telegram, and iMessage chat."""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any

from tools.base import BaseTool, make_tool_def


ROOT = Path(os.environ.get("CLAUDE_STACK_ROOT", Path.home() / "claude-stack"))
RENDERS_DIR = ROOT / "data" / "image_production" / "renders"
CONFIG_PATH = ROOT / "data" / "hustle" / "content_generator_config.json"


def _chat_image_timeout() -> int:
    try:
        return max(15, min(int(os.environ.get("CLAUDE_STACK_IMAGE_TIMEOUT_S", "300")), 300))
    except Exception:
        return 300


def _slug(text: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9]+", "-", str(text or "").strip().lower()).strip("-")
    return value[:80] or "local-image"


def _title_from_prompt(prompt: str) -> str:
    raw = re.sub(r"\s+", " ", str(prompt or "")).strip()
    cleaned = re.sub(
        r"^(?:running fully locally,\s*)?(?:can you|please)?\s*"
        r"(?:create|make|produce|render|generate|build|draw|design)\s+(?:an?\s+)?"
        r"(?:image|picture|photo|poster|banner|thumbnail|cover|graphic|visual|png|jpg|jpeg)"
        r"(?:\s+(?:where|about|for|on|of))?\s*",
        "",
        raw,
        flags=re.IGNORECASE,
    ).strip(" :-")
    return (cleaned or "Local Image")[:90]


def _load_config() -> dict[str, Any]:
    try:
        data = json.loads(CONFIG_PATH.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_config(data: dict[str, Any]) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(data, indent=2, sort_keys=True))


def _configure(tool_input: dict[str, Any]) -> str:
    current = _load_config()
    mode = str(tool_input.get("mode") or tool_input.get("generation_mode") or current.get("mode") or "auto").strip().lower()
    if mode not in {"auto", "manual"}:
        return "Content generator config failed: mode must be auto or manual."
    backend = str(tool_input.get("backend") or current.get("backend") or "auto").strip().lower()
    model_profile = str(tool_input.get("model_profile") or tool_input.get("model") or current.get("model_profile") or "").strip().lower()
    if model_profile not in {"", "sdxl", "pony"}:
        return "Content generator config failed: model_profile must be sdxl or pony."
    custom_prompt = str(tool_input.get("custom_prompt") or current.get("custom_prompt") or "").strip()
    auto_enabled = tool_input.get("auto_enabled", current.get("auto_enabled", mode == "auto"))
    payload = {
        **current,
        "mode": mode,
        "auto_enabled": bool(auto_enabled),
        "backend": backend,
        "model_profile": model_profile or ("pony" if backend == "pony" else "sdxl"),
        "checkpoint": str(tool_input.get("checkpoint") or current.get("checkpoint") or "").strip(),
        "negative": str(tool_input.get("negative") or current.get("negative") or "").strip(),
        "custom_prompt": custom_prompt,
    }
    _write_config(payload)
    return (
        "Content generator configured.\n"
        f"Mode: {payload['mode']}\n"
        f"Auto enabled: {payload['auto_enabled']}\n"
        f"Backend: {payload['backend']}\n"
        f"Model profile: {payload['model_profile']}\n"
        f"Checkpoint: {payload['checkpoint'] or '(env/default)'}\n"
        f"Custom prompt: {'set' if payload['custom_prompt'] else '(none)'}\n"
        f"Path: {CONFIG_PATH}"
    )


def _render(tool_input: dict[str, Any]) -> str:
    from scripts.image_production import render_local_image

    config = _load_config()
    prompt = str(tool_input.get("prompt") or tool_input.get("description") or config.get("custom_prompt") or "").strip()
    if not prompt:
        return "Image request failed: prompt is empty."
    title = str(tool_input.get("title") or "").strip() or _title_from_prompt(prompt)
    backend = str(tool_input.get("backend") or config.get("backend") or "auto").strip().lower()
    model_profile = str(tool_input.get("model_profile") or tool_input.get("model") or config.get("model_profile") or "").strip().lower()
    checkpoint = str(tool_input.get("checkpoint") or config.get("checkpoint") or "").strip()
    negative = str(tool_input.get("negative") or config.get("negative") or "").strip()
    width = int(tool_input.get("width") or 1080)
    height = int(tool_input.get("height") or 1080)
    timeout = int(tool_input.get("timeout") or _chat_image_timeout())
    path = render_local_image(
        prompt,
        title=title,
        width=width,
        height=height,
        backend=backend,
        timeout=timeout,
        checkpoint=checkpoint or None,
        model_profile=model_profile,
        negative=negative or None,
    )
    return (
        "Rendered local image.\n"
        f"Title: {title}\n"
        f"Path: {path}\n"
        f"Backend: {backend or 'auto'} / {model_profile or 'default'}; no paid model call."
    )


def _status(limit: int = 8) -> str:
    from scripts.image_production import backend_status

    RENDERS_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(RENDERS_DIR.glob("*/*"), key=lambda p: p.stat().st_mtime, reverse=True)
    images = [p for p in files if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}]
    lines = ["Image production status"]
    config = _load_config()
    status = backend_status()
    lines.append(f"Mode: {config.get('mode', 'auto')} | Auto enabled: {config.get('auto_enabled', True)}")
    lines.append(f"Configured backend: {config.get('backend', status.get('default_backend', 'auto'))}")
    lines.append(f"Configured model profile: {config.get('model_profile', 'sdxl')}")
    lines.append(f"Configured checkpoint: {config.get('checkpoint') or '(env/default)'}")
    lines.append(
        "Backends ready: "
        f"Comfy={bool(status.get('comfy', {}).get('ready'))}, "
        f"A1111={bool(status.get('a1111', {}).get('ready'))}, "
        f"Swarm={bool(status.get('swarm', {}).get('ready'))}"
    )
    if not images:
        lines.append("No rendered images yet.")
        return "\n".join(lines)
    for path in images[: max(1, min(limit, 25))]:
        meta = path.parent / "metadata.json"
        backend = ""
        title = path.stem
        if meta.exists():
            try:
                payload = json.loads(meta.read_text())
                backend = str(payload.get("backend") or "")
                title = str(payload.get("title") or title)
            except Exception:
                pass
        lines.append(f"- {title} | {backend or 'local'}")
        lines.append(f"  Path: {path}")
    return "\n".join(lines)


class ImageProductionTool(BaseTool):
    name = "image_production"
    description = "Render private local images on the Mini and return an attachable file path."

    def get_definitions(self) -> list[dict]:
        return [
            make_tool_def(
                "image_production_create",
                (
                    "Create a private local image artifact. Use for requested images, photos, "
                    "graphics, posters, thumbnails, social post visuals, or local-only image concepts."
                ),
                {
                    "prompt": {"type": "string", "description": "Detailed image prompt or creative brief."},
                    "title": {"type": "string", "description": "Short image title."},
                    "width": {"type": "integer", "description": "Output width in pixels."},
                    "height": {"type": "integer", "description": "Output height in pixels."},
                    "backend": {
                        "type": "string",
                        "description": "auto, comfy/local/sdxl, pony, swarm, a1111/automatic1111, or card.",
                    },
                    "model_profile": {"type": "string", "description": "sdxl or pony."},
                    "checkpoint": {"type": "string", "description": "Optional local checkpoint filename."},
                    "negative": {"type": "string", "description": "Optional negative prompt."},
                    "timeout": {"type": "integer", "description": "Render timeout in seconds."},
                },
                ["prompt"],
            ),
            make_tool_def(
                "image_production_configure",
                "Configure the local content/image generator mode, backend, model profile, checkpoint, and default custom prompt.",
                {
                    "mode": {"type": "string", "description": "auto or manual."},
                    "auto_enabled": {"type": "boolean", "description": "Whether autonomous content generation can use this lane."},
                    "backend": {"type": "string", "description": "auto, comfy, sdxl, pony, swarm, a1111, or card."},
                    "model_profile": {"type": "string", "description": "sdxl or pony."},
                    "checkpoint": {"type": "string", "description": "Optional checkpoint filename."},
                    "custom_prompt": {"type": "string", "description": "Optional default prompt/creative brief."},
                    "negative": {"type": "string", "description": "Optional default negative prompt."},
                },
            ),
            make_tool_def(
                "image_production_status",
                "List recent local image renders.",
                {
                    "limit": {
                        "type": "integer",
                        "description": "Number of recent images to show.",
                    },
                },
            ),
        ]

    async def handle(self, tool_name: str, tool_input: dict) -> str:
        loop = asyncio.get_event_loop()
        if tool_name == "image_production_create":
            return await loop.run_in_executor(None, _render, dict(tool_input or {}))
        if tool_name == "image_production_configure":
            return await loop.run_in_executor(None, _configure, dict(tool_input or {}))
        if tool_name == "image_production_status":
            limit = int((tool_input or {}).get("limit") or 8)
            return await loop.run_in_executor(None, _status, limit)
        return f"Unknown tool: {tool_name}"
