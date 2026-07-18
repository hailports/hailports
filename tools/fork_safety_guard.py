#!/usr/bin/env python3
"""Re-apply the macOS fork-safety shim to the active Homebrew sitecustomize.py.

`brew upgrade python@3.14` overwrites sitecustomize.py, which would silently bring
back the SIGSEGV "crashed on child side of fork pre-exec" crashes. Run this on a
schedule (and after upgrades) to guarantee the env-var block is present. Idempotent.
"""
from __future__ import annotations

import sys
from pathlib import Path

MARKER = "claude-stack fork-safety shim"
BLOCK = '''
# --- claude-stack fork-safety shim (idempotent) -------------------------------
import os as _os
_os.environ.setdefault("OBJC_DISABLE_INITIALIZE_FORK_SAFETY", "YES")
_os.environ.setdefault("no_proxy", "*")
_os.environ.setdefault("NO_PROXY", "*")
# -----------------------------------------------------------------------------
'''


def _sitecustomize_path() -> Path | None:
    for p in sys.path:
        cand = Path(p) / "sitecustomize.py"
        if cand.exists() and "python3.14" in str(cand) and "site-packages" not in str(cand):
            return cand
    # fall back: the stdlib lib dir
    import sysconfig
    cand = Path(sysconfig.get_path("stdlib")) / "sitecustomize.py"
    return cand if cand.exists() else None


def main() -> int:
    target = _sitecustomize_path()
    if not target:
        print("[fork-guard] no homebrew sitecustomize.py found — nothing to patch")
        return 0
    txt = target.read_text(encoding="utf-8")
    if MARKER in txt:
        print(f"[fork-guard] already patched: {target}")
        return 0
    try:
        target.write_text(txt.rstrip() + "\n" + BLOCK, encoding="utf-8")
        print(f"[fork-guard] re-applied fork-safety block to {target}")
    except PermissionError:
        print(f"[fork-guard] PERMISSION DENIED writing {target} — run with adequate perms")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
