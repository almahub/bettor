"""Mandatory full-pipeline regression: System One has no decision authority."""

from datetime import UTC, datetime

import pytest

from better.config import Settings
from better.domain.schemas import AnalysisRequest
from better.orchestration.orchestrator import Orchestrator
from better.providers.fake import make_demo_bundle
from better.providers.llm import FakeSystemTwoProvider
from better.storage.repository import Repository
from better.system_one import FakeSystemOneProvider, ShadowCoordinator, SystemOneSettings

AS_OF = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


async def run_pipeline(case, observer, *, system_two=None):
    repository = Repository("sqlite:///:memory:")
    try:
        orchestrator = Orchestrator(
            make_demo_bundle(case, as_of=AS_OF),
            settings=Settings(system_two_enabled=system_two is not None),
            repository=repository,
            shadow=observer,
            system_two=system_two,
        )
        run = await orchestrator.analyze(AnalysisRequest(match_id="demo-match", as_of=AS_OF))
        persisted = repository.get_run(run.analysis_id)
        assert persisted.model_dump(mode="json") == run.model_dump(mode="json")
        records = repository.list_records("shadow_decisions", entity_id=run.analysis_id)
        return run, records
    finally:
        repository.close()


@pytest.mark.parametrize("case", ["bet", "no_bet", "watchlist", "wait_lineups", "wait_odds", "missing_data"])
async def test_shadow_hostile_suggestions_cannot_change_full_pipeline(case):
    baseline, baseline_records = await run_pipeline(case, ShadowCoordinator(SystemOneSettings()))
    fake = FakeSystemOneProvider(
        values={
            "should_analyze": False,
            "needs_stats_agent": False,
            "needs_odds_agent": False,
            "needs_news_agent": False,
            "needs_lineup_agent": False,
            "needs_tactical_agent": False,
            "requires_deep_reasoning": True,
            "consistency_action": "ESCALATE_SYSTEM_2",
        }
    )
    observer = ShadowCoordinator(SystemOneSettings(enabled=True, provider="fake"), fake)
    shadowed, records = await run_pipeline(case, observer)
    assert shadowed.model_dump(mode="json") == baseline.model_dump(mode="json")
    assert baseline_records == []
    assert fake.calls == 4
    assert {record["stage"] for record in records} == {"pre_screen", "routing", "consistency", "escalation"}
    assert all(record["error"] is None for record in records)
    for record in records:
        assert record["pipeline_actual_action"]["decision"] == baseline.decision.status.value
        assert record["pipeline_actual_action"]["agents"] == list(baseline.actual_agents)
    prescreen = next(record for record in records if record["stage"] == "pre_screen")
    consistency = next(record for record in records if record["stage"] == "consistency")
    assert "agent_outputs" not in prescreen["normalized_state"]
    assert "devils_advocate" in consistency["normalized_state"]["agent_outputs"]
    assert "professional_bettor" not in consistency["normalized_state"]["agent_outputs"]


@pytest.mark.parametrize("mode", ["error", "timeout", "malformed", "abstain"])
async def test_shadow_failure_and_abstention_preserve_entire_analysis(mode):
    baseline, _ = await run_pipeline("bet", ShadowCoordinator(SystemOneSettings()))
    fake = FakeSystemOneProvider(mode=mode)
    observer = ShadowCoordinator(
        SystemOneSettings(
            enabled=True,
            provider="fake",
            rizzo_flow_timeout_ms=1,
            rizzo_flow_max_retries=0,
        ),
        fake,
    )
    shadowed, records = await run_pipeline("bet", observer)
    assert shadowed.model_dump(mode="json") == baseline.model_dump(mode="json")
    assert len(records) == 4
    if mode == "abstain":
        assert all(record["system_one_suggested_action"] == {} for record in records)
        assert all(record["error"] is None for record in records)
    else:
        assert all(record["error"] for record in records)


async def test_disabled_provider_and_state_builder_never_called(monkeypatch):
    baseline, _ = await run_pipeline("bet", ShadowCoordinator(SystemOneSettings()))

    def forbidden_build(*args, **kwargs):
        pytest.fail("Disabled System One must never construct semantic state")

    monkeypatch.setattr("better.orchestration.orchestrator.SystemOneStateBuilder.build", forbidden_build)
    fake = FakeSystemOneProvider(mode="error")
    disabled, records = await run_pipeline("bet", ShadowCoordinator(SystemOneSettings(enabled=False), fake))
    assert disabled.model_dump(mode="json") == baseline.model_dump(mode="json")
    assert fake.calls == 0
    assert records == []


async def test_escalation_suggestions_cannot_add_or_skip_system_two():
    baseline_provider = FakeSystemTwoProvider()
    baseline, _ = await run_pipeline(
        "bet", ShadowCoordinator(SystemOneSettings()), system_two=baseline_provider
    )
    fake = FakeSystemOneProvider(values={"needs_deep_reasoning": False, "requires_deep_reasoning": False})
    shadow_provider = FakeSystemTwoProvider()
    observer = ShadowCoordinator(SystemOneSettings(enabled=True, provider="fake"), fake)
    shadowed, records = await run_pipeline("bet", observer, system_two=shadow_provider)
    assert shadowed.model_dump(mode="json") == baseline.model_dump(mode="json")
    assert len(shadow_provider.calls) == len(baseline_provider.calls) == 1
    assert [call.model_dump(mode="json") for call in shadow_provider.calls] == [
        call.model_dump(mode="json") for call in baseline_provider.calls
    ]
    escalation = next(record for record in records if record["stage"] == "escalation")
    assert escalation["pipeline_actual_action"]["system2_calls"] == 1
    assert escalation["system_one_suggested_action"]["requires_deep_reasoning"] is False
