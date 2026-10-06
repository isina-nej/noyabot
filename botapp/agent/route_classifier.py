"""JEV-first structured request router (4-way).

Route selection is model-first; ``deterministic_parse`` is last-resort
validation only — never the primary selector.

Order:
0. Reply-analysis precedence → chat (replied text is UNTRUSTED data only).
1. JEV SystemOne 4-way verdict. Confident verdicts select the route;
   low-confidence admin/harness (< 0.80) falls through for second-opinion
   validation instead of executing.
2. 9Router structured JSON verdict (``provider.classify_request``).
3. Legacy binary provider verdict (``provider.classify_route``) mapped to
   4-way (compat with older doubles).
4. Deterministic parser last-resort validation only.

Route → path mapping (execution lives in the existing paths, never here):
- chat → Noya chat (fall-through; includes voice/TTS + reply analysis).
- admin (conf ≥ 0.80) → ``handle_admin_command``: parser validation, then
  the unchanged Security Gate (identity, permission, capability, target,
  protected-target, channel guard, risk, confirmation) and tool.
- harness (conf ≥ 0.80) → ``harness.investigate`` → planner → validator →
  security → executor → observer/store (existing bridge chain).
- media → Noya image generate/edit path (existing runbot handlers).
- conf < 0.80 on admin/harness → tool execution forbidden → chat or
  clarification (``AgentParseError`` text).

Every output is a validated ``RequestClassification`` (``extra="forbid"``):
route/intent/confidence/target_source/reason only. The model can never emit
a tool name, parameters, or target ids; ``target_source`` is re-derived from
message structure (reply → "reply", leading @chat/id → "explicit").
Empty/timeout/invalid JEV output returns None at the JEV layer and falls
through safely (fail-open to chat).
"""

from __future__ import annotations

import logging

from .parser import is_message_analysis_request
from .parser import parse as deterministic_parse
from .schemas import RequestClassification

logger = logging.getLogger("botapp.agent")

# Re-exported here so routing callers import one name for one contract.
__all__ = [
    "ROUTE_CONFIDENCE_THRESHOLD",
    "RouteVerdict",
    "classify_request",
    "is_tool_execution_allowed",
]

ROUTE_CONFIDENCE_THRESHOLD = 0.80

_TOOL_ROUTES = frozenset({"admin", "harness"})

# Single contract: the schema in schemas.py is the only verdict shape.
# Kept as an alias so callers/tests use one name for one contract.
RouteVerdict = RequestClassification


def _structural_target_source(text: str, *, has_reply: bool) -> str:
    """Derive target_source from message structure, never from model text."""
    if has_reply:
        return "reply"
    try:
        from .target_chat import extract_explicit_chat_ref

        ref, _ = extract_explicit_chat_ref(text or "")
        if ref:
            return "explicit"
    except Exception:
        logger.debug("explicit-ref check failed", exc_info=True)
    return "none"


def _verdict(
    route: str,
    intent: str,
    confidence: float,
    target_source: str,
    reason: str,
) -> RouteVerdict | None:
    try:
        return RouteVerdict(
            route=route,
            intent=(intent or "chat").strip()[:64] or "chat",
            confidence=confidence,
            target_source=target_source,
            reason=(reason or "").strip()[:200],
        )
    except Exception:
        logger.debug("invalid route verdict dropped", exc_info=True)
        return None


def _coerce_confidence(value) -> float:
    try:
        conf = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if conf != conf:  # NaN
        return 0.0
    return max(0.0, min(1.0, conf))


def is_tool_execution_allowed(verdict: RouteVerdict | None) -> bool:
    """True only for confident admin/harness verdicts. All else → chat."""
    return (
        verdict is not None
        and verdict.route in _TOOL_ROUTES
        and verdict.confidence >= ROUTE_CONFIDENCE_THRESHOLD
    )


def _clamp_target_source(model_source: str, structural: str, *, has_reply: bool) -> str:
    """The model must not invent reply/explicit targets; structure wins."""
    src = (model_source or "").strip().lower()
    if src == "reply" and has_reply:
        return "reply"
    if src == "explicit" and structural == "explicit":
        return "explicit"
    if src in {"reply", "explicit", "none"} and src == structural:
        return src
    return structural


