#!/usr/bin/env python3
"""local-code — Two-tier coding assistant.

from core.constants import LOCAL_MODEL
CHAT LAYER:  Opus API — understands intent, plans work, reviews results
BUILD LAYER: Local Qwen (30b-a3b) — writes code, edits files, runs commands
             Escalates to Sonnet API only when local model can't handle it

The user talks to Opus. Opus decides what needs to happen, then delegates
the actual file edits, code generation, and shell commands to the local model.
Result: Opus-quality reasoning at near-zero cost for the actual engineering.

Usage:
    .venv/bin/python tools/local_code.py              # current directory
    .venv/bin/python tools/local_code.py ~/claude-stack  # specific directory

Commands:
    /local <msg>    — force through local model only
    /sonnet <msg>   — force through Sonnet API
    /model          — show current models
    /cost           — show API cost this session
    /clear          — clear conversation history
    /cd <path>      — change working directory
    /run <cmd>      — run a shell command directly
    /files [glob]   — list files matching pattern
    /read <path>    — read a file into context
    /exit           — quit
"""

import asyncio
import json
import os
import re
import readline
import subprocess
import sys
import textwrap
import glob as globmod
from pathlib import Path

from core.constants import LOCAL_MODEL

try:
    import httpx
except ImportError:
    print("Installing httpx...")
    subprocess.run([sys.executable, "-m", "pip", "install", "httpx", "-q"])
    import httpx

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

OLLAMA_URL = "http://localhost:11434/api/chat"
OLLAMA_MODEL = LOCAL_MODEL
ANTHROPIC_URL = os.environ.get("ANTHROPIC_BASE_URL", "http://127.0.0.1:8099").rstrip("/") + "/v1/messages"
ANTHROPIC_MODEL_SONNET = "claude-sonnet-4-20250514"
ANTHROPIC_MODEL_OPUS = "claude-opus-4-20250414"  # Opus requires higher-tier API plan; using Sonnet as chat layer
TRUE_VALUES = {"1", "true", "yes", "on"}


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in TRUE_VALUES


PAID_LLM_DISABLED = _truthy_env("CLAUDE_STACK_DISABLE_PAID_LLM_API")

# Use current process policy only. Do not reload direct Anthropic keys from .env.
ANTHROPIC_API_KEY = ""
if not PAID_LLM_DISABLED:
    ANTHROPIC_API_KEY = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("ANTHROPIC_API_KEY", "")

# ANSI colors
C_RESET = "\033[0m"
C_BOLD = "\033[1m"
C_DIM = "\033[2m"
C_GREEN = "\033[32m"
C_YELLOW = "\033[33m"
C_BLUE = "\033[34m"
C_MAGENTA = "\033[35m"
C_CYAN = "\033[36m"
C_RED = "\033[31m"

# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

OPUS_SYSTEM = """You are the CHAT + PLANNING layer of a two-tier coding assistant.
You talk directly to the user. You understand their intent, plan work, and review results.

Working directory: {cwd}

IMPORTANT: You do NOT write code directly. Instead, you delegate engineering tasks to a local
build model by emitting BUILD blocks. The build model handles file reads, edits, and commands.

To delegate work to the build layer, emit one or more blocks like:

```BUILD
<clear natural-language instruction for the build model>
Include: which files to read, what to change, what commands to run.
Be specific about the desired outcome.
```

You can emit multiple BUILD blocks in one response for parallel tasks.

For simple questions (explaining code, answering questions, discussing architecture),
just respond directly — no BUILD block needed.

After the build model executes, you'll see the results and can:
- Approve and move on
- Request corrections via another BUILD block
- Explain the changes to the user

Keep your responses concise. Lead with the plan, then BUILD blocks."""

BUILD_SYSTEM = """You are a BUILD agent — a fast local coding model that executes engineering tasks.
You receive instructions from a planning layer and execute them precisely.

Working directory: {cwd}

Respond with ONLY the actions needed. Use these formats:

For file edits:
```edit:path/to/file.py
<<<< SEARCH
exact lines to find
====
replacement lines
>>>> END
```

For new files:
```write:path/to/file.py
file content here
```

For shell commands:
```shell
command to run
```

For reading files (when you need context):
```read:path/to/file.py
```

Rules:
- Be precise — exact string matches for edits
- Minimal changes — only what's needed
- No explanations — just actions
- If you need to read a file first, emit a read block"""

