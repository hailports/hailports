"""Fail-closed audit for sale-ready bundles."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from core.public_copy import scan_public_leaks

SAFE_EMAIL_DOMAINS = {
    "company.com",
    "email.com",
    "example.com",
    "example.net",
    "example.org",
    "redacted.example",
}

TEXT_SUFFIXES = {
    ".c",
    ".cc",
    ".cfg",
    ".command",
    ".conf",
    ".cpp",
    ".css",
    ".csv",
    ".env",
    ".example",
    ".go",
    ".h",
    ".html",
    ".ini",
    ".java",
    ".js",
    ".json",
    ".jsonl",
    ".md",
    ".mjs",
    ".py",
    ".rb",
    ".rs",
    ".sh",
    ".sql",
    ".svg",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}

EMAIL_RE = re.compile(r"\b([A-Z0-9._%+-]+)@([A-Z0-9.-]+\.[A-Z]{2,})\b", re.IGNORECASE)
PHONE_RE = re.compile(r"(?<!\w)(?:\+?1[\s.-]?)?(?:\(?\d{3}\)?[\s.-]?)\d{3}[\s.-]?\d{4}(?!\w)")
USER_PATH_RE = re.compile(r"/Users/([A-Za-z0-9._-]+)")
IP_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")


def _is_probably_text(path: Path) -> bool:
    if path.suffix.lower() in TEXT_SUFFIXES:
        return True
    if path.name.startswith(".env"):
        return True
    try:
        chunk = path.read_bytes()[:2048]
    except Exception:
        return False
    return b"\x00" not in chunk


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def _email_allowed(match: re.Match[str]) -> bool:
    domain = match.group(2).lower()
    if domain in SAFE_EMAIL_DOMAINS:
        return True
    if domain.endswith(".example"):
        return True
    return False


def _ip_allowed(value: str) -> bool:
    return value in {"127.0.0.1", "0.0.0.0"}


def _line_findings(rel_path: str, line_no: int, line: str) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for hit in scan_public_leaks(line):
        findings.append(
            {
                "path": rel_path,
                "line": line_no,
                "kind": "explicit_leak",
                "match": hit,
                "sample": line.strip()[:240],
            }
        )
    for match in EMAIL_RE.finditer(line):
        if _email_allowed(match):
            continue
        findings.append(
            {
                "path": rel_path,
                "line": line_no,
                "kind": "email",
                "match": match.group(0),
                "sample": line.strip()[:240],
            }
        )
    for match in PHONE_RE.finditer(line):
        findings.append(
            {
                "path": rel_path,
                "line": line_no,
                "kind": "phone",
                "match": match.group(0),
                "sample": line.strip()[:240],
            }
        )
    for match in USER_PATH_RE.finditer(line):
        username = match.group(1)
        if username in {"...", "redacted", "redacted-user"}:
            continue
        findings.append(
            {
                "path": rel_path,
                "line": line_no,
                "kind": "user_path",
                "match": match.group(0),
                "sample": line.strip()[:240],
            }
        )
    for match in IP_RE.finditer(line):
        if _ip_allowed(match.group(0)):
            continue
        findings.append(
            {
                "path": rel_path,
                "line": line_no,
                "kind": "ip_address",
                "match": match.group(0),
                "sample": line.strip()[:240],
            }
        )
    return findings


def audit_tree(root: Path, max_findings: int = 200) -> dict[str, Any]:
    root = Path(root).resolve()
    findings: list[dict[str, Any]] = []
    files_scanned = 0
    text_files = 0

    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        files_scanned += 1
        if not _is_probably_text(path):
            continue
        text_files += 1
        rel_path = path.relative_to(root).as_posix()
        try:
            content = _read_text(path)
        except Exception as exc:
            findings.append(
                {
                    "path": rel_path,
                    "line": 0,
                    "kind": "read_error",
                    "match": type(exc).__name__,
                    "sample": str(exc)[:240],
                }
            )
            if len(findings) >= max_findings:
                break
            continue
        for line_no, line in enumerate(content.splitlines(), start=1):
            findings.extend(_line_findings(rel_path, line_no, line))
            if len(findings) >= max_findings:
                break
        if len(findings) >= max_findings:
            break

    return {
        "ok": len(findings) == 0,
        "root": str(root),
        "files_scanned": files_scanned,
        "text_files_scanned": text_files,
        "blocked_count": len(findings),
        "findings": findings,
        "truncated": len(findings) >= max_findings,
    }


def write_report(report: dict[str, Any], output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2) + "\n")
    return output_path
