from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from better.config import Settings
from better.domain.salabet import BetCandidateCreate, BetSlipAction
from better.domain.schemas import AnalysisRequest, AnalysisRun
from better.providers.fake import make_demo_bundle
from better.services.application import make_application
from better.services.automatic_multiples import AutomaticMultiples, MultipleSwitch
from better.services.batch_analysis import BatchAnalysisService
from better.services.salabet_service import SalaBetService
from better.storage.repository import Repository

NOW = datetime(2026, 11, 3, 12, tzinfo=UTC)


@pytest.fixture
async def setup():
    repo = Repository('sqlite:///:memory:')
    settings = Settings(min_sample=1000, timing_comparison_competitions=('SYNTHETIC_LEAGUE',))
    app = make_application(settings=settings, repository=repo, providers=make_demo_bundle('bet', as_of=NOW))
    base = await app.analyze(AnalysisRequest(match_id='demo-match', as_of=NOW))
    clock = [NOW + timedelta(seconds=2)]
    runtime = SimpleNamespace(repository=repo, settings=settings)
    service = AutomaticMultiples(runtime, clock=lambda: clock[0])

    def seed(index, hours=6):
        data = base.model_dump_json().replace('demo-match', f'match-{index}').replace(str(base.analysis_id), str(uuid4()))
        data = data.replace(base.snapshot.match.home_team_id, f'home-{index}').replace(base.snapshot.match.away_team_id, f'away-{index}')
        run = AnalysisRun.model_validate_json(data)
        match = run.snapshot.match.model_copy(update={'kickoff': NOW + timedelta(hours=hours)})
        run = run.model_copy(update={'snapshot': run.snapshot.model_copy(update={'match': match})})
        repo.save_match(match)
        with repo.engine.begin() as conn:
            repo._append(conn, 'analysis_runs', identifier=str(run.analysis_id), entity_id=str(run.analysis_id), match_id=run.match_id, payload=run.model_dump(mode='json'))
        choice, _ = BatchAnalysisService(runtime)._best_candidate(run)
        candidate = SalaBetService(repo, clock=lambda: clock[0]).create_batch_candidate(BetCandidateCreate(analysis_id=run.analysis_id, prediction_id=choice['prediction_id'], market=choice['market'], selection=choice['selection']), {'batch_id': uuid4()})
        return run, candidate

    yield service, repo, clock, seed
    await app.close()
    repo.close()


def prepare(service, plan=None, request_id=None):
    return service.update(MultipleSwitch(request_id=request_id or uuid4(), enabled=True, plan_id=(plan or service.proposal())['plan_id']))


async def test_button_selects_six_visible_matches_and_creates_ready_without_placing(setup):
    service, repo, _, seed = setup
    for i in range(8):
        seed(i, hours=3+i)
    plan = service.proposal()
    assert len(plan['selected']) == 6
    assert len({c['match_id'] for c in plan['selected']}) == 6
    assert 'groups' not in plan
    before = {t: repo.all_records(t) for t in ('validation_observations', 'canonical_predictions')}
    bank = repo.all_records('annotations', entity_id='bank:EUR:v1')
    report = prepare(service, plan)
    slips = SalaBetService(repo).list_slips()
    assert slips and all(s.status == 'READY' for s in slips)
    assert sum(s.stake for s in slips) == 50
    assert all(not x.operationally_eligible for s in slips for x in s.selections)
    assert report['allocated_eur'] == '50.00' and report['unallocated_eur'] == '0.00'
    assert all(3 <= len(s.selections) <= 6 and s.bookmaker for s in slips)
    assert all(len({x.match_id for x in s.selections}) == len(s.selections) for s in slips)
    assert all(x.odds_at_placement is not None for s in slips for x in s.selections)
    assert repo.all_records('annotations', entity_id='bank:EUR:v1') == bank
    for table, rows in before.items():
        assert repo.all_records(table) == rows
    assert not any(e.get('kind') == 'BET_PLACED' for e in repo.all_records('bet_events'))


async def test_only_next_24h_visible_candidates_are_used(setup):
    service, repo, clock, seed = setup
    seed(0, hours=25)
    seed(1, hours=-1)
    for i in range(2, 6):
        seed(i, hours=12)
    candidate = next(c for c in SalaBetService(repo, clock=lambda: clock[0]).list_candidates() if c.match_id == 'match-2')
    SalaBetService(repo, clock=lambda: clock[0]).remove_candidate(candidate.id, BetSlipAction(request_id=uuid4()))
    plan = service.proposal()
    assert len(plan['selected']) == 3
    assert all(c['match_id'] not in {'match-0', 'match-1', candidate.match_id} for c in plan['selected'])


async def test_not_enough_matches_does_not_create_or_reserve_money(setup):
    service, repo, _, seed = setup
    seed(0)
    seed(1)
    with pytest.raises(ValueError, match='almeno tre'):
        prepare(service)
    assert not repo.all_records('bet_slips')
    assert service.report()['allocated_eur'] == '0.00'


