from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from better.config import Settings
from better.domain.enums import DecisionStatus
from better.domain.salabet import BetCandidateCreate, BetSlipCreate
from better.domain.schemas import AnalysisRequest
from better.providers.fake import make_demo_bundle
from better.services.application import make_application
from better.services.salabet_service import SalaBetService
from better.services.slip_checkpoint_review import SlipCheckpointReview
from better.services.timing_comparison import TimingComparisonService
from better.storage.repository import Repository

NOW = datetime(2026, 11, 3, 12, tzinfo=UTC)


@pytest.fixture
async def review_case():
    repo = Repository('sqlite:///:memory:')
    settings = Settings(timing_comparison_competitions=('SYNTHETIC_LEAGUE',))
    app = make_application(settings=settings, repository=repo, providers=make_demo_bundle('bet', as_of=NOW))
    original = await app.analyze(AnalysisRequest(match_id='demo-match', as_of=NOW))
    desk = SalaBetService(repo, clock=lambda: NOW, settings=settings)
    prediction = original.results['predictive_model'].payload.model_predictions[0]
    market = original.decision.market
    candidate = desk.create_candidate(BetCandidateCreate(analysis_id=original.analysis_id, prediction_id=prediction.prediction_id, market=market.type, selection=market.selection))
    slip = desk.create_slip(BetSlipCreate(request_id=uuid4(), candidate_ids=(candidate.id,)))
    clock = [NOW]

    def capture(transform=lambda x: x):
        at = original.snapshot.match.kickoff - timedelta(minutes=29)
        odds = original.snapshot.odds.model_copy(update={'quotes': tuple(q.model_copy(update={'captured_at': at}) for q in original.snapshot.odds.quotes)})
        current = original.model_copy(update={'analysis_id': uuid4(), 'as_of': at, 'created_at': at, 'snapshot': original.snapshot.model_copy(update={'as_of': at, 'odds': odds})})
        current = transform(current)
        with repo.engine.begin() as conn:
            repo._append(conn, 'analysis_runs', identifier=str(current.analysis_id), entity_id=str(current.analysis_id), match_id=current.match_id, payload=current.model_dump(mode='json'))
        clock[0] = at + timedelta(seconds=1)
        TimingComparisonService(repository=repo, settings=settings, clock=lambda: clock[0]).store(current.snapshot.match, 'T30', current)
        return current

    yield repo, settings, original, slip, clock, capture
    await app.close()
    repo.close()


def read(case):
    repo, settings, _, slip, clock, _ = case
    return SlipCheckpointReview(repo, settings, lambda: clock[0]).review(slip)


async def test_pending_missing_and_confirmation_are_read_only(review_case):
    repo, settings, original, slip, clock, capture = review_case
    assert read(review_case)['status'] == 'PENDING'
    clock[0] = original.snapshot.match.kickoff - timedelta(minutes=10)
    assert read(review_case)['selections'][0]['status'] == 'MISSING'
    capture()
    before = {t: repo.all_records(t) for t in ('bet_slips', 'bet_events', 'annotations', 'validation_observations')}
    first = read(review_case)
    assert first['status'] == 'CONFIRMED'
    assert first['selections'][0]['status'] == 'CONFIRMED'
    assert read(review_case) == first
    view = SalaBetService(repo, clock=lambda: clock[0], settings=settings).slip_view(slip)
    assert view['checkpoint_review'] is None  # Historical reader retained, normal UI has no T30.
    for table, rows in before.items():
        assert repo.all_records(table) == rows
    assert SalaBetService(repo).get_slip(slip.id) == slip


