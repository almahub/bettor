from decimal import Decimal
from typing import Literal
from uuid import UUID, uuid4

from pydantic import Field, model_validator

from better.domain.common import Contract, DecimalOdds, FiniteFloat, Probability, UTCDateTime
from better.domain.enums import ConcernSeverity, Direction, MarketType, PredictionStatus, ValidationStatus
from better.domain.provenance import Provenance


class Match(Contract):
    match_id: str = Field(min_length=1)
    competition: str = Field(min_length=1)
    season: str
    kickoff: UTCDateTime
    home_team: str
    away_team: str
    home_team_id: str
    away_team_id: str

    @model_validator(mode="after")
    def different_teams(self):
        if self.home_team_id == self.away_team_id:
            raise ValueError("teams must differ")
        return self


class Market(Contract):
    type: MarketType
    selection: str
    line: FiniteFloat | None = None

    @model_validator(mode="after")
    def valid_selection(self):
        choices = {
            MarketType.MATCH_WINNER: {"HOME", "DRAW", "AWAY"},
            MarketType.OVER_UNDER_2_5: {"OVER_2_5", "UNDER_2_5"},
            MarketType.BTTS: {"YES", "NO"},
            MarketType.DNB: {"HOME", "AWAY"},
            MarketType.ASIAN_HANDICAP: {"HOME", "AWAY"},
            MarketType.TEAM_TOTALS: {"HOME_OVER", "HOME_UNDER", "AWAY_OVER", "AWAY_UNDER"},
        }
        if self.selection not in choices[self.type]:
            raise ValueError("selection incompatible with market")
        if self.type in {MarketType.ASIAN_HANDICAP, MarketType.TEAM_TOTALS} and self.line is None:
            raise ValueError("market requires explicit line")
        return self


class DataQuality(Contract):
    completeness: Probability = 0
    freshness: Probability = 0
    source_reliability: Probability = 0
    cross_source_consistency: Probability = 0
    score: Probability = 0

    @model_validator(mode="before")
    @classmethod
    def calculate_score(cls, value):
        if isinstance(value, dict):
            value = dict(value)
            score = min(
                float(value.get(k, 0))
                for k in ("completeness", "freshness", "source_reliability", "cross_source_consistency")
            )
            if "score" in value and abs(float(value["score"]) - score) > 1e-8:
                raise ValueError("quality score must reflect all components")
            value["score"] = score
        return value


class Signal(Contract):
    direction: Direction = Direction.NOT_APPLICABLE
    strength: Probability = 0
    confidence: Probability = 0


class Degradation(Contract):
    """Versioned, deterministic impact of a missing check; never an LLM penalty."""

    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]+$")
    missing_checks: tuple[str, ...] = Field(min_length=1)
    critical: bool
    affected_gates: tuple[
        Literal[
            "CONTEXT_INTEGRITY",
            "DATA_QUALITY",
            "EVIDENCE_PROVENANCE",
            "MODEL_VALIDITY",
            "MARKET",
            "NUMERICAL_CONSISTENCY",
            "PRICE_FRESHNESS",
            "VALUE",
            "CONTRARIAN",
            "RISK",
            "TIMING",
            "CONFIDENCE",
        ],
        ...,
    ] = Field(min_length=1)
    market_scope: tuple[MarketType, ...] = ()
    impact: Literal["WARNING_ONLY", "CONFIDENCE_CAP", "BLOCK"]
    confidence_cap: Probability | None = None
    reason: str = Field(min_length=1)
    policy_version: Literal["explicit_degradations_v1"] = "explicit_degradations_v1"

    @model_validator(mode="after")
    def consistent_impact(self):
        if self.critical != (self.impact == "BLOCK"):
            raise ValueError("critical degradation must block; noncritical degradation cannot block")
        if (self.impact == "CONFIDENCE_CAP") != (self.confidence_cap is not None):
            raise ValueError("only CONFIDENCE_CAP requires an explicit confidence cap")
        if any(not check.strip() for check in self.missing_checks):
            raise ValueError("missing checks must be named")
        return self


class DecisionDegradation(Degradation):
    agent_id: str
    applies_to_decision: bool


