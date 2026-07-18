"""Tool / log output compressor (Phase 2).

Cleans shell, test, build, and lint output before it is sent to the model. It is
lossless with respect to *meaning*: it strips presentational noise (ANSI codes,
progress bars, install chatter) and collapses repetition, while preserving exit
codes, error lines, stack traces, and file/line references.

In v1 there is no live shell execution yet, so this is applied to review/feedback
text and is wired for real tool outputs via the TODOs in the agents.
"""

from __future__ import annotations

import re

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_PROGRESS_RE = re.compile(r"^\s*[=\-#*._\s]*\d{1,3}%\s*$")
_TRACEBACK_RE = re.compile(r"^\s*(Traceback|File \"|Error|Exception|Caused by|raise |\s+~).*", re.I)


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _is_noise(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if _PROGRESS_RE.match(line):
        return True
    low = stripped.lower()
    noise_markers = (
        "downloading", "installing", "using cached", "collecting",
        "warning: failed to hardlink", "npm warn", "compiling", "building wheel",
    )
    return low.startswith(noise_markers)


def compress_output(text: str, *, tail_lines: int = 160, keep_traceback: bool = True) -> str:
    """Compress a raw log/program output blob into a compact, model-ready string."""
    if not text:
        return ""

    out = strip_ansi(text)
    lines = out.splitlines()

    # Drop blank runs and pure-noise lines; collapse consecutive duplicates.
    cleaned: list[str] = []
    prev: str | None = None
    for ln in lines:
        if _is_noise(ln):
            continue
        if ln.strip() == "" and cleaned and cleaned[-1].strip() == "":
            continue
        cur = ln.rstrip()
        if cur == prev and not (keep_traceback and _TRACEBACK_RE.match(cur)):
            continue
        cleaned.append(cur)
        prev = cur

    # Always keep traceback/error context; otherwise keep the most recent tail.
    if keep_traceback:
        error_block = [ln for ln in cleaned if _TRACEBACK_RE.match(ln)]
        tail = cleaned[-tail_lines:]
        kept = _ordered_union(tail, error_block, extra_error_lines=4, cleaned=cleaned)
    else:
        kept = cleaned[-tail_lines:]

    removed = max(0, len(lines) - len(kept))
    header = f"[compressed: {len(lines)} -> {len(kept)} lines, {removed} noise/dup dropped]"
    return header + "\n" + "\n".join(kept)


def _ordered_union(tail: list[str], errors: list[str], *, extra_error_lines: int, cleaned: list[str]) -> list[str]:
    """Return the tail plus a little context around each error line, deduped, in order."""
    if not errors:
        return tail
    keep = set(id(o) for o in tail)
    out = list(tail)
    for err in errors:
        try:
            idx = cleaned.index(err)
        except ValueError:
            continue
        for j in range(max(0, idx - extra_error_lines), min(len(cleaned), idx + 1)):
            if id(cleaned[j]) not in keep:
                keep.add(id(cleaned[j]))
                out.append(cleaned[j])
    # Reorder back to original sequence.
    order = {id(o): i for i, o in enumerate(cleaned)}
    out.sort(key=lambda o: order.get(id(o), 1 << 30))
    return out