async def classify_request(
    text: str,
    *,
    chat_id: int,
    provider=None,
    has_reply: bool = False,
    use_jev: bool = True,
) -> RouteVerdict | None:
    """Return a validated 4-way verdict, or None when nothing fires (chat)."""
    stripped = (text or "").strip()
    if not stripped:
        return _verdict("chat", "empty", 0.0, "none", "empty-message")
    structural = _structural_target_source(stripped, has_reply=has_reply)

    # 0) Reply-analysis precedence: analysis of a replied message always wins
    # over media and analytics routes; replied content stays UNTRUSTED data.
    if is_message_analysis_request(stripped):
        return _verdict(
            "chat", "message_analysis", 0.95, structural, "reply-analysis-priority"
        )

    det = deterministic_parse(stripped)  # validation hint only, not a selector

    # 1) JEV SystemOne 4-way verdict first.
    if use_jev:
        try:
            from .jev_router import jev_classify_request

            jev = await jev_classify_request(stripped, chat_id=chat_id)
        except Exception:
            logger.info("jev_request_fail_open chat=%s", chat_id)
            jev = None
        if isinstance(jev, dict):
            route = str(jev.get("route") or "chat").strip().lower()
            if route in {"chat", "admin", "harness", "media"}:
                conf = _coerce_confidence(jev.get("confidence"))
                if route in _TOOL_ROUTES and conf < ROUTE_CONFIDENCE_THRESHOLD:
                    # No tool execution below 0.80: fall through for
                    # second-opinion validation (provider → deterministic).
                    logger.info(
                        "jev_request_low_conf chat=%s route=%s conf=%.2f fallback",
                        chat_id,
                        route,
                        conf,
                    )
                else:
                    intent = {
                        "admin": "admin_command",
                        "harness": "harness_investigate",
                        "media": "media_request",
                    }.get(route, "chat")
                    if det is not None and route in _TOOL_ROUTES:
                        intent = det.intent
                    logger.info(
                        "jev_request chat=%s route=%s conf=%.2f",
                        chat_id,
                        route,
                        conf,
                    )
                    return _verdict(route, intent, conf, structural, f"jev:{route}")
            else:
                logger.info("jev_request_unknown chat=%s route=%r", chat_id, route)

    # 2) 9Router structured JSON verdict.
    if provider is not None:
        structured_fn = getattr(provider, "classify_request", None)
        if callable(structured_fn):
            try:
                raw = await structured_fn(
                    stripped, chat_id=chat_id, has_reply=has_reply
                )
            except Exception:
                logger.info("route_provider_fail_open chat=%s", chat_id)
                raw = None
            if isinstance(raw, dict):
                verdict = _verdict(
                    str(raw.get("route") or "chat").strip().lower(),
                    str(raw.get("intent") or "chat"),
                    _coerce_confidence(raw.get("confidence")),
                    _clamp_target_source(
                        str(raw.get("target_source") or ""),
                        structural,
                        has_reply=has_reply,
                    ),
                    str(raw.get("reason") or "model"),
                )
                if verdict is not None:
                    logger.info(
                        "route_model chat=%s route=%s conf=%.2f",
                        chat_id,
                        verdict.route,
                        verdict.confidence,
                    )
                    return verdict

        # 3) Legacy binary provider verdict (compat with older doubles).
        legacy_fn = getattr(provider, "classify_route", None)
        if callable(legacy_fn):
            try:
                raw = await legacy_fn(stripped, chat_id=chat_id)
            except Exception:
                logger.info("route_legacy_fail_open chat=%s", chat_id)
                raw = None
            if isinstance(raw, dict):
                legacy_route = str(raw.get("route") or "chat").strip().lower()
                route = "admin" if legacy_route == "agent" else "chat"
                conf = _coerce_confidence(raw.get("confidence"))
                intent = det.intent if det is not None else (
                    "admin_command" if route == "admin" else "chat"
                )
                logger.info(
                    "route_legacy chat=%s route=%s conf=%.2f",
                    chat_id,
                    route,
                    conf,
                )
                return _verdict(
                    route, intent, conf, structural, "legacy-binary-map"
                )

    # 4) Deterministic last-resort validation only.
    if det is not None:
        route = "harness" if det.tool == "harness.investigate" else "admin"
        try:
            conf = float(det.confidence)
        except (TypeError, ValueError):
            conf = 1.0
        logger.info(
            "route_deterministic_fallback chat=%s route=%s tool=%s",
            chat_id,
            route,
            det.tool,
        )
        return _verdict(route, det.intent, conf, structural, "deterministic-fallback")
    return None
