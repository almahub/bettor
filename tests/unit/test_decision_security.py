"""Adversarial gate invariants, using an actual successful offline pipeline."""

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from better.config import Settings
from better.domain.enums import AgentStatus, DecisionStatus, ReliabilityTier
from better.domain.models import GateTrace, ModelPrediction, Stake, ValueCandidate
from better.domain.schemas import (
    AgentContext,
    AnalysisRequest,
    Decision,
    LineupReport,
    PredictiveReport,
    RiskReport,
)
from better.orchestration.orchestrator import Orchestrator
from better.providers.fake import make_demo_bundle
from better.services.decision_service import DecisionService
from better.storage.repository import Repository
from better.system_zero.math import expected_value, implied_probability


@pytest.fixture
def successful_context():
    as_of = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    providers = make_demo_bundle(as_of=as_of)
    run = asyncio.run(
        Orchestrator(providers, repository=Repository("sqlite:///:memory:")).analyze(
            AnalysisRequest(match_id="demo-match", as_of=as_of)
        )
    )
    assert run.decision.status == DecisionStatus.BET
    context = AgentContext(
        analysis_id=run.analysis_id,
        run_id=run.run_id,
        match=run.snapshot.match,
        requested_markets=("1X2", "OVER_UNDER_2_5", "BTTS"),
        as_of=as_of,
        portfolio=providers.portfolio,
        upstream_results={
            name: result
            for name, result in run.results.items()
            if name not in {"professional_bettor", "betting_journal"}
        },
    )
    return context, run.decision


def replace_payload(context, agent, payload):
    result = context.upstream_results[agent].model_copy(update={"payload": payload})
    return context.model_copy(update={"upstream_results": {**context.upstream_results, agent: result}})


def replace_candidate(context, **changes):
    risk = context.upstream_results["risk_manager"].payload
    original = risk.candidate
    candidate = original.model_copy(update=changes)
    context = replace_payload(context, "risk_manager", risk.model_copy(update={"candidate": candidate}))
    value = context.upstream_results["value_bettor"].payload
    candidates = tuple(candidate if item == original else item for item in value.candidates)
    return replace_payload(context, "value_bettor", value.model_copy(update={"candidates": candidates}))


def evaluate(context):
    return DecisionService(Settings.load()).evaluate(context)


def gate_status(decision, name):
    return next(trace.status for trace in decision.decision_trace if trace.gate == name)


def test_valid_baseline_survives_security_checks(successful_context):
    context, original = successful_context
    assert evaluate(context) == original


def test_partial_prediction_market_is_not_a_valid_distribution():
    with pytest.raises(ValidationError, match="complete supported outcome"):
        ModelPrediction(
            model="poisson",
            model_version="1",
            sample_size=10,
            data_cutoff=datetime.now(timezone.utc),
            probabilities={"1X2": {"HOME": 1}},
        )


def test_official_flag_cannot_exist_without_confirmed_snapshot():
    with pytest.raises(ValidationError, match="official lineup flag"):
        LineupReport(lineup=None, official=True, sensitivity=1)


def test_ensemble_cannot_fabricate_or_double_probability(successful_context):
    context, _ = successful_context
    report = context.upstream_results["predictive_model"].payload
    raw = report.model_dump()
    raw["ensemble"]["BTTS"] = {"YES": 1, "NO": 1}
    with pytest.raises(ValidationError):
        PredictiveReport.model_validate(raw)
    raw = report.model_dump()
    raw["ensemble"]["BTTS"] = {"YES": 0.9, "NO": 0.1}
    with pytest.raises(ValidationError, match="outside admitted member"):
        PredictiveReport.model_validate(raw)


def test_candidate_conservative_probability_cannot_exceed_point(successful_context):
    context, _ = successful_context
    raw = context.upstream_results["risk_manager"].payload.candidate.model_dump()
    raw["conservative_probability"] = 1
    with pytest.raises(ValidationError, match="conservative probability"):
        ValueCandidate.model_validate(raw)