async def test_quote_change_does_not_change_accepted_price_or_stake(review_case):
    repo, _, _, slip, _, capture = review_case
    selection = slip.selections[0]
    def change(run):
        quotes = tuple(q.model_copy(update={'odds': q.odds + 1}) if q.market.type == selection.market and q.market.selection == selection.selection else q for q in run.snapshot.odds.quotes)
        return run.model_copy(update={'snapshot': run.snapshot.model_copy(update={'odds': run.snapshot.odds.model_copy(update={'quotes': quotes})})})
    capture(change)
    row = read(review_case)['selections'][0]
    assert row['status'] == 'REVIEW_REQUIRED'
    assert row['updated_odds'] != row['initial_odds']
    assert 'QUOTE_CHANGED' in {n['code'] for n in row['notices']}
    assert SalaBetService(repo).get_slip(slip.id) == slip


async def test_no_bet_is_not_presented_as_confirmed(review_case):
    _, _, _, _, _, capture = review_case
    def reject(run):
        decision = run.decision.model_copy(update={'status': DecisionStatus.NO_BET, 'stake': run.decision.stake.model_copy(update={'amount': Decimal('0'), 'bankroll_fraction': 0})})
        return run.model_copy(update={'decision': decision})
    capture(reject)
    row = read(review_case)['selections'][0]
    assert row['status'] in {'NOT_ADMITTED', 'CHANGED'}
    assert read(review_case)['status'] == 'REVIEW_REQUIRED'


async def test_lineup_changes_and_missing_official_confirmation_are_distinct(review_case):
    _, _, _, _, _, capture = review_case
    def change(run):
        a = run.snapshot.lineups
        b = a.model_copy(update={'home_players': ('new-player', *a.home_players[1:]), 'sensitivity': 1.0})
        impact = run.results['lineup_player_impact']
        results = {**run.results, 'lineup_player_impact': impact.model_copy(update={'payload': impact.payload.model_copy(update={'lineup': b, 'sensitivity': 1.0})})}
        return run.model_copy(update={'results': results, 'snapshot': run.snapshot.model_copy(update={'lineups': b})})
    capture(change)
    assert 'LINEUP_MATERIAL_CHANGE' in {n['code'] for n in read(review_case)['selections'][0]['notices']}

async def test_future_checkpoint_and_missing_same_bookmaker_quote_are_not_green(review_case):
    repo, settings, original, slip, clock, capture = review_case
    capture()
    reader = SlipCheckpointReview(repo, settings, lambda: clock[0])
    reader.rows = [{**r, 'at': original.snapshot.match.kickoff.isoformat()} for r in reader.rows]
    assert reader.review(slip)['selections'][0]['status'] == 'INVALID_CHECKPOINT'
    reader = SlipCheckpointReview(repo, settings, lambda: clock[0])
    changed_bookmaker = slip.model_copy(update={'bookmaker': 'another-bookmaker'})
    row = reader.review(changed_bookmaker)['selections'][0]
    assert row['updated_odds'] is None
    assert 'QUOTE_UNAVAILABLE' in {n['code'] for n in row['notices']}
    assert row['status'] != 'CONFIRMED'


async def test_indicative_model_never_becomes_an_operational_confirmation(review_case):
    from better.domain.enums import PredictionStatus
    _, _, _, _, _, capture = review_case
    def indicative(run):
        report = run.results['predictive_model']
        models = tuple(p.model_copy(update={'operationally_eligible': False, 'prediction_status': PredictionStatus.INDICATIVE}) for p in report.payload.model_predictions)
        report = report.model_copy(update={'payload': report.payload.model_copy(update={'model_predictions': models, 'ensemble': {}, 'operationally_eligible': False})})
        decision = run.decision.model_copy(update={'status': DecisionStatus.NO_BET, 'stake': run.decision.stake.model_copy(update={'amount': Decimal('0'), 'bankroll_fraction': 0})})
        return run.model_copy(update={'results': {**run.results, 'predictive_model': report}, 'decision': decision})
    capture(indicative)
    row = read(review_case)['selections'][0]
    assert row['status'] in {'INDICATIVE', 'CHANGED'}
    assert read(review_case)['status'] == 'REVIEW_REQUIRED'
