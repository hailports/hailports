#!/usr/bin/env python3
"""check_no_headful_steal.py — enforce the no-foreground-steal invariant.

An autonomous Chrome must NEVER flash the macOS foreground or warp the cursor.
A headful Chrome self-activates the instant its window is created, so the ONLY
sanctioned ways to bring one up are:

  * Playwright:   core.chrome_launch.safe_launch / resolve_launch  (fail-closes
                  to headless, forces the survivor fully off-screen); OR
  * raw binary:   core.chrome_launch.launch_headful_offscreen(cmd, port=...)
                  (spawns via `open -gjn` — never activates — parks off-screen).

This checker FAILS (exit 1) on any of the following outside the sanctioned
chokepoint (core/chrome_launch.py), unless the offending line carries a trailing
`# headful-ok` marker (reserved for genuinely-interactive, TTY-gated human-login
tools that legitimately open a focusable window only while a human is driving):

  A. `chromium.launch(... headless=False ...)`               — raw headful launch
  B. `launch_persistent_context(...)` that is not provably    — persistent_context
     headless (headless=True literal absent)                    defaults HEADFUL
  C. `subprocess.Popen/run/call([CHROME*, ...])` with no       — raw binary exec
     `--headless` flag                                           (activates!)

Forms B and C were the long-standing blind spots: the old checker only matched a
literal `headless=False` on the call line, so every `headless=<var>` and every
raw `[CHROME_BIN, ...]` subprocess spawn (the CDP-profile launchers) slipped
through and kept jacking the foreground.

Wire into invariants_guard / CI:  python3 tools/check_no_headful_steal.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".venv", "node_modules", "site-packages", "dist", "build", ".git", ".claude"}
# The chokepoint itself is the ONE place allowed to exec the binary / launch headful.
SKIP_FILES = {"core/chrome_launch.py", "tools/check_no_headful_steal.py"}

OK_MARKER = "# headful-ok"
# how many following lines to scan for kwargs of a multi-line launch/spawn call
WINDOW = 8

LAUNCH_RE = re.compile(r"\.launch\s*\(")
PERSIST_RE = re.compile(r"\.launch_persistent_context\s*\(")
# a subprocess spawn whose first argv element is a Chrome binary constant
SPAWN_RE = re.compile(
    r"subprocess\.(?:Popen|run|call)\s*\(\s*\[\s*(CHROME|CHROME_BIN|ZOOM_CHROME|chrome_bin)\b"
)
# also catch the bare `_sp.Popen([CHROME...` alias form
SPAWN_ALIAS_RE = re.compile(
    r"\b\w+\.(?:Popen|run|call)\s*\(\s*\[\s*(CHROME|CHROME_BIN|ZOOM_CHROME|chrome_bin)\b"
)

HEADLESS_TRUE = re.compile(r"headless\s*=\s*True\b")
HEADLESS_FALSE = re.compile(r"headless\s*=\s*False\b")
HEADLESS_ANY = re.compile(r"headless\s*=")


def _window(lines: list[str], i: int) -> str:
    return "\n".join(lines[i : i + WINDOW])


def main() -> int:
    violations: list[str] = []
    for f in sorted(ROOT.rglob("*.py")):
        rel = f.relative_to(ROOT).as_posix()
        if any(part in SKIP_DIRS or part.startswith(".chrome-cdp-profile") for part in f.parts):
            continue
        if rel in SKIP_FILES:
            continue
        try:
            lines = f.read_text(errors="ignore").split("\n")
        except Exception:
            continue
        for i, line in enumerate(lines):
            if OK_MARKER in line:
                continue
            win = _window(lines, i)

            # ── A: raw Playwright chromium.launch(... headless=False ...) ──────
            if LAUNCH_RE.search(line) and "launch_persistent_context" not in line:
                if HEADLESS_FALSE.search(win):
                    violations.append(f"{rel}:{i+1}: [launch headless=False] {line.strip()[:90]}")
                continue

            # ── B: launch_persistent_context — defaults HEADFUL ───────────────
            if PERSIST_RE.search(line):
                # OK only if it is provably headless (explicit headless=True)
                if HEADLESS_TRUE.search(win):
                    continue
                kind = "headless=False" if HEADLESS_FALSE.search(win) else (
                    "no explicit headless (defaults HEADFUL)" if not HEADLESS_ANY.search(win)
                    else "headless=<non-literal>")
                violations.append(
                    f"{rel}:{i+1}: [persistent_context {kind}] {line.strip()[:90]}")
                continue

            # ── C: raw subprocess spawn of the Chrome binary (activates) ──────
            m = SPAWN_RE.search(line) or SPAWN_ALIAS_RE.search(line)
            if m:
                if "--headless" in win:
                    continue  # headless=new renders no window -> never foregrounds
                violations.append(
                    f"{rel}:{i+1}: [raw {m.group(1)} spawn, no --headless] {line.strip()[:90]}")
                continue

    if violations:
        print("NO-FOREGROUND-STEAL INVARIANT VIOLATED — unguarded headful Chrome:")
        for v in violations:
            print("  " + v)
        print(
            "\nFix each by routing through core.chrome_launch:"
            "\n  * Playwright headful  -> safe_launch()/resolve_launch()"
            "\n  * persistent context  -> spawn off-screen CDP + connect_over_cdp,"
            "\n                           or pass headless=True if no window is needed"
            "\n  * raw [CHROME,...]     -> launch_headful_offscreen(cmd, port=...)"
            "\nor, for a genuinely-interactive TTY-gated human login only, append"
            f" '{OK_MARKER}' to the line.")
        return 1
    print("no-foreground-steal invariant: OK (no unguarded headful Chrome launches)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
