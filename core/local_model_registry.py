"""Registry and active-model state for local Ollama chat models."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Iterable


BASE_DIR = Path(__file__).resolve().parent.parent
ACTIVE_MODEL_PATH = BASE_DIR / "data" / "runtime" / "local_model_selection.json"
ACTIVE_VIDEO_MODEL_PATH = BASE_DIR / "data" / "runtime" / "video_model_selection.json"

DEFAULT_LOCAL_MODEL = os.environ.get("CLAUDE_STACK_LOCAL_MODEL", os.environ.get("LOCAL_MODEL", "qwen2.5:7b"))


LOCAL_MODEL_REGISTRY = [
    {
        "label": "Qwen2.5 7B",
        "model": "qwen2.5:7b",
        "alias": "qwen",
        "family": "ops",
        "description": "Current default resident local ops/chat model for the Mini.",
        "options": {"temperature": 0.35, "num_ctx": 32768},
    },
    {
        "label": "Qwen3 14B",
        "model": "qwen3:14b",
        "alias": "qwen3-14b",
        "family": "ops",
        "description": "Higher-capability on-demand work model. Do not keep resident during normal work mode.",
        "install_source": "qwen3:14b",
        "options": {"temperature": 0.35, "num_ctx": 32768},
    },
    {
        "label": "Qwen3 30B A3B",
        "model": "qwen3:30b-a3b",
        "alias": "qwen3-30b",
        "family": "ops",
        "description": "Quality on-demand work model for complex JIT-routed tasks.",
        "install_source": "qwen3:30b-a3b",
        "options": {"temperature": 0.35, "num_ctx": 32768},
    },
    {
        "label": "Qwen3 Coder 30B A3B",
        "model": "qwen3-coder:30b",
        "alias": "qwen3-coder",
        "family": "code",
        "description": "Strongest local coder (MoE, ~3B active). 18GB — NOT kept installed: it breaches the disk-guardian work-lane floor (WARN 35 / CRIT 26 GiB free) on the 24GB box. The strong-coder tier is served by the FREE CLOUD lane (qwen/qwen3-coder:free) which the SWE agents try first; pull this to External only for a fully-offline strong coder.",
        "install_source": "qwen3-coder:30b",
        "not_resident": True,
        "options": {"temperature": 0.2, "num_ctx": 32768},
    },
    {
        "label": "Qwen2.5 Coder 14B",
        "model": "qwen2.5-coder:14b",
        "alias": "qwen-coder-14b",
        "family": "code",
        "description": "Default local coder (LOCAL_CODE_MODEL). Big jump over 7B, ~9GB, safe for unattended self-heal/SWE codegen without crushing RAM.",
        "install_source": "qwen2.5-coder:14b",
        "options": {"temperature": 0.35, "num_ctx": 32768},
    },
    {
        "label": "Qwen2.5 Coder 7B",
        "model": "qwen2.5-coder:7b",
        "alias": "qwen-coder",
        "family": "code",
        "description": "Fast local coding fallback (fastest, lowest RAM).",
        "options": {"temperature": 0.35, "num_ctx": 16384},
    },
    {
        "label": "Psyfighter 13B",
        "model": "hf.co/TheBloke/Psyfighter-13B-GGUF:Q4_K_M",
        "alias": "psyfighter-13b",
        "family": "creative",
        "description": "GGUF import from Hugging Face.",
        "install_source": "hf.co/TheBloke/Psyfighter-13B-GGUF:Q4_K_M",
        "options": {"temperature": 0.95, "top_p": 0.95, "repeat_penalty": 1.08, "num_ctx": 4096},
    },
    {
        "label": "L3 8B Stheno v3.2",
        "model": "fluffy/l3-8b-stheno-v3.2:q4_K_M",
        "alias": "stheno-8b",
        "family": "creative",
        "description": "8B creative writing model.",
        "install_source": "fluffy/l3-8b-stheno-v3.2:q4_K_M",
        "options": {"temperature": 1.16, "min_p": 0.075, "top_k": 50, "repeat_penalty": 1.1, "num_ctx": 8192},
    },
    {
        "label": "MythoMax L2 13B",
        "model": "nollama/mythomax-l2-13b:Q4_K_M",
        "alias": "mythomax-l2-13b",
        "family": "creative",
        "description": "13B Llama 2 story/role-play model.",
        "install_source": "nollama/mythomax-l2-13b:Q4_K_M",
        "options": {"temperature": 0.9, "top_p": 0.95, "repeat_penalty": 1.1, "num_ctx": 4096},
    },
    {
        "label": "Dolphin 3.0 Llama 3.2 1B",
        "model": "dolphin3:1b",
        "alias": "dolphin3-1b",
        "family": "general",
        "description": "Small fast Dolphin 3.0 variant.",
        "install_source": "dolphin3:1b",
        "options": {"temperature": 0.7, "top_p": 0.9, "num_ctx": 8192},
    },
    {
        "label": "Dolphin 3.0 Llama 3.2 3B",
        "model": "dolphin3:3b",
        "alias": "dolphin3-3b",
        "family": "general",
        "description": "Balanced fast Dolphin 3.0 variant.",
        "install_source": "dolphin3:3b",
        "options": {"temperature": 0.7, "top_p": 0.9, "num_ctx": 8192},
    },
    {
        "label": "Dolphin 3.0 Llama 3.1 8B",
        "model": "dolphin3:8b",
        "alias": "dolphin3-8b",
        "family": "general",
        "description": "Best local Dolphin 3.0 option for this machine.",
        "install_source": "dolphin3:8b",
        "options": {"temperature": 0.7, "top_p": 0.9, "num_ctx": 8192},
    },
]

UNAVAILABLE_REQUESTED_MODELS = [
    {
        "label": "Midnight Rose 13B",
        "requested": "Midnight Rose 13B",
        "reason": "No credible 13B GGUF/Ollama model was found. Public Midnight Rose releases are 70B/103B-class and are not safe to make the default on a 24GB Mac mini.",
    }
]


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9./:_-]+", "", str(value or "").strip().lower())


def _aliases() -> dict[str, str]:
    out = {}
    for item in LOCAL_MODEL_REGISTRY:
        model = str(item["model"])
        for value in (model, item.get("alias"), f"local:{model}", f"ollama:{model}"):
            if value:
                out[_norm(str(value))] = model
    return out


def normalize_model_selector(value: str | None) -> str:
    raw = str(value or "").strip()
    if raw.lower().startswith("ollama:"):
        raw = "local:" + raw.split(":", 1)[1]
    if raw.lower().startswith("local:"):
        raw = raw.split(":", 1)[1]
    if not raw:
        return ""
    return _aliases().get(_norm(raw), raw)


def is_registered_model(value: str | None) -> bool:
    return bool(normalize_model_selector(value) in {str(item["model"]) for item in LOCAL_MODEL_REGISTRY})


def is_local_selector(value: str | None) -> bool:
    raw = str(value or "").strip().lower()
    return raw.startswith(("local:", "ollama:")) or _norm(raw) in _aliases()


def _read_active_model(path: Path, default: str | None = None) -> str:
    fallback = normalize_model_selector(default or DEFAULT_LOCAL_MODEL) or DEFAULT_LOCAL_MODEL
    try:
        data = json.loads(path.read_text())
        model = normalize_model_selector(data.get("model"))
        if model:
            return model
    except Exception:
        pass
    return fallback


def _write_active_model(path: Path, model: str, source: str = "unknown") -> str:
    resolved = normalize_model_selector(model)
    if not resolved:
        raise ValueError("missing local model")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"model": resolved, "source": source, "ts": time.time()}, indent=2))
    return resolved


def get_active_local_model(default: str | None = None) -> str:
    return _read_active_model(ACTIVE_MODEL_PATH, default)


def set_active_local_model(model: str, source: str = "unknown") -> str:
    return _write_active_model(ACTIVE_MODEL_PATH, model, source)


def get_active_video_model(default: str | None = None) -> str:
    return _read_active_model(ACTIVE_VIDEO_MODEL_PATH, default or get_active_local_model(DEFAULT_LOCAL_MODEL))


def set_active_video_model(model: str, source: str = "unknown") -> str:
    return _write_active_model(ACTIVE_VIDEO_MODEL_PATH, model, source)


def model_options(installed_models: Iterable[str] | None = None, active_model: str | None = None) -> list[dict]:
    installed = {_norm(m) for m in (installed_models or [])}
    active = normalize_model_selector(active_model) if active_model else get_active_local_model()
    out = []
    for item in LOCAL_MODEL_REGISTRY:
        model = str(item["model"])
        is_installed = not installed or _norm(model) in installed
        out.append(
            {
                "id": f"local:{model}",
                "model": model,
                "label": item["label"],
                "alias": item.get("alias", ""),
                "family": item.get("family", ""),
                "description": item.get("description", ""),
                "installed": is_installed,
                "active": _norm(model) == _norm(active),
            }
        )
    return out


def options_for_model(model: str | None) -> dict:
    resolved = normalize_model_selector(model)
    for item in LOCAL_MODEL_REGISTRY:
        if str(item["model"]) == resolved:
            return dict(item.get("options") or {})
    return {}


def install_targets() -> list[dict]:
    return [
        {
            "label": item["label"],
            "model": item["model"],
            "source": item.get("install_source") or item["model"],
            "sources": item.get("install_sources") or [item.get("install_source") or item["model"]],
        }
        for item in LOCAL_MODEL_REGISTRY
        if item.get("install_source")
    ]
