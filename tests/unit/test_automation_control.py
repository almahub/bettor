from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from better.config import Settings
from better.domain.models import Match
from better.services.automation_control import AutomationControl, AutomationSwitch, ResearchLimits
from better.storage.repository import Repository

NOW = datetime(2026, 11, 3, 12, tzinfo=UTC)


@pytest.fixture
def control(monkeypatch):
    monkeypatch.setenv('OPENAI_COST_USD_EUR_RATE', '0.9')
    repo = Repository('sqlite:///:memory:')
    clock = [NOW]
    service = AutomationControl(repo, Settings(), clock=lambda: clock[0])
    yield service, repo, clock
    repo.close()


def match(identifier='match'):
    return Match(match_id=identifier, competition='SA', season='2026', kickoff=NOW+timedelta(days=1),
        home_team='Home', away_team='Away', home_team_id='h', away_team_id='a')


def enable(service):
    return service.update(AutomationSwitch(request_id=uuid4(), enabled=True))


def limits(service, **changes):
    return service.update(ResearchLimits(request_id=uuid4(), **{
        'per_match_eur':'0.25','per_day_eur':'5','per_week_eur':'25',**changes}))


def test_persisted_controls_idempotency_and_bank_unchanged(control):
    service, repo, clock = control
    before = repo.all_records('annotations', entity_id='bank:EUR:v1')
    request = ResearchLimits(request_id=uuid4(), per_match_eur='0.40', per_day_eur='2', per_week_eur='8')
    service.update(request)
    service.update(request)
    assert len(service._rows('automation:controls:v1')) == 1
    enable(service)
    restarted = AutomationControl(repo, Settings(), clock=lambda: clock[0])
    assert restarted.report()['enabled'] is True
    assert restarted.report()['per_match_eur'] == '0.40'
    assert repo.all_records('annotations', entity_id='bank:EUR:v1') == before
    assert not repo.all_records('bet_slips')
    assert not repo.all_records('validation_observations')
    with pytest.raises(ValueError, match='diverse'):
        service.update(request.model_copy(update={'per_day_eur': Decimal('3')}))


def test_euro_reservation_and_existing_usd_caps_are_both_enforced(control):
    service, _, _ = control
    enable(service)
    guard = service.guard(match(), 'T24')
    ticket = guard.reserve('data', Decimal('0.25'))
    assert ticket['committed_eur'] == '0.225'
    assert service.report()['day_committed_eur'] == '0.225'
    assert guard.reserve('external', '.01') is None  # Existing USD 0.25/match unchanged.
    assert service.guard(match(), 'T30').reserve('external', '.25') is None


def test_zero_limits_and_off_block_paid_work(control):
    service, _, _ = control
    assert service.guard(match(), 'T24').reserve('data', '.1') is None
    enable(service)
    limits(service, per_match_eur='0')
    assert not service.report()['paid_research']
    assert service.guard(match('other'), 'T24').reserve('data', '.1') is None
    service.update(AutomationSwitch(request_id=uuid4(), enabled=False))
    assert not service.report()['enabled']


async def test_valid_cache_never_reserves_or_spends(control):
    service, _, _ = control
    enable(service)
    calls = []
    cached = SimpleNamespace(estimated_cost=Decimal('1'), cached=True)

    async def operation(*, cache_only):
        calls.append(cache_only)
        return cached

    assert await service.guard(match(), 'T24').execute('external', '.25', operation) is cached
    assert calls == [True]
    assert service.report()['day_committed_eur'] == '0'


async def test_paid_operation_releases_only_known_estimated_cost(control):
    service, _, _ = control
    enable(service)
    calls=[]

    async def operation(*, cache_only):
        calls.append(cache_only)
        return None if cache_only else SimpleNamespace(estimated_cost=Decimal('.02'))

    await service.guard(match(), 'T24').execute('external', '.25', operation)
    assert calls == [True,False]
    assert service.report()['day_committed_eur'] == '0.018'
    assert service.report()['uncertain_reservations'] == 0


async def test_failure_and_unknown_cost_keep_reservation_after_restart(control):
    service, repo, clock = control
    enable(service)

    async def failure(*, cache_only):
        if cache_only:
            return None
        raise RuntimeError('network failure')

    with pytest.raises(RuntimeError):
        await service.guard(match(), 'T24').execute('external', '.25', failure)
    restarted=AutomationControl(repo, Settings(), clock=lambda: clock[0])
    assert restarted.report()['uncertain_reservations'] == 1
    assert restarted.report()['day_committed_eur'] == '.225' or Decimal(restarted.report()['day_committed_eur']) == Decimal('.225')
    assert restarted.guard(match(), 'T24').reserve('external', '.25') is None


def test_day_week_limits_and_monday_reset(control):
    service, _, clock = control
    enable(service)
    limits(service, per_day_eur='.10', per_week_eur='.10')
    assert service.guard(match('one'), 'T24').reserve('external', '.1')
    assert service.guard(match('two'), 'T24').reserve('external', '.02') is None
    clock[0] += timedelta(days=1)
    assert service.report()['day_committed_eur'] == '0'
    assert service.guard(match('three'), 'T24').reserve('external', '.02') is None
    clock[0] += timedelta(days=6)
    assert service.report()['week_committed_eur'] == '0'
    assert service.guard(match('four'), 'T24').reserve('external', '.1')


def test_unknown_exchange_rate_prevents_paid_reservation(control,monkeypatch,tmp_path):
    service, _, _ = control
    enable(service)
    monkeypatch.delenv('OPENAI_COST_USD_EUR_RATE')
    monkeypatch.chdir(tmp_path)
    assert service.report()['usd_eur_rate'] is None
    assert service.guard(match(), 'T24').reserve('external', '.1') is None
    assert service.report()['last_block']['reason'] == 'EXCHANGE_RATE_UNAVAILABLE'


def test_existing_cohort_spend_is_counted_in_usd_safety_cap(control):
    service, repo, _ = control
    enable(service)
    with repo.engine.begin() as conn:
        repo._append(conn,'validation_cost_events',identifier='prior',entity_id='match',match_id='match',
            payload={'match_id':'match','at':NOW.isoformat(),'estimated_cost':'.20'})
    assert service.guard(match(),'T24').reserve('external','.1') is None


@pytest.mark.parametrize('amount',['-1','NaN','Infinity','0.001'])
def test_invalid_budget_values_are_rejected(amount):
    with pytest.raises(ValidationError):
        ResearchLimits(request_id=uuid4(),per_match_eur=amount,per_day_eur='5',per_week_eur='25')


def test_off_is_rechecked_before_next_call(control):
    service, _, _ = control
    enable(service)
    guard=service.guard(match(),'T30')
    service.update(AutomationSwitch(request_id=uuid4(),enabled=False))
    assert guard.reserve('external','.1') is None


async def test_cache_populated_between_check_and_dispatch_releases_reservation(control):
    service, _, _ = control
    enable(service)
    guard=service.guard(match(),'T30')

    async def operation(*, cache_only):
        return None if cache_only else SimpleNamespace(cached=True,estimated_cost=Decimal('.10'))

    await guard.execute('external','.25',operation)
    assert service.report()['day_committed_eur']=='0.0'
    assert guard.summary()['calls']==0
    assert guard.summary()['known_cost_usd']==0
