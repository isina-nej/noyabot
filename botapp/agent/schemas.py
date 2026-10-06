"""Pydantic v2 schemas that constrain both AI output and tool parameters.

The AI is only ever allowed to emit an :class:`AgentDecision`. Raw model output
is validated against this schema before anything else happens; unknown fields
are rejected and the model can never smuggle in extra behaviour.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

RiskLevel = Literal["low", "medium", "high"]
TargetSource = Literal["reply", "user_id", "self", "none"]


class ToolParameters(BaseModel):
    """Loosely-typed parameter bag emitted by the parser/AI.

    Individual tools re-validate the subset they care about with their own
    strict schema (see ``registry.AgentTool.input_schema``); this bag only
    guarantees the shape is a flat, JSON-serialisable object.
    """

    model_config = ConfigDict(extra="allow")

    target_source: TargetSource = "none"
    target_user_id: int | None = None
    duration_minutes: int | None = Field(default=None, ge=1, le=525600)
    delay_minutes: int | None = Field(default=None, ge=0, le=525600)
    reason: str = ""
    value: Any | None = None

    @field_validator("reason")
    @classmethod
    def _trim_reason(cls, value: str) -> str:
        return (value or "").strip()[:500]


class AgentDecision(BaseModel):
    """The only structure the AI model is permitted to produce."""

    model_config = ConfigDict(extra="forbid")

    intent: str = Field(min_length=1, max_length=64)
    tool: str = Field(min_length=1, max_length=64)
    confidence: float = Field(ge=0.0, le=1.0)
    risk_level: RiskLevel = "low"
    requires_confirmation: bool = True
    parameters: ToolParameters = Field(default_factory=ToolParameters)
    human_summary: str = Field(default="", max_length=300)

    @field_validator("tool", "intent")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()


# --- Common strict tool-parameter schemas reused across tools -----------------


class EmptyParams(BaseModel):
    model_config = ConfigDict(extra="ignore")


class TargetParams(BaseModel):
    model_config = ConfigDict(extra="ignore")

    target_user_id: int
    reason: str = ""

    @field_validator("reason")
    @classmethod
    def _trim(cls, value: str) -> str:
        return (value or "").strip()[:500]


class MuteParams(BaseModel):
    model_config = ConfigDict(extra="ignore")

    target_user_id: int
    duration_minutes: int | None = Field(default=None, ge=1, le=525600)
    delay_minutes: int = Field(default=0, ge=0, le=525600)
    reason: str = ""

    @field_validator("reason")
    @classmethod
    def _trim(cls, value: str) -> str:
        return (value or "").strip()[:500]


class LockParams(BaseModel):
    model_config = ConfigDict(extra="ignore")

    duration_minutes: int | None = Field(default=None, ge=1, le=525600)
    delay_minutes: int = Field(default=0, ge=0, le=525600)
    reason: str = ""


class IntValueParams(BaseModel):
    model_config = ConfigDict(extra="ignore")

    value: int = Field(ge=1, le=1_000_000)


class WordParams(BaseModel):
    model_config = ConfigDict(extra="ignore")

    value: str = Field(min_length=1, max_length=100)

    @field_validator("value")
    @classmethod
    def _trim(cls, value: str) -> str:
        return value.strip()[:100]


class MessageTargetParams(BaseModel):
    model_config = ConfigDict(extra="ignore")

    message_id: int = Field(ge=1)


class TextContentParams(BaseModel):
    model_config = ConfigDict(extra="ignore")

    value: str = Field(min_length=1, max_length=4000)

    @field_validator("value")
    @classmethod
    def _trim(cls, value: str) -> str:
        return (value or "").strip()[:4000]


class HarnessStep(BaseModel):
    """One planning step inside the ReAct investigation harness."""

    model_config = ConfigDict(extra="ignore")

    action: Literal["call_tool", "run_code", "finish"]
    tool: str = ""
    args: dict[str, Any] = Field(default_factory=dict)
    code: str = Field(default="", max_length=4000)
    answer: str = Field(default="", max_length=4000)
    thinking: str = Field(default="", max_length=400)

    @field_validator("tool", "code", "answer", "thinking")
    @classmethod
    def _strip_fields(cls, value: str) -> str:
        return (value or "").strip()


class RouteClassification(BaseModel):
    """AI router: admin-agent vs ordinary Noya chat (legacy binary verdict)."""

    model_config = ConfigDict(extra="ignore")

    route: Literal["agent", "chat"] = "chat"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    thinking: str = Field(default="", max_length=400)
    reason: str = Field(default="", max_length=200)


class RequestClassification(BaseModel):
    """JEV-first structured request router verdict (4-way).

    Data only: the model may only ever emit this shape. It carries no tool
    name, no parameters, and no target ids — ``target_source`` is a
    structural hint (reply/explicit/none) that the orchestrator re-derives
    from the live message; it never authorizes anything by itself.
    """

    model_config = ConfigDict(extra="forbid")

    route: Literal["chat", "admin", "harness", "media"] = "chat"
    intent: str = Field(default="chat", min_length=1, max_length=64)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    target_source: Literal["reply", "explicit", "none"] = "none"
    reason: str = Field(default="", max_length=200)


class DailyScheduleParams(BaseModel):
    model_config = ConfigDict(extra="ignore")

    action: Literal["lock", "unlock"]
    value: str = Field(min_length=1, max_length=16)

    @field_validator("value")
    @classmethod
    def _trim(cls, value: str) -> str:
        return (value or "").strip()[:16]


class CancelScheduleParams(BaseModel):
    model_config = ConfigDict(extra="ignore")

    action: Literal["lock", "unlock"]
    value: str = Field(min_length=1, max_length=16)

    @field_validator("value")
    @classmethod
    def _trim(cls, value: str) -> str:
        return (value or "").strip()[:16]
