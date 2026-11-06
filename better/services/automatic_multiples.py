"""One-shot preparation from visible candidates with kickoff in the next 24 hours."""

from contextlib import contextmanager
from copy import copy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import NAMESPACE_URL, UUID, uuid5
from zoneinfo import ZoneInfo

from pydantic import StrictBool

from better.domain.common import Contract
from better.domain.salabet import BetCandidateCreate, BetSlipAction, BetSlipCreate, BetSlipUpdate
from better.services.automation_control import AutomationControl
from better.services.batch_analysis import BatchAnalysisService
from better.services.salabet_service import SalaBetService
from better.services.timing_comparison import moment
from better.storage.repository import canonical_hash

ENTITY = "automatic-multiples:v1"
ROME = ZoneInfo("Europe/Rome")
ALLOWED = {"1X2", "OVER_UNDER_2_5", "BTTS"}
MODE = "CANDIDATES_NEXT_24H"


class MultipleSwitch(Contract):
    request_id: UUID
    enabled: StrictBool
    plan_id: str | None = None


class _BoundEngine:
    """Reuse existing SalaBet operations in the outer atomic transaction."""
    def __init__(self, connection):
        self.connection, self.dialect = connection, connection.dialect

    @contextmanager
    def connect(self):
        yield self.connection

    begin = connect


