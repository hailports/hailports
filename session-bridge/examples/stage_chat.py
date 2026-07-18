#!/usr/bin/env python3
"""Example chat connector.

Same contract as stage_email.py: read a job on stdin, stage it, print a JSON
result on stdout. The reference build writes the message to a local drafts
folder; replace `stage()` with a call into your own chat system (API, webhook,
local bridge). Kept deliberately minimal so it's obvious where your code goes.
"""
import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path


def stage(job: dict, drafts: Path) -> dict:
    body = str(job.get("body", "")).strip()
    to = str(job.get("to", "")).strip()
    if not (body and to):
        raise ValueError("chat job needs 'to' and 'body'")
    drafts.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", str(job.get("id", "draft")))
    path = drafts / f"{safe}.chat.txt"
    path.write_text(
        f"To: {to}\nStaged: {datetime.now(timezone.utc).isoformat()}\n\n{body}\n"
    )
    return {"staged_to": str(path), "to": to, "chars": len(body)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--drafts", default="~/bridge-drafts")
    args = ap.parse_args()
    job = json.load(sys.stdin)
    print(json.dumps(stage(job, Path(args.drafts).expanduser())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
