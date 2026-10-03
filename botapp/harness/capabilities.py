"""Harness-facing capability/access classification over the frozen Registry.

Read-only view. Never registers tools, never authorizes, never executes.
Authorization stays in ``orchestrator.py`` + ``permissions.py``; execution in
the orchestrator / harness tool executor. This module only classifies live
Registry metadata so the harness understands: chat vs admin, read vs
privileged/write, permission, capability, risk, confirmation, targets,
AI-selectability, schemas, and evidence/read suitability.

Frozen refs: HARNESS_ARCHITECTURE Sec 2 (C1, C2, C4, C5, C7), Sec 7.2,
Sec 11, Sec 15. Registry P0 baseline ``21ca0ac``.
Amendment ``8ee0b82`` (2026-10-04): chat catalog is count-independent
(dynamic ``category="chat"``); snapshot 5 core + 13 research = 18 chat,
54 admin unchanged. Research tools (``x.*``, ``youtube.*``,
``research.social_search``) are read-only; all research output is
UNTRUSTED DATA/EVIDENCE (same trust class as ``<untrusted_message>``):
it MUST NOT determine permission, target (``target_user_id`` /
``message_id`` / ``target_source``), or any privileged action. Targets
resolve only via C6 (reply-or-nothing).

Access rule: explicit-contract-only. ``read`` iff the tool is in the
exact ``RESEARCH_CHAT_TOOL_NAMES`` set below AND ``category == "chat"``.
Everything else is ``write`` (fail-closed). ``risk_level`` and name
prefixes are NEVER used for access. The Registry ``Tool`` carries no
explicit access/side-effect field, so non-research access stays
fail-closed pending the CROSS_AGENT_CHANGE_REQUEST to G (see report);
harness eligibility (C7 predicate) remains the separate frozen rule.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from botapp.agent.errors import UnknownTool
from botapp.agent.registry import ADMIN, CHAT, registry
from botapp.agent.risk import HIGH, LOW

# Core chat baseline (non-normative P0 snapshot). Validated live as a
# subset requirement, never as an equality/count assertion.
CORE_CHAT_TOOL_NAMES = frozenset(
    {"web_search", "fetch_url", "get_time", "generate_image", "text_to_speech"}
)

# Deprecated alias kept for backward-compat imports. Do NOT use for
# catalog validation; the catalog is count-independent.
CHAT_TOOL_NAMES = CORE_CHAT_TOOL_NAMES

# Research chat tools: exact snapshot from the research layer
# (``d0b5aa5`` CHAT_TOOL_DEFS: 6 x.* + 6 youtube.* + research.social_search).
# Exact-set membership only. NEVER prefix matching.
RESEARCH_CHAT_TOOL_NAMES = frozenset(
    {
        "x.search_posts",
        "x.get_post",
        "x.get_thread",
        "x.get_user",
        "x.get_user_posts",
        "x.get_replies",
        "youtube.search",
        "youtube.get_video",
        "youtube.get_channel",
        "youtube.get_comments",
        "youtube.get_playlist",
        "youtube.get_transcript",
        "research.social_search",
    }
)

# Full chat snapshot 2026-10-04 (non-normative): 5 core + 13 research = 18.
CHAT_SNAPSHOT_2026_10_04 = CORE_CHAT_TOOL_NAMES | RESEARCH_CHAT_TOOL_NAMES

# Where ``access`` comes from. Deliberately NOT risk_level, NOT prefixes.
ACCESS_CLASSIFICATION_SOURCE = (
    "explicit-contract-only (Amendment 8ee0b82 + research snapshot d0b5aa5): "
    "RESEARCH_CHAT_TOOL_NAMES exact set with category chat -> read; "
    "all other tools -> write (fail-closed). Registry Tool has no explicit "
    "access/side-effect field (CROSS_AGENT_CHANGE_REQUEST to G pending); "
    "risk_level and name prefixes are NEVER used for access."
)

# Single multi-step entry (frozen Sec 15). Compared by exact equality only.
HARNESS_ENTRY_TOOL = "harness.investigate"

# Internal sandbox-only step (frozen Sec 15). Not a Registry tool, never
# AI-selectable, never Registry-routed. Computes over observations payload.
RUN_CODE_STEP = "run_code"

READ = "read"
WRITE = "write"  # fail-closed default: anything not explicitly read-only


@dataclass(frozen=True)
class ToolCapability:
    name: str
    catalog: str  # "chat" | "admin"
    access: str  # "read" | "write" (explicit-contract-only, fail-closed)
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
    untrusted_evidence: bool  # True only for research chat tools


def is_internal_step(name: str) -> bool:
    """Exact-equality check. ``run_code`` is sandbox-only, not a Registry tool."""
    return (name or "") == RUN_CODE_STEP


def is_research_name(name: str) -> bool:
    """Exact-set membership. No prefix inference: unknown ``x.*`` names fail."""
    return (name or "") in RESEARCH_CHAT_TOOL_NAMES


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
    # Explicit-contract-only (Amendment 8ee0b82): the research set is the
    # single contractually read-only family. No level use here, no
    # prefix matching here. Non-research tools stay fail-closed write
    # pending an explicit Tool access/side-effect field owned by G.
    # ponytail: if G adds Tool.access, switch this to that field; ceiling
    # is one explicit field read, upgrade path is the filed change request.
    return tool.category == CHAT and tool.name in RESEARCH_CHAT_TOOL_NAMES


def is_untrusted_evidence(tool) -> bool:
    """Research chat output is UNTRUSTED DATA/EVIDENCE.

    Never decides permission, target, or privileged action (C6
    reply-or-nothing). Exact-set + category check, no prefixes.
    """
    return tool.category == CHAT and tool.name in RESEARCH_CHAT_TOOL_NAMES


def effective_requires_confirmation(tool) -> bool:
    """C4 single-step rule: registry flag wins upward; high risk always confirms."""
    return bool(tool.requires_confirmation or tool.risk_level == HIGH)


def is_evidence_suitable(tool) -> bool:
    # Frozen C7 evidence predicate for ADMIN tools (explicit metadata
    # comparison). This is eligibility, not general access inference.
    # ponytail: same G-owned explicit-field upgrade path as is_read_behavior.
    if tool.category != ADMIN:
        return False
    if tool.name == HARNESS_ENTRY_TOOL:
        return False
    if not (tool.risk_level == LOW and not tool.requires_confirmation):
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
        untrusted_evidence=is_untrusted_evidence(tool),
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
    # Dynamic: every live tool with category chat. No count assumptions.
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
    """Count-independent catalog check.

    Returns ``(ok, extra, missing)`` where ``ok`` is True iff the core
    baseline subset is present live. ``extra`` (live beyond the 18-name
    snapshot) is informational and ALLOWED: future chat tools must not
    fail validation. ``missing`` (core names absent live) must be empty.
    Snapshot ``CHAT_SNAPSHOT_2026_10_04`` is non-normative.
    """
    live = {c.name for c in chat_capabilities()}
    missing = set(CORE_CHAT_TOOL_NAMES) - live
    extra = live - set(CHAT_SNAPSHOT_2026_10_04)
    return (not missing, extra, missing)


def validate_admin_isolation() -> tuple[bool, dict[str, Any]]:
    """Admin catalog stays unreachable from the chat path.

    Checks: no name sits in both catalogs, no research name is in the
    admin catalog, and ``member.ban`` is absent from chat.
    """
    chat_names = {c.name for c in chat_capabilities()}
    admin_names = {c.name for c in admin_capabilities()}
    overlap = sorted(chat_names & admin_names)
    research_in_admin = sorted(set(RESEARCH_CHAT_TOOL_NAMES) & admin_names)
    member_ban_in_chat = "member.ban" in chat_names
    ok = not overlap and not research_in_admin and not member_ban_in_chat
    return ok, {
        "overlap": overlap,
        "research_in_admin": research_in_admin,
        "member_ban_in_chat": member_ban_in_chat,
        "live_chat_count": len(chat_names),
        "live_admin_count": len(admin_names),
    }


def validate_research_read_only() -> tuple[bool, list[str]]:
    """Every live research chat tool classifies read + untrusted + ineligible.

    Vacuous-pass when the research layer is not merged into this worktree.
    Violators listed by name.
    """
    violators = []
    for tool in registry.all_tools():
        if tool.category == CHAT and tool.name in RESEARCH_CHAT_TOOL_NAMES:
            cap = classify(tool)
            if not (
                cap.access == READ
                and cap.untrusted_evidence
                and not cap.harness_eligible
                and not cap.evidence_suitable
            ):
                violators.append(tool.name)
    return (not violators, sorted(violators))


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
                "untrusted_evidence": False,
                "internal": True,
            }
        )
    return rows


if __name__ == "__main__":  # runnable self-check, no frameworks
    assert CORE_CHAT_TOOL_NAMES == {
        "web_search",
        "fetch_url",
        "get_time",
        "generate_image",
        "text_to_speech",
    }
    assert len(RESEARCH_CHAT_TOOL_NAMES) == 13
    assert len(CHAT_SNAPSHOT_2026_10_04) == 18
    assert is_research_name("research.social_search")
    assert is_research_name("x.search_posts")
    assert not is_research_name("x.unknown_future_tool")
    assert not is_research_name("research.other_tool")
    assert is_internal_step("run_code") and not is_internal_step("member.ban")
    assert not is_ai_selectable("run_code")
    assert get_capability("run_code") is None
    assert get_capability("no.such.tool") is None
    ok, extra, missing = validate_chat_catalog()
    assert ok, (extra, missing)
    ok_iso, iso_info = validate_admin_isolation()
    assert ok_iso, iso_info
    ok_ro, viol = validate_research_read_only()
    assert ok_ro, viol
    allow = build_harness_allowlist()
    assert HARNESS_ENTRY_TOOL not in allow
    assert RUN_CODE_STEP not in allow
    for row in capability_matrix():
        if not row["internal"]:
            assert row["catalog"] in (CHAT, ADMIN)
            assert row["access"] in (READ, WRITE)
            assert isinstance(row["untrusted_evidence"], bool)
    live_chat = {c.name for c in chat_capabilities()}
    for name in live_chat & set(RESEARCH_CHAT_TOOL_NAMES):
        cap = require_capability(name)
        assert cap.access == READ and cap.untrusted_evidence
    print(
        "capabilities self-check OK "
        f"(live chat={len(live_chat)}, snapshot=18, extra={sorted(extra)})"
    )