async def test_polling_never_prepares_and_restart_or_request_retry_never_duplicate(setup):
    service, repo, clock, seed = setup
    for i in range(6):
        seed(i)
    assert service.collect()['created'] == []
    plan = service.proposal()
    request_id = uuid4()
    prepare(service, plan, request_id)
    before = repo.all_records('bet_events')
    prepare(service, plan, request_id)
    prepare(AutomaticMultiples(service.runtime, clock=lambda: clock[0]), plan)
    assert service.collect()['created'] == []
    assert repo.all_records('bet_events') == before


async def test_changed_pool_requires_new_review_and_cannot_reset_turn_budget(setup):
    service, _, _, seed = setup
    for i in range(3):
        seed(i)
    old = service.proposal()
    seed(4)
    with pytest.raises(ValueError, match='cambiata'):
        prepare(service, old)
    prepare(service)
    seed(5)
    with pytest.raises(ValueError, match='gia assegnato'):
        prepare(service)


async def test_rollback_during_ready_keeps_no_partial_slips_or_reservations(setup, monkeypatch):
    service, repo, _, seed = setup
    for i in range(6):
        seed(i)
    original = SalaBetService.confirm_slip
    def fail(*args, **kwargs):
        raise RuntimeError('interruption')
    monkeypatch.setattr(SalaBetService, 'confirm_slip', fail)
    with pytest.raises(RuntimeError):
        prepare(service)
    assert not repo.all_records('bet_slips')
    assert service.report()['allocated_eur'] == '0.00'
    monkeypatch.setattr(SalaBetService, 'confirm_slip', original)
    prepare(service)
    assert service.report()['allocated_eur'] == '50.00'


async def test_concurrent_clicks_share_one_budget(setup, tmp_path):
    service, repo, clock, seed = setup
    for i in range(6):
        seed(i)
    plan = service.proposal()
    target = Repository('sqlite:///' + (tmp_path / 'concurrent.db').as_posix())
    a, b = repo.engine.raw_connection(), target.engine.raw_connection()
    try:
        a.driver_connection.backup(b.driver_connection)
    finally:
        a.close()
        b.close()
    runtime = SimpleNamespace(repository=target, settings=service.settings)
    def click(_):
        return prepare(AutomaticMultiples(runtime, clock=lambda: clock[0]), plan)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(click, range(2)))
        assert sum(s.stake for s in SalaBetService(target).list_slips()) == Decimal('50')
        assert len([r for r in target.all_records('annotations', entity_id='automatic-multiples:v1') if r['kind'] == 'PREPARED']) == 1
    finally:
        target.close()


def test_alternatives_rotate_markets_and_limit_recurrence():
    choices = [{'match_id': str(i), 'market': m, 'selection': 'S', 'ranking_score': 100-i, 'bookmaker': '888'} for i in range(6) for m in ('1X2', 'BTTS', 'OVER_UNDER_2_5')]
    slips = AutomaticMultiples.compose(choices)
    assert len(slips) == 3
    used, counts = set(), {}
    for slip in slips:
        for c in slip:
            key = (c['match_id'], c['market'])
            assert key not in used
            used.add(key)
            counts[c['match_id']] = counts.get(c['match_id'], 0) + 1
    assert max(counts.values()) == 2


def test_six_primaries_without_alternatives_make_two_disjoint_triples():
    choices = [{'match_id': str(i), 'market': '1X2', 'selection': 'HOME', 'ranking_score': 50, 'bookmaker': '888'} for i in range(6)]
    slips = AutomaticMultiples.compose(choices)
    assert len(slips) == 2
    assert all(len(s) == 3 for s in slips)
    assert set(c['match_id'] for c in slips[0]).isdisjoint(c['match_id'] for c in slips[1])

async def test_future_sources_are_rejected_before_ranking(setup, monkeypatch):
    service, repo, _, seed = setup
    run, _ = seed(0)
    for i in range(1, 3):
        seed(i)
    get_run = repo.get_run
    quotes = list(run.snapshot.odds.quotes)
    quotes[0] = quotes[0].model_copy(update={'captured_at': NOW + timedelta(hours=1)})
    invalid = run.model_copy(update={'snapshot': run.snapshot.model_copy(update={'odds': run.snapshot.odds.model_copy(update={'quotes': tuple(quotes)})})})
    monkeypatch.setattr(repo, 'get_run', lambda identifier: invalid if str(identifier) == str(run.analysis_id) else get_run(identifier))
    plan = service.proposal()
    assert len(plan['selected']) == 2
    assert any(r['reason'] == 'LEAKAGE_REJECTED' for r in plan['rejected'])
    with pytest.raises(ValueError):
        prepare(service, plan)
    assert not repo.all_records('bet_slips')


async def test_existing_user_ticket_is_not_changed_or_duplicated(setup):
    from better.domain.salabet import BetSlipCreate
    service, repo, clock, seed = setup
    candidates = [seed(i)[1] for i in range(3)]
    desk = SalaBetService(repo, clock=lambda: clock[0])
    original = desk.create_slip(BetSlipCreate(request_id=uuid4(), candidate_ids=tuple(c.id for c in candidates)))
    try:
        prepare(service)
    except ValueError as error:
        assert 'gia presenti' in str(error)
    assert desk.get_slip(original.id) == original
    signatures = [tuple(sorted((s.match_id, s.market.value, s.selection) for s in slip.selections)) for slip in desk.list_slips()]
    assert len(signatures) == len(set(signatures))