class AutomaticMultiples:
    def __init__(self, runtime, *, clock=None):
        self.runtime, self.repository, self.settings = runtime, runtime.repository, runtime.settings
        self.clock = clock or (lambda: datetime.now(UTC))

    def _rows(self, repo=None):
        return (repo or self.repository).all_records('annotations', entity_id=ENTITY)

    @contextmanager
    def _transaction(self):
        with AutomationControl(self.repository, self.settings, clock=self.clock).transaction() as conn:
            if conn.dialect.name == 'postgresql':
                conn.exec_driver_sql('SELECT pg_advisory_xact_lock(718349126)')
            yield conn

    def _write(self, repo, conn, identifier, payload, entity=ENTITY):
        repo._append(conn, 'annotations', identifier=identifier, entity_id=entity, match_id=None, payload=payload)

    def _scope(self):
        local = self.clock().astimezone(ROME)
        start = local.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=local.weekday())
        return f'{start.date()}/{(start + timedelta(days=7)).date()}'

    def _spent(self, repo):
        rows = self._rows(repo)
        plans = {r['plan']['plan_id']: r['plan'] for r in rows if r['kind'] == 'CONTROL' and r.get('plan')}
        return sum((Decimal(r['allocated_eur']) for r in rows
                    if r['kind'] in {'GROUP', 'PREPARED'}
                    and (r.get('turn') or plans.get(r.get('plan_id'), {}).get('turn')) == self._scope()), Decimal('0'))

    def _options(self, repo):
        desk = SalaBetService(repo, clock=self.clock)
        candidates = desk.list_candidates()
        eligible = [c for c in candidates if self.clock() < c.kickoff <= self.clock() + timedelta(hours=24)
                    and c.competition_id in self.settings.timing_comparison_competitions]
        ranker = BatchAnalysisService(SimpleNamespace(repository=repo, settings=self.settings), clock=self.clock)
        runs, choices, rejected, latest = {}, [], [], {}
        for c in eligible:
            run = runs.setdefault(str(c.analysis_id), repo.get_run(c.analysis_id))
            if run:
                latest[c.match_id] = max(latest.get(c.match_id, run.as_of), run.as_of)
        primary = {}
        alternatives = {}
        for c in eligible:
            run = runs[str(c.analysis_id)]
            if run is None or run.as_of != latest.get(c.match_id):
                continue
            match = run.snapshot.match
            if (run.as_of > self.clock() or run.created_at > self.clock() or run.as_of >= c.kickoff
                    or match.kickoff != c.kickoff or match.match_id != c.match_id
                    or match.competition != c.competition_id
                    or (match.home_team_id, match.away_team_id) != c.team_ids):
                rejected.append({'match_id': c.match_id, 'reason': 'IDENTITY_OR_TIME_INVALID'})
                continue
            snapshots = [run.snapshot.stats, run.snapshot.news, run.snapshot.lineups]
            if any(s.captured_at > run.as_of or any(not p.available_at(run.as_of) for p in s.provenance)
                   for s in snapshots if s is not None) or any(
                       q.captured_at > run.as_of or not q.provenance.available_at(run.as_of)
                       for q in (run.snapshot.odds.quotes if run.snapshot.odds else ())):
                rejected.append({'match_id': c.match_id, 'reason': 'LEAKAGE_REJECTED'})
                continue
            prediction_result = run.results.get('predictive_model')
            predictions = getattr(getattr(prediction_result, 'payload', None), 'model_predictions', ())
            if any(p.data_cutoff > run.as_of for p in predictions):
                rejected.append({'match_id': c.match_id, 'reason': 'LEAKAGE_REJECTED'})
                continue
            ranked, reason = ranker.ranked_candidates(run)
            valid = []
            for row in ranked:
                if row['market'] not in ALLOWED or row['odds'] <= 1:
                    continue
                quotes = [q for q in run.snapshot.odds.quotes if q.market.type.value == row['market']
                          and q.market.selection == row['selection'] and float(q.odds) == row['odds']
                          and q.bookmaker == c.bookmaker_at_analysis and not q.is_opening
                          and 0 <= (run.as_of - q.captured_at).total_seconds() <= self.settings.odds_ttl_seconds] if run.snapshot.odds else []
                if quotes:
                    valid.append({**row, 'bookmaker': c.bookmaker_at_analysis})
            visible = [r for r in valid if r['prediction_id'] == str(c.prediction_id)
                       and r['market'] == c.market.value and r['selection'] == c.selection
                       and c.odds_at_analysis is not None and r['odds'] == float(c.odds_at_analysis)]
            if not visible:
                rejected.append({'match_id': c.match_id, 'reason': reason or 'CANDIDATE_NO_LONGER_USABLE'})
                continue
            chosen = max(visible, key=lambda r: (r['ranking_score'], r['selection']))
            if c.match_id not in primary or chosen['ranking_score'] > primary[c.match_id]['ranking_score']:
                primary[c.match_id] = {**chosen, 'candidate_id': str(c.id)}
                alternatives[c.match_id] = valid
        top = sorted(primary.values(), key=lambda r: (-r['ranking_score'], r['match_id']))[:6]
        # A ready slip has one bookmaker; alternatives never substitute another bookmaker's quote.
        for row in top:
            choices.extend(r for r in alternatives[row['match_id']] if r['bookmaker'] == row['bookmaker'])
        return top, choices, rejected, len(candidates), len(eligible)

    def proposal(self):
        top, choices, rejected, considered, eligible = self._options(self.repository)
        remaining = max(Decimal('0'), Decimal('50') - self._spent(self.repository))
        plan = {'turn': self._scope(), 'budget_eur': f'{remaining:.2f}', 'max_matches': 6, 'max_slips': 3,
                'mode': MODE, 'selected': top, 'alternatives': choices, 'rejected': rejected,
                'considered': considered, 'eligible_24h': eligible,
                'note': 'Sceglie fino a sei partite distinte da Da valutare, con kickoff entro 24 ore. Ranking esistente, non garanzia di vincita. Crea multiple READY di test; conferma giocata manuale. INDICATIVE resta tale. Nessun gruppo o attesa di checkpoint.'}
        plan['plan_id'] = canonical_hash(plan)
        return plan

    @staticmethod
    def compose(choices, maximum=3):
        by_match = {}
        for c in sorted(choices, key=lambda r: (-r['ranking_score'], r['match_id'], r['market'], r['selection'])):
            by_match.setdefault(c['match_id'], {}).setdefault(c['market'], c)
        top = sorted(by_match, key=lambda m: (-max(c['ranking_score'] for c in by_match[m].values()), m))[:6]
        target = min(maximum, 3 if len(top) >= 5 and all(len(by_match[m]) >= 2 for m in top)
                     else 2 if len(top) == 6 else 1)
        result, used, counts = [], set(), dict.fromkeys(top, 0)
        cap = min(2, target-1) if target > 1 else 1
        for _ in range(target):
            pool = []
            for match in top:
                valid = [c for market, c in by_match[match].items() if (match, market) not in used and counts[match] < cap]
                if valid:
                    pool.append(max(valid, key=lambda c: (c['ranking_score'], c['market'])))
            pool.sort(key=lambda c: (counts[c['match_id']], -c['ranking_score'], c['match_id']))
            size = min(4 if target == 3 and len(top) == 6 else 3 if target > 1 else 6, len(pool))
            if size < 3:
                break
            selected = pool[:size]
            if len({c['bookmaker'] for c in selected}) != 1:
                break
            result.append(selected)
            for c in selected:
                used.add((c['match_id'], c['market']))
                counts[c['match_id']] += 1
        if len(result) > 1 and set.intersection(*(set(c['match_id'] for c in slip) for slip in result)):
            result = result[:1]
        return result

    def report(self):
        plan = self.proposal()
        rows = [r for r in self._rows() if r['kind'] == 'PREPARED' and r['turn'] == self._scope()]
        spent = self._spent(self.repository)
        return {'enabled': False, 'proposal': plan, 'approved_plan': None,
                'allocated_eur': f'{spent:.2f}', 'unallocated_eur': f'{max(Decimal("0"), Decimal("50")-spent):.2f}',
                'preparations': rows, 'groups': [], 'errors': [r for r in self._rows() if r['kind'] == 'ERROR'][:5],
                'note': 'Preparazione immediata su approvazione, non automazione periodica. Tetto 50 EUR per turno settimanale Europe/Rome; nessun movimento Banca.'}

    def update(self, request):
        if not request.enabled:
            return self.report()
        with self._transaction() as conn:
            repo = copy(self.repository)
            repo.engine = _BoundEngine(conn)
            worker = AutomaticMultiples(SimpleNamespace(repository=repo, settings=self.settings), clock=self.clock)
            prior = next((r for r in self._rows(repo) if r['kind'] == 'PREPARED'
                          and (r['plan_id'] == request.plan_id or r.get('request_id') == str(request.request_id))), None)
            if prior:
                if prior['plan_id'] != request.plan_id:
                    raise ValueError('Richiesta gia utilizzata per una proposta diversa')
            else:
                plan = worker.proposal()
                if plan['plan_id'] != request.plan_id:
                    raise ValueError('Proposta cambiata: aggiorna Da valutare e verifica le sei selezioni prima di approvare')
                if len(plan['selected']) < 3:
                    raise ValueError('Servono almeno tre partite utilizzabili in Da valutare con kickoff nelle prossime 24 ore')
                if Decimal(plan['budget_eur']) <= 0:
                    raise ValueError('Budget di preparazione del turno gia assegnato: nessuna nuova schedina creata')
                desk = SalaBetService(repo, clock=self.clock)
                combinations = worker.compose(plan['alternatives'])
                existing = {tuple(sorted((s.match_id, s.market.value, s.selection) for s in slip.selections)) for slip in desk.list_slips()}
                combinations = [rows for rows in combinations if tuple(sorted((c['match_id'], c['market'], c['selection']) for c in rows)) not in existing]
                if not combinations:
                    raise ValueError('Nessuna nuova multipla valida: selezioni incompatibili o schedine gia presenti')
                cents = int(Decimal(plan['budget_eur'])*100)
                ids = []
                for index, rows in enumerate(combinations):
                    identifier = uuid5(NAMESPACE_URL, f"candidate-preparation:{plan['plan_id']}:{index}")
                    candidate_ids = []
                    for position, row in enumerate(rows, 1):
                        if not self.clock() < moment(row['kickoff']) <= self.clock() + timedelta(hours=24):
                            raise ValueError('MATCH_ALREADY_STARTED: finestra delle prossime 24 ore non piu valida')
                        candidate = desk.create_batch_candidate(BetCandidateCreate(analysis_id=row['analysis_id'], prediction_id=row['prediction_id'], market=row['market'], selection=row['selection']),
                            {'batch_id': identifier, 'ranking_position': position, 'ranking_score': row['ranking_score'],
                             'ranking_reasons': row['reasons'], 'batch_status': row['status'], 'data_quality': row['data_quality'],
                             'sample_size': row['sample_size'], 'score_breakdown': row['score_breakdown'], 'blockers': row['blockers']})
                        if candidate.removed:
                            raise ValueError('Candidata rimossa: aggiorna prima della preparazione')
                        candidate_ids.append(candidate.id)
                    slip = desk.create_slip(BetSlipCreate(request_id=identifier, candidate_ids=tuple(candidate_ids)))
                    amount = Decimal(cents//len(combinations) + (index < cents % len(combinations))) / 100
                    desk.update_slip(slip.id, BetSlipUpdate(request_id=uuid5(identifier, 'details'), stake=amount,
                        bookmaker=rows[0]['bookmaker'], odds_at_placement={c: Decimal(str(r['odds'])) for c,r in zip(candidate_ids, rows, strict=True)}))
                    desk.confirm_slip(slip.id, BetSlipAction(request_id=uuid5(identifier, 'ready')))
                    metadata = {'origin': MODE, 'turn': plan['turn'], 'plan_id': plan['plan_id'], 'amount_eur': str(amount),
                                'status': 'READY', 'selections': rows, 'reason': 'Pronta come schedina di test, non piazzata. INDICATIVE non diventa BET. Quote precompilate da verificare prima della conferma manuale. Mercati diversi della stessa partita restano correlati.'}
                    self._write(repo, conn, 'auto-multiple-slip:'+str(identifier), metadata, entity='auto-multiple-slip:'+str(identifier))
                    ids.append(str(identifier))
                self._write(repo, conn, 'candidate-prepared:'+plan['plan_id'], {'kind': 'PREPARED', 'plan_id': plan['plan_id'],
                    'request_id': str(request.request_id), 'turn': plan['turn'], 'slips': ids, 'allocated_eur': plan['budget_eur'],
                    'selected': plan['selected'], 'at': self.clock().isoformat()})
        return self.report()

    def collect(self):
        # Preparation only happens on the user's explicit button; never on polling/restart.
        return {'enabled': False, 'created': [], 'diagnostics': []}
