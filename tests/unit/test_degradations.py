import asyncio
from dataclasses import replace
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from better.agents.base import BaseAgent
from better.config import Settings
from better.domain.models import Degradation
from better.domain.schemas import AgentContext, AgentResult, AnalysisRequest, AnalysisRun, Decision
from better.orchestration.orchestrator import Orchestrator
from better.providers.fake import make_demo_bundle
from better.providers.llm import FakeSystemTwoProvider
from better.services.decision_service import DecisionService
from better.services.report_service import human_report
from better.storage.repository import Repository
from better.system_one import ShadowCoordinator, SystemOneSettings

AS_OF = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)


async def run_pipeline(*, settings=None, system_two=None):
    bundle = replace(make_demo_bundle("bet", AS_OF), system_two=system_two)
    engine = Orchestrator(
        bundle,
        settings or Settings(),
        Repository("sqlite:///:memory:"),
        shadow=ShadowCoordinator(SystemOneSettings(enabled=False)),
    )
    run = await engine.analyze(AnalysisRequest(match_id="demo-match", as_of=AS_OF))
    context = AgentContext(
        analysis_id=run.analysis_id,
        run_id=run.run_id,
        match=run.snapshot.match,
        as_of=AS_OF,
        requested_markets=("1X2", "BTTS", "OVER_UNDER_2_5"),
        portfolio=bundle.portfolio,
        upstream_results={
            k: v for k, v in run.results.items() if k not in {"professional_bettor", "betting_journal"}
        },
    )
    return run, context


@pytest.fixture
def baseline():
    return asyncio.run(run_pipeline())


def change_result(context, agent_id, **updates):
    result = context.upstream_results[agent_id].model_copy(update=updates)
    return context.model_copy(update={"upstream_results": {**context.upstream_results, agent_id: result}})


def test_partial_validator_and_tactics_explain_unchanged_quantitative_confidence(baseline):
    run, _ = baseline
    assert run.decision.status == "BET"
    assert run.decision.confidence == min(run.decision.confidence_components.values())
    assert run.decision.confidence == run.decision.confidence_components["model_uncertainty"]
    assert run.results["tactical_analyst"].signal.confidence == 0.5
    assert run.decision.confidence > 0.5  # Not an agent-confidence average.
    for agent in ("model_validator", "tactical_analyst"):
        result = run.results[agent]
        assert result.status == "PARTIAL"
        assert result.degradations[0].impact == "WARNING_ONLY"
        assert result.degradations[0].missing_checks
    model_gate = next(g for g in run.decision.decision_trace if g.gate == "MODEL_VALIDITY")
    assert model_gate.status == "PASS"
    assert "HISTORICAL_BANKROLL_UNAVAILABLE:WARNING_ONLY" in model_gate.reason
    assert AnalysisRun.model_validate_json(run.model_dump_json()) == run
    report = human_report(run)
    for text in (
        "bankroll_roi",
        "fitted_tactical_interaction_effect",
        "CONFIDENCE COMPONENTS",
        "<- weakest",
        "no additional confidence penalty",
        "MINIMUM ODDS BASIS",
    ):
        assert text in report


@pytest.mark.parametrize("critical_agent", ["model_validator", "tactical_analyst"])
def test_critical_partial_blocks_even_an_optional_agent(baseline, critical_agent):
    _, context = baseline
    limitation = Degradation(
        code="REQUIRED_CHECK_MISSING",
        missing_checks=("required_check",),
        critical=True,
        affected_gates=("MODEL_VALIDITY",),
        impact="BLOCK",
        reason="Required check absent",
    )
    context = change_result(context, critical_agent, degradations=(limitation,))
    decision = DecisionService(Settings()).evaluate(context)
    assert decision.status == "NO_BET"
    assert decision.stake.amount == 0
    assert decision.confidence == 0
    assert next(g.status for g in decision.decision_trace if g.gate == "MODEL_VALIDITY") == "FAIL"
    assert any(d.agent_id == critical_agent and d.critical for d in decision.degradations)


