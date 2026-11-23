"""Selected-candidate styles. Preparation is append-only and never places bets."""
from copy import copy
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Literal
from uuid import UUID, uuid5

from pydantic import Field

from better.domain.common import Contract
from better.domain.salabet import BetCandidateCreate, BetSlipCreate, BetSlipUpdate
from better.services.automatic_multiples import AutomaticMultiples, _BoundEngine
from better.services.salabet_service import SalaBetService
from better.services.system_composition import system_preview
from better.services.timing_comparison import moment
from better.storage.repository import canonical_hash


class StyleSelection(Contract):
    style: Literal['MULTIPLE', 'SYSTEM', 'SINGLES']
    candidate_ids: tuple[UUID, ...] = Field(min_length=1, max_length=6)
    budget_eur: Decimal = Field(gt=0, max_digits=12, decimal_places=2)


class StylePreparation(StyleSelection):
    request_id: UUID
    preview_id: str = Field(min_length=64, max_length=64)


class BettingStyles:
    def __init__(self, runtime, *, clock=None):
        self.runtime = runtime
        self.repo, self.settings = runtime.repository, runtime.settings
        self.clock = clock or (lambda: datetime.now(UTC))

    def preview(self, request):
        desk = SalaBetService(self.repo, settings=self.settings, clock=self.clock)
        identifiers = list(request.candidate_ids)
        if len(set(identifiers)) != len(identifiers):
            raise ValueError('STYLE_DUPLICATE_SELECTION: selezioni duplicate')
        candidates = [desk._candidate(identifier) for identifier in identifiers]
        if len({c.match_id for c in candidates}) != len(candidates):
            raise ValueError('STYLE_DUPLICATE_MATCH: scegli una sola proposta per partita')
        for candidate in candidates:
            review = desk.candidate_review(candidate)
            if review:
                return {'ready': False, 'reason_code': review['reason_code'], 'message': review['message'], 'tickets': []}
        owner = AutomaticMultiples(self.runtime, clock=self.clock)
        selected, alternatives, rejected, _, _ = owner._options(self.repo, candidate_ids=identifiers)
        if {r['candidate_id'] for r in selected} != {str(i) for i in identifiers}:
            return {'ready': False, 'reason_code': 'STYLE_SELECTION_NOT_USABLE',
                    'message': 'Una selezione non e piu utilizzabile: aggiorna le analisi e verifica le quote.',
                    'rejected': rejected, 'tickets': []}
        selected.sort(key=lambda r: r['match_id'])
        if request.style == 'SYSTEM':
            result = system_preview(alternatives, [r['match_id'] for r in selected], budget_eur=request.budget_eur)
            if not result['ready']:
                return result
        else:
            if request.style == 'MULTIPLE' and len(selected) < 2:
                return {'ready': False, 'reason_code': 'STYLE_MULTIPLE_NEEDS_TWO_MATCHES',
                        'message': 'La multipla richiede da due a sei partite.', 'tickets': []}
            groups = [selected] if request.style == 'MULTIPLE' else [[r] for r in selected]
            cents = int(request.budget_eur * 100)
            if cents < len(groups):
                raise ValueError('INVALID_STYLE_BUDGET: budget insufficiente per tutte le schedine')
            tickets = [{'selections': group, 'amount_eur': str(Decimal(cents // len(groups) + (i < cents % len(groups))) / 100)}
                       for i, group in enumerate(groups)]
            result = {'ready': True, 'tickets': tickets, 'ticket_count': len(tickets),
                      'total_eur': str(request.budget_eur), 'reason_code': None,
                      'distribution': [], 'note': 'Quote e probabilita delle analisi selezionate.'}
        for ticket in result['tickets']:
            odds = Decimal('1')
            for row in ticket['selections']:
                odds *= Decimal(str(row['odds']))
            ticket['combined_odds'] = str(odds)
        result.update(style=request.style, candidate_ids=sorted(str(i) for i in identifiers),
                      bookmaker=selected[0]['bookmaker'], bank_unchanged=True,
                      creation_status='DRAFT', confirmation_required=True)
        if any(r['bookmaker'] != result['bookmaker'] for r in selected) and request.style == 'MULTIPLE':
            return {'ready': False, 'reason_code': 'SYSTEM_BOOKMAKER_CONFLICT',
                    'message': 'Le selezioni della multipla devono avere lo stesso bookmaker.', 'tickets': []}
        result['preview_id'] = canonical_hash(result)
        return result

    def status(self, request_id):
        rows = self.repo.all_records('annotations', entity_id='salabet-style-job:' + str(request_id))
        # Windows timestamps can tie: a terminal event must win over REQUESTED.
        return (next((r for r in rows if r['status'] == 'COMPLETED'), None)
                or next((r for r in rows if r['status'] == 'FAILED'), None)
                or (rows[0] if rows else None))

    def request(self, request):
        existing = self.status(request.request_id)
        document = request.model_dump(mode='json')
        if existing:
            if existing['request'] != document:
                raise ValueError('STYLE_REQUEST_CONFLICT: richiesta gia usata con dati diversi')
            return existing
        preview = self.preview(request)
        if not preview['ready']:
            raise ValueError(preview['reason_code'] + ': ' + preview['message'])
        if preview['preview_id'] != request.preview_id:
            raise ValueError('STYLE_PREVIEW_CHANGED: anteprima cambiata, verifica nuovamente')
        self._job(request, 'REQUESTED')
        return self.status(request.request_id)

    def _job(self, request, status, *, repo=None, connection=None, **details):
        repo = repo or self.repo
        payload = {'status': status, 'request_id': str(request.request_id),
                   'request': request.model_dump(mode='json'), 'at': self.clock().isoformat(), **details}
        values = dict(identifier=f'style-job:{request.request_id}:{status}',
                      entity_id='salabet-style-job:' + str(request.request_id), match_id=None, payload=payload)
        if connection is not None:
            repo._append(connection, 'annotations', **values)
        else:
            with repo.engine.begin() as conn:
                repo._append(conn, 'annotations', **values)

    def prepare(self, request):
        """Called in a background thread: all DRAFT slips commit together."""
        existing = self.status(request.request_id)
        if existing and existing['status'] in {'COMPLETED', 'FAILED'}:
            return existing
        owner = AutomaticMultiples(self.runtime, clock=self.clock)
        with owner._transaction() as conn:
            repo = copy(self.repo)
            repo.engine = _BoundEngine(conn)
            worker = BettingStyles(SimpleNamespace(repository=repo, settings=self.settings), clock=self.clock)
            existing = worker.status(request.request_id)
            if existing and existing['status'] in {'COMPLETED', 'FAILED'}:
                return existing
            prepared = repo.all_records('annotations', entity_id='salabet-style-prepared:' + request.preview_id)
            if prepared:
                ids = prepared[0]['slips']
            else:
                preview = worker.preview(request)
                if not preview['ready'] or preview['preview_id'] != request.preview_id:
                    raise ValueError('STYLE_PREVIEW_CHANGED: dati o selezioni cambiati; aggiorna l’anteprima')
                desk = SalaBetService(repo, settings=self.settings, clock=self.clock)
                cached, ids = {}, []
                for index, ticket in enumerate(preview['tickets']):
                    if any(moment(row['kickoff']) <= self.clock() for row in ticket['selections']):
                        raise ValueError('MATCH_ALREADY_STARTED: una partita e gia iniziata')
                    candidate_ids = []
                    for row in ticket['selections']:
                        key = (row['analysis_id'], row['prediction_id'], row['market'], row['selection'])
                        if key not in cached:
                            candidate = desk.create_batch_candidate(BetCandidateCreate(analysis_id=row['analysis_id'],
                                prediction_id=row['prediction_id'], market=row['market'], selection=row['selection']),
                                {'batch_id': request.request_id, 'ranking_score': row['ranking_score'],
                                 'ranking_reasons': row['reasons'], 'batch_status': row['status'],
                                 'data_quality': row['data_quality'], 'sample_size': row['sample_size'],
                                 'score_breakdown': row['score_breakdown'], 'blockers': row['blockers']})
                            if candidate.removed:
                                raise ValueError('STYLE_SELECTION_REMOVED: una candidata e stata rimossa')
                            cached[key] = candidate.id
                        candidate_ids.append(cached[key])
                    identifier = uuid5(request.request_id, str(index))
                    slip = desk.create_slip(BetSlipCreate(request_id=identifier, candidate_ids=tuple(candidate_ids)))
                    desk.update_slip(slip.id, BetSlipUpdate(request_id=uuid5(identifier, 'style-details'),
                        stake=Decimal(ticket['amount_eur']), bookmaker=ticket['selections'][0]['bookmaker'],
                        odds_at_placement={cid: Decimal(str(row['odds'])) for cid, row in zip(candidate_ids, ticket['selections'], strict=True)},
                        note=f'Stile {request.style} - gruppo {str(request.request_id)[:8]} - {index + 1}/{len(preview["tickets"])}. '
                             'Quote di analisi: verificare prima della conferma. INDICATIVE resta tale.'))
                    ids.append(str(identifier))
                    repo._append(conn, 'annotations', identifier='style-slip:' + str(identifier),
                        entity_id='salabet-style-slip:' + str(identifier), match_id=None,
                        payload={'style': request.style, 'group_id': str(request.request_id),
                                 'index': index + 1, 'count': len(preview['tickets']),
                                 'budget_eur': preview['total_eur']})
                if any(desk.candidate_review(desk._candidate(cid)) for cid in cached.values()):
                    raise ValueError('STYLE_PREVIEW_CHANGED: analisi scaduta durante la preparazione')
                if any(moment(row['kickoff']) <= self.clock() for ticket in preview['tickets'] for row in ticket['selections']):
                    raise ValueError('MATCH_ALREADY_STARTED: una partita e iniziata durante la preparazione')
                repo._append(conn, 'annotations', identifier='style-prepared:' + request.preview_id,
                    entity_id='salabet-style-prepared:' + request.preview_id, match_id=None,
                    payload={'slips': ids, 'style': request.style, 'total_eur': preview['total_eur']})
            self._job(request, 'COMPLETED', repo=repo, connection=conn, slips=ids, bank_unchanged=True)
        return self.status(request.request_id)

    def fail(self, request, error):
        message = str(error) if isinstance(error, (ValueError, KeyError)) else 'STYLE_PREPARATION_FAILED: preparazione interrotta, nessuna nuova schedina o addebito'
        self._job(request, 'FAILED', message=message)
