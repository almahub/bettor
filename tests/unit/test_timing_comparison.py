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
        run = run_at(clock[0])
        repo.save_run(run)
        return run

    runtime = SimpleNamespace(repository=repo, settings=settings, analyze=analyze)
    service = TimingComparisonService(runtime, clock=lambda: clock[0])
    yield service, repo, match, clock, calls, run_at
    await app.close()
    repo.close()


async def test_t24_candidate_persisted_once_and_no_t30_after_restart(experiment):
    service, repo, match, clock, calls, _ = experiment
    before = {t: repo.all_records(t) for t in ('bet_slips','canonical_predictions','validation_observations')}
    bank = repo.all_records('annotations', entity_id='bank:EUR:v1')
    first = await service.collect_due([match.model_dump(mode='json')])
    assert len(first['captured']) == 1
    assert len(first['transferred']) == 1
    candidate = repo.all_records('bet_candidates')[0]
    assert candidate['provenance']['automation_checkpoint'] == 'T24'
    await service.collect_due([match.model_dump(mode='json')])
    clock[0] = match.kickoff - timedelta(minutes=29)
    restarted = TimingComparisonService(service.runtime, clock=lambda: clock[0])
    second = await restarted.collect_due([])
    assert second['captured'] == []
    assert second['transferred'] == []
    assert calls == [{'cache_only_research': True}]
    assert len(repo.all_records('bet_candidates')) == 1
    assert set(restarted.view(match.match_id, match.kickoff.isoformat())['checkpoints']) == {'T24'}
    assert not any(r['checkpoint'] == 'T30' for r in service.rows())
    for table, rows in before.items():
        assert repo.all_records(table) == rows
    assert repo.all_records('annotations', entity_id='bank:EUR:v1') == bank


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


async def test_t24_report_has_no_comparison_or_double_validation(experiment):
    service, repo, match, _, _, _ = experiment
    await service.collect_due([match.model_dump(mode='json')])
    report = service.report()
    assert report['mode'] == 'T24_ONLY'
    assert report['checkpoints'] == ['T24']
    assert report['metrics'] == []
    assert report['transferred'] == 1
    assert not repo.all_records('validation_observations')


async def test_capture_before_crash_is_transferred_without_research_again(experiment):
    service, repo, match, clock, calls, run_at = experiment
    run = run_at(clock[0])
    repo.save_run(run)
    assert service._claim(match, 'T24')
    service.store(match, 'T24', run)
    restarted = TimingComparisonService(service.runtime, clock=lambda: clock[0])
    await restarted.collect_due([])
    await restarted.collect_due([])
    assert calls == []
    assert len(repo.all_records('bet_candidates')) == 1


async def test_unusable_forecast_is_explicit_and_not_transferred(experiment):
    service, repo, match, clock, _, run_at = experiment
    run = run_at(clock[0]).model_copy(update={'forecast': None})
    repo.save_run(run)
    assert service._claim(match, 'T24')
    service.store(match, 'T24', run)
    await service.collect_due([])
    assert not repo.all_records('bet_candidates')
    assert service.report()['not_eligible'][0]['reason'] == 'MODEL_INPUT_INSUFFICIENT'


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
