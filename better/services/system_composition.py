"""Diversified system composition from already admitted pre-match alternatives.

No research, model inference, bank writes or slip confirmation happens here.
"""
from collections import Counter
from decimal import ROUND_FLOOR, Decimal
from itertools import combinations
from math import isfinite

MARKETS = {'1X2', 'OVER_UNDER_2_5', 'BTTS'}


def system_preview(choices, match_ids, *, budget_eur=Decimal('50')):
    ids = sorted(set(match_ids))
    if len(ids) != 6:
        return {'ready': False, 'reason_code': 'SYSTEM_SIX_MATCHES_REQUIRED',
                'message': 'Seleziona una sola proposta per ciascuna di sei partite diverse. Bettor recupera automaticamente gli altri mercati dalla stessa analisi.',
                'selection_mode': 'AUTO_FROM_ANALYSIS', 'tickets': []}
    by_match = {identifier: {} for identifier in ids}
    for row in sorted(choices, key=lambda r: (-r['ranking_score'], r['match_id'], r['market'], r['selection'])):
        if row['match_id'] not in by_match or row['market'] not in MARKETS:
            continue
        numbers = [row.get('probability'), row.get('odds'), row.get('expected_value'), row.get('edge')]
        if (any(value is None or not isfinite(float(value)) for value in numbers)
                or not 0 < row['probability'] < 1 or row['odds'] <= 1
                or row['expected_value'] <= 0 or row['edge'] <= 0):
            continue
        by_match[row['match_id']].setdefault(row['market'], row)
    missing = [{'match_id': identifier, 'match': next((r.get('match', identifier) for r in markets.values()), identifier),
                'available_markets': sorted(markets)}
               for identifier, markets in by_match.items() if len(markets) < 2]
    if missing:
        return {'ready': False, 'reason_code': 'SYSTEM_DIVERSIFICATION_INSUFFICIENT',
                'message': 'Non devi cercare o selezionare altri pronostici. Bettor li ha cercati automaticamente, ma per le partite indicate non risultano almeno due mercati utilizzabili con quota reale e valore positivo. Aggiorna l’analisi oppure sostituisci soltanto quelle partite.',
                'selection_mode': 'AUTO_FROM_ANALYSIS',
                'missing_alternatives': missing, 'tickets': []}
    bookmakers = {row.get('bookmaker') for markets in by_match.values() for row in markets.values()}
    if None in bookmakers or len(bookmakers) != 1:
        return {'ready': False, 'reason_code': 'SYSTEM_BOOKMAKER_CONFLICT',
                'message': 'Le alternative devono avere quote reali dello stesso bookmaker.', 'tickets': []}
    budget = Decimal(str(budget_eur))
    if not budget.is_finite() or budget < Decimal('.42') or budget.as_tuple().exponent < -2:
        raise ValueError('INVALID_SYSTEM_BUDGET: importo positivo con al massimo due decimali')
    usage = {identifier: Counter() for identifier in ids}
    exposure = {identifier: Counter() for identifier in ids}
    tickets = []
    # Fixed, non-exhaustive market rotation within the 42 requested match subsets.
    for size, weight in ((3, Decimal('1.50')), (4, Decimal('1')), (5, Decimal('1')), (6, Decimal('1'))):
        for subset in combinations(ids, size):
            selected = []
            for identifier in subset:
                row = min(by_match[identifier].values(), key=lambda r: (
                    usage[identifier][r['market']], exposure[identifier][r['market']],
                    -r['ranking_score'], r['market'], r['selection']))
                selected.append(row)
                usage[identifier][row['market']] += 1
                exposure[identifier][row['market']] += weight
            tickets.append({'selections': selected, 'weight': weight})
    cents = int(budget * 100)
    ideal = [Decimal(cents) * ticket['weight'] / Decimal('52') for ticket in tickets]
    amounts = [int(value.to_integral_value(rounding=ROUND_FLOOR)) for value in ideal]
    order = sorted(range(len(tickets)), key=lambda i: (-(ideal[i] - amounts[i]), i))
    for i in order[:cents - sum(amounts)]:
        amounts[i] += 1
    if min(amounts) < 1:
        raise ValueError('INVALID_SYSTEM_BUDGET: importo insufficiente per tutte le combinazioni')
    for ticket, amount in zip(tickets, amounts, strict=True):
        ticket['amount_eur'] = str(Decimal(amount) / 100)
        del ticket['weight']
    distribution = []
    for identifier in ids:
        markets = by_match[identifier]
        distribution.append({'match_id': identifier, 'match': next(iter(markets.values())).get('match', identifier),
            'appearances': sum(usage[identifier].values()),
            'markets': [{'market': market, 'selection': row['selection'],
                         'appearances': usage[identifier][market],
                         'stake_exposure_eur': str(sum(Decimal(t['amount_eur']) for t in tickets
                             if any(r['match_id'] == identifier and r['market'] == market for r in t['selections'])))}
                        for market, row in sorted(markets.items())]})
    return {'ready': True, 'reason_code': None, 'tickets': tickets, 'distribution': distribution,
            'selection_mode': 'AUTO_FROM_ANALYSIS',
            'total_eur': str(budget), 'ticket_count': len(tickets),
            'note': 'Alternative recuperate automaticamente dalle sei analisi selezionate. Mercati distribuiti tra le combinazioni; ogni partita compare una sola volta per schedina. '
                    'Le ripetizioni tra schedine e la correlazione residua restano visibili.'}
