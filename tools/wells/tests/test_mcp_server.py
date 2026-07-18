"""Tests for the MCP server tool registration and fast-path tool invocations.

Slow tools that require LLM calls (``run_agent_task``, ``plan_task``,
``review_code``, ``run_executor``, ``spawn_subagent``) are NOT exercised here —
they are covered by the executor test suite (with a mock model) and the manual
smoke test against the live provider.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from coding_harness.mcp_server import (
    _compress_logs,
    _get_harness_info,
    _get_memory,
    _read_file,
    _run_command,
    _search_repo,
    handle_call_tool,
    handle_list_tools,
    server,
)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_server_name() -> None:
    assert server.name == "coding-harness"


EXPECTED_TOOLS = {
    "run_agent_task",
    "plan_task",
    "review_code",
    "run_executor",
    "spawn_subagent",
    "search_repo",
    "read_file",
    "run_command",
    "git_status",
    "get_memory",
    "get_principles",
    "compress_logs",
    "get_harness_info",
}


def test_all_expected_tools_registered() -> None:
    tools = asyncio.run(handle_list_tools())
    names = {t.name for t in tools}
    assert EXPECTED_TOOLS <= names, f"missing: {EXPECTED_TOOLS - names}"


@pytest.mark.parametrize(
    "name, required",
    [
        ("run_agent_task", ["goal"]),
        ("run_executor", ["task"]),
        ("spawn_subagent", ["task"]),
        ("search_repo", []),
        ("read_file", ["path"]),
        ("run_command", ["command"]),
        ("git_status", []),
        ("get_memory", []),
        ("compress_logs", ["text"]),
        ("get_harness_info", []),
    ],
)
def test_tool_required_args(name: str, required: list[str]) -> None:
    tools = asyncio.run(handle_list_tools())
    tool = next(t for t in tools if t.name == name)
    assert set(required).issubset(set(tool.inputSchema.get("required", [])))


# ---------------------------------------------------------------------------
# Fast-path tool invocations (no LLM calls)
# ---------------------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "hello.py").write_text("print('hi')\n")
    (tmp_path / "AGENTS.md").write_text(
        "# memory\n\n## Lessons Learned\n\n### 2026-01-01 — test\nDid a thing.\n"
    )
    return tmp_path


def test_compress_logs() -> None:
    result = _compress_logs({"text": "hello\nworld\nworld\nworld\nerror\n\n\n"})
    assert len(result) == 1
    text = result[0].text
    assert "hello" in text
    assert "error" in text
    assert "compressed" in text


def test_compress_logs_with_tail() -> None:
    text = "\n".join(f"line {i}" for i in range(200))
    result = _compress_logs({"text": text, "tail_lines": 10})
    lines = result[0].text.splitlines()
    assert 1 <= len(lines) <= 14


def test_get_harness_info() -> None:
    result = _get_harness_info({})
    text = result[0].text
    assert "coding-harness" in text
    assert "active_profile" in text
    assert "workspace_root" in text


def test_read_file_tool(workspace: Path) -> None:
    result = _read_file({"path": "hello.py", "workspace": str(workspace)})
    assert "print('hi')" in result[0].text


def test_read_file_missing(workspace: Path) -> None:
    result = _read_file({"path": "nope.py", "workspace": str(workspace)})
    assert "error" in result[0].text.lower() or "not found" in result[0].text.lower()


def test_search_repo_glob(workspace: Path) -> None:
    result = _search_repo({"glob": "**/*.py", "workspace": str(workspace)})
    assert "hello.py" in result[0].text


def test_search_repo_grep(workspace: Path) -> None:
    result = _search_repo({"pattern": "print", "workspace": str(workspace)})
    assert "hello.py" in result[0].text


def test_search_repo_needs_arg(workspace: Path) -> None:
    result = _search_repo({"workspace": str(workspace)})
    assert "error" in result[0].text.lower()


def test_run_command_tool(workspace: Path) -> None:
    result = _run_command({"command": "echo mcp-test", "workspace": str(workspace)})
    assert "mcp-test" in result[0].text


def test_run_command_blocked(workspace: Path) -> None:
    result = _run_command({"command": "rm -rf /", "workspace": str(workspace)})
    assert "error" in result[0].text.lower() or "refused" in result[0].text.lower()


def test_get_memory(workspace: Path) -> None:
    result = _get_memory({"workspace": str(workspace)})
    assert "Lessons Learned" in result[0].text


def test_get_memory_empty(tmp_path: Path) -> None:
    result = _get_memory({"workspace": str(tmp_path)})
    assert "empty" in result[0].text.lower() or "false" in result[0].text.lower()


def test_git_status(workspace: Path) -> None:
    # Workspace may or may not be a git repo; the tool should not crash either way.
    result = asyncio.run(handle_call_tool("git_status", {"workspace": str(workspace)}))
    assert len(result) == 1


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def test_unknown_tool_returns_error() -> None:
    result = asyncio.run(handle_call_tool("nonexistent_tool", {}))
    text = result[0].text
    assert "Unknown tool" in text or "error" in text.lower()


@pytest.mark.parametrize(
    "name, bad_args",
    [
        ("compress_logs", {}),
        ("run_agent_task", {}),
        ("plan_task", {}),
        ("run_executor", {}),
        ("read_file", {}),
        ("run_command", {}),
    ],
)
def test_missing_required_arg(name: str, bad_args: dict) -> None:
    # Most require a workspace fallback; only run those that don't touch workspace.
    result = asyncio.run(handle_call_tool(name, bad_args))
    text = result[0].text
    assert "error" in text.lower()
