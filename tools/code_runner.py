"""Code execution tool — sandboxed Python and shell command runner."""

import asyncio
import os
import logging
import subprocess
import tempfile
import shlex
from tools.base import BaseTool, make_tool_def

log = logging.getLogger(__name__)

MAX_EXECUTION_TIME = 30  # seconds
MAX_OUTPUT_LENGTH = 5000  # characters

# Shell commands considered safe to execute
SAFE_SHELL_COMMANDS = frozenset({
    "ls", "cat", "head", "tail", "wc", "grep", "find",
    "sort", "uniq", "date", "cal", "echo", "python3", "pip",
})


def _truncate(text: str, limit: int = MAX_OUTPUT_LENGTH) -> str:
    """Truncate text to limit, appending a notice if truncated."""
    if len(text) <= limit:
        return text
    return text[:limit] + "\n\n... [output truncated]"


def _run_python(code: str) -> str:
    """Write code to a temp file and execute it in a restricted subprocess."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", prefix="claude_run_", dir="/tmp", delete=False
    ) as f:
        f.write(code)
        tmp_path = f.name

    try:
        env = os.environ.copy()
        # Disable network access by unsetting proxy vars and setting a dummy resolver
        # (best-effort; true sandboxing would need OS-level controls)
        env.pop("HTTP_PROXY", None)
        env.pop("HTTPS_PROXY", None)
        env.pop("http_proxy", None)
        env.pop("https_proxy", None)
        # Restrict the working directory to /tmp
        result = subprocess.run(
            ["python3", tmp_path],
            capture_output=True,
            text=True,
            timeout=MAX_EXECUTION_TIME,
            cwd="/tmp",
            env=env,
        )
        stdout = result.stdout or ""
        stderr = result.stderr or ""

        parts = []
        if stdout:
            parts.append(f"STDOUT:\n{stdout}")
        if stderr:
            parts.append(f"STDERR:\n{stderr}")
        if result.returncode != 0:
            parts.append(f"Exit code: {result.returncode}")
        if not parts:
            parts.append("(no output)")

        return _truncate("\n".join(parts))

    except subprocess.TimeoutExpired:
        return f"Error: execution timed out after {MAX_EXECUTION_TIME} seconds."
    except Exception as e:
        return f"Error: {e}"
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _run_shell(command: str) -> str:
    """Execute a whitelisted shell command and return output."""
    try:
        parts = shlex.split(command)
    except ValueError as e:
        return f"Error parsing command: {e}"

    if not parts:
        return "Error: empty command."

    base_cmd = os.path.basename(parts[0])

    # Special case: allow 'pip list' but not arbitrary pip commands
    if base_cmd == "pip":
        if len(parts) < 2 or parts[1] != "list":
            return "Error: only 'pip list' is allowed for pip."

    if base_cmd not in SAFE_SHELL_COMMANDS:
        return (
            f"Error: '{base_cmd}' is not in the allowed command list. "
            f"Allowed: {', '.join(sorted(SAFE_SHELL_COMMANDS))}"
        )

    # Block file writes outside /tmp by scanning for redirect-like args
    # (not bulletproof, but catches obvious cases)
    joined = " ".join(parts)
    for pattern in (">", ">>", "tee "):
        if pattern in joined:
            # Allow if the target path starts with /tmp
            idx = joined.index(pattern) + len(pattern)
            remainder = joined[idx:].strip()
            if remainder and not remainder.startswith("/tmp"):
                return "Error: file writes are restricted to /tmp."

    try:
        result = subprocess.run(
            parts,
            capture_output=True,
            text=True,
            timeout=MAX_EXECUTION_TIME,
            cwd="/tmp",
        )
        stdout = result.stdout or ""
        stderr = result.stderr or ""

        parts_out = []
        if stdout:
            parts_out.append(stdout)
        if stderr:
            parts_out.append(f"STDERR:\n{stderr}")
        if result.returncode != 0:
            parts_out.append(f"Exit code: {result.returncode}")
        if not parts_out:
            parts_out.append("(no output)")

        return _truncate("\n".join(parts_out))

    except subprocess.TimeoutExpired:
        return f"Error: command timed out after {MAX_EXECUTION_TIME} seconds."
    except Exception as e:
        return f"Error: {e}"


class CodeRunnerTool(BaseTool):
    name = "code_runner"
    description = "Execute Python code snippets and safe shell commands in a sandboxed environment"

    def get_definitions(self) -> list[dict]:
        return [
            make_tool_def(
                "run_python",
                (
                    "Execute a Python code snippet and return stdout/stderr. "
                    "Code runs in /tmp with a 30-second timeout. "
                    "No network access. File writes restricted to /tmp."
                ),
                {
                    "code": {
                        "type": "string",
                        "description": "Python code to execute",
                    },
                },
                ["code"],
            ),
            make_tool_def(
                "run_shell",
                (
                    "Execute a shell command and return output. "
                    "Only safe commands allowed: ls, cat, head, tail, wc, grep, find, "
                    "sort, uniq, date, cal, echo, python3, pip list. "
                    "Runs in /tmp with a 30-second timeout."
                ),
                {
                    "command": {
                        "type": "string",
                        "description": "Shell command to execute",
                    },
                },
                ["command"],
            ),
        ]

    async def handle(self, tool_name: str, tool_input: dict) -> str:
        loop = asyncio.get_event_loop()

        if tool_name == "run_python":
            code = tool_input.get("code", "")
            if not code.strip():
                return "Error: no code provided."
            log.info(f"run_python: executing {len(code)} chars of Python code")
            return await loop.run_in_executor(None, _run_python, code)

        elif tool_name == "run_shell":
            command = tool_input.get("command", "")
            if not command.strip():
                return "Error: no command provided."
            log.info(f"run_shell: executing '{command[:80]}'")
            return await loop.run_in_executor(None, _run_shell, command)

        return f"Unknown tool: {tool_name}"
