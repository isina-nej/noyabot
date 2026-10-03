"""Shared tool registry for the Noya chat loop and the admin agent.

Single registry, two strictly separated catalogs:

- chat catalog (``category="chat"``): exactly the five conversational
  tools (web_search, fetch_url, get_time, generate_image,
  text_to_speech). The normal Noya loop in ``botapp/agent/loop.py`` only
  ever offers ``chat_definitions()`` to the model.
- admin catalog (``category="admin"``): group/channel/member/message/
  settings/analytics/audit/harness tools. They are resolved only through
  ``botapp/agent/orchestrator.py``, which enforces admin permission, bot
  capability, risk level, confirmation and reply-only targets.

Registration never implies authorization: admin metadata (permission,
capability, risk_level, requires_confirmation, target_kind,
input_schema) is stored explicitly on the Tool and re-checked by the
orchestrator at request time and again at confirmation-execution time.
``dispatch``/``dispatch_async`` (the chat-loop execution path) refuse
admin-category tools outright, so even a disobedient model cannot reach
an admin handler through the chat loop.
"""
from __future__ import annotations
import logging
from dataclasses import dataclass
from typing import Any, Callable

from .errors import InvalidToolInput, UnknownTool
from .risk import RISK_ORDER

logger = logging.getLogger(__name__)

CHAT = "chat"
ADMIN = "admin"
VALID_CATEGORIES = frozenset({CHAT, ADMIN})
VALID_TARGET_KINDS = frozenset({"none", "member", "message"})


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict | None = None  # legacy JSON-schema dict (chat tools)
    handler: Callable[..., Any] = None
    emoji: str = "🔧"
    # Admin-contract metadata. Strict fail-closed defaults: no permission,
    # highest risk, confirmation required, not AI-selectable.
    input_schema: Any = None  # Pydantic BaseModel subclass for admin tools
    permission: str | None = None
    capability: str | None = None
    risk_level: str = "high"
    requires_confirmation: bool = True
    target_kind: str = "none"
    human_verb: str | None = None
    ai_selectable: bool = False
    category: str = ADMIN

    def __post_init__(self):
        if self.parameters is None:
            self.parameters = {}
        if self.category not in VALID_CATEGORIES:
            raise ValueError(f"unknown tool category for {self.name!r}: {self.category!r}")
        if self.target_kind not in VALID_TARGET_KINDS:
            raise ValueError(f"unknown target_kind for {self.name!r}: {self.target_kind!r}")
        if self.risk_level not in RISK_ORDER:
            raise ValueError(f"unknown risk_level for {self.name!r}: {self.risk_level!r}")

    def schema_dict(self) -> dict:
        """JSON schema for this tool: Pydantic model wins, legacy dict next."""
        raw = self.input_schema
        if raw is not None:
            if hasattr(raw, "model_json_schema"):
                try:
                    return raw.model_json_schema()
                except Exception:
                    pass
            if hasattr(raw, "schema"):
                try:
                    return raw.schema()
                except Exception:
                    pass
            if isinstance(raw, dict):
                return raw
        if isinstance(self.parameters, dict) and self.parameters:
            return self.parameters
        return {"type": "object", "properties": {}}

    def to_openai(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.schema_dict(),
            },
        }

    def validate_params(self, params: dict | None):
        """Validate raw params against this tool's Pydantic ``input_schema``.

        Returns the validated model instance. Raises
        :class:`InvalidToolInput` (an :class:`AgentError`) on any failure so
        callers get a controlled, user-safe error — never a raw traceback.
        """
        from pydantic import BaseModel, ValidationError

        schema = self.input_schema
        if not (isinstance(schema, type) and issubclass(schema, BaseModel)):
            raise InvalidToolInput()
        try:
            return schema.model_validate(dict(params or {}))
        except ValidationError as exc:
            raise InvalidToolInput() from exc


