from __future__ import annotations

from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import Field, JsonValue, model_validator

from better.domain.common import (
    Contract,
    DecimalOdds,
    FiniteFloat,
    Probability,
    UTCDateTime,
)
from better.domain.enums import MarketType, PredictionStatus, ValidationStatus
from better.domain.models import Market

BetSlipType = Literal["SINGLE", "MULTIPLE"]
BetSlipStatus = Literal[
    "DRAFT",
    "READY",
    "PLACED",
    "OPEN",
    "WON",
    "LOST",
    "VOID",
    "PARTIAL_VOID",
    "CANCELLED",
]
SelectionResult = Literal["WON", "LOST", "VOID", "UNRESOLVED"]


class BetCandidate(Contract):
    id: UUID
    analysis_id: UUID
    prediction_id: UUID
    match_id: str
    competition_id: str
    kickoff: UTCDateTime
    home_team: str
    away_team: str
    team_ids: tuple[str, str]
    market: MarketType
    selection: str
    probability: Probability
    fair_odds: FiniteFloat = Field(ge=1)
    odds_at_analysis: DecimalOdds | None = None
    edge: FiniteFloat | None = None
    ev: FiniteFloat | None = None
    bookmaker_at_analysis: str | None = None
    model_name: str
    model_version: str
    prediction_status: PredictionStatus
    validation_status: ValidationStatus
    validation_sample: int = Field(ge=0)
    validation_required: int = Field(ge=1)
    input_completeness: Probability
    operationally_eligible: bool
    created_at: UTCDateTime
    provenance: dict[str, JsonValue]
    batch_id: UUID | None = None
    ranking_position: int | None = Field(default=None, ge=1, le=6)
    ranking_score: FiniteFloat | None = Field(default=None, ge=0, le=100)
    ranking_reasons: tuple[str, ...] = ()
    batch_status: Literal["BET", "INDICATIVE"] | None = None
    batch_data_quality: Probability | None = None
    batch_sample_size: int | None = Field(default=None, ge=0)
    ranking_breakdown: dict[str, JsonValue] = Field(default_factory=dict)
    batch_blockers: tuple[str, ...] = ()
    removed: bool = False

    @model_validator(mode="after")
    def fair_price_and_status(self):
        if self.probability <= 0 or abs(self.fair_odds - 1 / self.probability) > 1e-8:
            raise ValueError("candidate fair odds must match its stored model probability")
        if self.operationally_eligible and self.prediction_status != PredictionStatus.VALIDATED:
            raise ValueError("only validated predictions can be operationally eligible")
        return self


class BetCandidateCreate(Contract):
    analysis_id: UUID
    prediction_id: UUID
    market: MarketType
    selection: str = Field(min_length=1)


class BetSlipSelection(Contract):
    bet_candidate_id: UUID
    analysis_id: UUID
    prediction_id: UUID
    match_id: str
    competition_id: str
    kickoff: UTCDateTime
    home_team: str
    away_team: str
    team_ids: tuple[str, str]
    market: MarketType
    selection: str
    odds_at_analysis: DecimalOdds | None = None
    odds_at_placement: DecimalOdds | None = None
    probability: Probability
    fair_odds: FiniteFloat = Field(ge=1)
    edge: FiniteFloat | None = None
    validation_status: ValidationStatus
    prediction_status: PredictionStatus
    operationally_eligible: bool
    result: SelectionResult = "UNRESOLVED"
    settlement_status: Literal["OPEN", "SETTLED"] = "OPEN"


class BetSlip(Contract):
    id: UUID
    type: BetSlipType
    selections: tuple[BetSlipSelection, ...]
    bookmaker: str | None = None
    combined_odds: DecimalOdds | None = None
    stake: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    potential_return: Decimal | None = Field(default=None, ge=0, max_digits=16, decimal_places=2)
    status: BetSlipStatus = "DRAFT"
    warnings: tuple[str, ...] = ()
    note: str | None = Field(default=None, max_length=2000)
    created_at: UTCDateTime
    confirmed_at: UTCDateTime | None = None
    placed_at: UTCDateTime | None = None
    settled_at: UTCDateTime | None = None

    @model_validator(mode="after")
    def structural_rules(self):
        size = len(self.selections)
        if self.type == "SINGLE" and size != 1:
            raise ValueError("SINGLE requires exactly one selection")
        if self.type == "MULTIPLE" and not 2 <= size <= 6:
            raise ValueError("MULTIPLE requires between two and six selections")
        identities = {
            (selection.match_id, selection.market, selection.selection) for selection in self.selections
        }
        if len(identities) != size:
            raise ValueError("a slip cannot contain duplicate selections")
        if self.status in {"READY", "PLACED", "OPEN", "WON", "LOST", "VOID", "PARTIAL_VOID"}:
            if (
                not self.bookmaker
                or self.stake is None
                or any(selection.odds_at_placement is None for selection in self.selections)
                or self.combined_odds is None
                or self.potential_return is None
            ):
                raise ValueError("prepared slips require bookmaker, odds, stake and return")
        return self


class BetSlipCreate(Contract):
    request_id: UUID
    candidate_ids: tuple[UUID, ...] = Field(min_length=1, max_length=6)


class BetSlipUpdate(Contract):
    request_id: UUID
    candidate_ids: tuple[UUID, ...] | None = Field(default=None, min_length=1, max_length=6)
    bookmaker: str | None = Field(default=None, min_length=1, max_length=120)
    odds_at_placement: dict[UUID, DecimalOdds] | None = None
    stake: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    note: str | None = Field(default=None, max_length=2000)


class BetSlipAction(Contract):
    request_id: UUID


class BetSelectionVoid(Contract):
    request_id: UUID
    note: str = Field(min_length=1, max_length=1000)


def selection_market(value: BetSlipSelection) -> Market:
    return Market(type=value.market, selection=value.selection)
