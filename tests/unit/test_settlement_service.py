from datetime import datetime, timedelta, timezone

import pytest

from better.config import Settings
from better.domain.schemas import AnalysisRequest
from better.providers.fake import make_demo_bundle
from better.services.application import make_application
from better.services.settlement_service import MatchResultInput, SettlementService
from better.storage.repository import Repository
from better.system_one import SystemOneSettings


async def prepared():
    as_of = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)
    repository = Repository("sqlite:///:memory:")
    runtime = make_application(
        settings=Settings(),
        repository=repository,
        providers=make_demo_bundle(as_of=as_of),
        system_one_settings=SystemOneSettings(enabled=False),
    )
    run = await runtime.analyze(AnalysisRequest(match_id="demo-match", as_of=as_of))
    snapshots = {
        name: value
        for name, value in {
            "stats": run.snapshot.stats,
            "odds": run.snapshot.odds,
            "news": run.snapshot.news,
            "lineups": run.snapshot.lineups,
        }.items()
        if value is not None
    }
    repository.archive_pre_match_observation(
        match=run.snapshot.match,
        as_of=run.as_of,
        collected_at=run.as_of,
        snapshots=snapshots,
        failures={},
        purpose="TEST",
        checkpoint="T24",
    )
    return repository, run


async def test_result_creates_closing_prices_and_cutoff_safe_history():
    repository, run = await prepared()
    settled_at = run.snapshot.match.kickoff + timedelta(hours=2)
    service = SettlementService(repository, clock=lambda: settled_at)
    request = MatchResultInput(
        match_id=run.match_id,
        home_score=2,
        away_score=1,
        source_url="https://competition.example/results/demo-match",
        publisher="Official competition",
        source_tier="A",
    )
    result = service.record(request)
    assert result["closing_prices"] == 7
    assert result["settled_predictions"] > 0
    history = repository.settled_validation_history(settled_at)
    assert history
    assert all(row.predicted_at < row.settled_at for row in history)
    assert {row.outcome for row in history if row.market == "1X2"} == {0, 1}
    assert service.record(request)["cached"] is True


async def test_result_rejects_early_or_conflicting_record():
    repository, run = await prepared()
    request = MatchResultInput(
        match_id=run.match_id,
        home_score=0,
        away_score=0,
        source_url="https://competition.example/results/demo-match",
        publisher="Official competition",
        source_tier="A",
    )
    with pytest.raises(ValueError, match="regulation"):
        SettlementService(repository, clock=lambda: run.snapshot.match.kickoff).record(request)
    service = SettlementService(repository, clock=lambda: run.snapshot.match.kickoff + timedelta(hours=2))
    service.record(request)
    with pytest.raises(ValueError, match="different immutable result"):
        service.record(request.model_copy(update={"home_score": 1}))