# ---------------------------------------------------------------------------
# LLM clients
# ---------------------------------------------------------------------------

class LLMClient:
    def __init__(self):
        self.api_cost = 0.0
        self.local_tokens = 0
        self.api_tokens = 0
        self.opus_history = []   # Chat layer history
        self.build_history = []  # Build layer history (reset per task)

    async def opus_chat(self, user_msg, cwd):
        """Send to Opus API (chat/planning layer)."""
        if PAID_LLM_DISABLED:
            print(f"{C_RED}Anthropic API blocked: CLAUDE_STACK_DISABLE_PAID_LLM_API=1.{C_RESET}")
            return None

        if not ANTHROPIC_API_KEY:
            print(f"{C_RED}No ANTHROPIC_API_KEY found.{C_RESET}")
            return None

        self.opus_history.append({"role": "user", "content": user_msg})

        try:
            async with httpx.AsyncClient(timeout=180) as client:
                resp = await client.post(
                    ANTHROPIC_URL,
                    headers={
                        "x-api-key": ANTHROPIC_API_KEY,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": ANTHROPIC_MODEL_OPUS,
                        "max_tokens": 4096,
                        "system": OPUS_SYSTEM.format(cwd=cwd),
                        "messages": self.opus_history,
                        "stream": True,
                    },
                )
                resp.raise_for_status()

                full_response = []
                input_tokens = 0
                output_tokens = 0

                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line[6:]
                    if data == "[DONE]":
                        break
                    try:
                        event = json.loads(data)
                        if event.get("type") == "content_block_delta":
                            text = event.get("delta", {}).get("text", "")
                            if text:
                                full_response.append(text)
                                print(text, end="", flush=True)
                        elif event.get("type") == "message_delta":
                            output_tokens = event.get("usage", {}).get("output_tokens", 0)
                        elif event.get("type") == "message_start":
                            input_tokens = event.get("message", {}).get("usage", {}).get("input_tokens", 0)
                    except json.JSONDecodeError:
                        continue

                print()
                assistant_msg = "".join(full_response)
                self.opus_history.append({"role": "assistant", "content": assistant_msg})

                self.api_tokens += input_tokens + output_tokens
                self.api_cost += (input_tokens * 15 + output_tokens * 75) / 1_000_000

                return assistant_msg

        except Exception as e:
            print(f"{C_RED}Opus API error: {e}{C_RESET}")
            self.opus_history.pop()
            return None

    async def build_local(self, instruction, cwd):
        """Send to local Ollama (build layer)."""
        messages = [
            {"role": "system", "content": BUILD_SYSTEM.format(cwd=cwd)},
            {"role": "user", "content": instruction},
        ]

        try:
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(OLLAMA_URL, json={
                    "model": OLLAMA_MODEL,
                    "messages": messages,
                    "stream": True,
                    "options": {"num_ctx": 8192},
                })
                resp.raise_for_status()

                full_response = []
                thinking = False
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                        content = chunk.get("message", {}).get("content", "")
                        if content:
                            if "<think>" in content:
                                thinking = True
                                content = content.split("<think>")[0]
                            if "</think>" in content:
                                thinking = False
                                content = content.split("</think>")[-1]
                            if not thinking and content:
                                full_response.append(content)
                                print(content, end="", flush=True)
                        if chunk.get("done"):
                            self.local_tokens += chunk.get("eval_count", 0)
                    except json.JSONDecodeError:
                        continue

                print()
                return "".join(full_response)

        except httpx.ConnectError:
            print(f"{C_RED}Ollama not running. Falling back to Sonnet...{C_RESET}")
            return await self.build_sonnet(instruction, cwd)
        except Exception as e:
            print(f"{C_RED}Local model error: {e}. Falling back to Sonnet...{C_RESET}")
            return await self.build_sonnet(instruction, cwd)

    async def build_sonnet(self, instruction, cwd):
        """Fallback: send build task to Sonnet API."""
        if not ANTHROPIC_API_KEY:
            print(f"{C_RED}No API key — can't fall back to Sonnet.{C_RESET}")
            return None

        print(f"{C_YELLOW}[Sonnet fallback]{C_RESET} ", end="")

        try:
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(
                    ANTHROPIC_URL,
                    headers={
                        "x-api-key": ANTHROPIC_API_KEY,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": ANTHROPIC_MODEL_SONNET,
                        "max_tokens": 4096,
                        "system": BUILD_SYSTEM.format(cwd=cwd),
                        "messages": [{"role": "user", "content": instruction}],
                        "stream": True,
                    },
                )
                resp.raise_for_status()

                full_response = []
                input_tokens = 0
                output_tokens = 0

                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line[6:]
                    if data == "[DONE]":
                        break
                    try:
                        event = json.loads(data)
                        if event.get("type") == "content_block_delta":
                            text = event.get("delta", {}).get("text", "")
                            if text:
                                full_response.append(text)
                                print(text, end="", flush=True)
                        elif event.get("type") == "message_delta":
                            output_tokens = event.get("usage", {}).get("output_tokens", 0)
                        elif event.get("type") == "message_start":
                            input_tokens = event.get("message", {}).get("usage", {}).get("input_tokens", 0)
                    except json.JSONDecodeError:
                        continue

                print()
                self.api_tokens += input_tokens + output_tokens
                self.api_cost += (input_tokens * 3 + output_tokens * 15) / 1_000_000
                return "".join(full_response)

        except Exception as e:
            print(f"{C_RED}Sonnet API error: {e}{C_RESET}")
            return None

    async def direct_local(self, user_msg, cwd):
        """Direct local chat (bypass Opus, for /local command)."""
        messages = [
            {"role": "system", "content": BUILD_SYSTEM.format(cwd=cwd)},
            {"role": "user", "content": user_msg},
        ]

        try:
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(OLLAMA_URL, json={
                    "model": OLLAMA_MODEL,
                    "messages": messages,
                    "stream": True,
                    "options": {"num_ctx": 8192},
                })
                resp.raise_for_status()

                full_response = []
                thinking = False
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                        content = chunk.get("message", {}).get("content", "")
                        if content:
                            if "<think>" in content:
                                thinking = True
                                content = content.split("<think>")[0]
                            if "</think>" in content:
                                thinking = False
                                content = content.split("</think>")[-1]
                            if not thinking and content:
                                full_response.append(content)
                                print(content, end="", flush=True)
                        if chunk.get("done"):
                            self.local_tokens += chunk.get("eval_count", 0)
                    except json.JSONDecodeError:
                        continue

                print()
                return "".join(full_response)

        except Exception as e:
            print(f"{C_RED}Local model error: {e}{C_RESET}")
            return None

    def clear(self):
        self.opus_history = []
        self.build_history = []


