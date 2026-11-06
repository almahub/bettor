from dataclasses import replace
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from better.config import Settings
from better.domain.enums import AgentStatus, MarketType, PredictionStatus
from better.domain.schemas import AgentResult, OddsReport
from better.providers.fake import make_demo_bundle
from better.services.forecast_service import ForecastService

NOW = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)


def result(agent_id, payload):
    return AgentResult(
        analysis_id=uuid4(),
        agent_id=agent_id,
        status=AgentStatus.OK,
        summary="test",
        generated_at=NOW,
        payload=payload,
    )


@pytest.mark.asyncio
async def test_short_sample_produces_indicative_raw_forecast_but_never_ensemble():
    from tests.unit.test_agents_pipeline import execute

    results = await execute(settings=Settings(min_training_sample=100))
    predictive = results["predictive_model"]
    assert predictive.status == AgentStatus.PARTIAL
    assert predictive.payload.model_predictions
    assert predictive.payload.model_predictions[0].prediction_status == PredictionStatus.INDICATIVE
    assert predictive.payload.ensemble == {}
    assert results["professional_bettor"].payload.decision.stake.amount == 0


def validation_sample(bundle, size):
    grouped = {}
    for row in bundle.validation_history:
        grouped.setdefault(row.market, []).append(row)
    return tuple(row for rows in grouped.values() for row in rows[:size])


@pytest.mark.asyncio
@pytest.mark.parametrize("sample", [0, 199])
async def test_unvalidated_model_keeps_math_visible_but_blocks_bet_and_stake(sample):
    from tests.unit.test_agents_pipeline import execute

    bundle = make_demo_bundle(as_of=NOW)
    bundle = replace(bundle, validation_history=validation_sample(bundle, sample))
    results = await execute(bundle=bundle)
    predictive = results["predictive_model"]
    prediction = predictive.payload.model_predictions[0]
    value = results["value_bettor"].payload
    risk = results["risk_manager"].payload
    decision = results["professional_bettor"].payload.decision

    assert prediction.prediction_status == PredictionStatus.UNVALIDATED
    assert prediction.validation_sample_size == sample
    assert prediction.validation_minimum == 200
    assert prediction.input_completeness == 1
    assert prediction.fair_odds
    assert predictive.payload.ensemble == {}
    assert predictive.analysis_available is True
    assert predictive.operationally_eligible is False
    assert value.candidates == ()
    assert value.indicative_candidates
    assert all(
        candidate.expected_value == pytest.approx(candidate.model_probability * candidate.current_odds - 1)
        for candidate in value.indicative_candidates
    )
    assert all(not candidate.operationally_eligible for candidate in value.indicative_candidates)
    assert risk.accepted is False and risk.stake.amount == 0
    assert decision.stake.amount == 0
    assert "MODEL_VALIDATION_INSUFFICIENT" in decision.reason_codes
    assert results["betting_journal"].payload.operational_entry is False


@pytest.mark.asyncio
async def test_validation_boundary_200_preserves_existing_operational_path():
    from tests.unit.test_agents_pipeline import execute

    bundle = make_demo_bundle(as_of=NOW)
    bundle = replace(bundle, validation_history=validation_sample(bundle, 200))
    results = await execute(bundle=bundle)
    prediction = results["predictive_model"].payload.model_predictions[0]

    assert prediction.prediction_status == PredictionStatus.VALIDATED
    assert prediction.validation_sample_size == 200
    assert results["predictive_model"].payload.ensemble
    assert results["value_bettor"].payload.candidates


@pytest.mark.asyncio
async def test_missing_model_input_and_quotes_have_precise_reason_codes():
    from tests.unit.test_agents_pipeline import execute

    bundle = make_demo_bundle(as_of=NOW)
    bundle.stats.stats = bundle.stats.stats.model_copy(
        update={"home": bundle.stats.stats.home.model_copy(update={"sample_size": 0})}
    )
    missing_input = await execute(bundle=bundle)
    assert missing_input["predictive_model"].payload is None
    assert "MODEL_INPUT_INSUFFICIENT" in missing_input["professional_bettor"].payload.decision.reason_codes

    missing_quotes = await execute("odds_offline")
    assert missing_quotes["predictive_model"].payload.model_predictions
    assert missing_quotes["value_bettor"].payload is None
    assert "QUOTE_MISSING" in missing_quotes["professional_bettor"].payload.decision.reason_codes


@pytest.mark.asyncio
async def test_market_supported_for_analysis_is_distinct_from_bet_eligibility():
    from tests.unit.test_agents_pipeline import execute

    results = await execute(settings=Settings(supported_markets=(MarketType.MATCH_WINNER.value,)))
    report = results["market_selection"].payload

    assert report.supported_markets == (MarketType.MATCH_WINNER,)
    assert MarketType.MATCH_WINNER in report.eligible_markets
    assert report.rejections[MarketType.BTTS.value] == "MARKET_UNSUPPORTED"


def test_market_forecast_devigs_all_complete_demo_markets():
    bundle = make_demo_bundle(as_of=NOW)
    report = OddsReport(quotes=bundle.odds.odds.quotes)
    forecast = ForecastService().build(
        {"football_odds_analyst": result("football_odds_analyst", report)},
        NOW,
        (MarketType.MATCH_WINNER, MarketType.OVER_UNDER_2_5, MarketType.BTTS),
    )
    assert {entry.market for entry in forecast.markets} == {
        MarketType.MATCH_WINNER,
        MarketType.OVER_UNDER_2_5,
        MarketType.BTTS,
    }
    assert all(entry.method == "MARKET_NO_VIG" for entry in forecast.markets)
    assert all(not entry.admitted_for_decision for entry in forecast.markets)
    assert all(sum(entry.probabilities.values()) == pytest.approx(1) for entry in forecast.markets)


def test_incomplete_market_is_not_presented_as_probability_distribution():
    bundle = make_demo_bundle(as_of=NOW)
    quotes = tuple(q for q in bundle.odds.odds.quotes if q.market.selection != "DRAW")
    forecast = ForecastService().build(
        {"football_odds_analyst": result("football_odds_analyst", OddsReport(quotes=quotes))},
        NOW,
        (MarketType.MATCH_WINNER,),
    )
    assert forecast.markets == ()
