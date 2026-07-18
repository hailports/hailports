from __future__ import annotations

import json
import re
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

from core import BASE_DIR

SF_REPO = Path("/home/user/AG-CDT-SALESFORCE")
OUT_PATH = BASE_DIR / "data" / "learning" / "sfdc_commit_patterns.jsonl"
WATERMARK_PATH = BASE_DIR / "data" / "learning" / "sfdc_commit_patterns.watermark"

_TICKET_RE = re.compile(r"\bSF[-\s]?(\d{3,5})\b", re.IGNORECASE)
_PR_RE = re.compile(r"#(\d{1,6})\b")
_REC = re.compile(r"^@@@")

# force-app/main/default/<type>/...
_FA_RE = re.compile(r"force-app/main/default/([a-zA-Z]+)/(.+)$")

# path segment -> canonical component type
_TYPE_MAP = {
    "permissionsets": "permissionset",
    "classes": "class",
    "triggers": "trigger",
    "objects": "object",
    "flows": "flow",
    "layouts": "layout",
    "labels": "label",
    "lwc": "lwc",
    "aura": "aura",
    "digitalExperiences": "digitalExperience",
    "email": "email",
    "flexipages": "flexipage",
    "staticresources": "staticresource",
    "dashboards": "dashboard",
    "reports": "report",
    "workflows": "workflow",
    "profiles": "profile",
    "tabs": "tab",
    "quickActions": "quickAction",
}

_META_SUFFIXES = (
    ".permissionset-meta.xml",
    ".field-meta.xml",
    ".object-meta.xml",
    ".flow-meta.xml",
    ".layout-meta.xml",
    ".labels-meta.xml",
    ".cls-meta.xml",
    ".cls",
    ".trigger-meta.xml",
    ".trigger",
    ".js-meta.xml",
    ".flexipage-meta.xml",
    ".workflow-meta.xml",
    ".profile-meta.xml",
    ".tab-meta.xml",
    ".quickAction-meta.xml",
    ".dashboard-meta.xml",
    ".report-meta.xml",
    ".email-meta.xml",
    "-meta.xml",
)

_STOP = {
    "the", "a", "an", "to", "of", "for", "and", "or", "in", "on", "with", "so",
    "can", "see", "add", "adds", "added", "fix", "fixes", "fixed", "update",
    "updates", "updated", "create", "created", "new", "merge", "pull", "request",
    "from", "into", "user", "users", "field", "fields", "report", "reports",
    "this", "that", "set", "sets", "permission", "view", "make", "allow", "allows",
}


def _strip_meta(basename: str) -> str:
    for suf in _META_SUFFIXES:
        if basename.endswith(suf):
            return basename[: -len(suf)]
    return re.sub(r"\.[a-zA-Z]+$", "", basename)


def _component_from_path(path: str) -> tuple[str, str] | None:
    m = _FA_RE.search(path)
    if not m:
        return None
    seg, rest = m.group(1), m.group(2)
    ctype = _TYPE_MAP.get(seg)
    if ctype is None:
        return None
    parts = rest.split("/")
    if ctype in ("object", "digitalExperience", "lwc", "aura"):
        name = parts[0]
    else:
        name = _strip_meta(parts[-1])
    return ctype, name


def _tokens(text: str) -> set[str]:
    toks = re.findall(r"[a-zA-Z][a-zA-Z0-9]+", (text or "").lower())
    return {t for t in toks if len(t) > 2 and t not in _STOP}


def _iter_git_records(max_commits: int, since_hash: str | None):
    rng = f"{since_hash}..HEAD" if since_hash else "HEAD"
    cmd = [
        "git", "-C", str(SF_REPO), "log", rng,
        f"--max-count={max_commits}", "--name-status", "--no-renames",
        "--pretty=format:@@@%H|%ci|%s",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"git log failed: {proc.stderr.strip()}")
    cur: dict[str, Any] | None = None
    for line in proc.stdout.splitlines():
        if _REC.match(line):
            if cur is not None:
                yield cur
            h, date, subject = line[3:].split("|", 2)
            cur = {"hash": h, "date": date, "message": subject, "_paths": []}
        elif cur is not None and line.strip():
            # name-status line: "A\tpath" or "M\tpath"
            piece = line.split("\t", 1)
            if len(piece) == 2:
                cur["_paths"].append(piece[1])
    if cur is not None:
        yield cur


def _build_record(raw: dict[str, Any]) -> dict[str, Any]:
    msg = raw["message"]
    tickets = sorted({f"SF-{m}" for m in _TICKET_RE.findall(msg)})
    prs = sorted({int(m) for m in _PR_RE.findall(msg)})
    comps: list[dict[str, str]] = []
    seen = set()
    for p in raw["_paths"]:
        c = _component_from_path(p)
        if c is None:
            continue
        key = (c[0], c[1])
        if key in seen:
            continue
        seen.add(key)
        comps.append({"type": c[0], "name": c[1]})
    ctypes = sorted({c["type"] for c in comps})
    names = [c["name"] for c in comps]
    tok = _tokens(msg) | {n.lower() for n in names}
    return {
        "hash": raw["hash"],
        "date": raw["date"],
        "message": msg,
        "tickets": tickets,
        "prs": prs,
        "component_types": ctypes,
        "components": comps,
        "tokens": sorted(tok),
    }


