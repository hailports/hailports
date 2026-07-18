"""admin_action_agent — approval-gated Salesforce access-request handler.

Detects access requests (activate user / sandbox access / permission) in an inbound
message, verifies the target's REAL status across orgs (read-only), and — only on an
EXPLICIT approval — executes the admin change audit-clean (no AI trace, no chatter),
then produces a confirmation reply for staging.

HARD SAFETY: prod / access changes NEVER run autonomously. execute() fires only for a
pending action whose status was flipped to "approved" by the human approval path
(zoom `approve <key>`). Sandbox no-ops and user CREATION are flagged for the human, not
auto-done (license/role/profile are human calls).
"""
from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
PENDING = ROOT / "data" / "runtime" / "admin_actions_pending.json"
PROD_ORG = "vrm_prod"
SANDBOX_ORGS = ["fullsand_live", "partial", "tavantfull"]

# deterministic access-request classifier (no LLM) — keeps it $0 + auditable
_ACCESS_RE = re.compile(
    r"\b(activate|reactivat\w*|inactive|(grant|need|request)\w*\s+access|"
    r"permission set|create (a )?user|user for me|can'?t log ?in|enable my|my (user|access|login))\b",
    re.I,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sf(args: list[str], timeout: int = 90) -> dict[str, Any]:
    try:
        r = subprocess.run(["sf", *args, "--json"], capture_output=True, text=True, timeout=timeout)
        if r.stdout.strip():
            return json.loads(r.stdout)
        return {"error": (r.stderr or "no output")[:300]}
    except Exception as e:  # never raise into the caller
        return {"error": str(e)[:300]}


def _query(org: str, soql: str) -> list[dict[str, Any]]:
    d = _sf(["data", "query", "--query", soql, "--target-org", org])
    if isinstance(d, dict) and not d.get("error"):
        return d.get("result", {}).get("records", []) or []
    return []


def is_access_request(text: str) -> bool:
    return bool(text and _ACCESS_RE.search(text))


def _esc(s: str) -> str:
    return s.replace("'", r"\'")


def verify_targets(name: str) -> dict[str, Any]:
    """Read-only status of the person's User across prod + sandboxes."""
    soql = ("SELECT Id, Name, Username, IsActive, Profile.Name, LastLoginDate "
            f"FROM User WHERE Name LIKE '%{_esc(name)}%' OR Username LIKE '%{_esc(name)}%'")
    out: dict[str, Any] = {}
    for org in [PROD_ORG, *SANDBOX_ORGS]:
        recs = _query(org, soql)
        if recs:
            r = recs[0]
            out[org] = {
                "id": r.get("Id"),
                "username": r.get("Username"),
                "active": r.get("IsActive"),
                "profile": (r.get("Profile") or {}).get("Name"),
                "last_login": r.get("LastLoginDate"),
            }
        else:
            out[org] = None
    return out


def sso_maps_federation_id() -> bool:
    """Read-only proof that CompanyA SAML resolves NameID against User.FederationIdentifier."""
    rows = _query(
        PROD_ORG,
        "SELECT IdentityMapping, IdentityLocation FROM SamlSsoConfig WHERE DeveloperName='sso' LIMIT 1",
    )
    return bool(rows and rows[0].get("IdentityMapping") == "FederationId")


# ── read-first status answer (the "do the work before drafting" path) ─────────
# A person's access/login STATUS question ("can marie log in?", "check on X's access",
# "is my user active?") is answerable with a READ, not a change. This resolves the
# subject to a real User and returns a DELIVERED, fact-grounded reply. Change verbs
# (activate/grant/reset/create) return None so the approval-gated change path handles it.

_CHANGE_VERB_RE = re.compile(
    r"\b(activat|reactivat|grant|creat|reset|enabl|deactivat|disabl|remov|add\s+(a\s+)?(perm|role|profile))",
    re.I)
_STATUS_CUE_RE = re.compile(
    r"\b(can'?t log ?in|cannot log ?in|can't get in|locked out|access|login|log in|"
    r"is\s+\w+\s+active|status|able to log)\b", re.I)
_STOP = {"the","and","for","her","his","him","she","can","you","your","this","that","access",
         "login","user","account","please","hey","hi","team","help","issue","status","still",
         "with","from","into","about","marie","'s"}  # 'marie' NOT here — see subject extraction


def _fmt_login(iso: str | None) -> str:
    if not iso:
        return "no login on record"
    try:
        d = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return f"last login {d:%b %-d} at {d:%H:%M} UTC"
    except Exception:
        return f"last login {str(iso)[:16]}"


def _subject_names(text: str, sender: str | None) -> list[str]:
    """Who is the access question ABOUT. Possessive first ('marie's access'), then
    'for/on <Name>', then capitalized names, then first-person -> the sender."""
    names: list[str] = []
    for m in re.finditer(r"\b([a-z]{2,})(?:'s|’s)\s+(?:access|login|user|account)", text, re.I):
        names.append(m.group(1))
    for m in re.finditer(r"\b(?:for|on|with)\s+([a-z]{2,})\b", text, re.I):
        if m.group(1).lower() not in _STOP:
            names.append(m.group(1))
    if not names:
        for w in re.findall(r"\b([A-Z][a-z]{2,})\b", text):
            if w.lower() not in _STOP and (not sender or w.lower() not in sender.lower()):
                names.append(w)
    names = [n[:1].upper() + n[1:].lower() for n in names]
    if not names and re.search(r"\b(i can'?t|my (access|login|user|account)|let me in)\b", text, re.I):
        if sender:
            names.append(sender.split(",")[-1].strip().split()[0] if "," in sender else sender.split()[0])
    seen, out = set(), []
    for n in names:
        k = n.lower()
        if k not in seen:
            seen.add(k); out.append(n)
    return out[:3]


def access_status_answer(text: str, sender: str | None = None) -> str | None:
    """Delivered, fact-grounded reply to a person's access/login STATUS question, or None.
    None => not a status question, a CHANGE request, or the subject didn't resolve (fall through)."""
    if not text or _CHANGE_VERB_RE.search(text) or not _STATUS_CUE_RE.search(text):
        return None
    resolved: list[tuple[str, dict]] = []
    for name in _subject_names(text, sender):
        prod = verify_targets(name).get(PROD_ORG)
        if prod:
            resolved.append((name, prod))
    if not resolved:
        return None
    parts: list[str] = []
    for name, p in resolved:
        first = name.split()[0].lower()
        if p["active"]:
            parts.append(
                f"just checked {first}'s prod user ({p['username']}) -- it's active, "
                f"{p['profile']} profile, {_fmt_login(p['last_login'])}. the account's fine, "
                f"so if {first}'s still stuck it's a password/SSO reset not the user itself -- "
                f"want me to trigger a reset?")
        else:
            parts.append(
                f"checked {first}'s prod user ({p['username']}) -- it's INACTIVE right now "
                f"({p['profile']} profile, {_fmt_login(p['last_login'])}). i can reactivate it on your ok.")
    return " ".join(parts)


def build_plan(name: str, request_text: str, approval_key: str) -> dict[str, Any]:
    """Turn a verified access request into a concrete, gated action plan."""
    targets = verify_targets(name)
    prod = targets.get(PROD_ORG)
    actions, notes = [], []

    if prod and prod["active"] is False:
        actions.append({"org": PROD_ORG, "kind": "activate_user",
                        "record_id": prod["id"], "username": prod["username"],
                        "profile": prod["profile"]})
    elif prod and prod["active"]:
        notes.append(f"prod user already active ({prod['username']})")
    elif not prod:
        notes.append("no prod user found — CREATION is a human call (license/role); flagged, not auto-done")

    for sb in SANDBOX_ORGS:
        t = targets.get(sb)
        if t and t["active"]:
            notes.append(f"{sb}: already active ({t['username']}) — no action")
        elif t and t["active"] is False:
            notes.append(f"{sb}: user exists but INACTIVE — flag for human (sandbox activate on request)")

    return {
        "approval_key": approval_key,
        "person": name,
        "request": request_text[:200],
        "targets": targets,
        "actions": actions,
        "notes": notes,
        "status": "pending",
        "created": _now(),
    }


def build_sso_mapping_plan(*, user_id: str, person: str, current: str, target: str,
                           approval_key: str, request_text: str = "") -> dict[str, Any]:
    """Build the only allowed SSO mapping mutation: one known User, one internal email, rollback kept."""
    target = str(target or "").strip().lower()
    current = str(current or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9._%+-]+@CompanyA\.com", target, re.I):
        raise ValueError("SSO target must be an exact @CompanyA.com address")
    if not str(user_id).startswith("005") or len(str(user_id)) not in {15, 18}:
        raise ValueError("SSO target must be an exact Salesforce User id")
    if not current or current == target:
        raise ValueError("SSO mapping plan requires a real old -> new change")
    return {
        "approval_key": approval_key,
        "person": person,
        "request": request_text[:200],
        "targets": {PROD_ORG: {"id": user_id, "federation_identifier": current}},
        "actions": [{
            "org": PROD_ORG,
            "kind": "update_federation_identifier",
            "record_id": user_id,
            "before": current,
            "after": target,
            "rollback": {"field": "FederationIdentifier", "value": current},
        }],
        "notes": [f"verified mismatch: {current} -> {target}"],
        "status": "pending",
        "created": _now(),
    }


def approval_card(plan: dict[str, Any]) -> str:
    lines = [f"\U0001f510 access request — {plan['person']}"]
    if plan["actions"]:
        for a in plan["actions"]:
            if a["kind"] == "activate_user":
                lines.append(f"PROD user {a['username']} is INACTIVE. profile = {a['profile']} (privileged).")
    for n in plan["notes"]:
        lines.append(n)
    if plan["actions"]:
        verb = ", ".join(a["kind"].replace("_", " ") for a in plan["actions"])
        lines.append(f"\nproposed: {verb}, audit-clean.")
        lines.append(f"reply `approve {plan['approval_key']}` to proceed, or `no` to hold.")
    else:
        lines.append("\nno admin action needed (all access already in place).")
    return "\n".join(lines)


# ── pending store ───────────────────────────────────────────────────────────

def _load() -> dict[str, Any]:
    try:
        return json.loads(PENDING.read_text())
    except Exception:
        return {}


def _save(d: dict[str, Any]) -> None:
    PENDING.parent.mkdir(parents=True, exist_ok=True)
    PENDING.write_text(json.dumps(d, indent=2))


def enqueue(plan: dict[str, Any]) -> dict[str, Any]:
    d = _load()
    key = plan["approval_key"]
    existing = d.get(key)
    if existing and existing.get("status") in {"pending", "approved", "done"}:
        return existing
    d[key] = plan
    _save(d)
    return plan


def get_plan(approval_key: str) -> dict[str, Any] | None:
    return _load().get(approval_key)


def mark_approved(approval_key: str) -> bool:
    d = _load()
    if approval_key in d and d[approval_key]["status"] == "pending":
        d[approval_key]["status"] = "approved"
        d[approval_key]["approved_at"] = _now()
        _save(d)
        return True
    return False


def execute(approval_key: str) -> dict[str, Any]:
    """Run the admin actions for an APPROVED plan. Refuses unless status==approved."""
    d = _load()
    plan = d.get(approval_key)
    if not plan:
        return {"ok": False, "error": "no such pending action"}
    if plan["status"] != "approved":
        return {"ok": False, "error": f"not approved (status={plan['status']}) — refusing to touch SF"}

    results = []
    for a in plan["actions"]:
        if a["kind"] == "activate_user":
            upd = _sf(["data", "update", "record", "--sobject", "User",
                       "--record-id", a["record_id"], "--values", "IsActive=true",
                       "--target-org", a["org"]])
            ok = isinstance(upd, dict) and not upd.get("error")
            # verify
            ver = _query(a["org"], f"SELECT IsActive FROM User WHERE Id='{a['record_id']}'")
            active = ver[0].get("IsActive") if ver else None
            results.append({"action": a["kind"], "org": a["org"], "username": a["username"],
                            "ok": bool(ok and active), "verified_active": active,
                            "err": (upd.get("error") if isinstance(upd, dict) else None)})
        elif a["kind"] == "update_federation_identifier":
            before = _query(a["org"],
                            f"SELECT FederationIdentifier FROM User WHERE Id='{_esc(a['record_id'])}' LIMIT 1")
            observed = str((before[0] if before else {}).get("FederationIdentifier") or "").lower()
            if observed == str(a["after"]).lower():
                results.append({"action": a["kind"], "org": a["org"], "ok": True,
                                "verified_value": a["after"], "already_done": True,
                                "rollback": a.get("rollback")})
                continue
            if observed != str(a["before"]).lower():
                results.append({"action": a["kind"], "org": a["org"], "ok": False,
                                "err": f"mapping changed since approval ({observed!r}); refusing",
                                "rollback": a.get("rollback")})
                continue
            upd = _sf(["data", "update", "record", "--sobject", "User",
                       "--record-id", a["record_id"],
                       "--values", f"FederationIdentifier={a['after']}",
                       "--target-org", a["org"]])
            ver = _query(a["org"],
                         f"SELECT FederationIdentifier FROM User WHERE Id='{_esc(a['record_id'])}' LIMIT 1")
            actual = str((ver[0] if ver else {}).get("FederationIdentifier") or "").lower()
            ok = isinstance(upd, dict) and not upd.get("error") and actual == str(a["after"]).lower()
            results.append({"action": a["kind"], "org": a["org"], "ok": bool(ok),
                            "verified_value": actual or None,
                            "err": (upd.get("error") if isinstance(upd, dict) else None),
                            "rollback": a.get("rollback")})
    plan["status"] = "done" if all(r.get("ok") for r in results) else "failed"
    plan["results"] = results
    plan["done_at"] = _now()
    d[approval_key] = plan
    _save(d)
    return {"ok": all(r["ok"] for r in results) if results else True, "results": results, "plan": plan}


def reply_draft(plan: dict[str, Any]) -> str:
    """Operator-voice confirmation to the requester, staged (never auto-sent)."""
    first = plan["person"].split()[0].lower()
    done = [r for r in plan.get("results", []) if r.get("ok")]
    parts = [f"hey {first} -- you're all set."]
    for r in done:
        if r["action"] == "activate_user":
            parts.append(f"reactivated your prod user ({r['username']}) just now, "
                         f"{plan['actions'][0].get('profile','')} access is back.")
    if any("already active" in n for n in plan["notes"]):
        parts.append("your sandbox user was already active so nothing needed there.")
    parts.append("give it a try and lmk if anything's still off.")
    return " ".join(parts)


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="approval-gated SF access-request handler")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("plan"); p.add_argument("name"); p.add_argument("--key", required=True); p.add_argument("--request", default="")
    a = sub.add_parser("approve"); a.add_argument("key")
    e = sub.add_parser("execute"); e.add_argument("key")
    args = ap.parse_args()

    if args.cmd == "plan":
        pl = build_plan(args.name, args.request, args.key)
        enqueue(pl)
        print(approval_card(pl))
        print("\n[pending saved — awaiting `approve " + args.key + "`]")
    elif args.cmd == "approve":
        print("approved" if mark_approved(args.key) else "no pending action / already handled")
    elif args.cmd == "execute":
        res = execute(args.key)
        print(json.dumps(res.get("results"), indent=2))
        if res.get("ok"):
            print("\n--- staged reply (NOT sent) ---")
            print(reply_draft(res["plan"]))
    else:
        ap.print_help()
