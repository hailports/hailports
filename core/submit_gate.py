#!/usr/bin/env python3
"""submit_gate — the human-gated SUBMISSION choke for the bounty/env autopilot lanes.

The design (owner 2026-07-03): for every lane whose ONLY remaining blocker is a manual
submit + prove-you're-human step (0din, Gray Swan, huntr, Prime Intellect, Opire), the
harness does ALL the automatable work + internal validation, then calls stage() with a
VALIDATED, ready-to-paste artifact. submit_gate stages it to disk and fires ONE high-signal
iMessage — "it's time to submit" — so Operator does the single human step. This is:
  * COMPLIANT: manual submission satisfies programs that BAN auto-submit (Gray Swan mandates
    manual; 0din/huntr take a human form). We never auto-submit; the human is the gate.
  * ANTI-SLOP: we only ever ping on an artifact that passed the lane's own validator, so
    Operator never wastes a submit on a reject, and we never poison a pseudonym with AI-slop.

Rails: NEVER calls osascript/iMessage directly — routes through core.alert_gateway (the
single alert choke). The est-value "$" token is what lets the money-gate deliver it to
iMessage; a submit-ready bounty is exactly the "a deal Operator must help close" case the
zero-noise policy allows.
"""
from __future__ import annotations
import hashlib, json, os, shutil, sys, argparse
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(os.path.expanduser("~/claude-stack"))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

QUEUE = ROOT / "data" / "hustle" / "bounty_submit_queue"
LEDGER = QUEUE / ".ledger.jsonl"
DESKTOP = Path(os.path.expanduser("~/Desktop"))


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _seen_keys() -> set[str]:
    if not LEDGER.exists():
        return set()
    keys = set()
    for line in LEDGER.read_text().splitlines():
        try:
            keys.add(json.loads(line)["key"])
        except Exception:
            pass
    return keys


def _alert(subject: str, body: str, key: str, dry: bool) -> bool:
    """Route through the alert choke (money-gated -> iMessage). dry = log only, never text."""
    if dry:
        print(f"[dry: alert suppressed] {subject}")
        return True
    try:
        from core import alert_gateway
        # critical + a "$" token in the subject = the 'deal Operator must close' path -> iMessage.
        alert_gateway.route("critical", "bounty-submit", subject, body, issue_key=key)
        return True
    except Exception as e:  # never let a broken alert leg lose the staged artifact
        (QUEUE / ".alert_errors.log").parent.mkdir(parents=True, exist_ok=True)
        with (QUEUE / ".alert_errors.log").open("a") as fh:
            fh.write(f"{_now()} {key} alert-failed: {e}\n")
        return False


def stage(lane: str, title: str, where_url: str, paste_content: str,
          *, evidence: str = "", est_value_usd=None, key: str | None = None,
          meta: dict | None = None, files=None, dry: bool = False) -> dict | None:
    """Queue a VALIDATED artifact, drop the COMPLETE package as a ZIP on the Desktop, and ping
    Operator once. Returns the record (None if duplicate). Callers pass ONLY validated work + any
    attachments (PoC files, env dir, code, screenshots) via `files` so the ZIP is submit-complete."""
    key = key or hashlib.sha256(f"{lane}|{title}|{paste_content[:400]}".encode()).hexdigest()[:20]
    if key in _seen_keys():
        return None  # already staged + pinged; never re-nag
    sid = key[:10]
    d = QUEUE / lane / sid
    d.mkdir(parents=True, exist_ok=True)
    est = f"${est_value_usd}" if est_value_usd not in (None, "") else "$?"
    (d / "SUBMIT_THIS.md").write_text(
        f"# SUBMIT THIS → {lane}\n\n"
        f"- what: {title}\n- where: {where_url}\n- est reward: {est}\n- staged: {_now()}\n"
        f"- validation: {evidence or '(see meta.json)'}\n\n"
        f"## paste / attach exactly this\n\n{paste_content}\n"
    )
    for f in (files or []):                       # bundle the actual baked work into the package
        src = Path(os.path.expanduser(str(f)))
        try:
            if src.is_dir():
                shutil.copytree(src, d / src.name, dirs_exist_ok=True)
            elif src.exists():
                shutil.copy2(src, d / src.name)
        except Exception:
            pass
    zip_path = ""                                 # the grab-and-submit artifact, dropped on the Desktop
    try:
        DESKTOP.mkdir(parents=True, exist_ok=True)
        safe = "".join(c if (c.isalnum() or c in "-_") else "-" for c in title)[:40].strip("-")
        zip_path = shutil.make_archive(str(DESKTOP / f"SUBMIT-{lane}-{safe or sid}-{sid}"), "zip", root_dir=str(d))
    except Exception:
        pass
    rec = {"key": key, "sid": sid, "lane": lane, "title": title, "where": where_url,
           "est_value_usd": est_value_usd, "staged_at": _now(), "submitted": False,
           "path": str(d / "SUBMIT_THIS.md"), "zip": zip_path, "meta": meta or {}}
    (d / "meta.json").write_text(json.dumps(rec, indent=2))
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a") as fh:
        fh.write(json.dumps(rec) + "\n")
    subject = f"🎯 submit-ready {est} → {lane}: {title[:80]}"
    zname = Path(zip_path).name if zip_path else "(see queue)"
    body = (f"validated + packaged. your one step: submit manually (prove-human).\n"
            f"reward est {est}. where: {where_url}\n📦 ZIP on your Desktop: {zname}")
    _alert(subject, body, key, dry)
    return rec


