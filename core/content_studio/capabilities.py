"""Detect which local content tools are actually usable RIGHT NOW.

Read-only probes. No network, no model loads, no renders. Every probe is
best-effort and returns a small dict so the dispatcher can route a job to the
best available local backend (or skip the modality cleanly).
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import socket
from pathlib import Path

ROOT = Path(os.environ.get("CLAUDE_STACK_ROOT", Path.home() / "claude-stack"))
VENV_BIN = ROOT / ".venv" / "bin"
COMFY_URL = os.environ.get("COMFY_URL", "http://127.0.0.1:8188")


def _have_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


def _have_bin(*candidates: str) -> str | None:
    for c in candidates:
        p = Path(c)
        if p.is_absolute() and p.exists():
            return str(p)
        found = shutil.which(c)
        if found:
            return found
    return None


def _comfy_up(timeout: float = 1.5) -> bool:
    host, _, port = COMFY_URL.replace("http://", "").replace("https://", "").partition(":")
    port = int(port or 8188)
    try:
        with socket.create_connection((host or "127.0.0.1", port), timeout=timeout):
            return True
    except Exception:
        return False


def _image() -> dict:
    return {
        "diffusers": _have_module("diffusers") and _have_module("torch"),
        "comfyui_server": _comfy_up(),
        "core_image_gen": _have_module("core.image_gen"),
        "sdxl_turbo_cached": (
            Path.home() / ".cache/huggingface/hub/models--stabilityai--sdxl-turbo"
        ).exists(),
        "remote_fallback_pollinations": True,  # core.image_gen falls back to this
    }


def _voice() -> dict:
    voice_sample = ROOT / "data/hustle/ima_test_voice.mp3"
    return {
        "edge_tts": bool(_have_bin(str(VENV_BIN / "edge-tts"), "edge-tts")),
        "piper": bool(_have_bin(str(VENV_BIN / "piper"), str(ROOT / "venv/bin/piper"), "piper")),
        "f5_tts": bool(_have_bin(str(VENV_BIN / "f5-tts_infer-cli"))),
        "macos_say": bool(_have_bin("say")),
        "ima_voice_sample": voice_sample.exists(),
        "core_ima_voice": _have_module("core.ima_voice"),
    }


def _video() -> dict:
    return {
        "ffmpeg": bool(_have_bin("ffmpeg", "/opt/homebrew/bin/ffmpeg")),
        "moviepy": _have_module("moviepy"),
        "pillow": _have_module("PIL"),
        "picture_sequence_script": (ROOT / "scripts/build_picture_sequence_videos.py").exists(),
        "comfyui_motion": _comfy_up(),  # AnimateDiff/SVD via ComfyUI when up
    }


def _pdf() -> dict:
    return {
        "weasyprint": _have_module("weasyprint") or bool(_have_bin("weasyprint")),
        "reportlab": _have_module("reportlab"),
        "python_docx": _have_module("docx"),
        "core_doc_generator": _have_module("core.doc_generator"),
        "core_document_output": _have_module("core.document_output"),
    }


def _text() -> dict:
    ollama_up = False
    try:
        with socket.create_connection(("127.0.0.1", 11434), timeout=1.0):
            ollama_up = True
    except Exception:
        ollama_up = False
    return {
        "ollama_server": ollama_up,
        "local_model": os.environ.get("LOCAL_MODEL", "qwen2.5:7b"),
        "core_llm_router": _have_module("core.llm_router"),
    }


def capabilities() -> dict:
    """Return a per-modality map of available local backends."""
    return {
        "image": _image(),
        "voice": _voice(),
        "video": _video(),
        "pdf": _pdf(),
        "text": _text(),
    }


if __name__ == "__main__":
    import json

    print(json.dumps(capabilities(), indent=2))