# ---------------------------------------------------------------------------
# Action execution
# ---------------------------------------------------------------------------

def execute_actions(response, cwd, auto_approve=False):
    """Parse and execute edit/write/shell/read blocks from build model response."""
    if not response:
        return cwd, ""

    outputs = []

    # Handle read requests
    for match in re.finditer(r'```read:(.+?)\n```', response, re.DOTALL):
        filepath = match.group(1).strip()
        full_path = Path(cwd) / filepath
        if full_path.exists():
            content = full_path.read_text()[:10000]
            outputs.append(f"[{filepath}]\n{content}")
            print(f"{C_DIM}Read: {filepath} ({len(content)} chars){C_RESET}")
        else:
            outputs.append(f"[{filepath}] FILE NOT FOUND")
            print(f"{C_RED}File not found: {filepath}{C_RESET}")

    # Handle shell commands
    for match in re.finditer(r'```shell\n(.*?)\n```', response, re.DOTALL):
        cmd = match.group(1).strip()
        print(f"\n{C_YELLOW}Run:{C_RESET} {cmd}")
        if auto_approve:
            confirm = "y"
        else:
            confirm = input(f"{C_DIM}[y/n]:{C_RESET} ").strip().lower()
        if confirm in ("y", "yes", ""):
            try:
                result = subprocess.run(
                    cmd, shell=True, capture_output=True, text=True,
                    timeout=60, cwd=cwd
                )
                out = ""
                if result.stdout:
                    out += result.stdout[:3000]
                    print(result.stdout[:3000])
                if result.stderr:
                    out += result.stderr[:1000]
                    print(f"{C_RED}{result.stderr[:1000]}{C_RESET}")
                outputs.append(f"$ {cmd}\n{out}")
            except subprocess.TimeoutExpired:
                print(f"{C_RED}Timed out (60s){C_RESET}")
                outputs.append(f"$ {cmd}\nTIMEOUT")

    # Handle file writes
    for match in re.finditer(r'```write:(.+?)\n(.*?)\n```', response, re.DOTALL):
        filepath = match.group(1).strip()
        content = match.group(2)
        full_path = Path(cwd) / filepath
        print(f"\n{C_CYAN}Create:{C_RESET} {filepath} ({len(content)} chars)")
        if auto_approve:
            confirm = "y"
        else:
            confirm = input(f"{C_DIM}[y/n]:{C_RESET} ").strip().lower()
        if confirm in ("y", "yes", ""):
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(content)
            print(f"{C_GREEN}Written.{C_RESET}")
            outputs.append(f"Created {filepath}")

    # Handle file edits
    for match in re.finditer(r'```edit:(.+?)\n<<<< SEARCH\n(.*?)\n====\n(.*?)\n>>>> END\n```', response, re.DOTALL):
        filepath = match.group(1).strip()
        search = match.group(2)
        replace = match.group(3)
        full_path = Path(cwd) / filepath
        print(f"\n{C_MAGENTA}Edit:{C_RESET} {filepath}")
        print(f"{C_RED}- {len(search.splitlines())} lines{C_RESET} → {C_GREEN}+ {len(replace.splitlines())} lines{C_RESET}")
        if auto_approve:
            confirm = "y"
        else:
            confirm = input(f"{C_DIM}[y/n/diff]:{C_RESET} ").strip().lower()
        if confirm == "diff":
            for line in search.splitlines():
                print(f"{C_RED}- {line}{C_RESET}")
            for line in replace.splitlines():
                print(f"{C_GREEN}+ {line}{C_RESET}")
            confirm = input(f"{C_DIM}[y/n]:{C_RESET} ").strip().lower()
        if confirm in ("y", "yes", ""):
            if full_path.exists():
                content = full_path.read_text()
                if search in content:
                    content = content.replace(search, replace, 1)
                    full_path.write_text(content)
                    print(f"{C_GREEN}Applied.{C_RESET}")
                    outputs.append(f"Edited {filepath}")
                else:
                    print(f"{C_RED}Search string not found in file.{C_RESET}")
                    outputs.append(f"FAILED: search string not found in {filepath}")
            else:
                print(f"{C_RED}File not found: {filepath}{C_RESET}")
                outputs.append(f"FAILED: file not found {filepath}")

    return cwd, "\n".join(outputs)


