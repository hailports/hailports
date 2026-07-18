"""Tests for Layer 3: memory (AGENTS.md), git helpers, finisher, subagents."""

from __future__ import annotations

from pathlib import Path

import pytest

from coding_harness import finisher, gitops, memory, subagents, tools


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    return tmp_path


def test_memory_load_empty(ws: Path):
    m = memory.load(str(ws))
    assert not m.exists
    assert m.section_for_context() == ""


def test_memory_load_with_file(ws: Path):
    (ws / "AGENTS.md").write_text(
        "# Project\n\n## Lessons Learned\n\n### lesson one\n", encoding="utf-8"
    )
    m = memory.load(str(ws))
    assert m.exists
    assert "Lessons Learned" in m.text


def test_memory_load_tolerates_non_utf8(ws: Path):
    # Write bytes that aren't valid UTF-8 (cp1252 em-dash byte 0x97).
    (ws / "AGENTS.md").write_bytes(b"# mem\nword \x97 dash\n")
    m = memory.load(str(ws))
    assert m.exists  # must not silently disappear
    assert "mem" in m.text


def test_memory_inject_noop_when_empty(ws: Path):
    assert memory.inject_into_prompt("do task", str(ws)) == "do task"


def test_memory_inject_prepends(ws: Path):
    (ws / "AGENTS.md").write_text("# Project\nkey facts here\n", encoding="utf-8")
    out = memory.inject_into_prompt("do task", str(ws))
    assert "PROJECT MEMORY" in out
    assert "do task" in out
    assert out.index("PROJECT MEMORY") < out.index("do task")


def test_memory_append_creates_file(ws: Path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(ws))
    monkeypatch.setenv("HARNESS_SAFETY", "auto")
    path = memory.append_lesson(
        str(ws),
        goal="Fix bug",
        summary="off-by-one",
        key_files=["a.py"],
        commands=["pytest"],
        gotchas=["watch out"],
    )
    assert path is not None and path.exists()
    text = path.read_text(encoding="utf-8")
    assert "Lessons Learned" in text
    assert "Fix bug" in text
    assert "a.py" in text
    assert "watch out" in text


def test_memory_append_skipped_in_dryrun(ws: Path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(ws))
    monkeypatch.setenv("HARNESS_SAFETY", "dryrun")
    path = memory.append_lesson(str(ws), goal="x", summary="y")
    assert path is None
    assert not (ws / "AGENTS.md").exists()


def test_memory_append_merges_into_existing(ws: Path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(ws))
    monkeypatch.setenv("HARNESS_SAFETY", "auto")
    (ws / "AGENTS.md").write_text(
        "# Existing\n\n## Lessons Learned\n\n### old\nold text\n", encoding="utf-8"
    )
    memory.append_lesson(str(ws), goal="new", summary="new text")
    text = (ws / "AGENTS.md").read_text(encoding="utf-8")
    assert "old text" in text
    assert "new text" in text


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def test_slugify_basic():
    assert gitops._slugify("Fix the auth bug") == "fix-the-auth-bug" or gitops._slugify(
        "Fix the auth bug"
    ).startswith("fix-the-auth-bug")
    assert gitops._slugify("a").startswith("a")


def test_slugify_special_chars():
    s = gitops._slugify("TODO: refactor everything!!! @#$")
    assert "@" not in s and "!" not in s
    assert all(c.isalnum() or c in "-._" for c in s.split("-0628")[0] + "-x")


def test_safe_branch_name_has_prefix():
    assert gitops._safe_branch_name("fix-bug").startswith("wells/")


