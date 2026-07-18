"""Sale-readiness tools for packaging and Admin AI product status."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tools.base import BaseTool, make_tool_def


ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def _load_json(path: Path, fallback: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text())
    except Exception:
        pass
    return fallback


def _artifact(path: Path) -> dict[str, Any]:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return {"path": str(path.relative_to(ROOT)), "exists": False}
    payload = _load_json(path, {})
    stamp = ""
    if isinstance(payload, dict):
        stamp = str(payload.get("generated_at") or payload.get("as_of_iso") or payload.get("as_of") or "")
    return {
        "path": str(path.relative_to(ROOT)),
        "exists": True,
        "bytes": stat.st_size,
        "modified_ts": stat.st_mtime,
        "generated_at": stamp,
    }


def _latest_audit() -> dict[str, Any]:
    audits = sorted((ROOT / "dist").glob("claude-stack-sale-ready-*.audit.json"), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    if not audits:
        return {"exists": False}
    report = _load_json(audits[0], {})
    return {
        "exists": True,
        "path": str(audits[0].relative_to(ROOT)),
        "ok": bool(report.get("ok")),
        "blocked_count": int(report.get("blocked_count") or 0),
        "files_scanned": int(report.get("files_scanned") or 0),
        "text_files_scanned": int(report.get("text_files_scanned") or 0),
    }


def build_sale_readiness_status() -> dict[str, Any]:
    playbooks = _load_json(DATA / "sf_ticket_playbooks.json", {})
    packets = _load_json(DATA / "sf_playbook_packets.json", {})
    eval_report = _load_json(DATA / "evals" / "admin_ai_eval_report.json", {})
    gap_backlog = _load_json(DATA / "admin_ai_gap_backlog.json", {})
    continuation = _load_json(DATA / "admin_ai_continuation_backlog.json", {})

    eval_summary = eval_report.get("summary") if isinstance(eval_report, dict) else {}
    gap_summary = gap_backlog.get("summary") if isinstance(gap_backlog, dict) else {}
    tasks = continuation.get("tasks") if isinstance(continuation, dict) else []
    pending_tasks = [row for row in tasks or [] if isinstance(row, dict) and row.get("status") != "completed"]

    return {
        "ok": True,
        "admin_ai": {
            "playbooks": len(playbooks.get("playbooks") or []) if isinstance(playbooks, dict) else 0,
            "ticket_count": int(playbooks.get("ticket_count", 0) or 0) if isinstance(playbooks, dict) else 0,
            "packets": int(packets.get("packet_count", 0) or 0) if isinstance(packets, dict) else 0,
            "eval_total_tickets": int((eval_summary or {}).get("total_tickets", 0) or 0),
            "eval_covered": int((eval_summary or {}).get("covered", 0) or 0),
            "eval_partial": int((eval_summary or {}).get("partial", 0) or 0),
            "eval_uncovered": int((eval_summary or {}).get("uncovered", 0) or 0),
            "gap_open": int((gap_summary or {}).get("open_gaps", 0) or 0),
            "continuation_pending": len(pending_tasks),
        },
        "artifacts": [
            _artifact(DATA / "sf_ticket_playbooks.json"),
            _artifact(DATA / "sf_playbook_packets.json"),
            _artifact(DATA / "sf_process_docs_report.json"),
            _artifact(DATA / "evals" / "admin_ai_eval_report.json"),
            _artifact(DATA / "admin_ai_gap_backlog.json"),
            _artifact(DATA / "admin_ai_continuation_backlog.json"),
        ],
        "sale_package": {
            "latest_audit": _latest_audit(),
            "manifest": _artifact(ROOT / "distribution_manifest.json"),
            "inventory": _artifact(DATA / "sale_ready_packaging_inventory.json"),
        },
    }


def format_sale_readiness_status(status: dict[str, Any]) -> str:
    admin = status.get("admin_ai") or {}
    audit = ((status.get("sale_package") or {}).get("latest_audit") or {})
    lines = [
        "AI Admin sale-readiness:",
        f"- Playbooks: {admin.get('playbooks', 0)} from {admin.get('ticket_count', 0)} mined tickets; packets: {admin.get('packets', 0)}.",
        f"- Eval: {admin.get('eval_covered', 0)} covered, {admin.get('eval_partial', 0)} partial, {admin.get('eval_uncovered', 0)} uncovered.",
        f"- Continuation backlog: {admin.get('continuation_pending', 0)} pending improvement task(s).",
    ]
    if audit.get("exists"):
        verdict = "clean" if audit.get("ok") else f"blocked ({audit.get('blocked_count', 0)} findings)"
        lines.append(f"- Latest package audit: {verdict} at {audit.get('path')}.")
    else:
        lines.append("- Latest package audit: none yet. Run sale_ready_package_build to create one.")
    lines.append("- Packaging excludes runtime data, browser sessions, local output, prospect lists, API keys, sessions, and customer artifacts by default.")
    return "\n".join(lines)


class SaleReadinessTool(BaseTool):
    name = "sale_readiness"
    description = "Check and build scrubbed sale-ready packages for the AI Admin stack."

    def get_definitions(self) -> list[dict]:
        return [
            make_tool_def(
                "sale_ready_status",
                "Report Admin AI learning/coverage status and latest scrubbed package audit status.",
                {},
                [],
            ),
            make_tool_def(
                "sale_ready_package_build",
                "Build a scrubbed sale-ready tarball and fail closed if PII/secrets/customer data are detected.",
                {
                    "bundle_name": {
                        "type": "string",
                        "description": "Optional stable bundle name without .tar.gz.",
                    }
                },
                [],
            ),
        ]

    async def handle(self, tool_name: str, tool_input: dict) -> str:
        if tool_name == "sale_ready_status":
            return format_sale_readiness_status(build_sale_readiness_status())
        if tool_name == "sale_ready_package_build":
            from scripts import package_for_sale

            bundle_name = str((tool_input or {}).get("bundle_name") or "").strip() or None
            result = package_for_sale.build_package(bundle_name=bundle_name)
            if result.get("ok"):
                return (
                    "Sale-ready package built and audit passed.\n"
                    f"Tarball: {result.get('tarball')}\n"
                    f"Audit: {result.get('audit_report')}\n"
                    f"Sanitized files: {result.get('sanitized_count', 0)}; excluded paths: {result.get('excluded_count', 0)}"
                )
            samples = result.get("audit_samples") or []
            return (
                "Sale-ready package blocked by audit.\n"
                f"Audit: {result.get('audit_report')}\n"
                f"Blocked findings: {result.get('audit_blocked_count', 0)}\n"
                f"Samples: {json.dumps(samples[:5], indent=2)}"
            )
        return f"Unknown tool: {tool_name}"