def pending() -> list[dict]:
    out = []
    seen = {}
    if LEDGER.exists():
        for line in LEDGER.read_text().splitlines():
            try:
                r = json.loads(line); seen[r["key"]] = r
            except Exception:
                pass
    for r in seen.values():
        mp = Path(r["path"]).parent / "meta.json"
        if mp.exists():
            try:
                r = json.loads(mp.read_text())
            except Exception:
                pass
        if not r.get("submitted"):
            out.append(r)
    return sorted(out, key=lambda r: r.get("staged_at", ""), reverse=True)


def mark_submitted(sid: str) -> bool:
    for mp in QUEUE.glob(f"*/{sid}/meta.json"):
        r = json.loads(mp.read_text())
        r["submitted"] = True
        r["submitted_at"] = _now()
        mp.write_text(json.dumps(r, indent=2))
        return True
    return False


def _cli():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("list")
    d = sub.add_parser("done"); d.add_argument("sid")
    st = sub.add_parser("selftest")
    args = ap.parse_args()
    if args.cmd == "list":
        for r in pending():
            print(f"  [{r['sid']}] {r['lane']}: {r['title']} (est ${r.get('est_value_usd','?')}) -> {r['where']}")
        if not pending():
            print("  (nothing pending)")
    elif args.cmd == "done":
        print("marked" if mark_submitted(args.sid) else "not found")
    elif args.cmd == "selftest":
        r = stage("selftest", "dummy validated finding", "https://example.com/submit",
                  "PASTE-ME payload", evidence="validator=OK", est_value_usd=250, dry=True)
        assert r and Path(r["path"]).exists() and r.get("zip") and Path(r["zip"]).exists(), "stage/zip failed"
        assert stage("selftest", "dummy validated finding", "https://example.com/submit",
                     "PASTE-ME payload", est_value_usd=250, dry=True) is None, "dedup failed"
        assert any(p["sid"] == r["sid"] for p in pending()), "pending missing"
        assert mark_submitted(r["sid"]) and all(p["sid"] != r["sid"] for p in pending()), "mark failed"
        # cleanup selftest artifacts (queue + desktop zip + ledger)
        shutil.rmtree(QUEUE / "selftest", ignore_errors=True)
        if r.get("zip") and Path(r["zip"]).exists():
            os.remove(r["zip"])
        lines = [l for l in LEDGER.read_text().splitlines() if '"lane": "selftest"' not in l] if LEDGER.exists() else []
        LEDGER.write_text("\n".join(lines) + ("\n" if lines else ""))
        print("✅ submit_gate selftest passed (stage/dedup/pending/mark/ZIP-on-desktop/cleanup)")
    else:
        ap.print_help()


if __name__ == "__main__":
    _cli()
