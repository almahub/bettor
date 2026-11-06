from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from better.config import Settings
from better.domain.schemas import AnalysisRequest
from better.providers.fake import make_demo_bundle
from better.services.application import make_application
from better.services.timing_comparison import TimingComparisonService
from better.storage.repository import Repository

NOW = datetime.now(UTC)


@pytest.fixture
async def experiment():
    repo = Repository('sqlite:///:memory:')
    settings = Settings(timing_comparison_enabled=True, timing_comparison_competitions=('SYNTHETIC_LEAGUE',))
    app = make_application(settings=settings, repository=repo, providers=make_demo_bundle('bet', as_of=NOW))
    original = await app.analyze(AnalysisRequest(match_id='demo-match', as_of=NOW))
    match = original.snapshot.match.model_copy(update={'kickoff': NOW + timedelta(hours=24)})
    clock = [NOW + timedelta(seconds=2)]
    calls = []

    def run_at(at):
        return original.model_copy(update={'analysis_id': uuid4(), 'as_of': at,
            'created_at': at, 'snapshot': original.snapshot.model_copy(update={'match': match, 'as_of': at})})

    async def analyze(request, **options):
        calls.append(options)
        return run_at(clock[0])

    runtime = SimpleNamespace(repository=repo, settings=settings, analyze=analyze)
    service = TimingComparisonService(runtime, clock=lambda: clock[0])
    yield service, repo, match, clock, calls, run_at
    await app.close()
    repo.close()


async def test_checkpoints_are_paired_persisted_and_idempotent_after_restart(experiment):
    service, repo, match, clock, calls, _ = experiment
    before = {table: repo.all_records(table) for table in ('bet_slips', 'canonical_predictions', 'validation_observations')}
    bank_before = repo.all_records('annotations', entity_id='bank:EUR:v1')
    first = await service.collect_due([match.model_dump(mode='json')])
    assert len(first['captured']) == 1
    assert first['captured'][0]['checkpoint'] == 'T24'
    await service.collect_due([match.model_dump(mode='json')])
    clock[0] = match.kickoff - timedelta(minutes=29)
    restarted = TimingComparisonService(service.runtime, clock=lambda: clock[0])
    second = await restarted.collect_due([])  # persisted enrolment survives restart
    assert len(second['captured']) == 1
    await restarted.collect_due([])
    assert calls == [{'cache_only_research': True}] * 2
    view = restarted.view(match.match_id, match.kickoff.isoformat())
    assert view['paired']
    assert view['checkpoints']['T24']['capture']['analysis_id'] != view['checkpoints']['T30']['capture']['analysis_id']
    for table, rows in before.items():
        assert repo.all_records(table) == rows
    assert repo.all_records('annotations', entity_id='bank:EUR:v1') == bank_before


async def test_late_start_does_not_recreate_missing_baseline(experiment):
    service, _, match, clock, calls, _ = experiment
    clock[0] = match.kickoff - timedelta(minutes=30)
    await service.collect_due([match.model_dump(mode='json')])
    assert not calls
    view = service.view(match.match_id, match.kickoff.isoformat())
    assert view['checkpoints']['T24']['status'] == 'MISSING'
    assert not view['paired']


async def test_disabled_and_other_competitions_do_not_call_runtime(experiment):
    service, _, match, _, calls, _ = experiment
    await service.collect_due([match.model_copy(update={'competition': 'OTHER'}).model_dump(mode='json')])
    service.settings = service.settings.model_copy(update={'timing_comparison_enabled': False})
    await service.collect_due([match.model_dump(mode='json')])
    assert calls == []


async def test_failed_checkpoint_is_not_retried_or_duplicated(experiment):
    service, repo, match, clock, calls, _ = experiment

    async def fail(*args, **kwargs):
        calls.append(kwargs)
        raise ValueError('network failure')

    service.runtime.analyze = fail
    first = await service.collect_due([match.model_dump(mode='json')])
    assert len(first['failed']) == 1
    await TimingComparisonService(service.runtime, clock=lambda: clock[0]).collect_due([match.model_dump(mode='json')])
    assert len(calls) == 1
    assert service.view(match.match_id, match.kickoff.isoformat())['checkpoints']['T24']['status'] == 'FAILED'
    assert len([r for r in service.rows() if r['kind'] == 'FAILED']) == 1


async def test_crash_claim_is_explicit_and_never_reruns(experiment):
    service, _, match, clock, calls, _ = experiment
    assert service._claim(match, 'T24')
    clock[0] += timedelta(minutes=11)
    await service.collect_due([match.model_dump(mode='json')])
    assert not calls
    assert service.view(match.match_id, match.kickoff.isoformat())['checkpoints']['T24']['status'] == 'FAILED'


