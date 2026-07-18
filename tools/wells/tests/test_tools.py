"""Tests for the tool layer: confinement, safety policy, and tool behaviour."""

from __future__ import annotations

from pathlib import Path

import pytest

from coding_harness import safety, tools


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A clean workspace with a couple of files to poke at."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text(
        "def foo():\n    return 1\n\ndef bar():\n    return foo()\n"
    )
    (tmp_path / "README.md").write_text("# hello\n")
    return tmp_path


@pytest.fixture
def ctx(workspace: Path) -> tools.ToolContext:
    return tools.ToolContext(workspace=str(workspace), safety="auto")


# ---------------------------------------------------------------------------
# Path confinement
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "../../../etc/passwd",
        "../../secret",
        "/etc/shadow",
        "/absolute/elsewhere",
    ],
)
def test_path_escapes_are_blocked(ctx: tools.ToolContext, bad: str):
    r = tools.dispatch("read_file", {"path": bad}, ctx)
    assert not r.ok
    assert "outside the workspace" in r.error


def test_path_inside_workspace_allowed(ctx: tools.ToolContext):
    r = tools.dispatch("read_file", {"path": "src/a.py"}, ctx)
    assert r.ok


def test_resolve_path_normalizes_dotdot(workspace: Path):
    p = safety.resolve_path("src/../src/a.py", str(workspace))
    assert p == (workspace / "src" / "a.py").resolve()


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------


def test_read_file_with_line_numbers(ctx: tools.ToolContext):
    r = tools.dispatch("read_file", {"path": "src/a.py"}, ctx)
    assert r.ok
    assert r.output.startswith("1: def foo():")


def test_read_file_offset_limit(ctx: tools.ToolContext):
    r = tools.dispatch("read_file", {"path": "src/a.py", "offset": 2, "limit": 1}, ctx)
    assert r.ok
    assert r.output.strip().startswith("2:")


def test_read_missing_file(ctx: tools.ToolContext):
    r = tools.dispatch("read_file", {"path": "nope.py"}, ctx)
    assert not r.ok
    assert "not found" in r.error.lower()


def test_list_dir(ctx: tools.ToolContext):
    r = tools.dispatch("list_dir", {"path": "."}, ctx)
    assert r.ok
    assert "src/" in r.output
    assert "README.md" in r.output


def test_glob_tool(ctx: tools.ToolContext):
    r = tools.dispatch("glob", {"pattern": "**/*.py"}, ctx)
    assert r.ok
    assert "a.py" in r.output


def test_grep_tool(ctx: tools.ToolContext):
    r = tools.dispatch("grep", {"pattern": r"def \w+", "include": "*.py"}, ctx)
    assert r.ok
    assert "def foo" in r.output
    assert "def bar" in r.output


def test_grep_bad_regex_errors(ctx: tools.ToolContext):
    r = tools.dispatch("grep", {"pattern": "(unclosed"}, ctx)
    assert not r.ok
    assert "Bad regex" in r.error


# ---------------------------------------------------------------------------
# Write / edit tools + safety policy
# ---------------------------------------------------------------------------


def test_write_file_auto(ctx: tools.ToolContext, workspace: Path):
    r = tools.dispatch("write_file", {"path": "src/new.py", "content": "x = 1\n"}, ctx)
    assert r.ok
    assert (workspace / "src" / "new.py").read_text() == "x = 1\n"


def test_write_file_creates_parents(ctx: tools.ToolContext, workspace: Path):
    r = tools.dispatch("write_file", {"path": "a/b/c.txt", "content": "hi"}, ctx)
    assert r.ok
    assert (workspace / "a" / "b" / "c.txt").read_text() == "hi"


def test_edit_file_unique(ctx: tools.ToolContext, workspace: Path):
    tools.dispatch("write_file", {"path": "e.py", "content": "old\n"}, ctx)
    r = tools.dispatch(
        "edit_file", {"path": "e.py", "old_string": "old", "new_string": "new"}, ctx
    )
    assert r.ok
    assert (workspace / "e.py").read_text() == "new\n"


def test_edit_file_ambiguous_requires_replace_all(ctx: tools.ToolContext):
    tools.dispatch("write_file", {"path": "e.py", "content": "x x x\n"}, ctx)
    r = tools.dispatch(
        "edit_file", {"path": "e.py", "old_string": "x", "new_string": "y"}, ctx
    )
    assert not r.ok
    assert "matches 3 times" in r.error
    r2 = tools.dispatch(
        "edit_file",
        {"path": "e.py", "old_string": "x", "new_string": "y", "replace_all": True},
        ctx,
    )
    assert r2.ok


