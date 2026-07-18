"""Per-modality handlers. Each wires to an EXISTING stack module and degrades
gracefully when its backend is missing.

A handler takes a job dict and returns a result dict:
    {"ok": bool, "modality": str, "output": str|None, "backend": str, "note": str}

No outbound network is initiated here directly. Image gen may fall through to
core.image_gen's remote fallback only if local is unavailable AND the caller
explicitly allows it (job["allow_remote"], default False for the night studio).
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path

ROOT = Path(os.environ.get("CLAUDE_STACK_ROOT", Path.home() / "claude-stack"))
OUT = ROOT / "data" / "content_studio"


def _slug(text: str, limit: int = 60) -> str:
    v = re.sub(r"[^a-zA-Z0-9]+", "-", str(text or "").strip().lower()).strip("-")
    return v[:limit] or "job"


def _outdir(modality: str) -> Path:
    d = OUT / modality
    d.mkdir(parents=True, exist_ok=True)
    return d


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _result(ok, modality, output=None, backend="none", note="") -> dict:
    return {
        "ok": bool(ok),
        "modality": modality,
        "output": str(output) if output else None,
        "backend": backend,
        "note": note,
    }


# --------------------------------------------------------------------------- #
# IMAGE  -> core.image_gen (local SDXL/ComfyUI first; remote only if allowed)
# --------------------------------------------------------------------------- #
def handle_image(job: dict) -> dict:
    prompt = job.get("prompt") or job.get("text") or ""
    if not prompt:
        return _result(False, "image", note="no prompt")
    try:
        from core import image_gen
    except Exception as e:  # pragma: no cover
        return _result(False, "image", note=f"core.image_gen unavailable: {e}")

    allow_remote = bool(job.get("allow_remote", False))
    model = job.get("model") or ("flux" if allow_remote else "sdxl")
    # local-only model keys make core.image_gen skip the remote fallback
    prev = os.environ.get("IMAGE_GEN_ALLOW_REMOTE")
    if not allow_remote:
        os.environ["IMAGE_GEN_ALLOW_REMOTE"] = "0"
    try:
        path = image_gen.generate_image(
            prompt,
            model=model,
            width=int(job.get("width", 1080)),
            height=int(job.get("height", 1920)),
            seed=job.get("seed"),
            negative=job.get("negative"),
            timeout=int(job.get("timeout", 180)),
        )
    except Exception as e:  # pragma: no cover
        return _result(False, "image", note=f"generate failed: {e}")
    finally:
        if prev is None:
            os.environ.pop("IMAGE_GEN_ALLOW_REMOTE", None)
        else:
            os.environ["IMAGE_GEN_ALLOW_REMOTE"] = prev

    if path:
        return _result(True, "image", output=path, backend=f"image_gen:{model}")
    return _result(False, "image", note="no local image backend produced output")


# --------------------------------------------------------------------------- #
# VOICE  -> core.ima_voice.speak (F5 clone -> edge-tts -> say fallback chain)
# --------------------------------------------------------------------------- #
def handle_voice(job: dict) -> dict:
    text = job.get("text") or job.get("script") or ""
    if not text:
        return _result(False, "voice", note="no text")
    out = _outdir("voice") / f"{_stamp()}_{_slug(text, 40)}.wav"
    try:
        from core.ima_voice import speak

        ok = speak(text, out)
        if ok and out.exists():
            return _result(True, "voice", output=out, backend="ima_voice")
    except Exception as e:
        # graceful degrade to macOS `say`
        note = f"ima_voice failed: {e}"
        try:
            import subprocess

            aiff = out.with_suffix(".aiff")
            subprocess.run(["say", "-o", str(aiff), text], check=True, timeout=120)
            if aiff.exists():
                return _result(True, "voice", output=aiff, backend="macos_say", note=note)
        except Exception as e2:
            return _result(False, "voice", note=f"{note}; say failed: {e2}")
    return _result(False, "voice", note="no voice backend produced output")


# --------------------------------------------------------------------------- #
# VIDEO  -> scripts/build_picture_sequence_videos.py (moviepy+ffmpeg 1080x1920)
# --------------------------------------------------------------------------- #
def handle_video(job: dict) -> dict:
    script = ROOT / "scripts" / "build_picture_sequence_videos.py"
    if not script.exists():
        return _result(False, "video", note="picture-sequence script missing")
    # Stub: real invocation is deliberately NOT run here (no long renders in the
    # scaffold). The overnight worker should shell out to the script with the
    # job's lane/images/audio. Wire-up point documented for the build session.
    return _result(
        False,
        "video",
        backend="picture_sequence",
        note=(
            "video backend present but not invoked by scaffold; "
            f"run `python {script} --lane <lane>` from the night worker"
        ),
    )


# --------------------------------------------------------------------------- #
# PDF/DOC -> core.doc_generator (research+docx) or reportlab/weasyprint direct
# --------------------------------------------------------------------------- #
def handle_pdf(job: dict) -> dict:
    # Simple, no-network path first: render provided HTML/text to PDF locally.
    html = job.get("html")
    out = _outdir("pdf") / f"{_stamp()}_{_slug(job.get('title') or 'doc', 40)}.pdf"
    if html:
        try:
            from weasyprint import HTML

            HTML(string=html).write_pdf(str(out))
            if out.exists():
                return _result(True, "pdf", output=out, backend="weasyprint")
        except Exception as e:
            return _result(False, "pdf", note=f"weasyprint failed: {e}")
    # Otherwise hand off to the existing docx research pipeline (stub — async,
    # needs a tool_registry; the night worker supplies it).
    return _result(
        False,
        "pdf",
        backend="doc_generator",
        note=(
            "provide job['html'] for direct local PDF, or route to "
            "core.doc_generator.research_and_generate (needs tool_registry)"
        ),
    )


# --------------------------------------------------------------------------- #
# TEXT  -> local Ollama via core.llm_router (local-forced, $0)
# --------------------------------------------------------------------------- #
def handle_text(job: dict) -> dict:
    prompt = job.get("prompt") or job.get("text") or ""
    if not prompt:
        return _result(False, "text", note="no prompt")
    try:
        # Force local routing for the night studio; never spend at 3am.
        os.environ.setdefault("CLAUDE_STACK_FORCE_LOCAL_ROUTING", "1")
        from core import llm_router  # noqa: F401
    except Exception as e:
        return _result(False, "text", note=f"core.llm_router unavailable: {e}")
    # Minimal direct-Ollama call to avoid coupling to router internals/signature.
    try:
        import json
        import urllib.request

        model = job.get("model") or os.environ.get("LOCAL_MODEL", "qwen2.5:7b")
        body = json.dumps(
            {"model": model, "prompt": prompt, "stream": False}
        ).encode()
        req = urllib.request.Request(
            "http://127.0.0.1:11434/api/generate", data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=int(job.get("timeout", 300))) as r:
            data = json.loads(r.read())
        text = (data.get("response") or "").strip()
        if text:
            out = _outdir("text") / f"{_stamp()}_{_slug(prompt, 40)}.md"
            out.write_text(text, encoding="utf-8")
            return _result(True, "text", output=out, backend=f"ollama:{model}")
    except Exception as e:
        return _result(False, "text", note=f"local LLM failed: {e}")
    return _result(False, "text", note="no text backend produced output")


HANDLERS = {
    "image": handle_image,
    "voice": handle_voice,
    "video": handle_video,
    "pdf": handle_pdf,
    "text": handle_text,
}