async def test_postkickoff_future_source_and_window_leakage_rejected(experiment):
    service, _, match, clock, _, run_at = experiment
    run = run_at(clock[0])
    source = run.snapshot.stats.provenance[0].model_copy(update={'retrieved_at': match.kickoff})
    stats = run.snapshot.stats.model_copy(update={'provenance': (source,)})
    with pytest.raises(ValueError, match='LEAKAGE'):
        service.store(match, 'T24', run.model_copy(update={'snapshot': run.snapshot.model_copy(update={'stats': stats})}))
    clock[0] = match.kickoff
    with pytest.raises(ValueError, match='WINDOW'):
        service.store(match, 'T30', run_at(clock[0]))
    clock[0] = match.kickoff - timedelta(hours=22)
    with pytest.raises(ValueError, match='WINDOW'):
        service.store(match, 'T24', run_at(clock[0]))


async def test_metrics_use_paired_same_model_markets_and_verified_result(experiment):
    service, repo, match, clock, _, _ = experiment
    await service.collect_due([match.model_dump(mode='json')])
    clock[0] = match.kickoff - timedelta(minutes=29)
    await service.collect_due([])
    assert service.report()['metrics'] == []
    result = {'match_id': match.match_id, 'final_status': 'FINAL', 'home_score': 2, 'away_score': 1,
              'source_tier': 'B', 'verified_at': (match.kickoff + timedelta(hours=2)).isoformat()}
    with repo.engine.begin() as conn:
        repo._append(conn, 'match_results', identifier='result', entity_id=match.match_id, match_id=match.match_id, payload=result)
    clock[0] = match.kickoff + timedelta(hours=3)
    report = service.report()
    assert report['metrics']
    for group in report['metrics']:
        assert group['pairs'] == 1
        assert group['T24']['brier'] == group['T30']['brier']
        assert group['T24']['calibration'][0]['count'] == 1
    assert report['calls'] == 0
    assert report['cost_per_match'] == 0
    assert not repo.all_records('validation_observations')
    assert not repo.all_records('annotations', entity_id='bank:EUR:v1')


async def test_candidate_comparison_does_not_change_candidate(experiment):
    service, _, match, clock, _, _ = experiment
    await service.collect_due([match.model_dump(mode='json')])
    clock[0] = match.kickoff - timedelta(minutes=29)
    await service.collect_due([])
    markets = service.view(match.match_id, match.kickoff.isoformat())['checkpoints']['T30']['capture']['markets']
    model = markets[0]
    candidate = {'model_name': model['model'], 'model_version': model['version'],
                 'market': model['market'], 'selection': 'IMPOSSIBLE'}
    view = service.view(match.match_id, match.kickoff.isoformat(), candidate=candidate)
    assert view['verdict'] == 'CHANGED'
    assert candidate['selection'] == 'IMPOSSIBLE'
    assert view['checkpoints']['T30']['capture']['lineup_status'] == 'CONFIRMED_LINEUP'


def test_timing_settings_do_not_change_decision_hash():
    original = Settings()
    assert original.version == original.model_copy(update={'timing_comparison_enabled': True,
        'timing_comparison_competitions': ('SA',)}).version


async def test_cache_only_research_does_not_call_saved_agents_on_miss(experiment):
    from better.observability.metrics import Metrics
    from better.services.external_research import (
        ExternalResearchConfiguration,
        ExternalResearchService,
        ResearchFieldPolicy,
    )
    from better.services.football_data_research import (
        FootballDataResearchConfiguration,
        FootballDataResearchService,
    )
    service, repo, match, clock, _, _ = experiment

    class NeverCall:
        agent_id = 'test-agent'

        async def research(self, *args, **kwargs):
            pytest.fail('Cache-only must never call OpenAI')

    external = ExternalResearchService(ExternalResearchConfiguration(enabled=True,
        fields={'news': ResearchFieldPolicy(enabled=True)}), NeverCall(), repository=repo,
        metrics=Metrics(), clock=lambda: clock[0])
    football = FootballDataResearchService(FootballDataResearchConfiguration(enabled=True,
        supported_competitions=(match.competition,)), NeverCall(), repository=repo,
        metrics=Metrics(), clock=lambda: clock[0])
    assert await external.research_missing(match, as_of=clock[0], missing_fields=('news',),
        source_state_version='test', cache_only=True) is None
    assert await football.research(match, clock[0], requested_fields=('recent_form',), cache_only=True) is None
    assert not repo.all_records('research_requests', match_id=match.match_id)


async def test_unpreselected_and_probable_lineup_are_not_promoted(experiment):
    service, _, match, clock, calls, run_at = experiment
    run = run_at(clock[0])
    run = run.model_copy(update={'forecast': None, 'snapshot': run.snapshot.model_copy(update={
        'lineups': run.snapshot.lineups.model_copy(update={'status': 'PROBABLE_LINEUP'})})})
    saved = service.store(match, 'T24', run)
    assert saved['lineup_status'] == 'PROBABLE_LINEUP'
    clock[0] = match.kickoff - timedelta(minutes=30)
    await service.collect_due([])
    assert calls == []
