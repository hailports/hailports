#!/usr/bin/env python3
"""Example email connector.

Reads a session-bridge job on stdin, "stages" it as a draft, and prints a JSON
result on stdout. This reference version writes the draft to a local folder so
you can see the whole loop work with no external accounts. Swap the body of
`stage()` for however you actually create a draft in your own mail system --
an API call, a local scripting bridge, an SMTP "save to Drafts", etc. The
contract the worker cares about is only: read JSON stdin, print JSON stdout,
exit 0 on success.
"""
import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path


def apply_style(body: str, style_dir: Path | None) -> str:
    """Hook for house-style enforcement. Reference build only trims trailing
    whitespace; wire your own normalizer here (signature, casing, name
    preferences) so every staged draft comes out consistent."""
    body = "\n".join(line.rstrip() for line in body.splitlines()).strip()
    return body


def stage(job: dict, drafts: Path, style_dir: Path | None) -> dict:
    body = apply_style(str(job.get("body", "")), style_dir)
    if not body:
        raise ValueError("empty body")
    drafts.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", str(job.get("id", "draft")))
    path = drafts / f"{safe}.email.txt"
    header = [
        f"To: {job.get('to','')}",
        f"Cc: {job.get('cc','')}",
        f"Subject: {job.get('subject','')}",
        f"Action: {job.get('action','reply')}",
        f"Thread: {job.get('thread','')}",
        f"Staged: {datetime.now(timezone.utc).isoformat()}",
        "",
    ]
    path.write_text("\n".join(header) + body + "\n")
    return {"staged_to": str(path), "chars": len(body)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--drafts", default="~/bridge-drafts")
    ap.add_argument("--style", default="", help="optional house-style dir")
    args = ap.parse_args()
    job = json.load(sys.stdin)
    drafts = Path(args.drafts).expanduser()
    style = Path(args.style).expanduser() if args.style else None
    print(json.dumps(stage(job, drafts, style)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