def test_edit_file_missing_old_string(ctx: tools.ToolContext):
    tools.dispatch("write_file", {"path": "e.py", "content": "hello\n"}, ctx)
    r = tools.dispatch(
        "edit_file", {"path": "e.py", "old_string": "nope", "new_string": "x"}, ctx
    )
    assert not r.ok
    assert "not found" in r.error.lower()


@pytest.mark.parametrize("mode", ["dryrun", "approve"])
def test_safety_modes_prevent_writes(mode: str, workspace: Path):
    ctx = tools.ToolContext(
        workspace=str(workspace), safety=mode
    )  # approve has no approver
    r = tools.dispatch("write_file", {"path": "blocked.py", "content": "x"}, ctx)
    assert r.simulated
    assert not (workspace / "blocked.py").exists()


def test_approve_allows_with_approver(workspace: Path):
    ctx = tools.ToolContext(
        workspace=str(workspace), safety="approve", approver=lambda a, d: True
    )
    r = tools.dispatch("write_file", {"path": "ok.py", "content": "x"}, ctx)
    assert r.ok and not r.simulated
    assert (workspace / "ok.py").exists()


def test_plan_mode_simulates_writes_keeps_reads(workspace: Path):
    ctx = tools.ToolContext(workspace=str(workspace), safety="auto", plan_mode=True)
    w = tools.dispatch("write_file", {"path": "p.py", "content": "x"}, ctx)
    assert w.simulated
    assert not (workspace / "p.py").exists()
    r = tools.dispatch("read_file", {"path": "README.md"}, ctx)
    assert r.ok  # reads are never gated


# ---------------------------------------------------------------------------
# Shell + command screening
# ---------------------------------------------------------------------------


def test_run_command_echo(ctx: tools.ToolContext):
    r = tools.dispatch("run_command", {"command": "echo hello123"}, ctx)
    assert r.ok
    assert "hello123" in r.output


@pytest.mark.parametrize("cmd", ["rm -rf /", "rm -rf /home", "mkfs.ext4 /dev/sda"])
def test_blocked_commands_refused(ctx: tools.ToolContext, cmd: str):
    r = tools.dispatch("run_command", {"command": cmd}, ctx)
    assert not r.ok
    assert "refused" in r.error.lower() or "blocked" in r.error.lower()


def test_run_command_timeout(workspace: Path):
    ctx = tools.ToolContext(workspace=str(workspace), safety="auto", shell_timeout=0.5)
    # A python one-liner that sleeps — cleanly killable cross-platform.
    r = tools.dispatch(
        "run_command", {"command": 'python -c "import time; time.sleep(30)"'}, ctx
    )
    assert not r.ok
    assert "timed out" in r.error.lower()


def test_run_tests_autodetect_pytest(ctx: tools.ToolContext):
    # No tests in this workspace, but the command should at least run (exit non-zero is fine).
    r = tools.dispatch("run_tests", {}, ctx)
    assert isinstance(r, tools.ToolResult)


# ---------------------------------------------------------------------------
# Registry + dispatch
# ---------------------------------------------------------------------------


def test_registry_contains_core_tools():
    names = {t.name for t in tools.ALL_TOOLS}
    assert {
        "read_file",
        "list_dir",
        "glob",
        "grep",
        "write_file",
        "edit_file",
        "run_command",
        "run_tests",
    } <= names


def test_registry_readonly_excludes_mutating():
    names = {t.name for t in tools.registry(include_mutating=False)}
    assert "write_file" not in names
    assert "run_command" not in names
    assert "read_file" in names


def test_dispatch_unknown_tool(ctx: tools.ToolContext):
    r = tools.dispatch("not_a_tool", {}, ctx)
    assert not r.ok
    assert "Unknown tool" in r.error


def test_dispatch_bad_args(ctx: tools.ToolContext):
    # read_file requires `path`.
    r = tools.dispatch("read_file", {}, ctx)
    assert not r.ok


def test_langchain_tool_schemas_well_formed():
    schemas = tools.langchain_tool_schemas()
    assert len(schemas) == len(tools.ALL_TOOLS)
    for s in schemas:
        assert s["type"] == "function"
        assert "name" in s["function"]
        assert "parameters" in s["function"]
        assert s["function"]["parameters"]["type"] == "object"
