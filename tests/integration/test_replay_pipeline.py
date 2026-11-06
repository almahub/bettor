from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from better.config import Settings
from better.domain.schemas import AnalysisRequest
from better.orchestration.orchestrator import Orchestrator
from better.providers.fake import make_demo_bundle
from better.providers.llm import FakeSystemTwoProvider
from better.services.replay_service import ReplayError, replay_analysis
from better.storage.repository import Repository
from better.system_one import ShadowCoordinator, SystemOneSettings

AS_OF = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)


async def original_run(case="bet", *, system_two=None, request_as_of=AS_OF):
    bundle = make_demo_bundle(case, as_of=request_as_of or datetime.now(timezone.utc))
    bundle = replace(bundle, system_two=system_two)
    runtime = Orchestrator(
        bundle,
        Settings(system_two_enabled=system_two is not None),
        Repository("sqlite:///:memory:"),
        shadow=ShadowCoordinator(SystemOneSettings(enabled=False)),
    )
    return await runtime.analyze(AnalysisRequest(match_id="demo-match", as_of=request_as_of))


@pytest.mark.parametrize(
    "case", ["bet", "no_bet", "wait_lineups", "wait_odds", "watchlist", "missing_data", "stale_odds"]
)
async def test_replay_from_complete_archived_inputs(case):
    run = await original_run(case)
    replay = await replay_analysis(run)
    assert replay.model_dump(mode="json") == run.model_dump(mode="json")
    assert run.input_manifest["resolved_config"]
    assert len(run.input_manifest["prompt_hashes"]) == 14


async def test_replay_preserves_live_null_as_of_request():
    run = await original_run(request_as_of=None)
    assert run.input_manifest["request"]["as_of"] is None
    assert await replay_analysis(run) == run


async def test_system_two_replay_uses_archived_output():
    provider = FakeSystemTwoProvider()
    run = await original_run(system_two=provider)
    assert len(run.system_two_records) == 1
    replay = await replay_analysis(run)
    assert replay == run
    assert len(provider.calls) == 1


async def test_replay_rejects_input_or_output_tampering():
    run = await original_run()
    manifest = {**run.input_manifest, "seed": 1234}
    with pytest.raises(ReplayError, match="hash"):
        await replay_analysis(run.model_copy(update={"input_manifest": manifest}))
    mutated = run.model_copy(update={"decision": run.decision.model_copy(update={"confidence": 0.1})})
    with pytest.raises(ReplayError, match="decision differs"):
        await replay_analysis(mutated)


async def test_post_lineup_link_does_not_overwrite_parent():
    bundle = make_demo_bundle("wait_lineups", as_of=AS_OF)
    repository = Repository("sqlite:///:memory:")
    runtime = Orchestrator(
        bundle, Settings(), repository, shadow=ShadowCoordinator(SystemOneSettings(enabled=False))
    )
    parent = await runtime.analyze(AnalysisRequest(match_id="demo-match", as_of=AS_OF))
    bundle.fixtures.lineups = bundle.fixtures.lineups.model_copy(update={"status": "CONFIRMED_LINEUP"})
    child = await runtime.analyze(
        AnalysisRequest(
            match_id="demo-match",
            as_of=AS_OF + timedelta(seconds=30),
            mode="POST_LINEUP",
            parent_analysis_id=parent.analysis_id,
        )
    )
    assert parent.decision.status.value == "WAIT_FOR_LINEUPS"
    assert child.decision.status.value == "BET"
    assert repository.get_run(parent.analysis_id) == parent
    with pytest.raises(ReplayError, match="parent"):
        await replay_analysis(child)
    assert await replay_analysis(child, parent_runs=(parent,)) == child
