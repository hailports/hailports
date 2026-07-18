#!/usr/bin/env python3
"""gen_runaway_allowlist.py — auto-derive runaway_guard's allowlist from the
launchd-scheduled + known-good stack job set, so legit scheduled work is NEVER
false-killed and nobody has to hand-maintain ~/.runaway-guard.allow.

Why: on 2026-07-13 runaway_guard killed 3 legit scheduled jobs
(maroon_salesintel_importer, hailports_nerd_leads, account_registrar) — bounded
CPU-bound batch importers whose RSS is flat while they process a stream, so the
runaway classifier's flat-RSS short fuse mistook them for busy-loops. Every one of
these is a real launchd/coordinator job, not a runaway. The durable fix is to treat
the set of scheduled jobs as authoritative-legit and exempt them from the CPU kill
(their own `timeout N` wrappers / quarantine-disable remain the backstop for a job
that truly hangs). Anything NOT in this set — an orphaned/unregistered spinner, a
system daemon, headless chrome — is still fully killable, so true-runaway protection
is intact.

What it does: parse every ~/Library/LaunchAgents/com.claude-stack.*.plist, pull the
stack script identity from ProgramArguments (`-m agents.x` -> `agents.x`;
`.../agents/x.py` -> `agents/x.py`), union with KNOWN_GOOD (coordinator-launched jobs
that have no plist of their own), and write them into a clearly-marked managed block
in ~/.runaway-guard.allow. Hand-added tokens OUTSIDE the block are preserved verbatim.

is_allowlisted() does a substring test (`token in command`), so these path/module
tokens match the running command regardless of interpreter path or wrapper.

  python3 tools/gen_runaway_allowlist.py            # rewrite the managed block
  python3 tools/gen_runaway_allowlist.py --print    # show derived tokens, write nothing
"""
from __future__ import annotations

import argparse
import plistlib
import re
import sys
from pathlib import Path

HOME = Path.home()
LA_DIR = HOME / "Library" / "LaunchAgents"
ALLOW_FILE = HOME / ".runaway-guard.allow"
BEGIN = "# >>> auto-derived launchd allowlist (gen_runaway_allowlist.py) — do not edit inside >>>"
END = "# <<< auto-derived launchd allowlist <<<"

# Coordinator-launched legit jobs that have NO plist of their own (spawned by an
# orchestrator, usually `timeout N .venv/bin/python3 -m agents.x`). Confirmed legit.
KNOWN_GOOD = (
    "agents.maroon_salesintel_importer",
    "agents/maroon_salesintel_importer.py",
)

_MODULE_RE = re.compile(r"-m\s+((?:agents|tools|core|apps)\.[\w.]+)")
_PATH_RE = re.compile(r"((?:agents|tools|core|apps|scripts|products)/[\w./-]+\.py)")


def _identity_from_args(argv: str) -> str | None:
    m = _MODULE_RE.search(argv)
    if m:
        return m.group(1)
    m = _PATH_RE.search(argv)
    if m:
        return m.group(1)
    return None


def derive_tokens() -> list[str]:
    tokens: set[str] = set(KNOWN_GOOD)
    if LA_DIR.exists():
        for pl in LA_DIR.glob("com.claude-stack.*.plist"):
            try:
                d = plistlib.load(open(pl, "rb"))
            except Exception:
                continue
            argv = " ".join(str(a) for a in d.get("ProgramArguments", []))
            tok = _identity_from_args(argv)
            if tok:
                tokens.add(tok)
    return sorted(tokens)


def _split_preserved(text: str) -> list[str]:
    """Lines OUTSIDE the managed block (hand-maintained tokens), block stripped."""
    lines = text.splitlines()
    out, skip = [], False
    for ln in lines:
        if ln.strip() == BEGIN:
            skip = True
            continue
        if ln.strip() == END:
            skip = False
            continue
        if not skip:
            out.append(ln)
    while out and not out[-1].strip():
        out.pop()
    return out


def write_allowlist(tokens: list[str]) -> tuple[int, int]:
    existing = ALLOW_FILE.read_text() if ALLOW_FILE.exists() else ""
    preserved = _split_preserved(existing)
    body = list(preserved)
    if body and body[-1].strip():
        body.append("")
    body.append(BEGIN)
    body.extend(tokens)
    body.append(END)
    ALLOW_FILE.write_text("\n".join(body) + "\n")
    return len(preserved), len(tokens)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--print", action="store_true", dest="show")
    args = ap.parse_args()
    tokens = derive_tokens()
    if args.show:
        for t in tokens:
            print(t)
        print(f"# {len(tokens)} derived tokens (no write)", file=sys.stderr)
        return 0
    kept, n = write_allowlist(tokens)
    print(f"wrote {n} auto-derived tokens (+{kept} preserved hand tokens) -> {ALLOW_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
