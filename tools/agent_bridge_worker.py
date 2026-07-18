#!/usr/bin/env python3
"""OneDrive -> local drafting bridge.

Remote machines (codex/claude on the other mac) drop a job JSON into the
OneDrive `agent-bridge/inbox`. This worker -- which lives on the mac that
actually holds the Zoom/Outlook runtimes -- picks it up, runs the REAL local
drafting pipeline (voice normalization, canonical sig, name_prefs, in-thread
reply-all, keep-unread), stages the draft, and writes a result back to
`outbox`. Nothing is ever sent; staging only.

Job schema (inbox/<id>.job.json):
  {
    "id":        "<uuid>",                 # required, unique
    "channel":   "outlook" | "zoom",       # required
    "action":    "reply" | "new",          # outlook only; default reply
    "match":     "sender name or subject",  # outlook reply: find the thread
    "to":        "email or zoom contact",   # outlook new / zoom
    "cc":        "user@example.com",             # optional
    "subject":   "...",                     # outlook new
    "body":      "the note, in Operator's voice", # required
    "reply_all": true,                       # outlook, default true
    "origin":    "workbook-codex"            # free label for the result
  }

The body should already be in Operator's voice (the remote prompt teaches it).
This worker re-applies name_prefs on the greeting and hands the body to the
local tools, which own final formatting/sig/threading.
"""
import json, os, re, sys, time, traceback, subprocess
from datetime import datetime, timezone
from pathlib import Path

STACK = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(STACK))

BRIDGE = Path(os.path.expanduser(
    "~/Library/CloudStorage/OneDrive-Operator.com/agent-bridge"))
INBOX, OUTBOX = BRIDGE / "inbox", BRIDGE / "outbox"
PROCESSED, FAILED = BRIDGE / "processed", BRIDGE / "failed"
for d in (INBOX, OUTBOX, PROCESSED, FAILED):
    d.mkdir(parents=True, exist_ok=True)

POLL = int(os.environ.get("BRIDGE_POLL", "20"))


def _now():
    return datetime.now(timezone.utc).isoformat()


def _apply_name_pref(body: str) -> str:
    """Swap a greeting first-name for its preferred form (Fernanda->Fern)."""
    try:
        from core import name_prefs as np
    except Exception:
        return body
    m = re.match(r"^(\s*(?:hi|hey|hello|thanks|thx)\s+)([A-Z][a-z]+)\b",
                 body, re.IGNORECASE)
    if not m:
        return body
    pref = np.preferred(m.group(2))
    if pref and pref.lower() != m.group(2).lower():
        return body[:m.start(2)] + pref + body[m.end(2):]
    return body


def _do_outlook(job: dict) -> dict:
    from tools import outlook_app as oa
    body = _apply_name_pref(job.get("body", "").strip())
    if not body:
        raise ValueError("empty body")
    cc = job.get("cc", "")
    if (job.get("action", "reply") == "new") or not job.get("match"):
        r = oa.create_draft(to=job.get("to", ""), cc=cc,
                            subject=job.get("subject", ""), body=body)
    else:
        r = oa.reply_draft(match=job["match"], body=body, cc=cc,
                           reply_all=bool(job.get("reply_all", True)),
                           keep_unread=True)
    return {"tool": "outlook", "result": r,
            "message_id": (r or {}).get("message_id") or (r or {}).get("id")}


def _do_zoom(job: dict) -> dict:
    to, text = job.get("to", ""), _apply_name_pref(job.get("body", "").strip())
    if not (to and text):
        raise ValueError("zoom needs 'to' and 'body'")
    # no --send: types/stages the message into the channel, never fires
    p = subprocess.run(
        [str(STACK / ".venv/bin/python"), str(STACK / "tools/zoom_send.py"),
         "--to", to, "--text", text],
        capture_output=True, text=True, timeout=180)
    return {"tool": "zoom", "returncode": p.returncode,
            "stdout": p.stdout[-2000:], "stderr": p.stderr[-1000:]}


def _write_result(job_id, origin, status, payload, preview=""):
    out = {"id": job_id, "origin": origin, "status": status,
           "finished": _now(), "preview": preview, **payload}
    (OUTBOX / f"{job_id}.result.json").write_text(
        json.dumps(out, indent=2, ensure_ascii=False))


def process(path: Path):
    try:
        job = json.loads(path.read_text())
    except Exception as e:
        path.rename(FAILED / path.name)
        _write_result(path.stem, "?", "error",
                      {"error": f"bad json: {e}"})
        return
    jid = job.get("id") or path.stem
    origin = job.get("origin", "?")
    try:
        ch = job.get("channel")
        payload = _do_outlook(job) if ch == "outlook" else \
            _do_zoom(job) if ch == "zoom" else None
        if payload is None:
            raise ValueError(f"unknown channel: {ch!r}")
        _write_result(jid, origin, "staged", payload,
                      preview=job.get("body", "")[:400])
        path.rename(PROCESSED / path.name)
        print(f"[{_now()}] staged {jid} ({ch}) from {origin}")
    except Exception as e:
        _write_result(jid, origin, "error",
                      {"error": str(e), "trace": traceback.format_exc()[-1500:]},
                      preview=job.get("body", "")[:400])
        path.rename(FAILED / path.name)
        print(f"[{_now()}] FAILED {jid}: {e}", file=sys.stderr)


def main():
    once = "--once" in sys.argv
    print(f"[{_now()}] agent_bridge_worker up; watching {INBOX} (poll {POLL}s)")
    while True:
        for p in sorted(INBOX.glob("*.job.json")):
            process(p)
        if once:
            break
        time.sleep(POLL)


if __name__ == "__main__":
    main()