def test_accepted_risk_requires_real_positive_amount(successful_context):
    context, _ = successful_context
    raw = context.upstream_results["risk_manager"].payload.model_dump()
    raw["stake"]["amount"] = Decimal(0)
    with pytest.raises(ValidationError, match="positive monetary stake"):
        RiskReport.model_validate(raw)


@pytest.mark.parametrize("mutation", ["gate", "zero_amount", "price", "ev", "confidence"])
def test_decision_contract_cannot_be_forged(successful_context, mutation):
    _, original = successful_context
    raw = original.model_dump()
    if mutation == "gate":
        raw["decision_trace"] = [GateTrace(gate="invented", status="PASS", reason="forged").model_dump()]
    elif mutation == "zero_amount":
        raw["stake"]["amount"] = Decimal(0)
    elif mutation == "price":
        raw["current_odds"] = 0.5
    elif mutation == "ev":
        raw["expected_value"] += 0.1
    else:
        raw["confidence_components"] = {}
    with pytest.raises(ValidationError):
        Decision.model_validate(raw)


def test_forged_novig_edge_is_recomputed_from_complete_quotes(successful_context):
    context, _ = successful_context
    candidate = context.upstream_results["risk_manager"].payload.candidate
    context = replace_candidate(context, no_vig_probability=0.01, edge=candidate.model_probability - 0.01)
    decision = evaluate(context)
    assert decision.status == DecisionStatus.NO_BET
    assert gate_status(decision, "NUMERICAL_CONSISTENCY") == "FAIL"


def test_internally_consistent_fake_price_cannot_replace_observed_quote(successful_context):
    context, _ = successful_context
    candidate = context.upstream_results["risk_manager"].payload.candidate
    odds = candidate.current_odds + 1
    context = replace_candidate(
        context,
        current_odds=odds,
        implied_probability=implied_probability(odds),
        expected_value=expected_value(candidate.model_probability, odds),
    )
    assert evaluate(context).status == DecisionStatus.NO_BET


def test_model_zero_probability_fails_closed_without_division_exception(successful_context):
    context, _ = successful_context
    context = replace_candidate(context, model_probability=0, conservative_probability=0)
    decision = evaluate(context)
    assert decision.status == DecisionStatus.NO_BET
    assert gate_status(decision, "NUMERICAL_CONSISTENCY") == "FAIL"


def test_minimum_odds_cannot_be_lowered_by_upstream_report(successful_context):
    context, _ = successful_context
    context = replace_candidate(context, minimum_acceptable_odds=1.0)
    assert gate_status(evaluate(context), "NUMERICAL_CONSISTENCY") == "FAIL"


@pytest.mark.parametrize("mutation", ["incomplete", "mixed_timestamp", "opening"])
def test_current_market_must_be_complete_and_simultaneous(successful_context, mutation):
    context, _ = successful_context
    candidate = context.upstream_results["risk_manager"].payload.candidate
    report = context.upstream_results["football_odds_analyst"].payload
    index = next(
        i
        for i, quote in enumerate(report.quotes)
        if quote.market.type == candidate.market.type and quote.market.selection != candidate.market.selection
    )
    quotes = list(report.quotes)
    if mutation == "incomplete":
        quotes.pop(index)
    elif mutation == "mixed_timestamp":
        quotes[index] = quotes[index].model_copy(
            update={"captured_at": quotes[index].captured_at - timedelta(seconds=1)}
        )
    else:
        quotes[index] = quotes[index].model_copy(update={"is_opening": True})
    context = replace_payload(
        context, "football_odds_analyst", report.model_copy(update={"quotes": tuple(quotes)})
    )
    assert evaluate(context).status == DecisionStatus.NO_BET


@pytest.mark.parametrize("mutation", ["analysis_id", "agent_id", "generated_at"])
def test_foreign_or_future_upstream_envelope_is_rejected(successful_context, mutation):
    context, _ = successful_context
    original = context.upstream_results["football_data_analyst"]
    updates = (
        {"analysis_id": uuid4()}
        if mutation == "analysis_id"
        else {"agent_id": "different_agent"}
        if mutation == "agent_id"
        else {"generated_at": context.as_of + timedelta(seconds=1)}
    )
    result = original.model_copy(update=updates)
    context = context.model_copy(
        update={"upstream_results": {**context.upstream_results, "football_data_analyst": result}}
    )
    decision = evaluate(context)
    assert decision.status == DecisionStatus.NO_BET
    assert gate_status(decision, "CONTEXT_INTEGRITY") == "FAIL"


