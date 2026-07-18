"""Shared parsing for chat responses that reference local files."""

from __future__ import annotations

import re
from pathlib import Path

from core import BASE_DIR, SETTINGS


_PATH_LINE_RE = re.compile(r"(?im)^\s*Path:\s*(.+?)\s*$")
_ABSOLUTE_PATH_RE = re.compile(r"(?<!\w)(/Users/[^\n`\"']+|~/[^\n`\"']+)")


def _output_roots() -> list[Path]:
    roots = [
        Path(SETTINGS.get("outputs", {}).get("default_path", "~/Documents/Claude Outputs")).expanduser(),
        BASE_DIR / "data" / "outputs",
    ]
    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        try:
            resolved = root.resolve()
        except Exception:
            resolved = root.absolute()
        key = str(resolved)
        if key not in seen:
            unique.append(resolved)
            seen.add(key)
    return unique


def _allowed_roots() -> list[Path]:
    roots = [Path.home().resolve(), *_output_roots()]
    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root)
        if key not in seen:
            unique.append(root)
            seen.add(key)
    return unique


def _clean_candidate(text: str) -> str:
    value = str(text or "").strip().strip("`").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1].strip()
    return value.rstrip(").,;")


def _candidate_paths(raw: str) -> list[Path]:
    value = _clean_candidate(raw)
    if not value:
        return []
    if value.startswith(("~/", "/")):
        return [Path(value).expanduser()]
    candidates: list[Path] = []
    for root in _output_roots():
        candidates.append(root / value)
        candidates.append(root / Path(value).name)
    return candidates


def _is_allowed(path: Path) -> bool:
    try:
        resolved = path.resolve()
    except Exception:
        return False
    for root in _allowed_roots():
        if resolved == root or root in resolved.parents:
            return True
    return False


def response_file_candidates(response: str, *, limit: int = 3) -> list[Path]:
    """Return existing local files referenced in a model response."""
    seen: set[str] = set()
    matches: list[Path] = []
    raw_candidates: list[str] = []
    raw_candidates.extend(_PATH_LINE_RE.findall(str(response or "")))
    raw_candidates.extend(_ABSOLUTE_PATH_RE.findall(str(response or "")))

    for raw in raw_candidates:
        for candidate in _candidate_paths(raw):
            try:
                resolved = candidate.resolve()
            except Exception:
                continue
            key = str(resolved)
            if key in seen or not resolved.exists() or not resolved.is_file():
                continue
            if not _is_allowed(resolved):
                continue
            seen.add(key)
            matches.append(resolved)
            if len(matches) >= max(1, int(limit or 1)):
                return matches
    return matches


def response_path_hint(response: str) -> str:
    """Best-effort first path hint for chat/UI rendering."""
    text = str(response or "")
    match = _PATH_LINE_RE.search(text)
    if match:
        return _clean_candidate(match.group(1))
    match = _ABSOLUTE_PATH_RE.search(text)
    if match:
        return _clean_candidate(match.group(1))
    return ""
