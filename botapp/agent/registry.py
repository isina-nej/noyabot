"""Hermes-style tool registry for Noya agent."""
from __future__ import annotations
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict  # JSON Schema
    handler: Callable[..., Any] = None
    emoji: str = "🔧"

    def to_openai(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    """Singleton tool registry — mirrors tools/registry.py pattern."""

    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool):
        self._tools[tool.name] = tool
        logger.debug(f"Registered tool: {tool.emoji} {tool.name}")

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def definitions(self) -> list[dict]:
        return [t.to_openai() for t in self._tools.values()]

    def names(self) -> list[str]:
        return list(self._tools.keys())

    def dispatch(self, name: str, args: dict) -> str | Any:
        """Sync dispatch — returns str. For async handlers, use dispatch_async."""
        tool = self.get(name)
        if not tool:
            return f"Error: unknown tool '{name}'"
        try:
            import inspect
            sig = inspect.signature(tool.handler)
            if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
                filtered = args
            else:
                valid = set(sig.parameters.keys()) - {'self'}
                filtered = {k: v for k, v in args.items() if k in valid}
            result = tool.handler(**filtered)
            if hasattr(result, "__await__"):
                raise RuntimeError(f"Tool {name} is async — use dispatch_async()")
            return str(result) if result is not None else "(no result)"
        except Exception as e:
            logger.exception(f"Tool {name} error")
            return f"Error executing {name}: {type(e).__name__}: {e}"

    async def dispatch_async(self, name: str, args: dict) -> str:
        """Async dispatch — supports both sync and async handlers."""
        tool = self.get(name)
        if not tool:
            return f"Error: unknown tool '{name}'"
        try:
            import inspect
            sig = inspect.signature(tool.handler)
            if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
                filtered = args
            else:
                valid = set(sig.parameters.keys()) - {'self'}
                filtered = {k: v for k, v in args.items() if k in valid}
            result = tool.handler(**filtered)
            if inspect.isawaitable(result):
                result = await result
            return str(result) if result is not None else "(no result)"
        except Exception as e:
            logger.exception(f"Tool {name} error")
            return f"Error executing {name}: {type(e).__name__}: {e}"


# Global singleton
registry = ToolRegistry()


def register_tool(name, description, *, input_schema=None, parameters=None,
                   requires_confirmation=True, human_verb=None,
                   handler=None, emoji="🔧", **extra):
    """Convenience wrapper used by botapp.agent_tools.*"""
    params = parameters or input_schema or {"type": "object", "properties": {}}
    tool = Tool(name=name, description=description, parameters=params,
                handler=handler, emoji=emoji)
    tool.requires_confirmation = requires_confirmation
    tool.human_verb = human_verb
    registry.register(tool)
    return tool
