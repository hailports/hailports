"""Base class for all tool integrations."""

from __future__ import annotations
from abc import ABC, abstractmethod


class BaseTool(ABC):
    """All tools inherit from this. Implement get_definitions() and handle()."""

    name: str
    description: str

    @abstractmethod
    def get_definitions(self) -> list[dict]:
        """Return Anthropic tool_use schema dicts for all methods this tool exposes."""
        ...

    @abstractmethod
    async def handle(self, tool_name: str, tool_input: dict) -> str:
        """Execute a tool call by name and return the result string."""
        ...


def make_tool_def(name: str, description: str, properties: dict, required=None) -> dict:
    """Helper to build an Anthropic tool_use definition."""
    schema = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return {"name": name, "description": description, "input_schema": schema}
