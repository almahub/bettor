from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import Field, JsonValue, model_validator

from better import __version__
from better.domain.common import Contract, DecimalOdds, FiniteFloat, Probability, UTCDateTime
from better.domain.enums import (
    AgentStatus,
    AnalysisDepth,
    AnalysisMode,
    DecisionStatus,
    MarketType,
    PredictionStatus,
    ValidationStatus,
)
from better.domain.errors import ErrorDetail
from better.domain.models import (
    Concern,
    DataQuality,
    DecisionDegradation,
    Degradation,
    Evidence,
    GateTrace,
    HistoricalPrediction,
    LineupSnapshot,
    Market,
    Match,
    ModelPrediction,
    ModelValidation,
    NewsItem,
    NewsSnapshot,
    OddsQuote,
    OddsSnapshot,
    Portfolio,
    Resolution,
    Signal,
    Stake,
    StatsSnapshot,
    TeamStats,
    ValueCandidate,
    WatchCondition,
)
from better.domain.provenance import Provenance
from better.domain.research import EnrichedSnapshot


class DataReport(Contract):
    kind: Literal["data"] = "data"
    team_profiles: dict[str, TeamStats]
    comparisons: dict[str, FiniteFloat] = Field(default_factory=dict)
    sample_warnings: tuple[str, ...] = ()
    data_cutoff: UTCDateTime


class OddsReport(Contract):
    kind: Literal["odds"] = "odds"
    quotes: tuple[OddsQuote, ...]
    movements: dict[str, FiniteFloat] = Field(default_factory=dict)
    anomalies: tuple[str, ...] = ()


class NewsReport(Contract):
    kind: Literal["news"] = "news"
    events: tuple[NewsItem, ...]
    conflicts: tuple[str, ...] = ()
    unverified_material: tuple[str, ...] = ()


class LineupReport(Contract):
    kind: Literal["lineup"] = "lineup"
    lineup: LineupSnapshot | None
    official: bool
    sensitivity: Probability
    impact_method: str | None = None
    pre_lineup_probability: Probability | None = None
    post_lineup_probability: Probability | None = None
    delta_probability: FiniteFloat | None = None

    @model_validator(mode="after")
    def numerical_method(self):
        if self.official != bool(self.lineup and self.lineup.status == "CONFIRMED_LINEUP"):
            raise ValueError("official lineup flag must reflect the supplied confirmed snapshot")
        if self.lineup is not None and self.sensitivity != self.lineup.sensitivity:
            raise ValueError("lineup sensitivity must match its snapshot")
        if self.delta_probability is not None:
            if (
                not self.impact_method
                or self.pre_lineup_probability is None
                or self.post_lineup_probability is None
            ):
                raise ValueError("lineup delta requires documented method and probabilities")
            if (
                abs(self.post_lineup_probability - self.pre_lineup_probability - self.delta_probability)
                > 1e-8
            ):
                raise ValueError("lineup delta inconsistent")
        return self


class TacticalMatchup(Contract):
    home_trait: str
    away_trait: str
    interaction: str
    possible_market_effect: str
    evidence_ids: tuple[UUID, ...]
    limitations: tuple[str, ...]


class TacticalReport(Contract):
    kind: Literal["tactical"] = "tactical"
    matchups: tuple[TacticalMatchup, ...] = ()
    analysis_text: str | None = None


class PredictiveReport(Contract):
    kind: Literal["predictive"] = "predictive"
    model_predictions: tuple[ModelPrediction, ...]
    ensemble: dict[MarketType, dict[str, Probability]]
    score_distribution: dict[str, Probability]
    method: str
    seed: int
    analysis_available: bool = True
    operationally_eligible: bool = False
    reason_codes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def normalized_ensemble(self):
        if not self.model_predictions:
            raise ValueError("predictive report requires model predictions")
        models = self.model_predictions
        if len({prediction.prediction_id for prediction in models}) != len(models):
            raise ValueError("duplicate model prediction identifiers")
        if len({(prediction.model, prediction.model_version) for prediction in models}) != len(models):
            raise ValueError("duplicate model/version predictions")
        markets = set().union(*(set(model.probabilities) for model in models))
        if not set(self.ensemble).issubset(markets):
            raise ValueError("ensemble markets must come from underlying predictions")
        for market, probabilities in self.ensemble.items():
            members = [model.probabilities[market] for model in models if market in model.probabilities]
            if set(probabilities) != set(members[0]) or abs(sum(probabilities.values()) - 1) > 1e-8:
                raise ValueError("ensemble probabilities must cover and normalize a complete market")
            # An ensemble cannot manufacture a probability outside its member estimates.
            for selection, probability in probabilities.items():
                estimates = [member[selection] for member in members]
                if not min(estimates) - 1e-8 <= probability <= max(estimates) + 1e-8:
                    raise ValueError("ensemble probability lies outside admitted member estimates")
        if not self.score_distribution or abs(sum(self.score_distribution.values()) - 1) > 1e-8:
            raise ValueError("predictive score distribution must be normalized")
        if self.operationally_eligible != bool(self.ensemble):
            raise ValueError("predictive operational eligibility must reflect the admitted ensemble")
        return self


