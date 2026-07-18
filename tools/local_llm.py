"""Deterministic local LLM tool wrapper with auditable call history."""

from __future__ import annotations

import json
import time
from collections import defaultdict

from core import local_client
from tools.base import BaseTool, make_tool_def


def _int_value(value, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(maximum, int(value)))
    except Exception:
        return default


def _read_call_rows(limit: int = 1000) -> list[dict]:
    path = local_client.LOCAL_LLM_CALL_LOG
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    rows: list[dict] = []
    for raw in reversed(lines):
        raw = raw.strip()
        if not raw:
            continue
        try:
            rows.append(json.loads(raw))
        except Exception:
            continue
        if len(rows) >= limit:
            break
    return rows


class LocalLLMTool(BaseTool):
    name = "local_llm"
    description = "Tracked deterministic local LLM calls and local inference audit history"

    def get_definitions(self) -> list[dict]:
        return [
            make_tool_def(
                "local_llm_generate",
                "Run a tracked local LLM generation. Use for deterministic formatting, scoring, extraction, classification, and summarization that would otherwise spend API credits.",
                {
                    "prompt": {"type": "string", "description": "Prompt to send to the local model."},
                    "system": {"type": "string", "description": "Optional system instruction."},
                    "model": {"type": "string", "description": "Optional Ollama model override."},
                    "purpose": {"type": "string", "description": "Short caller/purpose label for the audit ledger."},
                    "max_tokens": {"type": "integer", "description": "Maximum generated tokens; capped at 4096."},
                    "temperature": {"type": "number", "description": "Sampling temperature."},
                    "displaced_tier": {
                        "type": "string",
                        "description": "Paid tier this local call avoided: haiku, sonnet, or opus.",
                    },
                },
                required=["prompt"],
            ),
            make_tool_def(
                "local_llm_recent_calls",
                "Read recent tracked local LLM calls from the audit ledger.",
                {
                    "limit": {"type": "integer", "description": "Maximum rows to return; capped at 200."},
                    "source_contains": {"type": "string", "description": "Optional case-insensitive source filter."},
                    "status": {"type": "string", "description": "Optional status filter such as success, error, queue_timeout."},
                    "historical": {"type": "boolean", "description": "Optional filter for historical backfilled rows."},
                },
            ),
            make_tool_def(
                "local_llm_savings_summary",
                "Summarize tracked local LLM calls and displaced credit estimate.",
                {
                    "hours": {"type": "integer", "description": "Lookback window in hours; default 24, max 2160."},
                    "source_contains": {"type": "string", "description": "Optional case-insensitive source filter."},
                },
            ),
        ]

    async def handle(self, tool_name: str, tool_input: dict) -> str:
        tool_input = tool_input if isinstance(tool_input, dict) else {}

        if tool_name == "local_llm_generate":
            prompt = str(tool_input.get("prompt") or "").strip()
            if not prompt:
                return "Error: prompt is required"
            max_tokens = _int_value(tool_input.get("max_tokens"), 1024, 1, 4096)
            temperature = tool_input.get("temperature")
            try:
                temperature = float(temperature) if temperature is not None else 0.2
            except Exception:
                temperature = 0.2
            purpose = str(tool_input.get("purpose") or "manual").strip()[:80] or "manual"
            displaced_tier = str(tool_input.get("displaced_tier") or "haiku").strip().lower()
            if displaced_tier not in {"haiku", "sonnet", "opus"}:
                displaced_tier = "haiku"
            result = await local_client.generate(
                prompt,
                model=str(tool_input.get("model") or "").strip() or None,
                system=str(tool_input.get("system") or "").strip() or None,
                max_tokens=max_tokens,
                temperature=temperature,
                source=f"local_llm_generate:{purpose}",
                route="deterministic_tool",
                tier="deterministic",
                displaced_tier=displaced_tier,
                metadata={"tool_name": tool_name, "purpose": purpose},
            )
            return result or ""

        if tool_name == "local_llm_recent_calls":
            limit = _int_value(tool_input.get("limit"), 25, 1, 200)
            source_filter = str(tool_input.get("source_contains") or "").strip().lower()
            status_filter = str(tool_input.get("status") or "").strip().lower()
            historical_filter = tool_input.get("historical")
            rows = []
            for row in _read_call_rows(limit=2000):
                if source_filter and source_filter not in str(row.get("source") or "").lower():
                    continue
                if status_filter and status_filter != str(row.get("status") or "").lower():
                    continue
                if isinstance(historical_filter, bool) and bool(row.get("historical")) != historical_filter:
                    continue
                rows.append(row)
                if len(rows) >= limit:
                    break
            return json.dumps({"count": len(rows), "calls": rows}, indent=2, sort_keys=True)

        if tool_name == "local_llm_savings_summary":
            hours = _int_value(tool_input.get("hours"), 24, 1, 2160)
            source_filter = str(tool_input.get("source_contains") or "").strip().lower()
            cutoff = time.time() - (hours * 3600)
            summary = defaultdict(lambda: {"calls": 0, "success": 0, "errors": 0, "estimated_saved_usd": 0.0, "models": {}})
            total = {"calls": 0, "success": 0, "errors": 0, "estimated_saved_usd": 0.0}
            for row in _read_call_rows(limit=20000):
                if float(row.get("ts") or 0) < cutoff:
                    break
                source = str(row.get("source") or "unknown")
                if source_filter and source_filter not in source.lower():
                    continue
                bucket = summary[source]
                bucket["calls"] += 1
                total["calls"] += 1
                if row.get("status") == "success":
                    bucket["success"] += 1
                    total["success"] += 1
                else:
                    bucket["errors"] += 1
                    total["errors"] += 1
                saved = float(row.get("estimated_saved_usd") or 0)
                bucket["estimated_saved_usd"] = round(bucket["estimated_saved_usd"] + saved, 6)
                total["estimated_saved_usd"] = round(total["estimated_saved_usd"] + saved, 6)
                model = str(row.get("model") or "unknown")
                bucket["models"][model] = bucket["models"].get(model, 0) + 1
            return json.dumps(
                {
                    "hours": hours,
                    "total": total,
                    "by_source": dict(sorted(summary.items(), key=lambda item: item[1]["calls"], reverse=True)),
                },
                indent=2,
                sort_keys=True,
            )

        return f"Error: Unknown tool '{tool_name}'"
