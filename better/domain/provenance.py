from pydantic import Field, model_validator

from better.domain.common import Contract, UTCDateTime
from better.domain.enums import ReliabilityTier, SourceType


class Provenance(Contract):
    source_id: str = Field(min_length=1)
    source_type: SourceType
    url: str | None = None
    retrieved_at: UTCDateTime
    published_at: UTCDateTime | None = None
    reliability_tier: ReliabilityTier
    field: str = Field(min_length=1)
    raw_reference: str = Field(min_length=1)
    primary_source_id: str | None = None
    notes: str | None = None

    @model_validator(mode="after")
    def chronological(self):
        if self.published_at and self.published_at > self.retrieved_at:
            raise ValueError("publication cannot follow retrieval")
        return self

    def available_at(self, as_of) -> bool:
        return self.retrieved_at <= as_of and (self.published_at is None or self.published_at <= as_of)
