"""Hermes-style tool registry + agent loop for Noya."""
from . import tools  # noqa: F401 — registers tools on import
from botapp.research.chat_tools import register_chat_tools as _register_research_chat_tools

_register_research_chat_tools()
