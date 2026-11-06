"""Contracts for current, quantitative football-data web research."""

from enum import StrEnum

from pydantic import Field, JsonValue, model_validator

from better.domain.common import Contract, UTCDateTime
from better.domain.enums import ReliabilityTier


class FootballDataFindingStatus(StrEnum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    STALE = "STALE"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"


class FootballDataRunStatus(StrEnum):
    OK = "OK"
    DISABLED = "DISABLED"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"
    FOOTBALL_DATA_RESEARCH_ERROR = "FOOTBALL_DATA_RESEARCH_ERROR"
    FOOTBALL_DATA_RESEARCH_TIMEOUT = "FOOTBALL_DATA_RESEARCH_TIMEOUT"
    FOOTBALL_DATA_RESEARCH_RATE_LIMIT = "FOOTBALL_DATA_RESEARCH_RATE_LIMIT"
    FOOTBALL_DATA_RESEARCH_SCHEMA_ERROR = "FOOTBALL_DATA_RESEARCH_SCHEMA_ERROR"
    FOOTBALL_DATA_RESEARCH_BUDGET_EXCEEDED = "FOOTBALL_DATA_RESEARCH_BUDGET_EXCEEDED"


class FootballDataSource(Contract):
    url: str = Field(min_length=8)
    publisher: str = Field(min_length=1)
    source_type: str = Field(min_length=1)
    source_tier: ReliabilityTier
    retrieved_at: UTCDateTime
    published_at: UTCDateTime | None = None
    updated_at: UTCDateTime | None = None
    competition: str = Field(min_length=1)
    season: str = Field(min_length=1)
    sample_size: int = Field(ge=0)
    source_family: str | None = None
    lineage: tuple[str, ...] = ()

    @model_validator(mode="after")
    def temporal(self):
        for timestamp in (self.published_at, self.updated_at):
            if timestamp and timestamp > self.retrieved_at:
                raise ValueError("source timestamp cannot follow retrieval")
        if not self.url.lower().startswith(("http://", "https://")):
            raise ValueError("football-data sources require an HTTP(S) URL")
        return self


class RawMatchObservation(Contract):
    date: UTCDateTime
    opponent: str = Field(min_length=1)
    venue: str = Field(pattern="^(HOME|AWAY|NEUTRAL)$")
    goals_for: int = Field(ge=0)
    goals_against: int = Field(ge=0)
    competition: str = Field(min_length=1)


class FootballDataFinding(Contract):
    field: str = Field(min_length=1)
    status: FootballDataFindingStatus
    value: JsonValue | None = None
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    sources: tuple[FootballDataSource, ...] = ()
    conflicts: tuple[str, ...] = ()
    notes: str | None = None

    @model_validator(mode="after")
    def evidence(self):
        if self.status == FootballDataFindingStatus.FOUND and (self.value is None or not self.sources):
            raise ValueError("FOUND football data requires a value and provenance")
        if self.status != FootballDataFindingStatus.FOUND and self.value is not None:
            raise ValueError("unresolved football data cannot carry a value")
        if self.status == FootballDataFindingStatus.CONFLICT and not self.conflicts:
            raise ValueError("CONFLICT requires details")
        return self


class FootballDataResearchRequest(Contract):
    request_id: str = Field(min_length=1)
    match_id: str = Field(min_length=1)
    home_team: str = Field(min_length=1)
    away_team: str = Field(min_length=1)
    competition: str = Field(min_length=1)
    season: str = Field(min_length=1)
    kickoff: UTCDateTime
    as_of: UTCDateTime
    requested_fields: tuple[str, ...] = Field(min_length=1)
    requested_at: UTCDateTime

    @model_validator(mode="after")
    def pre_match(self):
        if self.as_of >= self.kickoff:
            raise ValueError("football-data research must be pre-match")
        if len(set(self.requested_fields)) != len(self.requested_fields):
            raise ValueError("requested fields must be unique")
        return self


class FootballDataResearchResult(Contract):
    request_id: str
    match_id: str
    as_of: UTCDateTime
    findings: tuple[FootballDataFinding, ...] = ()
    requested_fields: tuple[str, ...] = ()
    unresolved_fields: tuple[str, ...] = ()
    provider: str = "openai_football_data_research"
    model: str = ""
    transport: str = "agents_api"
    agent_id: str | None = None
    session_id: str | None = None
    response_id: str | None = None
    search_calls: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    estimated_cost: float | None = Field(default=None, ge=0)
    duration_ms: float | None = Field(default=None, ge=0)
    status: FootballDataRunStatus
    error: str | None = None
    cached: bool = False
    completed_at: UTCDateTime
