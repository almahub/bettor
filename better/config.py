import hashlib
import json
import os
from pathlib import Path

import yaml
from pydantic import Field

from better.domain.common import Contract, Probability


class BatchRankingSettings(Contract):
    probability_weight: float = Field(default=15, ge=0)
    price_coherence_weight: float = Field(default=10, ge=0)
    edge_weight: float = Field(default=20, ge=0)
    expected_value_weight: float = Field(default=20, ge=0)
    data_quality_weight: float = Field(default=15, ge=0)
    completeness_weight: float = Field(default=10, ge=0)
    freshness_weight: float = Field(default=5, ge=0)
    source_reliability_weight: float = Field(default=5, ge=0)
    edge_reference: float = Field(default=0.15, gt=0)
    expected_value_reference: float = Field(default=0.20, gt=0)
    validator_penalty: float = Field(default=20, ge=0)
    lineup_penalty: float = Field(default=8, ge=0)
    partial_stats_penalty: float = Field(default=8, ge=0)
    small_sample_penalty: float = Field(default=10, ge=0)
    max_candidates: int = Field(default=6, ge=1, le=6)
    max_concurrency: int = Field(default=2, ge=1, le=5)


class Settings(Contract):
    minimum_edge: Probability = 0.04
    minimum_ev: float = Field(default=0.01, ge=0, allow_inf_nan=False)
    minimum_data_quality: Probability = 0.75
    minimum_confidence: Probability = 0.60
    minimum_completeness: Probability = 0.75
    minimum_freshness: Probability = 0.75
    min_sample: int = Field(default=200, ge=1)
    min_training_sample: int = Field(default=10, ge=1)
    max_brier: Probability = 0.26
    max_calibration_error: Probability = 0.12
    suspend_calibration_error: Probability = 0.25
    kelly_fraction: Probability = 0.25
    max_single_bet_fraction: Probability = 0.01
    max_match_exposure: Probability = 0.015
    max_team_exposure: Probability = 0.03
    max_competition_exposure: Probability = 0.10
    max_portfolio_exposure: Probability = 0.20
    max_daily_exposure: Probability = 0.05
    max_weekly_exposure: Probability = 0.15
    max_drawdown: Probability = 0.20
    min_liquidity: float = Field(default=100, ge=0, allow_inf_nan=False)
    lineup_sensitivity_threshold: Probability = 0.5
    odds_ttl_seconds: int = Field(default=300, gt=0)
    stats_ttl_seconds: int = Field(default=86400, gt=0)
    news_ttl_seconds: int = Field(default=1800, gt=0)
    lineups_ttl_seconds: int = Field(default=300, gt=0)
    official_lineups_ttl_seconds: int = Field(default=120, gt=0)
    agent_timeout_seconds: float = Field(default=5, gt=0, allow_inf_nan=False)
    provider_timeout_seconds: float = Field(default=3, gt=0, allow_inf_nan=False)
    provider_retries: int = Field(default=1, ge=0, le=5)
    seed: int = 42
    database_url: str = "sqlite:///data/better.db"
    analysis_requests_per_minute: int = Field(default=20, gt=0)
    agents_enabled: dict[str, bool] = Field(default_factory=lambda: {"matched_betting": False})
    critical_agents: tuple[str, ...] = (
        "football_data_analyst",
        "football_odds_analyst",
        "predictive_model",
        "model_validator",
        "risk_manager",
        "professional_bettor",
    )
    supported_markets: tuple[str, ...] = ("1X2", "OVER_UNDER_2_5", "BTTS")
    league_capabilities: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    system_two_enabled: bool = False
    llm_max_repair_attempts: int = Field(default=1, ge=0, le=3)
    sources_config: str = "config/sources.yaml"
    validation_cohort_enabled: bool = True
    validation_cohort_auto_enroll: bool = True
    validation_canonical_checkpoint: str = Field(default="T6", pattern="^(EARLY|T24|T6|T1|LINEUP|CLOSING)$")
    validation_canonical_fallback: str | None = Field(
        default="T24", pattern="^(EARLY|T24|T6|T1|LINEUP|CLOSING)$"
    )
    validation_result_poll_enabled: bool = True
    active_competitions: tuple[str, ...] = ()
    timing_comparison_enabled: bool = False
    timing_comparison_competitions: tuple[str, ...] = ()
    result_identity_resolution_enabled: bool = True
    result_auto_settlement_enabled: bool = True
    result_provider_priority: tuple[str, ...] = (
        "serie_c_official",
        "api_football",
        "football_data_org",
        "manual",
    )
    result_identity_kickoff_tolerance_minutes: int = Field(default=15, ge=0, le=60)
    result_identity_require_competition: bool = True
    validation_live_forward_required: bool = True
    validation_feature_schema_version: str = "1.0.0"
    validation_verified_competitions: tuple[str, ...] = ()
    validation_research_budget_per_match: float = Field(default=0.25, ge=0, allow_inf_nan=False)
    validation_research_budget_per_day: float = Field(default=5.0, ge=0, allow_inf_nan=False)
    validation_research_budget_per_week: float = Field(default=25.0, ge=0, allow_inf_nan=False)
    batch_ranking: BatchRankingSettings = Field(default_factory=BatchRankingSettings)

    @property
    def version(self) -> str:
        raw = json.dumps(self.decision_configuration(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def decision_configuration(self) -> dict:
        # Connection strings may contain credentials. Infrastructure locations do not
        # affect decision rules and must never enter persisted manifests or hashes.
        return self.model_dump(
            mode="json",
            exclude={
                "database_url",
                "sources_config",
                "result_identity_resolution_enabled",
                "result_auto_settlement_enabled",
                "result_provider_priority",
                "result_identity_kickoff_tolerance_minutes",
                "result_identity_require_competition",
                "active_competitions",
                "timing_comparison_enabled",
                "timing_comparison_competitions",
            },
        )

    @classmethod
    def load(cls, directory: str | Path = "config") -> "Settings":
        values = {}
        directory = Path(directory)
        values["sources_config"] = (directory / "sources.yaml").as_posix()
        for filename in ("app.yaml", "risk.yaml", "agents.yaml"):
            path = directory / filename
            if path.exists():
                data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                if not isinstance(data, dict):
                    raise ValueError(f"{filename} must be a mapping")
                values.update(data)
        markets = directory / "markets.yaml"
        if markets.exists():
            values.update(yaml.safe_load(markets.read_text(encoding="utf-8")) or {})
        leagues = directory / "leagues.yaml"
        if leagues.exists():
            values["league_capabilities"] = yaml.safe_load(leagues.read_text(encoding="utf-8")) or {}
        for key in cls.model_fields:
            if (env_value := os.environ.get(f"BETTER_{key.upper()}")) is not None:
                values[key] = yaml.safe_load(env_value)
        return cls.model_validate(values)
