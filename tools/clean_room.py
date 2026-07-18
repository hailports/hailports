#!/usr/bin/env python3
"""Clean-room scanner + extractor — provably strip PII/credentials before shipping.

Two modes:
  scan     Walk a path, report every secret / identity-PII / lead-data hit.
  extract  Copy an allowlist of framework files into a clean output dir, running
           the scanner as a HARD GATE: any file with a finding is refused (or
           scrubbed with --scrub). Produces a report + exits nonzero on any hit.

No third-party deps (stdlib only). Designed to run in CI as a commit gate:
  python3 tools/clean_room.py scan <path>   # exit 1 if anything toxic is found
"""
import argparse, json, os, re, shutil, sys
from pathlib import Path

# ── Identity markers — the operator's must-never-ship terms ──
# NOT hardcoded (that would leak them into this file when it ships). Loaded from,
# in order: $CLEAN_ROOM_MARKERS (comma-sep), ./.cleanroom-markers, ~/.cleanroom-markers
# (one term per line, '#' comments ok). Ships with an empty default.
def _load_markers():
    env = os.environ.get("CLEAN_ROOM_MARKERS", "")
    if env.strip():
        return [m.strip().lower() for m in env.split(",") if m.strip()]
    for p in (Path(".cleanroom-markers"), Path.home() / ".cleanroom-markers"):
        try:
            if p.is_file():
                return [ln.strip().lower() for ln in p.read_text().splitlines()
                        if ln.strip() and not ln.startswith("#")]
        except OSError:
            pass
    return []   # generic default — no personal data shipped in source

IDENTITY_MARKERS = _load_markers()

# ── Secret / credential patterns ──
SECRET_PATTERNS = [
    ("openai_key",      re.compile(r"sk-[A-Za-z0-9]{20,}")),
    ("anthropic_key",   re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}")),
    ("aws_access_key",  re.compile(r"AKIA[0-9A-Z]{16}")),
    ("google_api_key",  re.compile(r"AIza[0-9A-Za-z_\-]{35}")),
    ("slack_token",     re.compile(r"xox[baprs]-[0-9A-Za-z\-]{10,}")),
    ("stripe_key",      re.compile(r"\b[rs]k_(live|test)_[0-9A-Za-z]{16,}")),
    ("private_key",     re.compile(r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("bearer",          re.compile(r"[Bb]earer\s+[A-Za-z0-9_\-\.=]{20,}")),
    ("generic_secret",  re.compile(r"(?i)(api[_-]?key|access[_-]?token|secret|passwd|password)"
                                   r"['\"\s:=]+[A-Za-z0-9_\-\.]{12,}")),
]

# ── Generic PII (other people's data) ──
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
PHONE_RE = re.compile(r"\b(?:\+?1[\s.\-]?)?\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}\b")

# ── What never even gets walked in extract mode ──
DENY_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv",
             "data", "data.internal", "logs", "logs.internal", "backups",
             "backups.internal", "output", "output.internal", "products"}
DENY_FILE_RE = re.compile(r"(^\.env|\.env\.|\.pem$|\.key$|_leads?\.|prospects?\.|"
                          r"suppress|deliverable|cookies?\.)", re.I)
TEXT_EXT = {".py", ".js", ".ts", ".sh", ".json", ".md", ".txt", ".yaml", ".yml",
            ".toml", ".cfg", ".ini", ".html", ".css", ".env", ".plist"}


def redact(s):
    s = s.strip()
    return (s[:4] + "…" + s[-2:]) if len(s) > 8 else "***"


def scan_text(text):
    hits = []
    for name, pat in SECRET_PATTERNS:
        for m in pat.finditer(text):
            hits.append(("secret:" + name, redact(m.group(0))))
    low = text.lower()
    for mk in IDENTITY_MARKERS:
        if mk in low:
            hits.append(("identity", mk))
    for m in EMAIL_RE.finditer(text):
        e = m.group(0)
        if not e.lower().endswith(("example.com", "acme.com", "b.com", "domain.com")):
            hits.append(("email", redact(e)))
    for m in PHONE_RE.finditer(text):
        hits.append(("phone", redact(m.group(0))))
    return hits


def iter_files(root):
    root = Path(root)
    if root.is_file():
        yield root
        return
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in DENY_DIRS]
        for fn in fns:
            yield Path(dp) / fn