class ToolRegistry:
    """Singleton tool registry — one store, two catalogs (chat / admin).

    ``get``/``has``/``all``/``names``/``ai_catalog`` expose the *admin*
    catalog (the historical contract used by the orchestrator, harness and
    tests). Chat tools live in the same store but are reachable only via
    ``chat_definitions``/``dispatch``/``dispatch_async``.
    """

    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool):
        self._tools[tool.name] = tool
        logger.debug(f"Registered tool: {tool.emoji} {tool.name} [{tool.category}]")

    def _admin_tools(self) -> list[Tool]:
        return [t for t in self._tools.values() if t.category == ADMIN]

    def _chat_tools(self) -> list[Tool]:
        return [t for t in self._tools.values() if t.category == CHAT]

    def get(self, name: str) -> Tool:
        """Return an *admin* tool or raise :class:`UnknownTool`."""
        tool = self._tools.get(name)
        if tool is None or tool.category != ADMIN:
            raise UnknownTool()
        return tool

    def has(self, name: str) -> bool:
        """True iff ``name`` is a registered *admin* tool."""
        tool = self._tools.get(name)
        return tool is not None and tool.category == ADMIN

    def all(self) -> list[Tool]:
        """All registered *admin* tools (orchestrator/harness contract)."""
        return self._admin_tools()

    def all_tools(self) -> list[Tool]:
        """Every registered tool, both catalogs (introspection only)."""
        return list(self._tools.values())

    def names(self) -> list[str]:
        return [t.name for t in self._admin_tools()]

    def ai_catalog(self) -> list[dict]:
        """Allowlist handed to the admin-agent model (selectable tools only)."""
        return [
            {
                "tool": t.name,
                "description": t.description,
                "parameters": t.schema_dict(),
            }
            for t in self._admin_tools()
            if t.ai_selectable
        ]

    def definitions(self, category: str | None = None) -> list[dict]:
        """OpenAI function definitions, optionally filtered by catalog."""
        tools = self._tools.values() if category is None else (
            self._chat_tools() if category == CHAT else self._admin_tools()
        )
        return [t.to_openai() for t in tools]

    def chat_definitions(self) -> list[dict]:
        """Only the chat catalog — the sole source for the Noya loop."""
        return self.definitions(category=CHAT)

    def admin_definitions(self) -> list[dict]:
        return self.definitions(category=ADMIN)

    def _checked_chat_tool(self, name: str) -> Tool | str:
        tool = self._tools.get(name)
        if tool is None:
            return f"Error: unknown tool '{name}'"
        if tool.category != CHAT:
            return f"Error: tool '{name}' is not available in this context"
        return tool

    def dispatch(self, name: str, args: dict) -> str | Any:
        """Sync dispatch — chat-loop path only. Returns str, never raises."""
        tool = self._checked_chat_tool(name)
        if isinstance(tool, str):
            return tool
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
        """Async dispatch — chat-loop path only. Returns str, never raises."""
        tool = self._checked_chat_tool(name)
        if isinstance(tool, str):
            return tool
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
                  permission, risk_level, requires_confirmation=True,
                  handler=None, emoji="🔧", capability=None,
                  target_kind="none", human_verb=None,
                  ai_selectable=True, category=ADMIN):
    """Register one admin tool with its full authorization metadata.

    ``permission`` and ``risk_level`` are required (no insecure defaults):
    omitting them is a ``TypeError`` at import time. Unknown keyword
    arguments are a ``TypeError`` as well — metadata is never silently
    discarded. ``handler`` must be an async callable ``(ctx, bot,
    validated)``.
    """
    from .permissions import CAP_DELETE, CAP_PIN, CAP_POST, CAP_RESTRICT

    if category not in VALID_CATEGORIES:
        raise ValueError(f"unknown tool category for {name!r}: {category!r}")
    if risk_level not in RISK_ORDER:
        raise ValueError(f"unknown risk_level for {name!r}: {risk_level!r}")
    if target_kind not in VALID_TARGET_KINDS:
        raise ValueError(f"unknown target_kind for {name!r}: {target_kind!r}")
    if not permission or not isinstance(permission, str):
        raise ValueError(f"permission is required for {name!r}")
    if capability is not None and capability not in {
        CAP_RESTRICT, CAP_DELETE, CAP_PIN, CAP_POST,
    }:
        raise ValueError(f"unknown capability for {name!r}: {capability!r}")
    if not callable(handler):
        raise ValueError(f"handler is required for {name!r}")
    import inspect
    if not inspect.iscoroutinefunction(handler):
        raise ValueError(f"handler for {name!r} must be async (ctx, bot, validated)")

    tool = Tool(
        name=name,
        description=description,
        parameters=parameters if isinstance(parameters, dict) else None,
        handler=handler,
        emoji=emoji,
        input_schema=input_schema,
        permission=permission,
        capability=capability,
        risk_level=risk_level,
        requires_confirmation=requires_confirmation,
        target_kind=target_kind,
        human_verb=human_verb,
        ai_selectable=ai_selectable,
        category=category,
    )
    # Legacy dict schemas flow through Tool.parameters; Pydantic classes
    # stay on input_schema for validate_params()/schema_dict().
    if input_schema is None and isinstance(parameters, dict):
        tool.parameters = parameters
    registry.register(tool)
    return tool
