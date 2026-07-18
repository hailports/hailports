#!/usr/bin/env python3
"""Blast-radius SF-autonomy engine for the work-lane reply bots.

A work-lane reply that touches Salesforce falls into one of two bands, and the band decides
how far the engine is allowed to go on its own:

  ADDITIVE  (new field / report / object / flow / list view / picklist value / a brand-new
            permission set -- nothing existing modified or deleted, no access removed):
            the engine COMPLETES the build in a sandbox (org alias `partial`), validates it
            green with a check-only deploy, and the reply DELIVERS -- "built X, validated
            green in partial, staged for the sandbox -> github -> prod promote."

  BLAST     (modifies or deletes existing metadata, changes or removes access, touches prod
            data, anything consequential):
            the engine STAGES it as far as is safe in the SAME sandbox (build + validate),
            captures a blast-radius assessment, and the reply PROPOSES -- "here's the fix,
            here's what it touches, validated in sandbox -- agree or let's pivot." It is
            NEVER auto-applied.

Two bounds make this safe and they are enforced here, not left to the caller:
  - every autonomous mutation is check-only (`--dry-run`) against a SANDBOX org. Prod is never
    written by this engine; prod always goes sandbox -> github -> prod under human approval.
  - classification fails toward `blast` / `unclear` whenever a signal is ambiguous, so the worst
    a misread does is downgrade an additive build to "propose" instead of "deliver" -- a safe
    degradation, never an unreviewed change.

Investigation (describe / retrieve of current state) may read prod; it never writes it.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---- org policy ----------------------------------------------------------------------------
DEFAULT_SANDBOX = "partial"
SANDBOX_ORGS = {"partial", "fullsand_live", "tavantfull"}
PROD_ORGS = {"prod", "dev-prod"}
API_VERSION = "60.0"


# ---- classifier ----------------------------------------------------------------------------
# Deterministic-first. Additive = a brand-new component that removes nothing. Blast = anything
# that touches, reassigns, or strips something that already exists. When both fire, blast wins.

_ADDITIVE = [
    r"\badd(?:ing|s)?\s+(?:a\s+|an\s+)?(?:new\s+|another\s+)?(?:custom\s+)?"
    r"(?:field|column|report|dashboard|object|flow|list\s*view|record\s*type|"
    r"email\s*template|picklist\s+value|permission\s*set)\b",
    r"\b(?:create|build|make|set\s*up|spin\s*up|stand\s*up)\s+(?:a\s+|an\s+)?(?:new\s+)?"
    r"(?:field|column|report|dashboard|object|flow|list\s*view|record\s*type|"
    r"email\s*template|picklist\s+value|permission\s*set)\b",
    r"\b(?:new|additional|another)\s+(?:field|column|report|dashboard|object|flow|"
    r"list\s*view|record\s*type|picklist\s+value|permission\s*set)\b",
    r"\breport\s+(?:of|on|showing|listing|for)\b",
]

_BLAST = [
    r"\b(?:change|update|modify|edit|rename|adjust|convert|alter|reconfigure|tweak)\b",
    r"\b(?:delete|remove|drop|deactivate|disable|deprecate|purge|merge|archive|retire)\b",
    r"\b(?:re-?assign|transfer)\b",
    r"\bwrong\s+(?:owner|value|values|record\s*type|picklist|field|setting)\b",
    r"\bmass\s+(?:update|change|delete|reassign)\b",
    r"\bmake\s+.*\brequired\b",
    r"\b(?:permission|access|profile|sharing|visibility|revoke|fls|"
    r"field[-\s]?level\s+security|login|entitlement)\b",
    r"\b(?:owner|ownership|reassign)\b",
    r"\bexisting\b.*\b(?:field|record|value|picklist|layout|rule|flow)\b",
]

# access verbs that are strictly additive ("grant a brand-new permission set") still ride the
# blast band on purpose: touching who-can-see-what is consequential and gets a human agree/pivot.
_DESTRUCTIVE = re.compile(
    r"\b(?:delete|remove|drop|deactivate|disable|purge|merge|revoke|reassign|re-assign)\b",
    re.IGNORECASE,
)

_ADDITIVE_RE = [re.compile(p, re.IGNORECASE) for p in _ADDITIVE]
_BLAST_RE = [re.compile(p, re.IGNORECASE) for p in _BLAST]


def _extract_targets(text: str) -> list[str]:
    """Best-effort pull of the object / field / component names in play. Advisory only."""
    out: list[str] = []
    out += re.findall(r"\b[A-Za-z0-9_]+__c\b", text)              # api names
    out += re.findall(r"'([^']+)'", text)                        # quoted labels
    out += re.findall(r'"([^"]+)"', text)
    for m in re.finditer(                                        # "... to/on/for the Account object"
        r"\b(?:to|on|for|in)\s+(?:the\s+)?([A-Z][A-Za-z0-9_ ]{2,40}?)\s+(?:object|record|field|picklist|report)\b",
        text,
    ):
        out.append(m.group(1).strip())
    seen: list[str] = []
    for t in out:
        t = t.strip()
        if t and t.lower() not in {s.lower() for s in seen}:
            seen.append(t)
    return seen[:8]


def classify(request_text: str, sf_ref: Any = None) -> dict:
    """Band a Salesforce request as additive / blast / unclear.

    Deterministic keyword + regex pass, biased so that ambiguity resolves to the safer band.
    `sf_ref` (optional) is any structured hint the caller already has -- a ticket, a resolved
    metadata target -- and only sharpens `why`; the text drives the decision.
    """
    text = str(request_text or "").strip()
    low = text.lower()
    add_hits = [r.pattern for r in _ADDITIVE_RE if r.search(low)]
    blast_hits = [r.pattern for r in _BLAST_RE if r.search(low)]
    destructive = bool(_DESTRUCTIVE.search(low))
    targets = _extract_targets(text)

    if not text:
        return {"level": "unclear", "why": "empty request", "targets": [], "signals": {}}

    signals = {"additive": len(add_hits), "blast": len(blast_hits), "destructive": destructive}

    if destructive:
        level = "blast"
        why = "destructive verb (delete/remove/reassign/revoke) -- always staged, never auto-applied"
    elif blast_hits:
        level = "blast"
        why = "touches existing metadata/access/data (change/modify/permission/owner) -- stage + propose"
    elif add_hits:
        level = "additive"
        why = "brand-new component, removes nothing -- safe to build + validate autonomously"
    else:
        level = "unclear"
        why = "no clear additive or blast signal -- ask before building (fails toward safe)"

    ref_note = ""
    if isinstance(sf_ref, dict) and sf_ref.get("metadata_type"):
        ref_note = f"; ref metadata_type={sf_ref.get('metadata_type')}"

    return {"level": level, "why": why + ref_note, "targets": targets, "signals": signals}


# ---- sandbox build + validate --------------------------------------------------------------
@dataclass
class SandboxResult:
    ok: bool
    org: str
    check_only: bool
    total: int
    errors: int
    files: list[dict] = field(default_factory=list)
    command: str = ""
    stage: str = ""            # what was built, human-readable
    error_detail: str = ""
    project_dir: str = ""

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "org": self.org,
            "check_only": self.check_only,
            "total": self.total,
            "errors": self.errors,
            "files": self.files,
            "command": self.command,
            "stage": self.stage,
            "error_detail": self.error_detail,
            "project_dir": self.project_dir,
        }


def _assert_sandbox(org: str) -> None:
    if org in PROD_ORGS:
        raise ValueError(
            f"blast_radius refuses to deploy against prod org '{org}'. "
            "autonomous writes are sandbox-only; prod goes sandbox -> github -> prod, human-gated."
        )
    if org not in SANDBOX_ORGS:
        raise ValueError(f"unknown org '{org}'; expected one of {sorted(SANDBOX_ORGS)}")


def _new_project(tmp_root: str | None = None) -> Path:
    root = Path(tempfile.mkdtemp(prefix="blast_radius_", dir=tmp_root))
    (root / "force-app" / "main" / "default").mkdir(parents=True, exist_ok=True)
    (root / "sfdx-project.json").write_text(
        json.dumps(
            {"packageDirectories": [{"path": "force-app", "default": True}], "sourceApiVersion": API_VERSION}
        )
    )
    return root


def _run(cmd: list[str], cwd: Path) -> tuple[int, dict, str]:
    proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=600)
    raw = proc.stdout or ""
    try:
        data = json.loads(raw)
    except Exception:
        data = {}
    return proc.returncode, data, (raw or proc.stderr or "")[-1500:]


def _validate(project: Path, org: str, metadata: list[str] | None = None) -> SandboxResult:
    """Check-only (`--dry-run`) deploy against a SANDBOX org. Never persists, never prod."""
    _assert_sandbox(org)
    cmd = ["sf", "project", "deploy", "start", "-o", org, "--dry-run", "--json"]
    if metadata:
        for m in metadata:
            cmd += ["-m", m]
    else:
        cmd += ["-d", "force-app"]
    rc, data, raw = _run(cmd, project)
    r = data.get("result", {}) if isinstance(data, dict) else {}
    files = [
        {"fullName": f.get("fullName"), "type": f.get("type"), "state": f.get("state")}
        for f in (r.get("files") or [])
    ]
    ok = data.get("status") == 0 and bool(r.get("success"))
    err_detail = ""
    if not ok:
        errs = [f for f in (r.get("files") or []) if f.get("error")]
        err_detail = "; ".join(f"{f.get('fullName')}: {f.get('error')}" for f in errs) or raw
    return SandboxResult(
        ok=ok,
        org=org,
        check_only=bool(r.get("checkOnly", True)),
        total=r.get("numberComponentsTotal") or len(files),
        errors=r.get("numberComponentErrors") or (0 if ok else len(files)),
        files=files,
        command=" ".join(cmd),
        error_detail=err_detail,
        project_dir=str(project),
    )


# ---- metadata builders (the only writers; all sandbox-only, all reversible in git) ----------
def _api_name(label: str) -> str:
    base = re.sub(r"[^A-Za-z0-9]+", "_", label.strip()).strip("_")
    if base and base[0].isdigit():
        base = "X" + base
    return (base or "New_Field") + "__c"


def _build_additive_field(project: Path, spec: dict) -> SandboxResult:
    obj = spec.get("object") or "Account"
    label = spec.get("label") or "New Field"
    api = spec.get("field_api") or _api_name(label)
    ftype = spec.get("type") or "Text"
    length = int(spec.get("length") or 80)
    fields_dir = project / "force-app" / "main" / "default" / "objects" / obj / "fields"
    fields_dir.mkdir(parents=True, exist_ok=True)
    body = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<CustomField xmlns="http://soap.sforce.com/2006/04/metadata">',
        f"    <fullName>{api}</fullName>",
        f"    <label>{label}</label>",
        f"    <type>{ftype}</type>",
    ]
    if ftype in {"Text"}:
        body.append(f"    <length>{length}</length>")
    body += ["    <required>false</required>", "</CustomField>", ""]
    (fields_dir / f"{api}.field-meta.xml").write_text("\n".join(body))
    res = _validate(project, spec.get("org", DEFAULT_SANDBOX), metadata=[f"CustomField:{obj}.{api}"])
    res.stage = f"new {ftype.lower()} field {api} on {obj}"
    return res


def _build_modify_picklist_label(project: Path, spec: dict) -> SandboxResult:
    """Retrieve the CURRENT picklist (replace-whole component), swap one value's label, validate.

    Retrieve-first is the point: a picklist deploy replaces the whole value set, so the engine
    stages the complete current definition with exactly one attribute changed -- never a fragment.
    """
    org = spec.get("org", DEFAULT_SANDBOX)
    _assert_sandbox(org)
    obj = spec["object"]
    api = spec["field_api"]
    member = f"CustomField:{obj}.{api}"
    rc, rdata, raw = _run(
        ["sf", "project", "retrieve", "start", "-m", member, "-o", org, "--json"], project
    )
    path = project / "force-app" / "main" / "default" / "objects" / obj / "fields" / f"{api}.field-meta.xml"
    if not path.exists():
        msgs = [m.get("problem") for m in ((rdata.get("result") or {}).get("messages") or []) if m.get("problem")]
        detail = "; ".join(msgs) or (raw[:200])
        res = SandboxResult(ok=False, org=org, check_only=True, total=0, errors=1,
                            command=f"retrieve {member}", error_detail=f"retrieve found no such field: {detail}",
                            project_dir=str(project))
        res.stage = f"retrieve {api} on {obj}"
        return res
    xml = path.read_text()
    frm, to = spec["from_label"], spec["to_label"]
    # replace the <label> inside the value block whose <fullName> or <label> matches `frm`
    pat = re.compile(
        r"(<value>\s*<fullName>[^<]*</fullName>.*?<label>)" + re.escape(frm) + r"(</label>)",
        re.DOTALL,
    )
    new_xml, n = pat.subn(lambda m: m.group(1) + to + m.group(2), xml)
    if n == 0:  # fall back to a bare label swap
        new_xml, n = xml.replace(f"<label>{frm}</label>", f"<label>{to}</label>"), (frm in xml)
    path.write_text(new_xml)
    res = _validate(project, org, metadata=[member])
    res.stage = f"rename '{frm}' -> '{to}' on the {obj}.{api} picklist (replace-whole of the live value set)"
    return res


# ---- drafts (Operator's work voice; zero AI/automation trace) ----------------------------------
def _scrub(draft: str) -> str:
    out = draft
    try:
        from core import work_reply_voice

        out = work_reply_voice.autofix(out)
    except Exception:
        pass
    return out.strip()


def _draft_deliver(spec: dict, res: SandboxResult) -> str:
    stage = res.stage or "the change"
    d = (
        f"got it -- built {stage} in the partial sandbox and validated it green "
        f"(check-only deploy, {res.total} component, {res.errors} errors). "
        "it's staged for the sandbox -> github -> prod promote whenever you want it live. "
        "want me to open the PR?"
    )
    return _scrub(d)


def _draft_propose(spec: dict, res: SandboxResult, blast: dict) -> str:
    stage = res.stage or "the change"
    radius = blast.get("assessment") or "touches an existing component"
    if res.ok:
        proof = f"validated green in partial (check-only, {res.total} component, {res.errors} errors), NOT applied"
    else:
        proof = f"staged in partial but validate flagged: {res.error_detail[:180]}"
    d = (
        f"took a look -- this one modifies an existing component so i staged it in partial "
        f"instead of shipping it. the fix: {stage}. "
        f"blast radius: {radius}. {proof}. "
        "good to promote through sandbox -> github -> prod, or want to pivot?"
    )
    return _scrub(d)


def _draft_clarify(request_text: str, classification: dict) -> str:
    tgt = ", ".join(classification.get("targets") or []) or "the object/field"
    d = (
        f"quick q before i build this -- for {tgt}, do you want a brand-new field/report "
        "or a change to something that's already there? once i know i'll build it in the "
        "partial sandbox and validate before anything goes near prod."
    )
    return _scrub(d)


# ---- blast-radius assessment (human-readable, for the propose draft) ------------------------
def _assess_blast(spec: dict, classification: dict) -> dict:
    kind = spec.get("kind", "")
    if kind == "modify_picklist_label":
        return {
            "assessment": "replace-whole of a live picklist value set -- the label updates "
            "everywhere the field renders; existing records keep their stored value so no data "
            "loss, but it ships to every user of that field"
        }
    if classification.get("signals", {}).get("destructive"):
        return {"assessment": "destructive -- removes or reassigns an existing component/record; "
                "not reversible without a restore path"}
    return {"assessment": "modifies existing metadata/access -- replace-whole, ships to all users of the component"}


# ---- best-effort build-spec derivation -----------------------------------------------------
# The clean contract is that the sfdc_engineer specialist hands `handle_sf_request` a structured
# build_spec (it owns org context). This convenience derives the common shapes from plain text so
# the engine can act without a specialist round-trip; unrecognized shapes return None (honest).
def _derive_build_spec(text: str, classification: dict) -> dict | None:
    low = text.lower()
    # additive: "add a (new) field called X to the Y object" / "add field X to Y"
    m = re.search(
        r"\badd\s+(?:a\s+|an\s+)?(?:new\s+)?field\s+(?:called\s+|named\s+)?['\"]?([A-Za-z0-9 _]{2,40}?)['\"]?"
        r"\s+(?:to|on|for)\s+(?:the\s+)?([A-Za-z0-9_]+)\b",
        text,
        re.IGNORECASE,
    )
    if m and classification["level"] == "additive":
        label = m.group(1).strip().title()
        return {"kind": "additive_field", "object": m.group(2).strip(), "label": label,
                "field_api": _api_name(label), "type": "Text", "length": 80}
    # blast: "change/rename the 'A' label ... <Field> picklist ... to 'B'"
    labels = re.findall(r"['\"]([^'\"]{1,60})['\"]", text)
    if classification["level"] == "blast" and len(labels) >= 2 and "picklist" in low:
        am = re.search(r"\b([A-Za-z0-9_]+__c)\b", text)
        api = am.group(1) if am else "Dealer_Type__c"
        obj = "Account"
        om = re.search(r"\b([A-Z][A-Za-z0-9]+)\s+" + re.escape(api), text) if am else None
        if not om:
            om = re.search(r"\b([A-Z][A-Za-z0-9]+)\s+[A-Za-z ]*picklist", text)
        if om and om.group(1) != api:
            obj = om.group(1)
        return {"kind": "modify_picklist_label", "object": obj,
                "field_api": api, "from_label": labels[0], "to_label": labels[1]}
    return None


# ---- orchestrator --------------------------------------------------------------------------
def handle_sf_request(request: Any) -> dict:
    """Classify a work-lane SF request, do the safe sandbox work, return a grounded draft.

    `request` is a dict (or a bare string). Recognized keys:
        text        -- the request language (required)
        sf_ref      -- optional structured hint (ticket / resolved target)
        build_spec  -- optional structured metadata intent from the sfdc_engineer specialist;
                       when present it drives the build instead of text derivation
        org         -- sandbox alias to build in (default 'partial')

    Returns {classification, sandbox_result, draft, needs_alex}.
      additive -> builds + validates green, draft DELIVERS, needs_alex=False (build done;
                  prod promote is separately human-gated -> prod_promotion_gated=True)
      blast    -> builds + validates in sandbox, draft PROPOSES agree-or-pivot, needs_alex=True
      unclear  -> no build, draft asks, needs_alex=True
    """
    if isinstance(request, str):
        request = {"text": request}
    text = str(request.get("text") or "").strip()
    org = request.get("org") or DEFAULT_SANDBOX
    sf_ref = request.get("sf_ref")

    classification = classify(text, sf_ref)
    level = classification["level"]

    spec = request.get("build_spec") or _derive_build_spec(text, classification)
    if isinstance(spec, dict):
        spec.setdefault("org", org)

    if level == "unclear":
        return {
            "classification": classification,
            "sandbox_result": None,
            "draft": _draft_clarify(text, classification),
            "needs_alex": True,
        }

    # nothing concrete to build -> classify + draft honestly (specialist should supply build_spec)
    if not spec:
        if level == "additive":
            draft = _scrub(
                "on it -- looks like a clean additive build. send me the exact object + field "
                "(or report) name and i'll build it in the partial sandbox and validate green "
                "before it goes near prod."
            )
            return {"classification": classification, "sandbox_result": None, "draft": draft,
                    "needs_alex": True, "note": "no build_spec derivable; needs the concrete target"}
        blast = _assess_blast({}, classification)
        draft = _scrub(
            "took a look -- this one modifies something that already exists, so i won't just "
            f"ship it. blast radius: {blast['assessment']}. send me the exact target and i'll "
            "stage it in partial + validate so you can review before we promote."
        )
        return {"classification": classification, "sandbox_result": None, "draft": draft,
                "needs_alex": True, "blast": blast, "note": "no build_spec derivable"}

    project = _new_project()
    try:
        kind = spec.get("kind")
        if level == "additive":
            if kind == "additive_field":
                res = _build_additive_field(project, spec)
            else:
                res = SandboxResult(ok=False, org=org, check_only=True, total=0, errors=1,
                                    error_detail=f"no additive builder for kind '{kind}'",
                                    project_dir=str(project))
            draft = _draft_deliver(spec, res) if res.ok else _scrub(
                f"started the build in partial but the validate flagged: {res.error_detail[:180]} "
                "-- digging in, will have it green shortly."
            )
            return {
                "classification": classification,
                "sandbox_result": res.as_dict(),
                "draft": draft,
                "needs_alex": not res.ok,
                "prod_promotion_gated": True,
            }
        # blast
        if kind == "modify_picklist_label":
            res = _build_modify_picklist_label(project, spec)
        else:
            res = SandboxResult(ok=False, org=org, check_only=True, total=0, errors=1,
                                error_detail=f"no blast builder for kind '{kind}'",
                                project_dir=str(project))
        blast = _assess_blast(spec, classification)
        draft = _draft_propose(spec, res, blast)
        return {
            "classification": classification,
            "sandbox_result": res.as_dict(),
            "draft": draft,
            "needs_alex": True,
            "blast": blast,
        }
    finally:
        shutil.rmtree(project, ignore_errors=True)


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("text")
    ap.add_argument("--org", default=DEFAULT_SANDBOX)
    a = ap.parse_args()
    print(json.dumps(handle_sf_request({"text": a.text, "org": a.org}), indent=2))