class ForecastMarket(Contract):
    """A displayable probability distribution, separate from betting admission."""

    market: MarketType
    probabilities: dict[str, Probability]
    method: Literal["RAW_POISSON", "MARKET_NO_VIG"]
    admitted_for_decision: bool = False
    source: str
    sample_size: int | None = Field(default=None, ge=0)
    limitations: tuple[str, ...] = ()
    fair_odds: dict[str, FiniteFloat] = Field(default_factory=dict)
    prediction_status: PredictionStatus = PredictionStatus.INDICATIVE
    operationally_eligible: bool = False
    reason: str = "MODEL_VALIDATION_INSUFFICIENT"
    input_completeness: Probability = 0
    missing_features: tuple[str, ...] = ()
    validation_sample_size: int = Field(default=0, ge=0)
    validation_minimum: int = Field(default=200, ge=1)
    validation_status: ValidationStatus = ValidationStatus.INSUFFICIENT_SAMPLE

    @model_validator(mode="after")
    def complete_distribution(self):
        expected = {
            MarketType.MATCH_WINNER: {"HOME", "DRAW", "AWAY"},
            MarketType.OVER_UNDER_2_5: {"OVER_2_5", "UNDER_2_5"},
            MarketType.BTTS: {"YES", "NO"},
            MarketType.DNB: {"HOME", "AWAY"},
        }
        if self.market not in expected or set(self.probabilities) != expected[self.market]:
            raise ValueError("forecast requires a complete supported market")
        if abs(sum(self.probabilities.values()) - 1) > 1e-8:
            raise ValueError("forecast probabilities must sum to one")
        if self.fair_odds and set(self.fair_odds) != set(self.probabilities):
            raise ValueError("forecast fair odds must cover every selection")
        if self.fair_odds and any(
            probability <= 0 or abs(self.fair_odds[selection] - 1 / probability) > 1e-8
            for selection, probability in self.probabilities.items()
        ):
            raise ValueError("forecast fair odds must match probabilities")
        if self.admitted_for_decision != self.operationally_eligible:
            raise ValueError("forecast admission must match operational eligibility")
        return self


class IndicativeForecast(Contract):
    """Probabilities available to the user even when decision gates stay closed."""

    generated_at: UTCDateTime
    markets: tuple[ForecastMarket, ...] = ()
    disclaimer: str = (
        "Indicative probabilities are not an operational betting decision and do not authorize stake."
    )


class ValueReport(Contract):
    kind: Literal["value"] = "value"
    candidates: tuple[ValueCandidate, ...]
    indicative_candidates: tuple[ValueCandidate, ...] = ()
    analysis_available: bool = False
    operationally_eligible: bool = False

    @model_validator(mode="after")
    def candidate_classes(self):
        if any(not candidate.operationally_eligible for candidate in self.candidates):
            raise ValueError("operational candidates must be eligible")
        if any(candidate.operationally_eligible for candidate in self.indicative_candidates):
            raise ValueError("indicative candidates cannot be operationally eligible")
        if self.operationally_eligible != bool(self.candidates):
            raise ValueError("value eligibility must reflect operational candidates")
        if self.analysis_available != bool(self.candidates or self.indicative_candidates):
            raise ValueError("value analysis availability must reflect candidates")
        return self


class MarketReport(Contract):
    kind: Literal["market_selection"] = "market_selection"
    eligible_markets: tuple[MarketType, ...]
    supported_markets: tuple[MarketType, ...] = ()
    rejections: dict[str, str] = Field(default_factory=dict)


class ValidationReport(Contract):
    kind: Literal["validation"] = "validation"
    validations: tuple[ModelValidation, ...]