def test_unknown_legacy_partial_is_readable_but_never_admitted(baseline):
    _, context = baseline
    old = context.upstream_results["model_validator"].model_dump()
    old.pop("degradations")
    restored = AgentResult.model_validate(old)
    assert restored.degradations == ()
    context = change_result(context, "model_validator", degradations=())
    decision = DecisionService(Settings()).evaluate(context)
    assert decision.status != "BET"
    assert any(d.code == "UNCLASSIFIED_PARTIAL" for d in decision.degradations)


def test_critical_missing_check_for_another_market_is_explicitly_out_of_scope(baseline):
    _, context = baseline
    limitation = Degradation(
        code="BTTS_CHECK_MISSING",
        missing_checks=("btts_specific_check",),
        critical=True,
        affected_gates=("MODEL_VALIDITY",),
        impact="BLOCK",
        market_scope=("BTTS",),
        reason="Missing check applies to BTTS only",
    )
    context = change_result(context, "model_validator", degradations=(limitation,))
    decision = DecisionService(Settings()).evaluate(context)
    assert decision.market.type == "1X2"
    assert decision.status == "BET"
    assert decision.degradations[0].applies_to_decision is False


@pytest.mark.parametrize("field", ["brier_score", "log_loss", "calibration_error"])
def test_missing_required_validation_metric_cannot_pass_with_a_normal_weight_label(baseline, field):
    _, context = baseline
    report = context.upstream_results["model_validator"].payload
    metrics = tuple(v.model_copy(update={field: None}) for v in report.validations)
    context = change_result(
        context, "model_validator", payload=report.model_copy(update={"validations": metrics})
    )
    decision = DecisionService(Settings()).evaluate(context)
    assert decision.status != "BET"
    assert next(g.status for g in decision.decision_trace if g.gate == "MODEL_VALIDITY") == "FAIL"


@pytest.mark.parametrize("provider", [None, "review"])
async def test_requested_reasoning_absent_or_needing_review_caps_confidence(provider):
    run, _ = await run_pipeline(
        settings=Settings(system_two_enabled=True),
        system_two=FakeSystemTwoProvider() if provider else None,
    )
    assert run.decision.status == "WATCHLIST"
    assert run.decision.confidence == 0.5
    key = "degradation:tactical_analyst:SYSTEM_TWO_INTERPRETATION_LIMITED"
    assert run.decision.confidence_components[key] == 0.5
    assert run.decision.stake.amount == 0
    baseline_run, _ = await run_pipeline()
    assert (
        run.results["predictive_model"].payload.ensemble
        == baseline_run.results["predictive_model"].payload.ensemble
    )
    assert run.decision.estimated_probability == baseline_run.decision.estimated_probability
    raw = run.decision.model_dump()
    raw["confidence"] = 0.9
    with pytest.raises(ValidationError, match="confidence cap"):
        Decision.model_validate(raw)


@pytest.mark.parametrize(
    "updates",
    [
        {"critical": True},
        {"impact": "BLOCK"},
        {"confidence_cap": 0.03},
        {"impact": "CONFIDENCE_CAP"},
        {"missing_checks": ()},
        {"affected_gates": ("TYPO",)},
    ],
)
def test_inconsistent_degradation_contract_is_rejected(updates):
    data = dict(
        code="OPTIONAL_CHECK",
        missing_checks=("optional_check",),
        critical=False,
        affected_gates=("MODEL_VALIDITY",),
        impact="WARNING_ONLY",
        reason="Optional only",
    )
    with pytest.raises(ValidationError):
        Degradation.model_validate({**data, **updates})


def test_bet_schema_cannot_smuggle_a_critical_degradation(baseline):
    run, _ = baseline
    raw = run.decision.model_dump()
    raw["degradations"][0].update(critical=True, impact="BLOCK")
    with pytest.raises(ValidationError, match="critical degradation"):
        Decision.model_validate(raw)


async def test_new_agent_execution_rejects_unclassified_partial(baseline):
    run, context = baseline

    class UnclassifiedAgent(BaseAgent):
        agent_id = "model_validator"

        async def _run(self, _):
            return run.results[self.agent_id].model_copy(update={"degradations": ()})

    result = await UnclassifiedAgent(Settings()).run(
        context.model_copy(update={"upstream_results": {}, "portfolio": None})
    )
    assert result.status == "VALIDATION_ERROR"
    assert "explicit degradation" in result.errors[0].message