def learn(max_commits: int = 2182) -> dict[str, Any]:
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    since = None
    if WATERMARK_PATH.exists():
        since = WATERMARK_PATH.read_text().strip() or None

    records = []
    newest_hash = None
    for raw in _iter_git_records(max_commits, since):
        if newest_hash is None:
            newest_hash = raw["hash"]
        rec = _build_record(raw)
        if not rec["components"] and not rec["tickets"] and not rec["prs"]:
            # merge/empty commit with no SF signal -> skip noise
            continue
        records.append(rec)

    written = 0
    if records:
        mode = "a" if since and OUT_PATH.exists() else "w"
        with OUT_PATH.open(mode) as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                written += 1
    if newest_hash:
        WATERMARK_PATH.write_text(newest_hash)

    total = 0
    if OUT_PATH.exists():
        with OUT_PATH.open() as f:
            total = sum(1 for _ in f)
    return {"scanned_new": written, "incremental_from": since, "total_patterns": total, "db": str(OUT_PATH)}


def _load_db() -> list[dict[str, Any]]:
    if not OUT_PATH.exists():
        return []
    out = []
    with OUT_PATH.open() as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def suggest(request_text: str, top_k: int = 5) -> dict[str, Any]:
    db = _load_db()
    q = _tokens(request_text)
    q_types = set()
    low = request_text.lower()
    for seg, ctype in _TYPE_MAP.items():
        if ctype in low or seg in low:
            q_types.add(ctype)
    if "permission" in low or "perm set" in low or "access" in low:
        q_types.add("permissionset")

    scored = []
    for rec in db:
        rt = set(rec.get("tokens", []))
        overlap = q & rt
        if not overlap:
            continue
        score = len(overlap)
        # weight component-name/type matches over generic words
        comp_names = {c["name"].lower() for c in rec.get("components", [])}
        score += 2 * len(q & comp_names)
        if q_types & set(rec.get("component_types", [])):
            score += 3
        scored.append((score, rec))

    scored.sort(key=lambda x: (x[0], x[1]["date"]), reverse=True)
    top = scored[:top_k]

    type_counter: Counter = Counter()
    for _, rec in top:
        type_counter.update(rec.get("component_types", []))

    likely_types = [t for t, _ in type_counter.most_common()]
    similar = []
    for score, rec in top:
        similar.append({
            "hash": rec["hash"][:8],
            "date": rec["date"][:10],
            "message": rec["message"],
            "tickets": rec["tickets"],
            "prs": rec["prs"],
            "component_types": rec["component_types"],
            "components": [f"{c['type']}:{c['name']}" for c in rec["components"]],
            "score": score,
        })

    max_possible = max(len(q), 1)
    confidence = 0.0
    if top:
        confidence = round(min(top[0][0] / (max_possible + 3), 1.0), 2)

    approach = _approach(likely_types, similar)
    return {
        "query": request_text,
        "similar_commits": similar,
        "likely_component_types": likely_types,
        "suggested_approach": approach,
        "confidence": confidence,
    }


def _approach(types: list[str], similar: list[dict[str, Any]]) -> str:
    if not similar:
        return "no deterministic precedent found — escalate to AI planning"
    parts = []
    if types:
        parts.append("touch " + ", ".join(types))
    ex = similar[0]
    ref = ""
    if ex["prs"]:
        ref = f"PR #{ex['prs'][0]}"
    elif ex["tickets"]:
        ref = ex["tickets"][0]
    else:
        ref = ex["hash"]
    comps = ", ".join(ex["components"][:4]) or "(none)"
    parts.append(f"precedent {ref} changed {comps}")
    return "; ".join(parts)


if __name__ == "__main__":
    print("== learn (last 200 commits) ==")
    # force a full local run scoped to 200 for the smoke test
    if WATERMARK_PATH.exists():
        WATERMARK_PATH.unlink()
    if OUT_PATH.exists():
        OUT_PATH.unlink()
    stats = learn(max_commits=200)
    print(json.dumps(stats, indent=2))

    print("\n== suggest('add a permission set so a user can see DPP report fields') ==")
    res = suggest("add a permission set so a user can see DPP report fields")
    print("likely_component_types:", res["likely_component_types"])
    print("confidence:", res["confidence"])
    print("approach:", res["suggested_approach"])
    for c in res["similar_commits"]:
        print(f"  [{c['score']}] {c['hash']} {c['date']} PR{c['prs']} {c['message'][:70]}")

    hits = [c for c in res["similar_commits"] if 368 in c["prs"] or any("DPP" in x for x in c["components"])]
    print("\nDPP #368 surfaced:", bool(hits))