class RiskReport(Contract):
    kind: Literal["risk"] = "risk"
    accepted: bool
    stake: Stake
    reasons: tuple[str, ...]
    candidate: ValueCandidate | None
    exposures: dict[str, FiniteFloat] = Field(default_factory=dict)
    operationally_eligible: bool = False

    @model_validator(mode="after")
    def accepted_stake(self):
        if self.accepted and (
            self.candidate is None or self.stake.amount <= 0 or self.stake.bankroll_fraction <= 0
        ):
            raise ValueError("accepted risk requires a candidate and positive monetary stake")
        if self.accepted != self.operationally_eligible:
            raise ValueError("risk eligibility must match acceptance")
        return self


class ContrarianReport(Contract):
    kind: Literal["contrarian"] = "contrarian"
    concerns: tuple[Concern, ...]


class MatchedReport(Contract):
    kind: Literal["matched"] = "matched"
    lay_stake: str
    liability: str
    profit_if_back_wins: str
    profit_if_lay_wins: str
    assumptions: tuple[str, ...]


class JournalReport(Contract):
    kind: Literal["journal"] = "journal"
    entry_id: str
    recorded: bool
    operational_entry: bool = False


REQUIRED_DECISION_GATES = frozenset(
    {
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
    }
)


class Decision(Contract):
    analysis_id: UUID
    match_id: str
    status: DecisionStatus
    market: Market | None = None
    current_odds: DecimalOdds | None = None
    minimum_acceptable_odds: FiniteFloat | None = Field(default=None, ge=1)
    estimated_probability: Probability | None = None
    fair_odds: FiniteFloat | None = Field(default=None, ge=1)
    estimated_edge: FiniteFloat | None = None
    expected_value: FiniteFloat | None = None
    confidence: Probability
    confidence_components: dict[str, Probability]
    degradations: tuple[DecisionDegradation, ...] = ()
    risk_level: Literal["LOW", "MEDIUM", "HIGH", "UNKNOWN"]
    stake: Stake = Field(default_factory=Stake)
    supporting_evidence: tuple[Evidence, ...] = ()
    contrary_evidence: tuple[Evidence, ...] = ()
    fatal_concerns: tuple[Concern, ...] = ()
    invalidation_conditions: tuple[str, ...] = Field(min_length=1)
    watch_conditions: tuple[WatchCondition, ...] = ()
    data_quality: DataQuality
    decision_trace: tuple[GateTrace, ...] = Field(min_length=1)
    generated_at: UTCDateTime
    reason_codes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def enforce_status(self):
        gates = [trace.gate for trace in self.decision_trace]
        if len(gates) != len(set(gates)):
            raise ValueError("decision gates must not be duplicated")
        for limitation in self.degradations:
            if limitation.applies_to_decision and limitation.confidence_cap is not None:
                key = f"degradation:{limitation.agent_id}:{limitation.code}"
                if (
                    self.confidence > limitation.confidence_cap
                    or self.confidence_components.get(key) != limitation.confidence_cap
                ):
                    raise ValueError("decision must record and respect every applicable confidence cap")
        if self.status == DecisionStatus.BET:
            if any(d.critical and d.applies_to_decision for d in self.degradations):
                raise ValueError("BET cannot bypass a critical degradation")
            required = (
                self.market,
                self.current_odds,
                self.minimum_acceptable_odds,
                self.estimated_probability,
                self.fair_odds,
                self.estimated_edge,
                self.expected_value,
            )
            if (
                any(v is None for v in required)
                or self.stake.bankroll_fraction <= 0
                or self.stake.amount <= 0
            ):
                raise ValueError("BET requires price, probability, value and positive stake")
            if not REQUIRED_DECISION_GATES.issubset(gates):
                raise ValueError("BET requires every mandatory deterministic gate")
            if (
                not self.confidence_components
                or abs(self.confidence - min(self.confidence_components.values())) > 1e-8
            ):
                raise ValueError("BET confidence must derive from its explicit weakest component")
            if not self.supporting_evidence:
                raise ValueError("BET requires supporting evidence")
            if self.estimated_probability <= 0 or abs(1 / self.estimated_probability - self.fair_odds) > 1e-8:
                raise ValueError("BET fair odds must match probability")
            if abs(self.estimated_probability * self.current_odds - 1 - self.expected_value) > 1e-8:
                raise ValueError("BET EV must match probability and price")
            if self.expected_value <= 0 or self.current_odds < self.minimum_acceptable_odds:
                raise ValueError("BET requires positive EV and adequate price")
            if self.fatal_concerns or any(t.status != "PASS" for t in self.decision_trace):
                raise ValueError("BET cannot bypass a failed gate")
        elif self.stake.bankroll_fraction != 0 or self.stake.amount != 0:
            raise ValueError("non-BET decisions must have zero stake")
        if self.status == DecisionStatus.WATCHLIST and not self.watch_conditions:
            raise ValueError("WATCHLIST requires observable conditions")
        return self


