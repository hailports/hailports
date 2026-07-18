"""Read-only filename + content search over the claude-stack repo, for the work GPT.

Lets StackGPT SEE the local working tree (scripts/tools/data/digests the team produced)
instead of replying "I can't access the filesystem". Search only — no writes, no exec.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # ~/claude-stack

# Directories that are noise or sensitive — never surfaced.
_SKIP_DIRS = {".git", "node_modules", "venv", ".venv", "__pycache__", ".mypy_cache",
              ".pytest_cache", "backups", "data/secrets", "secrets", ".chrome-cdp-profile",
              "browser_shots", "logs", ".cloudflared"}
_SKIP_NAME_HINTS = ("secret", ".key", "credential", "token", ".pem", "auth.json", ".env")
_TEXT_EXT = {".py", ".js", ".ts", ".sh", ".md", ".txt", ".json", ".yaml", ".yml", ".csv",
             ".html", ".sql", ".toml", ".cfg", ".ini"}
_STOP = {"file", "files", "the", "stack", "repo", "repository", "codebase", "folder", "folders",
         "script", "scripts", "find", "look", "search", "show", "list", "for", "related", "about",
         "that", "which", "where", "what", "with", "have", "done", "work", "worked", "this",
         "these", "those", "claude", "claude-stack", "code", "into", "from", "you", "we", "our",
         "all", "any", "some", "and", "tool", "tools", "data", "see", "view", "give", "get",
         "pull", "in", "on", "of", "to", "a", "is", "are", "do", "did", "me", "my", "please",
         "source", "python", "py", "handle", "handles", "built", "build", "wrote", "made",
         "created", "use", "used", "uses", "run", "runs", "there", "here", "them"}


def _terms(q: str) -> list[str]:
    raw = re.findall(r"[A-Za-z][A-Za-z0-9_\-]{2,}", q or "")
    # keep SF-#### / DPP-### style tokens intact + meaningful words
    keep = []
    for w in raw:
        lw = w.lower()
        if lw in _STOP:
            continue
        keep.append(w)
    # also pull explicit ticket tokens
    for t in re.findall(r"\b(?:SF|DPP|SPP)-?\d{2,6}\b", q or "", re.I):
        keep.append(t)
    # de-dupe preserving order
    seen, out = set(), []
    for w in keep:
        k = w.lower()
        if k not in seen:
            seen.add(k)
            out.append(w)
    return out[:8]


def _skip(path: str) -> bool:
    p = path.lower()
    if any(f"/{d.lower()}/" in p or p.endswith(f"/{d.lower()}") for d in _SKIP_DIRS):
        return True
    return any(h in p for h in _SKIP_NAME_HINTS)


def _rel(p: str) -> str:
    try:
        return str(Path(p).resolve().relative_to(ROOT))
    except Exception:
        return p


def _filename_hits(terms: list[str], limit: int = 25) -> list[str]:
    if not terms:
        return []
    tl = [t.lower() for t in terms]
    scored: dict[str, int] = {}
    fd = None
    for cand in ("/opt/homebrew/bin/fd", "/usr/local/bin/fd", "fd"):
        if os.path.exists(cand) or subprocess.run(["which", cand], capture_output=True).returncode == 0:
            fd = cand
            break
    try:
        cands: list[str] = []
        if fd:
            # union across terms (one fd per term) — not a combined glob that needs all terms adjacent
            for t in tl[:6]:
                r = subprocess.run([fd, "-i", "-t", "f", t, "."], cwd=str(ROOT),
                                   capture_output=True, text=True, timeout=8)
                cands.extend(l for l in r.stdout.splitlines() if l.strip())
        else:
            for dp, dns, fns in os.walk(ROOT):
                dns[:] = [d for d in dns if d not in _SKIP_DIRS and not d.startswith(".")]
                for fn in fns:
                    low = fn.lower()
                    if any(t in low for t in tl):
                        cands.append(os.path.join(dp, fn))
                if len(cands) > 1500:
                    break
        for c in cands:
            full = str((ROOT / c).resolve()) if not os.path.isabs(c) else c
            if _skip(full):
                continue
            rel = _rel(full)
            base = os.path.basename(rel).lower()
            # rank by # of query terms present in the filename (then by full path)
            score = sum(1 for t in tl if t in base) * 10 + sum(1 for t in tl if t in rel.lower())
            scored[rel] = max(scored.get(rel, 0), score)
    except Exception:
        pass
    return [r for r, _ in sorted(scored.items(), key=lambda kv: (-kv[1], len(kv[0])))][:limit]


def _content_hits(terms: list[str], limit: int = 20) -> list[tuple[str, int, str]]:
    if not terms:
        return []
    hits: list[tuple[str, int, str]] = []
    rg = None
    for cand in ("/opt/homebrew/bin/rg", "/usr/local/bin/rg", "rg"):
        if os.path.exists(cand) or subprocess.run(["which", cand], capture_output=True).returncode == 0:
            rg = cand
            break
    phrase = terms[0]
    if not rg:
        return hits  # content grep needs ripgrep; filename match still works without it
    try:
        r = subprocess.run([rg, "-i", "-n", "--max-count", "3", "--max-filesize", "1M",
                            "--glob", "!**/.git/**", "--glob", "!**/node_modules/**",
                            "--glob", "!**/venv/**", "--glob", "!**/.venv/**",
                            "--glob", "!**/__pycache__/**", "--glob", "!**/secrets/**",
                            "--glob", "!**/logs/**", "--glob", "!**/backups/**",
                            phrase, "."],
                           cwd=str(ROOT), capture_output=True, text=True, timeout=15)
        for line in r.stdout.splitlines():
            m = re.match(r"^(.*?):(\d+):(.*)$", line)
            if not m:
                continue
            rel, ln, snip = m.group(1), int(m.group(2)), m.group(3).strip()
            full = str((ROOT / rel).resolve())
            if _skip(full):
                continue
            hits.append((_rel(full), ln, snip[:200]))
            if len(hits) >= limit:
                break
    except Exception:
        pass
    return hits


_LIST_SKIP = {".git", "node_modules", "venv", ".venv", "__pycache__", "secrets", "logs", "backups"}


def _resolve_in_root(token: str) -> Path | None:
    """Resolve a path token (absolute, ~, ROOT-relative, or bare name) to a real path under ROOT."""
    token = token.strip().strip("`'\"")
    if not token:
        return None
    for cand in (Path(token).expanduser(), ROOT / token):
        try:
            r = cand.resolve()
            if r.exists() and str(r).startswith(str(ROOT)):
                return r
        except Exception:
            pass
    # bare name / partial path → fd/walk search for the best match
    base = os.path.basename(token)
    try:
        for dp, dns, fns in os.walk(ROOT):
            dns[:] = [d for d in dns if d not in _LIST_SKIP and not d.startswith(".")]
            for nm in fns + dns:
                if nm == base or token in os.path.join(dp, nm):
                    p = Path(dp) / nm
                    if str(p.resolve()).startswith(str(ROOT)):
                        return p.resolve()
    except Exception:
        pass
    return None


def _path_tokens(request_text: str) -> list[str]:
    t = request_text or ""
    toks = re.findall(r"[A-Za-z0-9_][\w./*\-]*\.[A-Za-z0-9]+", t)          # filenames w/ extension
    toks += re.findall(r"[A-Za-z0-9_][\w./\-]*/[\w./*\-]+", t)              # path-ish (has a slash)
    toks += re.findall(r"\bSF-?\d{3,6}\b", t, re.I)                        # ticket dirs
    toks += re.findall(r"\b[\w]+\*", t)                                     # globs like SF-33*
    # de-dupe preserving order
    seen, out = set(), []
    for w in toks:
        if w.lower() not in seen:
            seen.add(w.lower()); out.append(w)
    return out


def stack_list(request_text: str, maxdepth: int = 3, limit: int = 400) -> str:
    """Inventory a directory tree (supports globs like sprint3_audit/sprint3/work/SF-33*)."""
    import fnmatch
    toks = _path_tokens(request_text) or ["sprint3_audit/sprint3/work"]
    roots: list[Path] = []
    glob_pat = None
    for tk in toks:
        if "*" in tk:
            # split glob into a base dir + pattern
            base, _, pat = tk.rpartition("/")
            d = _resolve_in_root(base) if base else ROOT
            if d and d.is_dir():
                for child in sorted(d.iterdir()):
                    if fnmatch.fnmatch(child.name, pat) and child.name not in _LIST_SKIP:
                        roots.append(child)
            glob_pat = tk
        else:
            r = _resolve_in_root(tk)
            if r:
                roots.append(r if r.is_dir() else r.parent)
    roots = list(dict.fromkeys(roots)) or [ROOT / "sprint3_audit/sprint3/work"]
    lines = [f"STACK DIRECTORY INVENTORY (claude-stack, read-only). Roots: "
             + ", ".join(_rel(str(r)) for r in roots) + (f"  (glob {glob_pat})" if glob_pat else "")]
    count = 0
    for root in roots:
        if not root.exists():
            lines.append(f"\n[missing] {_rel(str(root))}")
            continue
        base_depth = len(root.resolve().parts)
        for dp, dns, fns in os.walk(root):
            dns[:] = [d for d in sorted(dns) if d not in _LIST_SKIP and not d.startswith(".")]
            depth = len(Path(dp).resolve().parts) - base_depth
            if depth > maxdepth:
                dns[:] = []
                continue
            rel = _rel(dp)
            indent = "  " * depth
            lines.append(f"{indent}{rel}/" if depth == 0 else f"{indent}{os.path.basename(dp)}/")
            for fn in sorted(fns):
                if fn.startswith(".") or any(h in fn.lower() for h in _SKIP_NAME_HINTS):
                    continue
                try:
                    sz = (Path(dp) / fn).stat().st_size
                except Exception:
                    sz = 0
                lines.append(f"{indent}  {fn}  ({sz} b)")
                count += 1
                if count >= limit:
                    lines.append("  … (truncated)")
                    return "\n".join(lines)[:9000]
    lines.append("\nTo read any file's full contents: runDeterministicTool stack_file_read {\"path\":\"<path>\"} "
                 "or just ask 'read <path>'.")
    return "\n".join(lines)[:9000]


def stack_read(request_text: str, max_chars: int = 8200) -> str:
    """Open named file(s) and return their FULL contents (capped per response)."""
    toks = _path_tokens(request_text)
    files: list[Path] = []
    for tk in toks:
        if "*" in tk:
            continue
        p = _resolve_in_root(tk)
        if p and p.is_file():
            files.append(p)
        elif p and p.is_dir():
            for child in sorted(p.iterdir()):
                if child.is_file() and child.suffix.lower() in _TEXT_EXT and not _skip(str(child)):
                    files.append(child)
    files = list(dict.fromkeys(files))
    if not files:
        return ("STACK FILE READ — no readable file resolved from the request. Name an exact path, e.g. "
                "'read sprint3_audit/sprint3/work/SF-3330/final_summary.json' or "
                "'show sprint3_audit/sprint3/work/SF-3128/build_spec.md'.")
    out = ["STACK FILE READ (claude-stack, read-only) — full contents below."]
    budget = max_chars * max(1, min(len(files), 4))  # allow a few files, shared budget
    per = max(1500, budget // min(len(files), 4) if files else budget)
    for f in files[:6]:
        if _skip(str(f)):
            continue
        try:
            data = f.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            out.append(f"\n===== {_rel(str(f))} ===== [unreadable: {e}]")
            continue
        body = data[:per]
        trunc = "" if len(data) <= per else f"\n…[truncated {len(data)-per} of {len(data)} chars — ask for this file alone for the rest]"
        out.append(f"\n===== {_rel(str(f))} ({len(data)} chars) =====\n{body}{trunc}")
        if sum(len(x) for x in out) > 8600:
            break
    return "\n".join(out)[:9200]


def stack_search(request_text: str, limit: int = 25) -> str:
    terms = _terms(request_text)
    if not terms:
        return ""
    fn = _filename_hits(terms, limit)
    ct = _content_hits(terms, 20)
    if not fn and not ct:
        return (f"STACK FILESYSTEM SEARCH (claude-stack repo) — no files or contents matched "
                f"{terms}. Try a more specific filename, ticket number (SF-####), or keyword.")
    parts = [f"STACK FILESYSTEM SEARCH — claude-stack repo at {ROOT} (read-only). "
             f"Matched on: {', '.join(terms)}."]
    if fn:
        parts.append("\nFILENAME MATCHES:\n" + "\n".join(f"  {p}" for p in fn))
    if ct:
        parts.append("\nCONTENT MATCHES (file:line — snippet):")
        for rel, ln, snip in ct:
            parts.append(f"  {rel}:{ln} — {snip}")
    parts.append("\nTo read any file in full, call runDeterministicTool with hands_list_folder "
                 "{\"path\":\"<dir>\"} or open it via hands_open_path. (Read-only view of the working tree.)")
    return "\n".join(parts)[:9000]


if __name__ == "__main__":
    import sys
    print(stack_search(" ".join(sys.argv[1:]) or "salesforce ticket"))
