"""Entry point for ``python -m coding_harness`` — starts the MCP server.

This makes ``python -m coding_harness`` behave identically to
``coding-harness-mcp``, which is convenient for MCP client configurations
that accept a ``python -m`` module path.
"""

from coding_harness.mcp_server import main

main()