def extract_build_blocks(response):
    """Extract BUILD instruction blocks from Opus response."""
    blocks = []
    for match in re.finditer(r'```BUILD\n(.*?)\n```', response, re.DOTALL):
        blocks.append(match.group(1).strip())
    return blocks


# ---------------------------------------------------------------------------
# Main REPL
# ---------------------------------------------------------------------------

async def main():
    cwd = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
    if not Path(cwd).is_dir():
        print(f"Not a directory: {cwd}")
        sys.exit(1)
    cwd = str(Path(cwd).resolve())

    llm = LLMClient()

    print(f"{C_BOLD}{C_CYAN}local-code{C_RESET} — Opus chat + local build")
    print(f"{C_DIM}Chat: Opus 4.6 | Build: {OLLAMA_MODEL} (→ Sonnet fallback){C_RESET}")
    print(f"{C_DIM}Dir: {cwd} | /help for commands{C_RESET}\n")

    histfile = Path.home() / ".local_code_history"
    try:
        readline.read_history_file(str(histfile))
    except FileNotFoundError:
        pass

    while True:
        try:
            prompt = f"{C_GREEN}{Path(cwd).name}{C_RESET} {C_BOLD}>{C_RESET} "
            user_input = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print(f"\n{C_DIM}Session: ${llm.api_cost:.4f} API | {llm.local_tokens} local tokens{C_RESET}")
            break

        if not user_input:
            continue

        readline.write_history_file(str(histfile))

        # --- Commands ---
        if user_input in ("/exit", "/quit"):
            print(f"{C_DIM}Session: ${llm.api_cost:.4f} API | {llm.local_tokens} local tokens{C_RESET}")
            break

        elif user_input == "/clear":
            llm.clear()
            print(f"{C_DIM}History cleared.{C_RESET}")
            continue

        elif user_input == "/model":
            print(f"{C_DIM}Chat: {ANTHROPIC_MODEL_OPUS} | Build: {OLLAMA_MODEL} → {ANTHROPIC_MODEL_SONNET}{C_RESET}")
            continue

        elif user_input == "/cost":
            print(f"{C_DIM}API: ${llm.api_cost:.4f} ({llm.api_tokens} tokens) | "
                  f"Local: {llm.local_tokens} tokens ($0){C_RESET}")
            continue

        elif user_input == "/help":
            print(textwrap.dedent("""
                /local <msg>   — bypass Opus, send directly to local model
                /sonnet <msg>  — bypass Opus, send directly to Sonnet API
                /model         — show current models
                /cost          — show session cost
                /clear         — clear conversation history
                /cd <path>     — change working directory
                /run <cmd>     — run shell command
                /files [glob]  — list files (default: *.py)
                /read <path>   — read file into chat context
                /exit          — quit
            """).strip())
            continue

        elif user_input.startswith("/cd "):
            new_dir = user_input[4:].strip()
            new_path = Path(new_dir).expanduser()
            if not new_path.is_absolute():
                new_path = Path(cwd) / new_path
            new_path = new_path.resolve()
            if new_path.is_dir():
                cwd = str(new_path)
                print(f"{C_DIM}→ {cwd}{C_RESET}")
            else:
                print(f"{C_RED}Not a directory: {new_path}{C_RESET}")
            continue

        elif user_input.startswith("/run "):
            cmd = user_input[5:].strip()
            try:
                result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60, cwd=cwd)
                if result.stdout:
                    print(result.stdout[:5000])
                if result.stderr:
                    print(f"{C_RED}{result.stderr[:2000]}{C_RESET}")
            except subprocess.TimeoutExpired:
                print(f"{C_RED}Timed out.{C_RESET}")
            continue

        elif user_input.startswith("/files"):
            pattern = user_input[6:].strip() or "**/*.py"
            matches = sorted(globmod.glob(os.path.join(cwd, pattern), recursive=True))
            for m in matches[:50]:
                print(f"{C_DIM}{os.path.relpath(m, cwd)}{C_RESET}")
            if len(matches) > 50:
                print(f"{C_DIM}...and {len(matches)-50} more{C_RESET}")
            continue

        elif user_input.startswith("/read "):
            filepath = user_input[6:].strip()
            full_path = Path(cwd) / filepath
            if full_path.exists():
                content = full_path.read_text()[:15000]
                # Add to Opus context
                llm.opus_history.append({"role": "user", "content": f"[File: {filepath}]\n```\n{content}\n```"})
                llm.opus_history.append({"role": "assistant", "content": f"Got it — I've read {filepath} ({len(content)} chars)."})
                print(f"{C_DIM}Loaded {filepath} into context ({len(content)} chars){C_RESET}")
            else:
                print(f"{C_RED}Not found: {filepath}{C_RESET}")
            continue

        elif user_input.startswith("/local "):
            msg = user_input[7:].strip()
            print(f"{C_DIM}[local]{C_RESET} ", end="")
            response = await llm.direct_local(msg, cwd)
            execute_actions(response, cwd)
            continue

        elif user_input.startswith("/sonnet "):
            msg = user_input[8:].strip()
            print(f"{C_YELLOW}[Sonnet]{C_RESET} ", end="")
            response = await llm.build_sonnet(msg, cwd)
            execute_actions(response, cwd)
            continue

        # --- Default: Opus chat ---
        print(f"{C_BLUE}[Opus]{C_RESET} ", end="")
        opus_response = await llm.opus_chat(user_input, cwd)
        if not opus_response:
            continue

        # Extract BUILD blocks and delegate to local model
        build_blocks = extract_build_blocks(opus_response)
        if build_blocks:
            for i, instruction in enumerate(build_blocks):
                print(f"\n{C_YELLOW}[Build {i+1}/{len(build_blocks)}]{C_RESET} ", end="")
                build_response = await llm.build_local(instruction, cwd)

                # Execute any actions from the build response
                _, action_output = execute_actions(build_response, cwd)

                # Feed results back to Opus context
                if action_output:
                    result_msg = f"Build results:\n{action_output}"
                    llm.opus_history.append({"role": "user", "content": result_msg})
                    # Brief Opus acknowledgment (don't print, just context)
                    llm.opus_history.append({"role": "assistant", "content": "Build actions applied."})


if __name__ == "__main__":
    asyncio.run(main())
