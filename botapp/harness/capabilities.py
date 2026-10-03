"""Harness-facing capability/access classification over the frozen Registry.

Read-only view. Never registers tools, never authorizes, never executes.
Authorization stays in ``orchestrator.py`` + ``permissions.py``; execution in
the orchestrator / harness tool executor. This module only classifies live
Registry metadata so the harness understands: chat vs admin, read vs
privileged/write, permission, capability, risk, confirmation, targets,
AI-selectability, schemas, and evidence/read suitability.

Frozen refs: HARNESS_ARCHITECTURE Sec 2 (C1, C2, C4, C5, C7), Sec 7.2,
Sec 11, Sec 15. Registry P0 baseline ``21ca0ac``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from botapp.agent.errors import UnknownTool
from botapp.agent.registry import ADMIN, CHAT, registry
from botapp.agent.risk import HIGH, LOW

# Chat catalog is exactly these 5 (frozen Sec 11). Validated live, never registered here.
CHAT_TOOL_NAMES = frozenset(
    {"web_search", "fetch_url", "get_time", "generate_image", "text_to_speech"}
)

# Single multi-step entry (frozen Sec 15). Compared by exact equality only.
HARNESS_ENTRY_TOOL = "harness.investigate"

# Internal sandbox-only step (frozen Sec 15). Not a Registry tool, never
# AI-selectable, never Registry-routed. Computes over observations payload.
RUN_CODE_STEP = "run_code"

READ = "read"
WRITE = "write"  # privileged/write behavior: anything not (low + no-confirm)


@dataclass(frozen=True)
class ToolCapability:
    name: str
    catalog: str  # "chat" | "admin"
    access: str  # "read" | "write"
    permission: str | None
    capability: str | None
    risk_level: str
    requires_confirmation: bool
    effective_requires_confirmation: bool  # C4: registry flag OR risk == high
    target_kind: str
    ai_selectable: bool
    input_schema: str | None
    evidence_suitable: bool
    harness_eligible: bool


def is_internal_step(name: str) -> bool:
    """Exact-equality check. ``run_code`` is sandbox-only, not a Registry tool."""
    return (name or "") == RUN_CODE_STEP


def _schema_name(tool) -> str | None:
    schema = tool.input_schema
    name = getattr(schema, "__name__", None)
    if isinstance(name, str) and name:
        return name
    if isinstance(tool.parameters, dict) and tool.parameters:
        return "legacy-dict"
    return None


def _accepts_empty_params(tool) -> bool:
    try:
        tool.validate_params({})
    except Exception:
        return False
    return True


def is_read_behavior(tool) -> bool:
    # ponytail: access derived from (risk, requires_confirmation); upgrade to
    # explicit Tool.access/effects flag owned by G if catalog needs true side-effect tiers.
    return tool.risk_level == LOW and not tool.requires_confirmation


def effective_requires_confirmation(tool) -> bool:
    """C4 single-step rule: registry flag wins upward; high risk always confirms."""
    return bool(tool.requires_confirmation or tool.risk_level == HIGH)


def is_evidence_suitable(tool) -> bool:
    # ponytail: suitability probed via validate_params({}); upgrade to explicit
    # evidence_suitable/harness_eligible flag owned by G if inference ever diverges.
    if tool.category != ADMIN:
        return False
    if tool.name == HARNESS_ENTRY_TOOL:
        return False
    if not is_read_behavior(tool):
        return False
    if tool.target_kind != "none":
        return False
    return _accepts_empty_params(tool)


def is_harness_eligible(tool) -> bool:
    """C7/V6 predicate. Explicit metadata comparison only, no name prefixes."""
    return (
        tool.category == ADMIN
        and tool.risk_level == LOW
        and not tool.requires_confirmation
        and tool.name != HARNESS_ENTRY_TOOL
        and tool.target_kind == "none"
        and _accepts_empty_params(tool)
    )


def classify(tool) -> ToolCapability:
    access = READ if is_read_behavior(tool) else WRITE
    suitable = is_evidence_suitable(tool)
    return ToolCapability(
        name=tool.name,
        catalog=tool.category,
        access=access,
        permission=tool.permission,
        capability=tool.capability,
        risk_level=tool.risk_level,
        requires_confirmation=bool(tool.requires_confirmation),
        effective_requires_confirmation=effective_requires_confirmation(tool),
        target_kind=tool.target_kind,
        ai_selectable=bool(tool.ai_selectable),
        input_schema=_schema_name(tool),
        evidence_suitable=suitable,
        harness_eligible=is_harness_eligible(tool),
    )


def _find_tool(name: str):
    for tool in registry.all_tools():
        if tool.name == name:
            return tool
    return None


def get_capability(name: str) -> ToolCapability | None:
    """Classify one Registry tool. None when unknown; None for ``run_code``."""
    if is_internal_step(name):
        return None
    tool = _find_tool(name)
    return classify(tool) if tool is not None else None


def require_capability(name: str) -> ToolCapability:
    cap = get_capability(name)
    if cap is None:
        raise UnknownTool()
    return cap


def chat_capabilities() -> list[ToolCapability]:
    return sorted(
        (classify(t) for t in registry.all_tools() if t.category == CHAT),
        key=lambda c: c.name,
    )


def admin_capabilities() -> list[ToolCapability]:
    return sorted(
        (classify(t) for t in registry.all_tools() if t.category == ADMIN),
        key=lambda c: c.name,
    )


def validate_chat_catalog() -> tuple[bool, set[str], set[str]]:
    """(ok, extra, missing) of live chat catalog vs frozen 5."""
    live = {c.name for c in chat_capabilities()}
    return (live == CHAT_TOOL_NAMES, live - CHAT_TOOL_NAMES, CHAT_TOOL_NAMES - live)


def build_harness_allowlist() -> list[str]:
    """Frozen-at-run-start C7 allowlist, computed from live Registry."""
    return sorted(t.name for t in registry.all() if is_harness_eligible(t))


def is_allowlisted(name: str, allowlist: list[str]) -> bool:
    return name in allowlist and not is_internal_step(name)


def is_ai_selectable(name: str) -> bool:
    if is_internal_step(name):
        return False
    tool = _find_tool(name)
    return bool(tool is not None and tool.ai_selectable)


def validate_params(name: str, params: dict | None):
    """Delegate to the Registry tool's strict schema. Unknown -> UnknownTool."""
    tool = _find_tool(name)
    if tool is None or is_internal_step(name):
        raise UnknownTool()
    return tool.validate_params(dict(params or {}))