class Evidence(Contract):
    evidence_id: UUID = Field(default_factory=uuid4)
    agent_id: str
    type: str
    direction: Direction = Direction.NEUTRAL
    strength: Probability = 0
    confidence: Probability = 0
    market_scope: tuple[MarketType, ...] = ()
    claim: str = Field(min_length=1)
    support: tuple[str, ...] = ()
    provenance: tuple[Provenance, ...] = Field(min_length=1)
    derived_from: tuple[UUID, ...] = ()


class Concern(Contract):
    concern_id: UUID = Field(default_factory=uuid4)
    severity: ConcernSeverity
    reason: str
    evidence_ids: tuple[UUID, ...] = ()
    condition: str


class Resolution(Contract):
    concern_id: UUID
    resolved: Literal[True] = True
    resolution: str = Field(min_length=1)
    evidence_ids: tuple[UUID, ...] = Field(min_length=1)
    resolved_at: UTCDateTime


class RecentTeamMatch(Contract):
    fixture_id: str
    kickoff: UTCDateTime
    competition_id: str
    venue: Literal["HOME", "AWAY"]
    opponent_id: str
    opponent_name: str
    goals_for: int = Field(ge=0, le=50)
    goals_against: int = Field(ge=0, le=50)
    outcome: Literal["W", "D", "L"]


class TeamStats(Contract):
    team_id: str
    sample_size: int = Field(ge=0)
    goals_for_per_match: FiniteFloat = Field(ge=0)
    goals_against_per_match: FiniteFloat = Field(ge=0)
    xg_per_match: FiniteFloat | None = Field(default=None, ge=0)
    xga_per_match: FiniteFloat | None = Field(default=None, ge=0)
    # Each record is a named segment and a set of explicitly supplied observed metrics.
    segments: dict[str, dict[str, FiniteFloat]] = Field(default_factory=dict)
    tactical_traits: dict[str, str] = Field(default_factory=dict)
    recent_matches: tuple[RecentTeamMatch, ...] = ()


class StatsSnapshot(Contract):
    snapshot_id: str
    match_id: str
    home: TeamStats
    away: TeamStats
    data_cutoff: UTCDateTime
    captured_at: UTCDateTime
    provenance: tuple[Provenance, ...] = Field(min_length=1)


class OddsQuote(Contract):
    match_id: str
    bookmaker: str
    market: Market
    odds: DecimalOdds
    captured_at: UTCDateTime
    is_opening: bool = False
    liquidity: Decimal | None = Field(default=None, ge=0)
    provenance: Provenance


class OddsSnapshot(Contract):
    snapshot_id: str
    match_id: str
    quotes: tuple[OddsQuote, ...]


class NewsItem(Contract):
    event_id: str
    headline: str
    category: str
    verified: bool = False
    material: bool = False
    contradicts_event_id: str | None = None
    provenance: Provenance


class NewsSnapshot(Contract):
    snapshot_id: str
    match_id: str
    items: tuple[NewsItem, ...]
    captured_at: UTCDateTime
    provenance: tuple[Provenance, ...] = Field(min_length=1)


