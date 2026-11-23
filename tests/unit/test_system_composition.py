from collections import Counter
from copy import deepcopy
from decimal import Decimal

import pytest

from better.services.system_composition import system_preview


def choices(markets=('1X2', 'OVER_UNDER_2_5', 'BTTS')):
    return [{'match_id': f'match-{i}', 'match': f'Home {i} - Away {i}', 'market': market,
             'selection': {'1X2': 'HOME', 'OVER_UNDER_2_5': 'OVER_2_5', 'BTTS': 'YES'}[market],
             'ranking_score': 90 - index, 'probability': .6, 'odds': 2, 'bookmaker': '888',
             'expected_value': .2, 'edge': .1}
            for i in range(6) for index, market in enumerate(markets)]


@pytest.mark.parametrize('markets,counts', [(('1X2', 'OVER_UNDER_2_5', 'BTTS'), [8, 9, 9]),
                                          (('1X2', 'BTTS'), [13, 13])])
def test_each_repeated_match_rotates_markets_and_keeps_42_unique_tickets(markets, counts):
    rows = choices(markets)
    original = deepcopy(rows)
    result = system_preview(rows, [f'match-{i}' for i in range(6)], budget_eur=Decimal('52'))
    assert result['ready']
    assert result['selection_mode'] == 'AUTO_FROM_ANALYSIS'
    assert Counter(len(t['selections']) for t in result['tickets']) == {3: 20, 4: 15, 5: 6, 6: 1}
    assert sum(Decimal(t['amount_eur']) for t in result['tickets']) == Decimal('52')
    signatures = set()
    for ticket in result['tickets']:
        assert len({r['match_id'] for r in ticket['selections']}) == len(ticket['selections'])
        assert Decimal(ticket['amount_eur']) == (Decimal('1.50') if len(ticket['selections']) == 3 else Decimal('1'))
        signatures.add(tuple((r['match_id'], r['market'], r['selection']) for r in ticket['selections']))
    assert len(signatures) == 42
    for row in result['distribution']:
        assert row['appearances'] == 26
        assert sorted(m['appearances'] for m in row['markets']) == counts
    assert rows == original
    assert system_preview(list(reversed(rows)), [f'match-{i}' for i in reversed(range(6))], budget_eur=Decimal('52')) == result


def test_missing_or_unusable_alternatives_block_instead_of_fixed_selection_fallback():
    rows = [r for r in choices() if r['match_id'] != 'match-0' or r['market'] == 'BTTS']
    result = system_preview(rows, [f'match-{i}' for i in range(6)])
    assert not result['ready'] and result['tickets'] == []
    assert result['reason_code'] == 'SYSTEM_DIVERSIFICATION_INSUFFICIENT'
    assert result['selection_mode'] == 'AUTO_FROM_ANALYSIS'
    assert 'Non devi cercare' in result['message']
    assert result['missing_alternatives'][0]['match_id'] == 'match-0'
    for r in choices():
        if r['match_id'] == 'match-0':
            r['expected_value'] = -.1
            rows.append(r)
    assert not system_preview(rows, [f'match-{i}' for i in range(6)])['ready']


def test_missing_sixth_match_does_not_generate_or_duplicate_games():
    assert system_preview(choices(), [f'match-{i}' for i in range(5)])['reason_code'] == 'SYSTEM_SIX_MATCHES_REQUIRED'


def test_different_bookmakers_cannot_be_mixed_in_system():
    rows = choices()
    rows[0]['bookmaker'] = 'other'
    result = system_preview(rows, [f'match-{i}' for i in range(6)])
    assert result['reason_code'] == 'SYSTEM_BOOKMAKER_CONFLICT' and not result['tickets']


@pytest.mark.parametrize('budget', ['48', '50', '52'])
def test_budget_rounding_is_exact_and_exposure_matches_real_ticket_amounts(budget):
    result = system_preview(choices(), [f'match-{i}' for i in range(6)], budget_eur=Decimal(budget))
    assert sum(Decimal(t['amount_eur']) for t in result['tickets']) == Decimal(budget)
    for row in result['distribution']:
        expected = sum(Decimal(t['amount_eur']) for t in result['tickets']
                       if any(r['match_id'] == row['match_id'] for r in t['selections']))
        assert sum(Decimal(m['stake_exposure_eur']) for m in row['markets']) == expected


def test_default_budget_is_fifty_not_screenshot_example():
    result = system_preview(choices(), [f'match-{i}' for i in range(6)])
    assert sum(Decimal(t['amount_eur']) for t in result['tickets']) == Decimal('50')