class DecisionReport(Contract):
    kind: Literal["decision"] = "decision"
    decision: Decision


Report = Annotated[
    DataReport
    | OddsReport
    | NewsReport
    | LineupReport
    | TacticalReport
    | PredictiveReport
    | ValueReport
    | MarketReport
    | ValidationReport
    | RiskReport
    | ContrarianReport
    | MatchedReport
    | JournalReport
    | DecisionReport,
    Field(discriminator="kind"),
]


class AgentResult(Contract):
    analysis_id: UUID
    agent_id: str
    agent_version: str = "1.0.0"
    prompt_version: str = "1.0.0"
    status: AgentStatus
    signal: Signal = Field(default_factory=Signal)
    summary: str
    evidence: tuple[Evidence, ...] = ()
    risks: tuple[str, ...] = ()
    unknowns: tuple[str, ...] = ()
    data_quality: DataQuality = Field(default_factory=DataQuality)
    provenance: tuple[Provenance, ...] = ()
    generated_at: UTCDateTime
    payload: Report | None = None
    errors: tuple[ErrorDetail, ...] = ()
    # Empty legacy PARTIAL records remain readable; new execution rejects them and
    # the Decision Engine treats an unclassified partial as a blocking limitation.
    degradations: tuple[Degradation, ...] = ()
    analysis_available: bool = False
    operationally_eligible: bool = False

    @model_validator(mode="after")
    def successful_payload(self):
        if self.status == AgentStatus.OK and self.payload is None:
            raise ValueError("successful agent result requires a typed payload")
        if self.status == AgentStatus.OK and self.degradations:
            raise ValueError("degraded output cannot claim OK")
        if len({d.code for d in self.degradations}) != len(self.degradations):
            raise ValueError("duplicate degradation codes")
        return self


class AgentContext(Contract):
    analysis_id: UUID
    run_id: UUID
    match: Match
    requested_markets: tuple[MarketType, ...]
    as_of: UTCDateTime
    upstream_results: dict[str, AgentResult] = Field(default_factory=dict)
    enriched_snapshot: EnrichedSnapshot | None = None
    # Orchestrator supplies only histories/portfolio to the agents explicitly authorized to read them.
    validation_history: tuple[HistoricalPrediction, ...] = ()
    portfolio: Portfolio | None = None
    resolutions: tuple[Resolution, ...] = ()
    mode: AnalysisMode = AnalysisMode.PRE_MATCH
    depth: AnalysisDepth = AnalysisDepth.FULL
    seed: int = 42


class AnalysisRequest(Contract):
    match_id: str = Field(min_length=1)
    markets: tuple[MarketType, ...] = (MarketType.MATCH_WINNER, MarketType.OVER_UNDER_2_5, MarketType.BTTS)
    mode: AnalysisMode = AnalysisMode.PRE_MATCH
    depth: AnalysisDepth = AnalysisDepth.FULL
    as_of: UTCDateTime | None = None
    parent_analysis_id: UUID | None = None


class InputSnapshot(Contract):
    match: Match
    as_of: UTCDateTime
    stats: StatsSnapshot | None = None
    odds: OddsSnapshot | None = None
    news: NewsSnapshot | None = None
    lineups: LineupSnapshot | None = None
    enriched: EnrichedSnapshot | None = None
    config_version: str
    model_versions: tuple[str, ...]
    input_hash: str


class AnalysisRun(Contract):
    analysis_id: UUID = Field(default_factory=uuid4)
    run_id: UUID = Field(default_factory=uuid4)
    match_id: str
    created_at: UTCDateTime
    as_of: UTCDateTime
    schema_version: str = "1.0"
    software_version: str = __version__
    config_version: str
    prompt_version: str = "1.0.0"
    seed: int = 42
    mode: AnalysisMode
    depth: AnalysisDepth
    parent_analysis_id: UUID | None = None
    results: dict[str, AgentResult]
    decision: Decision
    forecast: IndicativeForecast | None = None
    snapshot: InputSnapshot
    actual_agents: tuple[str, ...]
    input_hash: str
    input_manifest: dict[str, JsonValue] = Field(default_factory=dict)
    system_two_records: tuple[dict[str, JsonValue], ...] = ()
