"""Vendor-neutral contracts for targeted external research."""

from decimal import Decimal
from enum import StrEnum

from pydantic import Field, JsonValue, field_validator, model_validator

from better.domain.common import Contract, UTCDateTime
from better.domain.enums import ReliabilityTier


class ResearchFindingStatus(StrEnum):
    CONFIRMED = "CONFIRMED"
    PROBABLE = "PROBABLE"
    REPORTED = "REPORTED"
    # Kept readable for append-only Phase 1 records; providers normalize it before use.
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    STALE = "STALE"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"


class ResearchRunStatus(StrEnum):
    OK = "OK"
    DISABLED = "DISABLED"
    NOT_REQUIRED = "NOT_REQUIRED"
    RESEARCH_BUDGET_EXCEEDED = "RESEARCH_BUDGET_EXCEEDED"
    RESEARCH_PROVIDER_ERROR = "RESEARCH_PROVIDER_ERROR"
    EXTERNAL_RESEARCH_ERROR = "EXTERNAL_RESEARCH_ERROR"
    RESEARCH_TIMEOUT = "RESEARCH_TIMEOUT"
    RESEARCH_RATE_LIMIT = "RESEARCH_RATE_LIMIT"
    RESEARCH_SCHEMA_ERROR = "RESEARCH_SCHEMA_ERROR"
    RESEARCH_NOT_FOUND = "RESEARCH_NOT_FOUND"


class ResearchSource(Contract):
    url: str = Field(min_length=8)
    publisher: str | None = None
    source_type: str = Field(min_length=1)
    reliability_tier: ReliabilityTier
    published_at: UTCDateTime | None = None
    retrieved_at: UTCDateTime
    title: str | None = None
    canonical_url: str | None = None
    derived_from: tuple[str, ...] = ()

    @field_validator("url", "canonical_url")
    @classmethod
    def http_url(cls, value):
        if value is not None and not value.lower().startswith(("http://", "https://")):
            raise ValueError("research sources require an HTTP(S) URL")
        return value

    @model_validator(mode="after")
    def chronological(self):
        if self.published_at and self.published_at > self.retrieved_at:
            raise ValueError("publication cannot follow retrieval")
        return self


class ResearchFinding(Contract):
    field: str = Field(min_length=1)
    status: ResearchFindingStatus
    value: JsonValue | None = None
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    sources: tuple[ResearchSource, ...] = ()
    conflicts: tuple[str, ...] = ()
    notes: str | None = None

    @model_validator(mode="after")
    def evidence_consistency(self):
        evidenced = {
            ResearchFindingStatus.CONFIRMED,
            ResearchFindingStatus.PROBABLE,
            ResearchFindingStatus.REPORTED,
            ResearchFindingStatus.FOUND,
        }
        if self.status in evidenced and not self.sources:
            raise ValueError("FOUND requires at least one source")
        if self.status not in evidenced and self.value is not None:
            raise ValueError("only evidence-bearing findings may carry a value")
        if self.status == ResearchFindingStatus.CONFLICT and not self.conflicts:
            raise ValueError("CONFLICT requires conflict details")
        return self


def finding_is_resolved(status: ResearchFindingStatus) -> bool:
    return status in {
        ResearchFindingStatus.CONFIRMED,
        ResearchFindingStatus.PROBABLE,
        ResearchFindingStatus.REPORTED,
        ResearchFindingStatus.FOUND,
    }


class EnrichedField(Contract):
    """Canonical qualitative value with its complete point-in-time lineage."""

    field: str = Field(min_length=1)
    value: JsonValue | None = None
    status: ResearchFindingStatus
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    source_url: str | None = None
    publisher: str | None = None
    source_tier: ReliabilityTier | None = None
    published_at: UTCDateTime | None = None
    retrieved_at: UTCDateTime | None = None
    as_of: UTCDateTime
    lineage: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    sources: tuple[ResearchSource, ...] = ()


class EnrichedSnapshot(Contract):
    snapshot_id: str = Field(min_length=1)
    match_id: str = Field(min_length=1)
    as_of: UTCDateTime
    captured_at: UTCDateTime
    fields: dict[str, EnrichedField]
    structured_market_data: dict[str, JsonValue] = Field(default_factory=dict)
    football_data_research: dict[str, JsonValue] | None = None
    external_research: dict[str, JsonValue] | None = None
    research_request_id: str | None = None
    provider: str | None = None
    session_id: str | None = None
    response_id: str | None = None


class ResearchRequest(Contract):
    request_id: str = Field(min_length=1)
    match_id: str = Field(min_length=1)
    home_team: str = Field(min_length=1)
    away_team: str = Field(min_length=1)
    competition: str = Field(min_length=1)
    kickoff: UTCDateTime
    as_of: UTCDateTime
    missing_fields: tuple[str, ...] = Field(min_length=1)
    source_state_version: str = Field(min_length=1)
    requested_at: UTCDateTime

    @model_validator(mode="after")
    def pre_match(self):
        if self.as_of >= self.kickoff:
            raise ValueError("external research must be requested before kickoff")
        if self.requested_at < self.as_of:
            raise ValueError("request timestamp cannot precede as_of")
        if len(set(self.missing_fields)) != len(self.missing_fields):
            raise ValueError("missing_fields must be unique")
        return self


class ResearchResult(Contract):
    request_id: str = Field(min_length=1)
    match_id: str = Field(min_length=1)
    as_of: UTCDateTime
    findings: tuple[ResearchFinding, ...] = ()
    searched_fields: tuple[str, ...] = ()
    unresolved_fields: tuple[str, ...] = ()
    provider: str
    model: str
    transport: str = "unknown"
    response_id: str | None = None
    session_id: str | None = None
    agent_id: str | None = None
    search_calls: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    request_count: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    estimated_cost: Decimal | None = Field(default=None, ge=0)
    status: ResearchRunStatus = ResearchRunStatus.OK
    error: str | None = None
    cached: bool = False
    duration_ms: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    completed_at: UTCDateTime


class ResearchProviderFailure(RuntimeError):
    def __init__(self, status: ResearchRunStatus, message: str, *, attempts: int = 1):
        super().__init__(message)
        self.status = status
        self.attempts = attempts


def as_provenance(source: ResearchSource, field: str):
    """Map a cited web source to Better's existing provenance contract."""
    from better.domain.enums import SourceType
    from better.domain.provenance import Provenance

    return Provenance(
        source_id=source.publisher or "openai_web_search",
        source_type=SourceType.OFFICIAL if source.reliability_tier == ReliabilityTier.A else SourceType.MEDIA,
        url=source.url,
        retrieved_at=source.retrieved_at,
        published_at=source.published_at,
        reliability_tier=source.reliability_tier,
        field=field,
        raw_reference=source.title or source.url,
        primary_source_id=source.canonical_url or source.url,
        notes="External research via OpenAI Web Search; page content treated as untrusted data.",
    )
