#!/usr/bin/env python3
"""session-bridge — hand work off from a CLI agent to your own systems.

You run intense working sessions in the terminal (any coding agent, any
machine). When the agent produces something that belongs in a real system --
an email draft, a chat message, a ticket -- it shouldn't have to drive that
system directly, and often it can't reach it at all (the system lives on a
different machine, behind a login, or only speaks a native desktop API).

session-bridge decouples the two. The agent drops a small JSON *job* into a
queue directory. A worker -- running on whatever machine actually holds the
integration -- picks the job up, hands it to a *connector* for that channel,
and writes a result back. The queue directory can be anything the two sides
share: a synced cloud folder, an NFS mount, a shared git working tree.

Nothing here knows about any specific email client, chat app, or cloud
provider. A connector is just a command you configure. The worker passes the
job JSON on the command's stdin; the command stages the draft however it likes
and prints a JSON result on stdout. That's the whole contract, so the same
worker bridges to anything you can script.

    remote agent  ──drops──▶  <queue>/inbox/<id>.job.json
                                     │  (any shared/synced dir)
    worker (this)  ──runs──▶  connector command  ──stages──▶  your system
                                     │
                   ──writes──▶  <queue>/outbox/<id>.result.json

Config (TOML, see config.example.toml):

    queue = "~/some/shared/folder/bridge"
    poll_seconds = 20

    [connectors]
    email = "python3 ./examples/stage_email.py"
    chat  = "python3 ./examples/stage_chat.py"

Run:  python3 bridge.py --config config.toml
Once: python3 bridge.py --config config.toml --once   (single pass, for tests)
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

try:  # py3.11+ stdlib; fall back to a tiny parser if absent
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None

DEFAULT_POLL = 20
CONNECTOR_TIMEOUT = 180


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _expand(p: str) -> Path:
    return Path(os.path.expanduser(os.path.expandvars(p))).resolve()


def load_config(path: Path) -> dict:
    text = path.read_text()
    if tomllib is not None:
        cfg = tomllib.loads(text)
    else:
        cfg = _mini_toml(text)
    if "queue" not in cfg:
        raise ValueError("config needs a 'queue' path")
    cfg.setdefault("poll_seconds", DEFAULT_POLL)
    cfg.setdefault("connectors", {})
    return cfg


def _mini_toml(text: str) -> dict:
    """Minimal fallback: top-level key=val plus a single [connectors] table."""
    cfg: dict = {"connectors": {}}
    section = None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            continue
        if "=" not in line:
            continue
        k, v = (s.strip() for s in line.split("=", 1))
        v = v.strip('"').strip("'")
        if section == "connectors":
            cfg["connectors"][k] = v
        else:
            cfg[k] = int(v) if v.isdigit() else v
    return cfg


class Queue:
    def __init__(self, root: Path):
        self.root = root
        self.inbox = root / "inbox"
        self.outbox = root / "outbox"
        self.processed = root / "processed"
        self.failed = root / "failed"
        for d in (self.inbox, self.outbox, self.processed, self.failed):
            d.mkdir(parents=True, exist_ok=True)

    def pending(self):
        return sorted(self.inbox.glob("*.job.json"))

    def result(self, job_id: str, payload: dict):
        (self.outbox / f"{job_id}.result.json").write_text(
            json.dumps(payload, indent=2, ensure_ascii=False)
        )


def run_connector(command: str, job: dict) -> dict:
    """Run a connector command, feeding the job JSON on stdin.

    The command should print a JSON object on stdout. Anything it prints that
    isn't JSON is returned verbatim under 'output' so nothing is lost.
    """
    proc = subprocess.run(
        shlex.split(command),
        input=json.dumps(job),
        capture_output=True,
        text=True,
        timeout=CONNECTOR_TIMEOUT,
    )
    out = (proc.stdout or "").strip()
    parsed = None
    if out:
        try:
            parsed = json.loads(out)
        except json.JSONDecodeError:
            parsed = None
    result = {"returncode": proc.returncode}
    if parsed is not None:
        result["connector"] = parsed
    else:
        result["output"] = out[-4000:]
    if proc.stderr.strip():
        result["stderr"] = proc.stderr.strip()[-2000:]
    if proc.returncode != 0:
        raise RuntimeError(
            f"connector exited {proc.returncode}: {proc.stderr.strip()[:400]}"
        )
    return result


def process(path: Path, q: Queue, connectors: dict) -> None:
    job_id = path.stem.replace(".job", "")
    try:
        job = json.loads(path.read_text())
    except Exception as e:
        q.result(job_id, {"id": job_id, "status": "error", "finished": _now(),
                          "error": f"bad job json: {e}"})
        path.rename(q.failed / path.name)
        return

    job_id = str(job.get("id") or job_id)
    origin = job.get("origin", "?")
    channel = job.get("channel")
    try:
        command = connectors.get(channel)
        if not command:
            raise ValueError(
                f"no connector for channel {channel!r}; "
                f"configured: {sorted(connectors) or 'none'}"
            )
        payload = run_connector(command, job)
        q.result(job_id, {
            "id": job_id, "origin": origin, "channel": channel,
            "status": "staged", "finished": _now(),
            "preview": str(job.get("body", ""))[:400], **payload,
        })
        path.rename(q.processed / path.name)
        print(f"[{_now()}] staged {job_id} via {channel} (origin={origin})")
    except Exception as e:
        q.result(job_id, {
            "id": job_id, "origin": origin, "channel": channel,
            "status": "error", "finished": _now(),
            "error": str(e), "trace": traceback.format_exc()[-1500:],
            "preview": str(job.get("body", ""))[:400],
        })
        path.rename(q.failed / path.name)
        print(f"[{_now()}] FAILED {job_id}: {e}", file=sys.stderr)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="session-bridge worker")
    ap.add_argument("--config", required=True, help="path to config.toml")
    ap.add_argument("--once", action="store_true",
                    help="one pass over the inbox, then exit (for tests/cron)")
    args = ap.parse_args(argv)

    cfg = load_config(_expand(args.config))
    q = Queue(_expand(cfg["queue"]))
    connectors = dict(cfg["connectors"])
    poll = int(cfg["poll_seconds"])

    print(f"[{_now()}] session-bridge up; queue={q.root} "
          f"channels={sorted(connectors) or 'none'} poll={poll}s")
    while True:
        for p in q.pending():
            process(p, q, connectors)
        if args.once:
            return 0
        time.sleep(poll)


if __name__ == "__main__":
    raise SystemExit(main())
