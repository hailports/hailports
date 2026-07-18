"""Claude Code tool — invokes the claude CLI on the Mac Mini for autonomous
system maintenance, debugging, and code changes."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

from .base import BaseTool, make_tool_def

log = logging.getLogger(__name__)

CLAUDE_BIN = "/opt/homebrew/bin/claude"
DEFAULT_CWD = str(Path.home() / "claude-stack")
MAX_RUNTIME_SEC = 900  # 15 minutes
MAX_OUTPUT_CHARS = int(os.environ.get("CLAUDE_CODE_MAX_OUTPUT_CHARS", "100000"))
MAX_CHUNK_CHARS = int(os.environ.get("CLAUDE_CODE_MAX_CHUNK_CHARS", "4000"))
TRUE_VALUES = {"1", "true", "yes", "on"}


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in TRUE_VALUES


class ClaudeCodeTool(BaseTool):
    def get_definitions(self) -> list[dict]:
        return [
            make_tool_def(
                "claude_code_exec",
                (
                    "Invoke Claude Code on the Mac Mini to autonomously perform system "
                    "maintenance, debugging, code changes, or investigations on the "
                    "claude-stack itself. Use this when the user asks to: fix bugs in "
                    "the stack, debug issues, update code, investigate problems, check "
                    "logs, optimize performance, add features, or perform any multi-step "
                    "technical task on ~/claude-stack/. Claude Code has full file system "
                    "and shell access. It can edit files, run tests, restart services, "
                    "and commit to git. Use for ANY self-maintenance task — never try "
                    "to diagnose or fix stack issues yourself when this tool is available."
                ),
                {
                    "task": {
                        "type": "string",
                        "description": (
                            "Detailed natural language description of what Claude Code "
                            "should do. Be specific. Examples: 'Check why the hybrid "
                            "handoff is failing on queries with 5+ tools and fix it.' "
                            "or 'Investigate the error in telegram.err.log from the last "
                            "hour and resolve.'"
                        ),
                    },
                    "working_dir": {
                        "type": "string",
                        "description": f"Working directory. Default: {DEFAULT_CWD}",
                    },
                },
                ["task"],
            )
        ]

    async def handle(self, tool_name: str, tool_input: dict) -> str:
        task = (tool_input.get("task") or "").strip()
        cwd = tool_input.get("working_dir") or DEFAULT_CWD

        if not task:
            return "Error: task is required"

        # Prevent recursion — strip any mention of claude_code_exec from the task
        if "claude_code_exec" in task:
            return "Error: cannot invoke claude_code_exec recursively"

        if _truthy_env("CLAUDE_STACK_DISABLE_PAID_AGENT_HANDOFFS"):
            return "Claude Code exec blocked: CLAUDE_STACK_DISABLE_PAID_AGENT_HANDOFFS=1."

        env = os.environ.copy()
        env["PATH"] = "/opt/homebrew/bin:" + env.get("PATH", "")

        # Route Claude CLI through the local proxy/OpenRouter policy. Do not
        # resurrect a direct Anthropic key from .env.
        env.setdefault("ANTHROPIC_BASE_URL", "http://127.0.0.1:8099")
        if not env.get("ANTHROPIC_API_KEY") and env.get("OPENROUTER_API_KEY"):
            env["ANTHROPIC_API_KEY"] = env["OPENROUTER_API_KEY"]

        cmd = [
            CLAUDE_BIN,
            "-p", task,
            "--permission-mode", "acceptEdits",
            "--output-format", "text",
        ]

        log.info(f"Invoking Claude Code: {task[:120]}")
        t0 = time.monotonic()

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=cwd,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=MAX_RUNTIME_SEC
                )
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass
                elapsed = time.monotonic() - t0
                return f"Claude Code exceeded {MAX_RUNTIME_SEC}s timeout (actual: {elapsed:.0f}s). Task was too large or hung."

            out = stdout.decode("utf-8", errors="replace").strip()
            err = stderr.decode("utf-8", errors="replace").strip()
            rc = proc.returncode
            elapsed = time.monotonic() - t0

            # Log the run
            _log_invocation(task, rc, elapsed, len(out))

            if rc != 0:
                return (
                    f"Claude Code exited with code {rc} after {elapsed:.1f}s.\n\n"
                    f"Output:\n{_truncate(out, 3000)}\n\n"
                    f"Errors:\n{_truncate(err, 1500)}"
                )

            if not out:
                return f"Claude Code completed in {elapsed:.1f}s with no output."

            if len(out) > MAX_OUTPUT_CHARS:
                artifact = _write_output_artifact("claude-code", task, out)
                chunks = _chunk_text(out)
                return json.dumps({
                    "ok": True,
                    "completed": True,
                    "elapsed_sec": round(elapsed, 1),
                    "artifact": artifact,
                    "chunks": chunks,
                    "chunk_count": len(chunks),
                }, ensure_ascii=False)

            return f"Claude Code completed in {elapsed:.1f}s:\n\n{out}"

        except FileNotFoundError:
            return f"Error: claude CLI not found at {CLAUDE_BIN}"
        except Exception as e:
            log.exception("Claude Code invocation failed")
            return f"Error invoking Claude Code: {e}"


def _truncate(text: str, n: int) -> str:
    if len(text) <= n:
        return text
    return text[:n] + "\n... (truncated)"


def _log_invocation(task: str, rc: int, elapsed: float, output_chars: int):
    """Log each Claude Code invocation to data/logs/claude_code.jsonl."""
    try:
        log_path = Path.home() / "claude-stack" / "data" / "logs" / "claude_code.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone(timedelta(hours=-6)))
        entry = {
            "ts": now.isoformat(),
            "date": now.strftime("%Y-%m-%d"),
            "task": task[:500],
            "return_code": rc,
            "elapsed_sec": round(elapsed, 2),
            "output_chars": output_chars,
        }
        with open(log_path, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        log.warning(f"Failed to log Claude Code invocation: {e}")


def _write_output_artifact(prefix: str, task: str, output: str) -> str:
    """Persist oversized command output so chat can reference it without clipping."""
    try:
        from hashlib import sha1

        log_path = Path.home() / "claude-stack" / "data" / "logs" / f"{prefix}-output"
        log_path.mkdir(parents=True, exist_ok=True)
        digest = sha1(task.encode("utf-8", errors="ignore")).hexdigest()[:12]
        path = log_path / f"{digest}.txt"
        path.write_text(output, encoding="utf-8")
        return str(path)
    except Exception:
        return "(unable to persist output artifact)"


def _chunk_text(text: str, size: int = MAX_CHUNK_CHARS) -> list[str]:
    if size <= 0 or len(text) <= size:
        return [text]
    chunks = []
    for i in range(0, len(text), size):
        chunks.append(text[i : i + size])
    return chunks
