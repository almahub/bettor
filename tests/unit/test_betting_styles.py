import asyncio
import threading
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest

from better.api.app import create_app
from better.services.automatic_multiples import AutomaticMultiples
from better.services.betting_styles import BettingStyles, StylePreparation, StyleSelection
from better.services.salabet_service import SalaBetService
from tests.unit.test_automatic_multiples import setup as setup


@pytest.mark.parametrize('style,expected', [('MULTIPLE', 1), ('SINGLES', 3)])
def test_selected_styles_prepare_only_selected_games_without_bank_or_confirmation(setup, style, expected):
    owner, repo, clock, seed = setup
    candidates = [seed(i)[1] for i in range(6)]
    selected = [candidates[i].id for i in (1, 3, 5)]
    service = BettingStyles(owner.runtime, clock=lambda: clock[0])
    payload = StyleSelection(style=style, candidate_ids=selected, budget_eur='50')
    before = repo.all_records('annotations', entity_id='bank:EUR:v1')
    preview = service.preview(payload)
    assert preview['ready'] and preview['ticket_count'] == expected
    assert {r['match_id'] for t in preview['tickets'] for r in t['selections']} == {'match-1', 'match-3', 'match-5'}
    request = StylePreparation(**payload.model_dump(), request_id=uuid4(), preview_id=preview['preview_id'])
    service.request(request)
    result = service.prepare(request)
    assert result['status'] == 'COMPLETED'
    slips = SalaBetService(repo).list_slips()
    assert len(slips) == expected and all(s.status == 'DRAFT' for s in slips)
    assert sum(s.stake for s in slips) == Decimal('50')
    assert repo.all_records('annotations', entity_id='bank:EUR:v1') == before
    assert not any(r['kind'] in {'BETSLIP_PLACED', 'PLACED'} for r in repo.all_records('bet_events'))
    restarted = BettingStyles(owner.runtime, clock=lambda: clock[0])
    assert restarted.request(request)['status'] == 'COMPLETED'
    assert restarted.prepare(request)['slips'] == result['slips']
    another = request.model_copy(update={'request_id': uuid4()})
    restarted.request(another)
    repeated = restarted.prepare(another)
    assert repeated.get('slips') == result['slips'], repo.all_records('annotations', entity_id='salabet-style-job:' + str(another.request_id))
    assert len(SalaBetService(repo).list_slips()) == expected


def test_style_preview_changes_or_started_match_cannot_create_drafts(setup):
    owner, repo, clock, seed = setup
    selected = [seed(i)[1].id for i in range(3)]
    service = BettingStyles(owner.runtime, clock=lambda: clock[0])
    payload = StyleSelection(style='MULTIPLE', candidate_ids=selected, budget_eur='50')
    preview = service.preview(payload)
    request = StylePreparation(**payload.model_dump(), request_id=uuid4(), preview_id=preview['preview_id'])
    with pytest.raises(ValueError, match='STYLE_PREVIEW_CHANGED'):
        service.request(request.model_copy(update={'budget_eur': Decimal('51')}))
    service.request(request)
    from datetime import timedelta
    clock[0] += timedelta(days=1)
    with pytest.raises((ValueError, KeyError)):
        service.prepare(request)
    assert not repo.all_records('bet_slips')
    assert not repo.all_records('annotations', entity_id='bank:EUR:v1')


def test_duplicate_selection_and_missing_system_alternatives_are_blocked(setup, monkeypatch):
    owner, repo, clock, seed = setup
    selected = [seed(i)[1].id for i in range(6)]
    service = BettingStyles(owner.runtime, clock=lambda: clock[0])
    with pytest.raises(ValueError, match='STYLE_DUPLICATE_SELECTION'):
        service.preview(StyleSelection(style='MULTIPLE', candidate_ids=[selected[0], selected[0]], budget_eur='50'))
    original = AutomaticMultiples._options
    def one_market(self, *args, **kwargs):
        top, alternatives, rejected, considered, eligible = original(self, *args, **kwargs)
        markets = {row['match_id']: row['market'] for row in top}
        return top, [row for row in alternatives if row['market'] == markets[row['match_id']]], rejected, considered, eligible
    monkeypatch.setattr(AutomaticMultiples, '_options', one_market)
    result = service.preview(StyleSelection(style='SYSTEM', candidate_ids=selected, budget_eur='52'))
    assert not result['ready'] and result['reason_code'] == 'SYSTEM_DIVERSIFICATION_INSUFFICIENT'
    assert not repo.all_records('bet_slips')


def test_selected_manual_style_is_not_replaced_by_automatic_window_ranking(setup):
    owner, _, clock, seed = setup
    selected = [seed(i, hours=72 + i)[1].id for i in range(3)]
    preview = BettingStyles(owner.runtime, clock=lambda: clock[0]).preview(
        StyleSelection(style='MULTIPLE', candidate_ids=selected, budget_eur='50'))
    assert preview['ready'] and preview['candidate_ids'] == sorted(str(i) for i in selected)