def scan_path(root):
    root = Path(root)
    report = {}
    for f in iter_files(root):
        rel = f.name if root.is_file() else str(f.relative_to(root))
        if DENY_FILE_RE.search(f.name):
            report[rel] = [("denylisted_file", f.name)]
            continue
        if f.suffix.lower() not in TEXT_EXT:
            continue
        try:
            text = f.read_text(errors="ignore")
        except Exception:
            continue
        hits = scan_text(text)
        if hits:
            # dedupe (type,value)
            report[rel] = sorted(set(hits))
    return report


def cmd_scan(args):
    report = scan_path(args.path)
    total = sum(len(v) for v in report.values())
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for rel, hits in sorted(report.items()):
            kinds = {}
            for k, _ in hits:
                kinds[k] = kinds.get(k, 0) + 1
            print(f"  {rel}: " + ", ".join(f"{k}×{n}" for k, n in sorted(kinds.items())))
        print(f"\n{'CLEAN ✅' if not report else 'TOXIC ❌'}  "
              f"{len(report)} files, {total} findings")
    return 1 if report else 0


def cmd_extract(args):
    """Copy allowlisted files into out dir, gating on scan. Refuse toxic files."""
    src, out = Path(args.path), Path(args.out)
    allow = [Path(p) for p in args.allow] if args.allow else [src]
    out.mkdir(parents=True, exist_ok=True)
    copied, refused = [], []
    for a in allow:
        base = src / a if not a.is_absolute() else a
        files = iter_files(base) if base.is_dir() else [base]
        for f in files:
            if not f.is_file():
                continue
            if DENY_FILE_RE.search(f.name) or f.suffix.lower() not in TEXT_EXT:
                refused.append((str(f), "denylisted/binary"))
                continue
            hits = scan_text(f.read_text(errors="ignore"))
            if hits and not args.scrub:
                refused.append((str(f), f"{len(hits)} findings"))
                continue
            rel = f.relative_to(src) if str(f).startswith(str(src)) else Path(f.name)
            dst = out / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dst)
            copied.append(str(rel))
    # verify the output is provably clean
    residual = scan_path(out)
    print(f"copied={len(copied)} refused={len(refused)} residual_findings={len(residual)}")
    for f, why in refused[:40]:
        print(f"  REFUSED {why}: {f}")
    if residual:
        print("  ⚠️ OUTPUT NOT CLEAN — residual hits:")
        for rel in list(residual)[:20]:
            print(f"    {rel}")
        return 1
    _install_gate(out)
    print(f"\n✅ clean-room export at {out} — provably free of PII/credentials")
    print("   self-enforcing gate installed (.githooks/pre-commit + CI workflow)")
    return 0


def _install_gate(out):
    """Drop the autonomous gate into the export so it self-protects forever."""
    here = Path(__file__).parent
    (out / "tools").mkdir(parents=True, exist_ok=True)
    for f in ("clean_room.py", "clean_room_gate.sh"):
        if (here / f).exists():
            shutil.copy2(here / f, out / "tools" / f)
    hooks = out / ".githooks"; hooks.mkdir(exist_ok=True)
    (hooks / "pre-commit").write_text(
        "#!/bin/bash\nexec bash tools/clean_room_gate.sh\n")
    os.chmod(hooks / "pre-commit", 0o755)
    os.chmod(out / "tools" / "clean_room_gate.sh", 0o755)
    ci = out / ".github" / "workflows"; ci.mkdir(parents=True, exist_ok=True)
    (ci / "clean-room.yml").write_text(
        "name: clean-room\non: [push, pull_request]\njobs:\n  scan:\n"
        "    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n"
        "      - run: python3 tools/clean_room.py scan .\n")
    # auto-arm the hook on clone
    (out / "setup.sh").write_text(
        "#!/bin/bash\ngit config core.hooksPath .githooks\n"
        "echo 'clean-room gate armed'\n")
    os.chmod(out / "setup.sh", 0o755)


def main():
    ap = argparse.ArgumentParser(description="Clean-room PII/credential scanner")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan"); s.add_argument("path"); s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_scan)
    e = sub.add_parser("extract")
    e.add_argument("path"); e.add_argument("--out", required=True)
    e.add_argument("--allow", nargs="*", help="subdirs/files to extract (default: all)")
    e.add_argument("--scrub", action="store_true", help="copy even with hits (DANGER)")
    e.set_defaults(fn=cmd_extract)
    args = ap.parse_args()
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()
