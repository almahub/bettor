import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from better.agents.matched_betting import MatchedBettingAgent, MatchedBettingInput
from better.agents.registry import create_registry
from better.config import Settings
from better.domain.enums import AgentStatus, DecisionStatus, MarketType, ValidationStatus
from better.domain.schemas import AgentContext
from better.orchestration.workflow import SPORTS_LAYERS
from better.providers.fake import make_demo_bundle
from better.providers.llm import FakeSystemTwoProvider, ReasoningResponse


async def execute(case="bet", settings=None, bundle=None):
    now = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)
    bundle = bundle or make_demo_bundle(case, now)
    settings = settings or Settings()
    registry = create_registry(bundle, settings)
    base = AgentContext(
        analysis_id=uuid4(),
        run_id=uuid4(),
        match=bundle.fixtures.match,
        requested_markets=(MarketType.MATCH_WINNER, MarketType.OVER_UNDER_2_5, MarketType.BTTS),
        as_of=now,
    )
    results = {}
    for layer in SPORTS_LAYERS:

        async def invoke(agent_id):
            agent = registry.get(agent_id)
            context = base.model_copy(
                update={
                    "upstream_results": {key: results[key] for key in agent.dependencies if key in results},
                    "validation_history": bundle.validation_history if agent.needs_history else (),
                    "portfolio": bundle.portfolio if agent.needs_portfolio else None,
                }
            )
            return agent_id, await agent.run(context)

        completed = await asyncio.gather(*(invoke(id) for id in layer if registry.is_enabled(id)))
        results.update(completed)
    return results


@pytest.mark.asyncio
async def test_complete_offline_evidence_pipeline():
    results = await execute()
    decision = results["professional_bettor"].payload.decision
    assert len(results) == 13
    assert decision.status == DecisionStatus.BET
    assert decision.stake.amount > 0
    assert all(trace.status == "PASS" for trace in decision.decision_trace)
    assert results["betting_journal"].payload.recorded is False
    assert results["football_odds_analyst"].signal.direction.value == "NOT_APPLICABLE"
    assert results["value_bettor"].evidence[0].derived_from


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case,expected",
    [
        ("suspended_model", DecisionStatus.NO_BET),
        ("source_conflict", DecisionStatus.NO_BET),
        ("risk_limit", DecisionStatus.NO_BET),
        ("missing_data", DecisionStatus.INSUFFICIENT_DATA),
        ("odds_offline", DecisionStatus.INSUFFICIENT_DATA),
        ("stale_odds", DecisionStatus.NO_BET),
        ("wait_lineups", DecisionStatus.WAIT_FOR_LINEUPS),
        ("no_bet", DecisionStatus.NO_BET),
        ("wait_odds", DecisionStatus.WAIT_FOR_BETTER_ODDS),
        ("watchlist", DecisionStatus.WATCHLIST),
    ],
)
async def test_hard_gates_hold_despite_other_positive_reports(case, expected):
    results = await execute(case)
    assert results["professional_bettor"].payload.decision.status == expected


@pytest.mark.asyncio
async def test_validator_independent_and_future_settlements_ignored():
    now = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)
    bundle = make_demo_bundle(as_of=now)
    registry = create_registry(bundle, Settings())
    future = tuple(
        entry.model_copy(update={"settled_at": now + timedelta(days=1)})
        for entry in bundle.validation_history
    )
    context = AgentContext(
        analysis_id=uuid4(),
        run_id=uuid4(),
        match=bundle.fixtures.match,
        requested_markets=(MarketType.MATCH_WINNER,),
        as_of=now,
        validation_history=future,
    )
    result = await registry.get("model_validator").run(context)
    assert result.status == AgentStatus.INSUFFICIENT_DATA
    assert registry.get("model_validator").dependencies == ()


@pytest.mark.asyncio
async def test_model_suspension_excludes_ensemble_not_audit_prediction():
    results = await execute("suspended_model")
    assert results["predictive_model"].payload.ensemble == {}
    assert results["predictive_model"].payload.model_predictions
    assert all(
        v.validation_status == ValidationStatus.SUSPEND_MODEL
        for v in results["model_validator"].payload.validations
    )


@pytest.mark.asyncio
async def test_optional_tactical_agent_disable_preserves_decision():
    results = await execute(
        settings=Settings(agents_enabled={"matched_betting": False, "tactical_analyst": False})
    )
    assert "tactical_analyst" not in results
    assert results["professional_bettor"].payload.decision.status == DecisionStatus.BET


def test_matched_betting_is_independent_equal_profit_math():
    agent = MatchedBettingAgent(Settings())
    report = agent.calculate(
        MatchedBettingInput(
            promotion="Synthetic qualifying back/lay",
            requirements=("Terms verified separately",),
            back_stake=Decimal("100"),
            back_odds=2.5,
            lay_odds=2.6,
            commission=0.02,
        )
    )
    assert abs(Decimal(report.profit_if_back_wins) - Decimal(report.profit_if_lay_wins)) < Decimal("1e-20")
    assert agent.dependencies == ()
    assert "football_prediction" in agent.forbidden_tools


@pytest.mark.asyncio
async def test_system_two_injected_qualitative_only_and_records_versions():
    now = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)
    reasoning = FakeSystemTwoProvider()
    # A completed qualitative response leaves quantitative output untouched.
    # A requested review is tested separately as a conservative confidence cap.
    reasoning.response = reasoning.response.model_copy(update={"requires_review": False})
    bundle = replace(make_demo_bundle(as_of=now), system_two=reasoning)
    baseline = await execute(bundle=bundle)
    assert reasoning.calls == []
    results = await execute(bundle=bundle, settings=Settings(system_two_enabled=True))
    assert len(reasoning.calls) == 1
    assert results["tactical_analyst"].payload.analysis_text == "Synthetic reasoning fixture"
    assert (
        results["professional_bettor"].payload.decision.status
        == baseline["professional_bettor"].payload.decision.status
    )
    assert reasoning.records[0]["prompt_version"] == "1.0.0"
    assert len(reasoning.records[0]["input_hash"]) == 64


@pytest.mark.asyncio
async def test_system_two_missing_or_invented_citation_is_explicit():
    results = await execute(settings=Settings(system_two_enabled=True))
    assert "no provider configured" in results["tactical_analyst"].unknowns[0]
    now = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)
    bad = FakeSystemTwoProvider(
        ReasoningResponse(
            analysis="unsupported claim", evidence_ids=("invented",), limitations=(), requires_review=False
        )
    )
    bundle = replace(make_demo_bundle(as_of=now), system_two=bad)
    results = await execute(bundle=bundle, settings=Settings(system_two_enabled=True))
    assert results["tactical_analyst"].status == AgentStatus.VALIDATION_ERROR


@pytest.mark.asyncio
async def test_identical_news_headlines_from_distinct_sources_keep_distinct_evidence():
    from better.domain.models import NewsItem

    now = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)
    bundle = make_demo_bundle(as_of=now)
    source = bundle.news.news.provenance[0]
    one = NewsItem(
        event_id="event",
        headline="Synthetic same headline",
        category="INJURY",
        verified=True,
        provenance=source,
    )
    two = one.model_copy(
        update={
            "provenance": source.model_copy(
                update={"source_id": "independent-source", "raw_reference": "independent-record"}
            )
        }
    )
    bundle.news.news = bundle.news.news.model_copy(update={"items": (one, one, two)})
    results = await execute(bundle=bundle)
    news = results["news_intelligence"]
    assert len(news.payload.events) == 2
    assert len({e.evidence_id for e in news.evidence}) == 2