class LineupSnapshot(Contract):
    snapshot_id: str
    match_id: str
    status: Literal["PROBABLE_LINEUP", "CONFIRMED_LINEUP", "UNKNOWN"]
    home_players: tuple[str, ...] = ()
    away_players: tuple[str, ...] = ()
    unavailable: tuple[str, ...] = ()
    sensitivity: Probability = 0.5
    captured_at: UTCDateTime
    provenance: tuple[Provenance, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def confirmed_eleven(self):
        if self.status == "CONFIRMED_LINEUP":
            if (
                len(self.home_players) != 11
                or len(self.away_players) != 11
                or len(set(self.home_players)) != 11
                or len(set(self.away_players)) != 11
            ):
                raise ValueError("official lineup requires 11 distinct players per team")
            if set(self.home_players) & set(self.away_players):
                raise ValueError("a player cannot start for both teams")
            if not any(
                p.source_type.value == "OFFICIAL" and p.reliability_tier.value == "A" for p in self.provenance
            ):
                raise ValueError("official lineup requires official tier A provenance")
        return self


class HistoricalPrediction(Contract):
    match_id: str = "unknown"
    model_id: str
    model_version: str
    competition: str
    market: MarketType
    selection: str = "UNKNOWN"
    probability: Probability
    outcome: Literal[0, 1]
    predicted_at: UTCDateTime
    settled_at: UTCDateTime
    data_cutoff: UTCDateTime
    split: Literal["VALIDATION", "TEST", "LIVE"]
    odds: DecimalOdds
    closing_odds: DecimalOdds | None = None
    stake: Decimal = Field(default=Decimal("1"), gt=0)
    profit: Decimal
    season: str = "unknown"
    bookmaker: str = "unknown"
    source_type: Literal["LIVE_FORWARD", "HISTORICAL_BACKTEST"] = "HISTORICAL_BACKTEST"
    feature_schema_version: str = "1.0.0"
    config_hash: str = "unknown"
    canonical_checkpoint: str | None = None
    probabilities: dict[str, Probability] = Field(default_factory=dict)
    actual_selection: str | None = None
    software_version: str = "unknown"

    @model_validator(mode="after")
    def no_leakage(self):
        if not self.data_cutoff <= self.predicted_at < self.settled_at:
            raise ValueError("historical prediction violates temporal separation")
        if self.probabilities:
            if self.actual_selection not in self.probabilities:
                raise ValueError("full distribution requires its actual selection")
            if abs(sum(self.probabilities.values()) - 1) > 1e-8:
                raise ValueError("historical market probabilities must sum to one")
        return self


class Position(Contract):
    match_id: str
    team_ids: tuple[str, ...]
    competition: str
    market: Market
    bankroll_fraction: Probability
    placed_at: UTCDateTime
    stake_amount: Decimal | None = Field(default=None, gt=0)
    settled_at: UTCDateTime | None = None

    @model_validator(mode="after")
    def settlement_follows_placement(self):
        if self.settled_at and self.settled_at < self.placed_at:
            raise ValueError("settlement precedes placement")
        return self


class Portfolio(Contract):
    bankroll: Decimal = Field(default=Decimal("1000"), ge=0)
    positions: tuple[Position, ...] = ()
    daily_loss_fraction: Probability = 0
    weekly_loss_fraction: Probability = 0
    drawdown: Probability = 0


class ProbabilityEstimate(Contract):
    point_estimate: Probability
    lower: Probability
    upper: Probability
    method: str

    @model_validator(mode="after")
    def ordered(self):
        if not self.lower <= self.point_estimate <= self.upper:
            raise ValueError("invalid uncertainty interval")
        return self


class ModelPrediction(Contract):
    prediction_id: UUID = Field(default_factory=uuid4)
    model: str
    model_version: str
    probabilities: dict[MarketType, dict[str, Probability]]
    data_cutoff: UTCDateTime
    sample_size: int = Field(ge=0)
    feature_version: str = "1.0.0"
    features: dict[str, FiniteFloat] = Field(default_factory=dict)
    uncertainty: dict[str, ProbabilityEstimate] = Field(default_factory=dict)
    fair_odds: dict[MarketType, dict[str, FiniteFloat]] = Field(default_factory=dict)
    prediction_status: PredictionStatus = PredictionStatus.UNVALIDATED
    analysis_available: bool = True
    operationally_eligible: bool = False
    reason: str = "MODEL_VALIDATION_INSUFFICIENT"
    input_completeness: Probability = 0
    required_features: tuple[str, ...] = ()
    missing_features: tuple[str, ...] = ()
    validation_sample_size: int = Field(default=0, ge=0)
    validation_minimum: int = Field(default=200, ge=1)
    validation_status: ValidationStatus = ValidationStatus.INSUFFICIENT_SAMPLE

    @model_validator(mode="after")
    def distributions(self):
        expected = {
            MarketType.MATCH_WINNER: {"HOME", "DRAW", "AWAY"},
            MarketType.OVER_UNDER_2_5: {"OVER_2_5", "UNDER_2_5"},
            MarketType.BTTS: {"YES", "NO"},
            MarketType.DNB: {"HOME", "AWAY"},
        }
        if not self.probabilities:
            raise ValueError("model prediction requires at least one complete market")
        for market, probabilities in self.probabilities.items():
            if market not in expected or set(probabilities) != expected[market]:
                raise ValueError("model market requires its complete supported outcome set")
            if not probabilities or abs(sum(probabilities.values()) - 1) > 1e-8:
                raise ValueError("market probabilities must sum to one")
            for selection in probabilities:
                Market(type=market, selection=selection)
            supplied_fair = self.fair_odds.get(market, {})
            if supplied_fair and set(supplied_fair) != set(probabilities):
                raise ValueError("fair odds must cover the complete prediction market")
            if supplied_fair and any(
                probability <= 0 or abs(supplied_fair[selection] - 1 / probability) > 1e-8
                for selection, probability in probabilities.items()
            ):
                raise ValueError("prediction fair odds must match probabilities")
        if set(self.fair_odds) - set(self.probabilities):
            raise ValueError("fair odds cannot introduce a prediction market")
        if set(self.missing_features) - set(self.required_features):
            raise ValueError("missing features must belong to required features")
        expected_completeness = (
            (len(self.required_features) - len(self.missing_features)) / len(self.required_features)
            if self.required_features
            else self.input_completeness
        )
        if abs(self.input_completeness - expected_completeness) > 1e-8:
            raise ValueError("input completeness must reflect required and missing features")
        if self.operationally_eligible and (
            self.prediction_status != PredictionStatus.VALIDATED
            or self.validation_sample_size < self.validation_minimum
            or self.validation_status not in {ValidationStatus.NORMAL_WEIGHT, ValidationStatus.REDUCED_WEIGHT}
        ):
            raise ValueError("operational prediction requires mature independent validation")
        return self


class ModelValidation(Contract):
    model_id: str
    model_version: str
    market: MarketType
    validation_status: ValidationStatus
    recommended_weight_multiplier: Probability
    sample_size: int = Field(ge=0)
    brier_score: FiniteFloat | None = Field(default=None, ge=0, le=1)
    log_loss: FiniteFloat | None = Field(default=None, ge=0)
    calibration_error: Probability | None = None
    reason: str
    metrics: dict[str, FiniteFloat] = Field(default_factory=dict)


class ValueCandidate(Contract):
    market: Market
    bookmaker: str
    current_odds: DecimalOdds
    model_probability: Probability
    conservative_probability: Probability
    implied_probability: Probability
    no_vig_probability: Probability
    fair_odds: FiniteFloat = Field(ge=1)
    edge: FiniteFloat
    expected_value: FiniteFloat
    minimum_acceptable_odds: FiniteFloat = Field(ge=1)
    bookmaker_margin: FiniteFloat
    model_id: str
    model_version: str
    liquidity: Decimal | None = Field(default=None, ge=0)
    captured_at: UTCDateTime
    provenance: Provenance
    operationally_eligible: bool = True
    status: Literal["OPERATIONAL", "INDICATIVE"] = "OPERATIONAL"
    reason: str = "ALL_OPERATIONAL_CHECKS_PASSED"

    @model_validator(mode="after")
    def conservative_estimate(self):
        if not 0 < self.conservative_probability <= self.model_probability:
            raise ValueError(
                "finite value candidates require 0 < conservative probability <= model probability"
            )
        if self.operationally_eligible != (self.status == "OPERATIONAL"):
            raise ValueError("value status must match operational eligibility")
        return self


class Stake(Contract):
    method: Literal["FRACTIONAL_KELLY", "FIXED", "KELLY", "CONFIDENCE_ADJUSTED"] = "FRACTIONAL_KELLY"
    bankroll_fraction: Probability = 0
    amount: Decimal = Field(default=Decimal("0"), ge=0)
    capped: bool = False


class WatchCondition(Contract):
    type: Literal["ODDS", "LINEUP", "NEWS", "LIQUIDITY", "CONFIDENCE"]
    operator: Literal[">=", "CONFIRMED", "RESOLVED"]
    target: FiniteFloat | str


class GateTrace(Contract):
    gate: str
    status: Literal["PASS", "FAIL", "WAIT"]
    reason: str
