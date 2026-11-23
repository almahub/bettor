import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from better.services.settlement_service import SettlementService
from better.storage.repository import Repository


def test_pending_result_does_not_forget_an_old_result_after_thousand_rows():
    now = datetime.now(UTC)
    repo = Repository('sqlite:///:memory:')
    with repo.engine.begin() as conn:
        for i in range(1001):
            repo._append(conn,'match_results',identifier=f'r-{i:05}',entity_id=f'm-{i}',match_id=f'm-{i}',payload={'match_id':f'm-{i}'})
        repo._append(conn,'analysis_runs',identifier='run',entity_id='m-0',match_id='m-0',payload={'snapshot':{'match':{
            'match_id':'m-0','home_team':'Home','away_team':'Away','competition':'Serie A',
            'kickoff':(now-timedelta(days=2)).isoformat()}}})
    assert SettlementService(repo, clock=lambda:now).pending() == []
    repo.close()


async def test_parallel_football_research_calls_provider_once_and_reuses_result():
    from better.domain.models import Match
    from better.observability.metrics import Metrics
    from better.services.football_data_research import (
        FootballDataResearchConfiguration,
        FootballDataResearchService,
    )
    from tests.unit.test_football_data_research import NOW, football_result

    repo = Repository('sqlite:///:memory:')
    match = Match(match_id='m',home_team='Home',away_team='Away',home_team_id='h',away_team_id='a',
                  competition='Serie A',season='2026',kickoff=NOW+timedelta(days=1))

    class Slow:
        calls = 0

        async def research(self, request):
            self.calls += 1
            await asyncio.sleep(.02)
            return football_result(match).model_copy(update={'request_id':request.request_id})

    provider = Slow()
    service = FootballDataResearchService(FootballDataResearchConfiguration(enabled=True),provider,
        repository=repo,metrics=Metrics(),clock=lambda:NOW)
    a, b = await asyncio.gather(*(service.research(match,NOW,requested_fields=('recent_form',)) for _ in range(2)))
    assert provider.calls == 1 and not a.cached and b.cached
    assert not service._singleflight.tasks
    repo.close()


async def test_disconnect_does_not_cancel_shared_research():
    from better.domain.common import Contract
    from better.services.singleflight import SingleFlight

    class Result(Contract):
        cached: bool = False

    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def operation():
        calls.append(1)
        started.set()
        await release.wait()
        return Result()

    flight = SingleFlight()
    first = asyncio.create_task(flight.run('key', operation))
    await started.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    second = asyncio.create_task(flight.run('key', operation))
    await asyncio.sleep(0)
    release.set()
    assert (await second).cached and len(calls) == 1


def test_validator_history_reads_all_records_and_respects_settlement_cutoff():
    from better.providers.fake import make_demo_bundle
    now = datetime.now(UTC)
    sample = make_demo_bundle('bet', as_of=now).validation_history[0]
    repo = Repository('sqlite:///:memory:')
    with repo.engine.begin() as conn:
        for i in range(1001):
            payload = sample.model_copy(update={'match_id':f'm-{i}'}).model_dump(mode='json')
            repo._append(conn,'settled_predictions',identifier=f'p-{i:05}',entity_id='model',match_id=f'm-{i}',payload=payload)
    assert len(repo.settled_validation_history(now)) == 1001
    assert repo.settled_validation_history(sample.predicted_at) == ()
    repo.close()
