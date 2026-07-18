"""Tests for the behavioral principles layer (AGENT.md constitution)."""

from __future__ import annotations

from pathlib import Path

import pytest

from coding_harness import principles


@pytest.fixture(autouse=True)
def _clear_cache():
    """Principles are cached; reset before and after each test for isolation."""
    principles.clear_cache()
    yield
    principles.clear_cache()


def test_bundled_principles_load():
    """The bundled AGENT.md is always present and non-empty."""
    text = principles.principles_text()
    assert text.strip()
    assert "AGENT.md" in text or "OPERATING PRINCIPLES" in text.upper() or "Think Before Coding" in text


def test_bundled_contains_all_eleven_rules():
    text = principles.principles_text()
    for n in range(1, 12):
        assert f"## {n}." in text, f"Rule {n} missing from principles"


def test_bundled_source_label():
    assert principles.source_label() == "bundled (default)"
    assert principles.source_label(None) == "bundled (default)"


def test_principles_block_has_header():
    """The prompt-injection block is clearly labeled as mandatory rules."""
    block = principles.principles_block()
    assert "HARNESS OPERATING PRINCIPLES" in block
    assert "Follow them at all times" in block
    assert block.startswith("=== HARNESS OPERATING PRINCIPLES")


def test_inject_into_prompt_prepends():
    """inject_into_prompt puts the principles block before the agent prompt."""
    out = principles.inject_into_prompt("You are a planner.")
    assert out.index("HARNESS OPERATING PRINCIPLES") < out.index("planner")
    assert "planner" in out


def test_inject_is_idempotent_and_nonempty():
    """Even an empty prompt gets the principles."""
    out = principles.inject_into_prompt("")
    assert "HARNESS OPERATING PRINCIPLES" in out


def test_workspace_agent_md_overrides_bundled(tmp_path: Path, monkeypatch):
    """An AGENT.md in the workspace root takes precedence over the bundled default."""
    monkeypatch.delenv("WELLS_PRINCIPLES", raising=False)
    (tmp_path / "AGENT.md").write_text("# Custom Team Rules\n1. Use TypeScript.\n", encoding="utf-8")
    principles.clear_cache()
    assert principles.source_label(str(tmp_path)) == str(tmp_path / "AGENT.md")
    text = principles.principles_text(str(tmp_path))
    assert "TypeScript" in text
    assert "Karpathy" not in text  # bundled content replaced


def test_env_var_overrides_workspace(tmp_path: Path, monkeypatch):
    """$WELLS_PRINCIPLES beats <workspace>/AGENT.md."""
    (tmp_path / "AGENT.md").write_text("# Workspace rules\n", encoding="utf-8")
    custom = tmp_path / "custom.md"
    custom.write_text("# Env override\nUse Python 3.13.\n", encoding="utf-8")
    monkeypatch.setenv("WELLS_PRINCIPLES", str(custom))
    principles.clear_cache()
    assert principles.source_label(str(tmp_path)) == str(custom)
    assert "Python 3.13" in principles.principles_text(str(tmp_path))


def test_env_var_nonexistent_falls_back(tmp_path: Path, monkeypatch):
    """A non-existent $WELLS_PRINCIPLES falls back to workspace/bundled."""
    monkeypatch.setenv("WELLS_PRINCIPLES", str(tmp_path / "nope.md"))
    principles.clear_cache()
    # No workspace AGENT.md either -> bundled default
    assert principles.source_label(str(tmp_path)) == "bundled (default)"


def test_principles_block_truncates_oversize(tmp_path: Path, monkeypatch):
    """An oversized principles file is truncated to fit the budget."""
    monkeypatch.delenv("WELLS_PRINCIPLES", raising=False)
    (tmp_path / "AGENT.md").write_text("# Big\n\n" + ("x" * 6000), encoding="utf-8")
    principles.clear_cache()
    block = principles.principles_block(str(tmp_path), max_chars=2000)
    assert "trimmed" in block
    assert len(block) < 6000


def test_runtime_injects_principles(tmp_path: Path, monkeypatch):
    """run_step() prepends the principles to its system prompt."""
    from unittest.mock import patch
    from coding_harness import runtime

    monkeypatch.delenv("WELLS_PRINCIPLES", raising=False)
    captured_system = {}

    def fake_invoke(llm, messages):
        captured_system["content"] = messages[0].content
        # Return a minimal fake response
        class _R:
            content = "ok"
            usage_metadata = None
        return _R()

    with patch.object(runtime, "_invoke_with_retry", side_effect=fake_invoke):
        runtime.run_step(
            step="test", task_type="planning",
            system="You are a planner.", chunks={"x": "y"},
            workspace=str(tmp_path),
        )
    assert "HARNESS OPERATING PRINCIPLES" in captured_system["content"]
    assert "You are a planner." in captured_system["content"]
