"""Semantic contracts: never contain betting authority or stake calculations."""

from datetime import UTC, datetime
from enum import StrEnum
from math import isclose
from typing import Any, Literal, Protocol, Self, runtime_checkable

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class SemanticStatus(StrEnum):
    OK = "OK"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    UNCERTAIN = "UNCERTAIN"
    OUT_OF_RANGE = "OUT_OF_RANGE"
    PROVIDER_ERROR = "PROVIDER_ERROR"


Primitive = Literal["boolean", "choice", "score", "numeric"]
Stage = Literal["pre_screen", "routing", "consistency", "escalation"]


class Question(Contract):
    type: Primitive
    instructions: str = Field(min_length=1)
    options: dict[str, str] = Field(default_factory=dict)
    levels: list[str] = Field(default_factory=list)
    anchors: dict[float, str] = Field(default_factory=dict)
    unit: str | None = None
    allow_abstain: bool = True

    @model_validator(mode="after")
    def check_primitive(self) -> Self:
        if self.type == "choice" and len(self.options) < 2:
            raise ValueError("Choice requires at least two options")
        if self.type == "score" and (len(self.levels) < 2 or len(set(self.levels)) != len(self.levels)):
            raise ValueError("Score requires at least two distinct levels")
        if self.type == "numeric" and (len(self.anchors) < 2 or not self.unit):
            raise ValueError("Numeric semantics require anchors and explicit units")
        return self


class QuestionBatch(Contract):
    schema_version: str
    questions: dict[str, Question] = Field(min_length=1)


class SemanticAnswer(Contract):
    type: Primitive
    status: SemanticStatus
    value: bool | str | float | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    probabilities: dict[str, float] = Field(default_factory=dict)
    probability_status: str = "uncalibrated_conditional_option_scores"
    prompt_hash: str | None = None

    @model_validator(mode="after")
    def check_answer(self) -> Self:
        if any(not 0 <= p <= 1 for p in self.probabilities.values()):
            raise ValueError("Semantic probabilities must be within [0, 1]")
        if self.probabilities and not isclose(sum(self.probabilities.values()), 1, abs_tol=1e-5):
            raise ValueError("Semantic answer distribution must sum to one")
        if self.status != SemanticStatus.OK and self.value is not None:
            raise ValueError("Abstention must preserve a null answer")
        if self.status == SemanticStatus.OK:
            if self.value is None:
                raise ValueError("OK requires a typed value")
            if self.type == "boolean" and not isinstance(self.value, bool):
                raise ValueError("Boolean answer must be a bool")
            if self.type == "choice" and not isinstance(self.value, str):
                raise ValueError("Choice answer must be a string")
            if self.type in ("score", "numeric") and (
                isinstance(self.value, bool) or not isinstance(self.value, (int, float))
            ):
                raise ValueError("Score and numeric answers must be finite numbers")
        return self


class SystemOneResult(Contract):
    provider: str
    model_id: str
    model_fingerprint: str
    quantization: str
    answers: dict[str, SemanticAnswer]
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    latency_ms: float = Field(default=0, ge=0)


class ShadowRecord(Contract):
    analysis_id: str
    stage: Stage
    system_one_provider: str
    model_id: str
    model_fingerprint: str | None = None
    quantization: str | None = None
    state_hash: str
    state_schema_version: str = "normalized_state_v1"
    question_schema_version: str
    threshold_version: str
    normalized_state: dict[str, Any]
    decisions: dict[str, SemanticAnswer] = Field(default_factory=dict)
    pipeline_actual_action: dict[str, Any]
    system_one_suggested_action: dict[str, Any] = Field(default_factory=dict)
    latency_ms: float = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cache_hit: bool = False
    error: str | None = None
    created_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))


class SystemOneError(Exception):
    def __init__(self, code: str, *, transient: bool = False):
        self.code = code
        self.transient = transient
        super().__init__(code)


@runtime_checkable
class SystemOneProvider(Protocol):
    async def evaluate(
        self,
        *,
        state: dict[str, Any],
        questions: QuestionBatch,
        model: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> SystemOneResult: ...