def test_failed_style_transaction_leaves_no_partial_drafts_or_bank_movement(setup, monkeypatch):
    owner, repo, clock, seed = setup
    selected = [seed(i)[1].id for i in range(3)]
    service = BettingStyles(owner.runtime, clock=lambda: clock[0])
    payload = StyleSelection(style='SINGLES', candidate_ids=selected, budget_eur='50')
    preview = service.preview(payload)
    request = StylePreparation(**payload.model_dump(), request_id=uuid4(), preview_id=preview['preview_id'])
    service.request(request)
    original = SalaBetService.update_slip
    calls = []

    def fail_second(self, *args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise ValueError('Controlled preparation failure')
        return original(self, *args, **kwargs)

    monkeypatch.setattr(SalaBetService, 'update_slip', fail_second)
    with pytest.raises(ValueError, match='Controlled'):
        service.prepare(request)
    assert not repo.all_records('bet_slips')
    assert not repo.all_records('bet_events') or all('state' not in r for r in repo.all_records('bet_events'))
    assert not repo.all_records('annotations', entity_id='bank:EUR:v1')


def test_system_drafts_rotate_real_stored_market_probabilities_and_keep_bank_unchanged(setup, monkeypatch):
    from better.services.batch_analysis import BatchAnalysisService
    owner, repo, clock, seed = setup
    seeded = [seed(i) for i in range(6)]
    primary, alternatives = [], []
    for run, candidate in seeded:
        row, _ = BatchAnalysisService(owner.runtime)._best_candidate(run)
        primary.append({**row, 'candidate_id': str(candidate.id), 'bookmaker': candidate.bookmaker_at_analysis})
        predictions = run.results['predictive_model'].payload.model_predictions
        for market in ('1X2', 'OVER_UNDER_2_5', 'BTTS'):
            prediction = next(p for p in predictions if market in p.probabilities)
            probabilities = prediction.probabilities[market]
            selection = max(probabilities, key=probabilities.get)
            alternatives.append({**row, 'market': market, 'selection': selection, 'probability': probabilities[selection],
                'prediction_id': str(prediction.prediction_id), 'bookmaker': candidate.bookmaker_at_analysis,
                'edge': .1, 'expected_value': .2, 'odds': 2.5})
    monkeypatch.setattr(AutomaticMultiples, '_options', lambda *a, **kw: (primary, alternatives, [], 6, 6))
    service = BettingStyles(owner.runtime, clock=lambda: clock[0])
    payload = StyleSelection(style='SYSTEM', candidate_ids=[c.id for _, c in seeded], budget_eur='52')
    preview = service.preview(payload)
    assert preview['ready'] and preview['ticket_count'] == 42
    request = StylePreparation(**payload.model_dump(), request_id=uuid4(), preview_id=preview['preview_id'])
    service.request(request)
    result = service.prepare(request)
    assert len(result['slips']) == 42
    slips = SalaBetService(repo).list_slips()
    assert len(slips) == 42 and all(s.status == 'DRAFT' for s in slips)
    assert sum(s.stake for s in slips) == Decimal('52')
    assert not repo.all_records('annotations', entity_id='bank:EUR:v1')


async def test_api_preparation_returns_before_slow_work_and_repeated_post_has_one_job(setup, monkeypatch):
    owner, repo, _, _ = setup
    owner.runtime._locks = {}
    release = threading.Event()
    calls = []
    monkeypatch.setattr(BettingStyles, 'preview', lambda *args: {'ready': True, 'preview_id': 'a' * 64})

    def slow(self, request):
        calls.append(request.request_id)
        release.wait(3)
        self._job(request, 'COMPLETED', slips=[], bank_unchanged=True)

    monkeypatch.setattr(BettingStyles, 'prepare', slow)
    payload = {'style': 'MULTIPLE', 'candidate_ids': [str(uuid4()), str(uuid4())], 'budget_eur': '50',
               'request_id': str(uuid4()), 'preview_id': 'a' * 64}
    app = create_app(owner.runtime)
    from types import SimpleNamespace
    collection_lock = asyncio.Lock()
    app.state.data_pipeline_collector = SimpleNamespace(_lock=collection_lock)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        first = await client.post('/salabet/styles/prepare', json=payload)
        assert first.status_code == 202 and not release.is_set()
        retry = await client.post('/salabet/styles/prepare', json=payload)
        assert retry.status_code == 202
        assert (await client.get('/salabet/styles/preparations/' + payload['request_id'])).json()['running']
        assert any(lock.locked() for lock in owner.runtime._locks.values())
        assert collection_lock.locked()
        release.set()
        for _ in range(50):
            result = (await client.get('/salabet/styles/preparations/' + payload['request_id'])).json()
            if result['status'] == 'COMPLETED':
                break
            await asyncio.sleep(.02)
        assert result['status'] == 'COMPLETED' and len(calls) == 1
    assert not repo.all_records('bet_slips')