def test_gitops_non_git_workspace(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("HARNESS_SAFETY", "auto")
    ctx = tools.ToolContext(workspace=str(tmp_path), safety="auto")
    result = gitops.create_branch_and_commit(ctx, goal="test", message="m")
    assert result.ok
    assert "not a git repo" in result.message


# ---------------------------------------------------------------------------
# Finisher helpers
# ---------------------------------------------------------------------------


def test_extract_files():
    s = "Edited src/auth.py and tests/test_auth.py, plus config.yaml."
    files = finisher._extract_files(s)
    assert "src/auth.py" in files
    assert "tests/test_auth.py" in files
    assert "config.yaml" in files


def test_extract_files_empty():
    assert finisher._extract_files("") == []
    assert finisher._extract_files("no files here") == []


def test_extract_commands():
    s = "Run `pytest tests/` then `npm run build`."
    cmds = finisher._extract_commands(s)
    assert "pytest tests/" in cmds
    assert "npm run build" in cmds


def test_extract_gotchas():
    s = (
        "This is fine. There is a risk of overflow. Ensure bounds checked. "
        "Normal sentence. Beware of timezone edge case."
    )
    g = finisher._extract_gotchas(s)
    assert any("risk" in x.lower() for x in g)
    assert any("ensure" in x.lower() for x in g)
    assert any("beware" in x.lower() for x in g)


def test_commit_message_format():
    msg = finisher._commit_message("Fix auth", "Changed src/auth.py", True)
    assert "wells: Fix auth" in msg
    assert "[complete]" in msg
    assert "Changed src/auth.py" in msg


def test_finisher_dryrun_skips_git(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("HARNESS_SAFETY", "dryrun")
    state = {
        "goal": "test",
        "implementation_steps": "did stuff",
        "review_complete": True,
        "review_result": "",
        "code_changes": "did stuff",
        "workspace_root": str(tmp_path),
        "safety": "dryrun",
        "plan_mode": False,
    }
    out = finisher.finisher(state)
    assert out.get("finalized") is True
    assert "git_summary" not in out  # dry-run skips git


# ---------------------------------------------------------------------------
# Subagents
# ---------------------------------------------------------------------------


def test_research_subagent_is_readonly():
    spec = subagents.research_subagent("r1", "find the bug")
    assert spec.toolset == "readonly"
    assert spec.role == "research"
    toolset = subagents._resolve_toolset(spec.toolset)
    names = {t.name for t in toolset}
    assert "read_file" in names
    assert "write_file" not in names  # read-only


def test_fix_subagent_has_full_tools():
    spec = subagents.fix_subagent("f1", "change x to y")
    assert spec.toolset == "full"
    toolset = subagents._resolve_toolset(spec.toolset)
    names = {t.name for t in toolset}
    assert "write_file" in names
    assert "edit_file" in names


def test_dispatch_subagents_empty():
    assert (
        subagents.dispatch_subagents(
            [], tools.ToolContext(workspace=".", safety="dryrun")
        )
        == []
    )


def test_subagent_report_context_block():
    r = subagents.SubagentReport(name="r1", ok=True, summary="found it", steps_taken=2)
    block = r.as_context_block()
    assert "Subagent [r1]" in block
    assert "found it" in block


def test_run_subagent_with_scripted_model(tmp_path: Path, monkeypatch):
    """A research subagent should run end-to-end with a mock model."""
    from unittest.mock import patch
    from langchain_core.messages import AIMessage
    from coding_harness import config, executor
    from coding_harness.tokens import LEDGER

    LEDGER.reset()
    (tmp_path / "a.py").write_text("x = 1\n")
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))

    scripted = [
        AIMessage(
            content='<tool_call>{"name": "read_file", "args": {"path": "a.py"}}</tool_call>'
        ),
        AIMessage(content="Found x=1 on line 1."),
    ]
    it = iter(scripted)

    def fake(llm, msgs):
        return next(it)

    ctx = tools.ToolContext(workspace=str(tmp_path), safety="auto")
    spec = subagents.research_subagent("r1", "what is in a.py?")
    with (
        patch.object(config, "_invoke_with_retry", side_effect=fake),
        patch.object(executor, "_try_bind_tools", return_value=None),
    ):
        report = subagents.run_subagent(spec, ctx)
    assert report.ok
    assert report.steps_taken == 1
    assert "x=1" in report.summary or "x = 1" in report.summary