def test_future_model_features_are_rejected(successful_context):
    context, _ = successful_context
    report = context.upstream_results["predictive_model"].payload
    predictions = tuple(
        prediction.model_copy(update={"data_cutoff": context.as_of + timedelta(seconds=1)})
        for prediction in report.model_predictions
    )
    context = replace_payload(
        context, "predictive_model", report.model_copy(update={"model_predictions": predictions})
    )
    assert gate_status(evaluate(context), "CONTEXT_INTEGRITY") == "FAIL"


def test_unrelated_trusted_news_cannot_launder_rumor_based_model(successful_context):
    context, _ = successful_context
    original = context.upstream_results["football_data_analyst"]
    evidence = tuple(
        item.model_copy(
            update={
                "provenance": tuple(
                    source.model_copy(update={"reliability_tier": ReliabilityTier.D})
                    for source in item.provenance
                )
            }
        )
        for item in original.evidence
    )
    result = original.model_copy(update={"evidence": evidence})
    context = context.model_copy(
        update={"upstream_results": {**context.upstream_results, "football_data_analyst": result}}
    )
    decision = evaluate(context)
    assert decision.status == DecisionStatus.NO_BET
    assert gate_status(decision, "EVIDENCE_PROVENANCE") == "FAIL"


@pytest.mark.parametrize("mutation", ["stale", "error"])
def test_unusable_lineup_never_satisfies_timing_gate(successful_context, mutation):
    context, _ = successful_context
    original = context.upstream_results["lineup_player_impact"]
    if mutation == "stale":
        lineup = original.payload.lineup.model_copy(
            update={"captured_at": context.as_of - timedelta(hours=1)}
        )
        result = original.model_copy(
            update={"payload": original.payload.model_copy(update={"lineup": lineup})}
        )
    else:
        result = original.model_copy(update={"status": AgentStatus.SOURCE_ERROR})
    context = context.model_copy(
        update={"upstream_results": {**context.upstream_results, "lineup_player_impact": result}}
    )
    decision = evaluate(context)
    assert decision.status == DecisionStatus.WAIT_FOR_LINEUPS
    assert gate_status(decision, "TIMING") == "WAIT"


@pytest.mark.parametrize("amount", [Decimal("0"), Decimal("10.01")])
def test_final_risk_gate_rechecks_monetary_stake(successful_context, amount):
    context, _ = successful_context
    report = context.upstream_results["risk_manager"].payload
    stake = Stake(bankroll_fraction=0.01, amount=amount)
    context = replace_payload(context, "risk_manager", report.model_copy(update={"stake": stake}))
    decision = evaluate(context)
    assert decision.status == DecisionStatus.NO_BET
    assert gate_status(decision, "RISK") == "FAIL"


def test_float_fraction_display_does_not_lose_valid_monetary_cent(successful_context):
    context, _ = successful_context
    bankroll, amount = Decimal("3000"), Decimal("1.00")
    context = context.model_copy(
        update={"portfolio": context.portfolio.model_copy(update={"bankroll": bankroll})}
    )
    report = context.upstream_results["risk_manager"].payload
    stake = Stake(bankroll_fraction=float(amount / bankroll), amount=amount)
    context = replace_payload(context, "risk_manager", report.model_copy(update={"stake": stake}))
    decision = evaluate(context)
    assert decision.status == DecisionStatus.BET
    assert decision.stake.amount == Decimal("1.00")


def test_underreported_fraction_cannot_hide_monetary_exposure(successful_context):
    context, _ = successful_context
    report = context.upstream_results["risk_manager"].payload
    stake = Stake(bankroll_fraction=0.001, amount=Decimal("10"))
    context = replace_payload(context, "risk_manager", report.model_copy(update={"stake": stake}))
    assert gate_status(evaluate(context), "RISK") == "FAIL"
