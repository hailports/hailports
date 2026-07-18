#!/usr/bin/env python3
"""Local, guardrail-clean attachment reader: watches the spots Outlook stages an
attachment when you OPEN it (its temp dir) + your Downloads, extracts the text, and
caches it (text only) so the work-GPT can read attachment CONTENTS — no Graph, no
tokens, no IT, no corp credentials touched.

Performance: extraction happens here in the background; the GPT path only does a fast
cache lookup (no prompt lag). Storage: caches extracted TEXT only, hard-capped + LRU
pruned. Skips huge/unchanged files.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.attachment_extract import extract_text

HOME = os.path.expanduser("~")
WATCH = [
    os.path.join(HOME, "Library/Containers/com.microsoft.Outlook/Data/tmp/outlook temp"),
    os.path.join(HOME, "Library/Group Containers/UBF8T346G9.Office/Outlook/Outlook 15 Profiles/Main Profile/Data/Outlook Temp"),
    os.path.join(HOME, "Downloads"),
]
CACHE = Path(HOME) / "claude-stack" / "data" / "runtime" / "attachment_cache"
EXTS = {
    ".eml", ".pdf", ".docx", ".xlsx", ".pptx", ".txt", ".csv", ".md", ".rtf",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".webp", ".heic",
}
MAX_FILE = 25_000_000      # skip files larger than this (perf)
CAP_TOTAL = 30_000_000     # 30MB total text cache
CAP_ENTRIES = 1000
POLL = 2.0
TEXT_CAP = 50_000


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", os.path.splitext(os.path.basename(name))[0].lower())


def _index_file() -> Path:
    return CACHE / "_index.json"


def _load() -> dict:
    try:
        return json.loads(_index_file().read_text())
    except Exception:
        return {}


def _save(idx: dict) -> None:
    _index_file().write_text(json.dumps(idx))


def _prune(idx: dict) -> None:
    items = sorted(idx.items(), key=lambda kv: kv[1].get("when", 0))
    total = sum(v.get("size", 0) for v in idx.values())
    while (total > CAP_TOTAL or len(idx) > CAP_ENTRIES) and items:
        k, v = items.pop(0)
        try:
            (CACHE / v["file"]).unlink(missing_ok=True)
        except Exception:
            pass
        total -= v.get("size", 0)
        idx.pop(k, None)


def _capture(path: str, idx: dict) -> bool:
    try:
        st = os.stat(path)
        if st.st_size == 0 or st.st_size > MAX_FILE:
            return False
        key = _norm(path)
        if not key:
            return False
        prev = idx.get(key)
        if prev and prev.get("mtime") == int(st.st_mtime):
            return False
        text = extract_text(path, cap=TEXT_CAP)
        if not text or len(text) < 5:
            return False
        fn = hashlib.sha1(key.encode()).hexdigest()[:16] + ".txt"
        (CACHE / fn).write_text(text)
        idx[key] = {"name": os.path.basename(path), "file": fn, "size": len(text),
                    "when": int(time.time()), "mtime": int(st.st_mtime)}
        return True
    except Exception:
        return False


def cached_text(name: str, cap: int = 4000) -> str | None:
    """Fast lookup used by the GPT email path — text only, no extraction at call time."""
    try:
        v = _load().get(_norm(name))
        if v:
            return (CACHE / v["file"]).read_text()[:cap]
    except Exception:
        pass
    return None


def main() -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    seen: dict[str, int] = {}
    while True:
        idx = _load()
        changed = False
        for d in WATCH:
            if not os.path.isdir(d):
                continue
            try:
                for e in os.scandir(d):
                    if not e.is_file():
                        continue
                    if os.path.splitext(e.name)[1].lower() not in EXTS:
                        continue
                    m = int(e.stat().st_mtime)
                    if seen.get(e.path) == m:
                        continue
                    seen[e.path] = m
                    if _capture(e.path, idx):
                        changed = True
            except Exception:
                pass
        if changed:
            _prune(idx)
            _save(idx)
        time.sleep(POLL)


if __name__ == "__main__":
    main()