def schema_dict(name: str) -> dict:
    tool = _find_tool(name)
    if tool is None or is_internal_step(name):
        raise UnknownTool()
    return tool.schema_dict()


def capability_matrix(include_internal: bool = True) -> list[dict[str, Any]]:
    """JSON-safe rows for every Registry tool (+ ``run_code`` internal row)."""
    rows = [asdict(c) for c in chat_capabilities()]
    rows += [asdict(c) for c in admin_capabilities()]
    for row in rows:
        row["internal"] = False
    if include_internal:
        rows.append(
            {
                "name": RUN_CODE_STEP,
                "catalog": "internal",
                "access": "compute",
                "permission": None,
                "capability": None,
                "risk_level": "internal",
                "requires_confirmation": False,
                "effective_requires_confirmation": False,
                "target_kind": "none",
                "ai_selectable": False,
                "input_schema": "observations-payload",
                "evidence_suitable": False,
                "harness_eligible": False,
                "internal": True,
            }
        )
    return rows


if __name__ == "__main__":  # runnable self-check, no frameworks
    assert CHAT_TOOL_NAMES == {
        "web_search",
        "fetch_url",
        "get_time",
        "generate_image",
        "text_to_speech",
    }
    assert is_internal_step("run_code") and not is_internal_step("member.ban")
    assert not is_ai_selectable("run_code")
    assert get_capability("run_code") is None
    assert get_capability("no.such.tool") is None
    ok, extra, missing = validate_chat_catalog()
    assert ok, (extra, missing)
    allow = build_harness_allowlist()
    assert HARNESS_ENTRY_TOOL not in allow
    assert RUN_CODE_STEP not in allow
    for row in capability_matrix():
        if not row["internal"]:
            assert row["catalog"] in (CHAT, ADMIN)
            assert row["access"] in (READ, WRITE)
    print("capabilities self-check OK")
