"""Manual preparation and opt-in automatic test placement within 24 hours."""

import asyncio
from contextlib import contextmanager
from copy import copy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5
from zoneinfo import ZoneInfo

from pydantic import Field, StrictBool

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


class PlacementBlocked(ValueError):
    """Expected pre-placement condition; never a provider exception."""


class MultipleSwitch(Contract):
    request_id: UUID
    enabled: StrictBool
    plan_id: str | None = None


class MultipleBudget(Contract):
    request_id: UUID
    budget_eur: Decimal = Field(gt=0, max_digits=10, decimal_places=2)


class PreparationSettings(Contract):
    request_id: UUID
    preparation_enabled: StrictBool
    style: Literal['MULTIPLE', 'SYSTEM', 'SINGLES']
    budget_eur: Decimal = Field(gt=0, max_digits=10, decimal_places=2)


class MultipleAutoSwitch(Contract):
    request_id: UUID
    enabled: StrictBool
    preview_id: str | None = None
    approval_mode: Literal['CURRENT_PROPOSAL', 'FUTURE_PROPOSALS'] = 'CURRENT_PROPOSAL'


class _BoundConnection:
    """Nested service commits belong to the enclosing preparation transaction."""
    def __init__(self, connection):
        self.connection = connection

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def exec_driver_sql(self, statement, *args, **kwargs):
        if statement.strip().upper() == 'BEGIN IMMEDIATE':
            return None
        return self.connection.exec_driver_sql(statement, *args, **kwargs)

    def begin(self):
        return None

    def commit(self):
        pass

    def rollback(self):
        pass


class _BoundEngine:
    """Reuse existing SalaBet operations in the outer atomic transaction."""
    def __init__(self, connection):
        self.connection, self.dialect = connection, connection.dialect

    @contextmanager
    def connect(self):
        yield _BoundConnection(self.connection)

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

    def policy(self, repo=None):
        rows = [r for r in self._rows(repo) if r['kind'] == 'POLICY']
        return max(rows, key=lambda r: r['sequence']) if rows else {
            'enabled': False, 'budget_eur': '50.00', 'sequence': 0}

    def update_policy(self, request):
        document = request.model_dump(mode='json')
        requested = dict(document)
        with self._transaction() as conn:
            repo = copy(self.repository)
            repo.engine = _BoundEngine(conn)
            previous = next((r for r in self._rows(repo) if r.get('request_id') == str(request.request_id)), None)
            if previous:
                if previous.get('request') != requested:
                    raise ValueError('Richiesta gia utilizzata con parametri diversi')
            else:
                current = self.policy(repo)
                if isinstance(request, PreparationSettings):
                    pending = self.prepared_group(repo)
                    if pending and (request.style != current.get('style') or request.budget_eur != Decimal(current['budget_eur'])):
                        raise ValueError('PREPARED_GROUP_EXISTS: gestisci prima le schedine già preparate; non vengono modificate automaticamente')
                    # A new composition policy never inherits permission to debit Banca.
                    document = {**document, 'workflow_version': 2, 'enabled': False,
                                'preview_id': None, 'approved_budget_eur': None}
                elif isinstance(request, MultipleBudget) and current.get('workflow_version') == 2:
                    document = {**document, 'enabled': False, 'preview_id': None, 'approved_budget_eur': None}
                if document.get('enabled') and hasattr(self.runtime, 'providers'):
                    if current.get('workflow_version') == 2 and not current.get('preparation_enabled') and not self.prepared_group(repo):
                        raise ValueError('PREPARATION_DISABLED: attiva prima la preparazione o prepara una proposta')
                    preview = self.preview(repo)
                    if document.get('preview_id') != preview['preview_id']:
                        raise ValueError('PREVIEW_CHANGED: rivedi e approva l’anteprima aggiornata')
                    if document.get('approval_mode') == 'CURRENT_PROPOSAL' and not preview['tickets']:
                        raise ValueError('PROPOSAL_NOT_READY: non ci sono multiple confermabili')
                if 'budget_eur' in document and Decimal(document['budget_eur']) < self._spent(repo):
                    raise ValueError('Il budget non puo essere inferiore agli importi gia assegnati nel turno')
                updated = {**current, **{k:v for k,v in document.items() if k != 'request_id'},
                           'kind': 'POLICY', 'sequence': current['sequence']+1,
                           'activation_id': str(request.request_id) if 'enabled' in document else current.get('activation_id','legacy'),
                           'approved_budget_eur': current['budget_eur'] if document.get('enabled') else document.get('approved_budget_eur', current.get('approved_budget_eur')),
                           'request_id': str(request.request_id), 'request': requested,
                           'at': self.clock().isoformat()}
                self._write(repo, conn, 'multiple-policy:'+str(request.request_id), updated)
        return self.report()

    def prepared_group(self, repo=None):
        repo = repo or self.repository
        desk = SalaBetService(repo, clock=self.clock)
        for row in sorted(self._rows(repo), key=lambda r: r.get('at', ''), reverse=True):
            if row['kind'] != 'PREPARED' or not row.get('preview') or row.get('turn') != self._scope():
                continue
            slips = [desk.get_slip(UUID(i)) for i in row['slips']]
            if slips and all(s.status in {'DRAFT', 'READY'} for s in slips):
                return row
        return None

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

    def selection_window(self, repo=None):
        repo = repo or self.repository
        now = self.clock()
        # Anchor before ranking: excluding a weak early match must not postpone
        # the confirmation of all the other matches in the same group.
        matches = {}
        for row in repo.all_records('matches'):
            matches.setdefault(row['match_id'], row)
        kickoffs = [moment(r['kickoff']) for r in matches.values()
                    if r.get('competition') in self.settings.timing_comparison_competitions
                    and now < moment(r['kickoff']) <= now + timedelta(hours=48)]
        if not kickoffs:
            return None, None
        start = min(kickoffs)
        return start, start + timedelta(hours=24)

    def _options(self, repo, *, candidate_ids=None):
        desk = SalaBetService(repo, clock=self.clock)
        candidates = desk.list_candidates()
        selected_ids = {str(identifier) for identifier in candidate_ids} if candidate_ids is not None else None
        if selected_ids is not None:
            candidates = [c for c in candidates if str(c.id) in selected_ids]
        _, window_end = self.selection_window(repo)
        eligible = [c for c in candidates if self.clock() < c.kickoff
                    and (selected_ids is not None or c.kickoff <= (window_end or self.clock() + timedelta(hours=24)))
                    and (selected_ids is not None or c.competition_id in self.settings.timing_comparison_competitions)]
        ranker = BatchAnalysisService(SimpleNamespace(repository=repo, settings=self.settings), clock=self.clock)
        runs, choices, rejected, latest = {}, [], [], {}
        for c in eligible:
            run = runs.setdefault(str(c.analysis_id), repo.get_run(c.analysis_id))
            if run:
                latest[c.match_id] = max(latest.get(c.match_id, run.as_of), run.as_of)
        primary = {}
        alternatives = {}
        for c in eligible:
            review = desk.candidate_review(c)
            if review and review.get('refresh_failed'):
                rejected.append({'match_id': c.match_id, 'reason': review['reason_code']})
                continue
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
        start, end = self.selection_window()
        remaining = max(Decimal('0'), Decimal(self.policy()['budget_eur']) - self._spent(self.repository))
        plan = {'turn': self._scope(), 'budget_eur': f'{remaining:.2f}', 'max_matches': 6, 'max_slips': 3,
                'mode': MODE, 'selected': top, 'alternatives': choices, 'rejected': rejected,
                'window_start': start.isoformat() if start else None,
                'window_end': end.isoformat() if end else None,
                'considered': considered, 'eligible_24h': eligible,
                'note': 'Sceglie fino a sei partite distinte da Da valutare, con kickoff entro 24 ore. Ranking esistente, non garanzia di vincita. Crea multiple READY; conferma manuale o automatica autorizzata. INDICATIVE resta tale. Preparazione entro 48 ore; gruppo di 24 ore dalla prima partita futura; controllo unico del gruppo a T-24.'}
        if self.policy().get('workflow_version') == 2:
            plan['style'] = self.policy()['style']
            plan['max_slips'] = {'MULTIPLE': 1, 'SYSTEM': 42, 'SINGLES': 6}[plan['style']]
            plan['note'] = 'Analisi T48; fino a sei partite nello stesso intervallo di kickoff di 24 ore. Stile esplicito, budget cumulativo settimanale. Preparazione senza addebito; conferma separata.'
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

    def system_preview(self):
        from better.services.system_composition import system_preview
        selected, alternatives, _, _, _ = self._options(self.repository)
        desk = SalaBetService(self.repository, clock=self.clock, settings=self.settings)
        for row in selected:
            review = desk.candidate_review(desk._candidate(UUID(row['candidate_id'])))
            if review:
                return {'ready': False, 'reason_code': review['reason_code'],
                        'message': review['message'], 'tickets': []}
        return system_preview(alternatives, [row['match_id'] for row in selected])

    def preview(self, repo=None):
        repo = repo or self.repository
        worker = AutomaticMultiples(SimpleNamespace(repository=repo, settings=self.settings), clock=self.clock)
        plan = worker.proposal()
        desk = SalaBetService(repo, clock=self.clock)
        if worker.policy().get('workflow_version') == 2:
            pending = worker.prepared_group(repo)
            if pending:
                result = dict(pending['preview'])
            else:
                from better.services.betting_styles import BettingStyles, StyleSelection
                chosen = plan['selected']
                style = worker.policy()['style']
                minimum = {'MULTIPLE': 2, 'SYSTEM': 6, 'SINGLES': 1}[style]
                result = {'ready': False, 'tickets': [], 'style': style,
                          'reason_code': 'AUTOMATION_CANDIDATES_MISSING',
                          'message': f'Servono almeno {minimum} partite utilizzabili; disponibili {len(chosen)}.'}
                if len(chosen) >= minimum and Decimal(plan['budget_eur']) > 0:
                    result = BettingStyles(worker.runtime, clock=self.clock).preview(StyleSelection(
                        style=style, candidate_ids=tuple(UUID(r['candidate_id']) for r in chosen),
                        budget_eur=Decimal(plan['budget_eur'])))
                result = {**result, 'plan_id': plan['plan_id'],
                          'window_start': plan['window_start'], 'window_end': plan['window_end']}
                result['preview_id'] = canonical_hash(result)
            bank = desk.bank.status()
            total = sum((Decimal(t['amount_eur']) for t in result['tickets']), Decimal('0'))
            return {**result, 'total_eur': str(total), 'bank_available_eur': bank['available'],
                    'bank_after_eur': str(Decimal(bank['available']) - total),
                    'operationally_admitted': bool(result['tickets']) and all(
                        r.get('status') == 'BET' for t in result['tickets'] for r in t['selections']),
                    'bank_sufficient': bank['initialized'] and Decimal(bank['available']) >= total}
        existing = {tuple(sorted((s.match_id,s.market.value,s.selection) for s in slip.selections)) for slip in desk.list_slips()}
        combinations = [rows for rows in self.compose(plan['alternatives']) if tuple(sorted((r['match_id'],r['market'],r['selection']) for r in rows)) not in existing]
        cents = int(Decimal(plan['budget_eur'])*100)
        tickets = []
        if cents >= len(combinations) and cents > 0:
            for index, rows in enumerate(combinations):
                amount = Decimal(cents//len(combinations)+(index<cents%len(combinations)))/100
                odds = Decimal('1')
                for row in rows:
                    odds *= Decimal(str(row['odds']))
                tickets.append({'amount_eur': str(amount), 'combined_odds': str(odds), 'selections': rows})
        bank = desk.bank.status()
        total = sum((Decimal(t['amount_eur']) for t in tickets), Decimal('0'))
        identity = {'plan_id': plan['plan_id'], 'tickets': tickets,
                    'window_start': plan['window_start'], 'window_end': plan['window_end']}
        return {**identity, 'preview_id': canonical_hash(identity), 'total_eur': str(total),
                'bank_available_eur': bank['available'],
                'bank_after_eur': str(Decimal(bank['available'])-total),
                'bank_sufficient': bank['initialized'] and Decimal(bank['available'])>=total,
                'note': 'Giocate registrate con addebito Banca; nessun invio al bookmaker.'}

    def execution_status(self, repo=None):
        policy = self.policy(repo)
        rows = [r for r in self._rows(repo) if r['kind']=='EXECUTION_STATUS' and r.get('activation_id')==policy.get('activation_id','legacy')]
        return max(rows,key=lambda r:r['sequence']) if rows else {'consecutive_errors':0,'paused':False,'sequence':0}

    def record_execution(self, message, *, technical=False, success=False):
        with self._transaction() as conn:
            repo = copy(self.repository)
            repo.engine = _BoundEngine(conn)
            current = self.execution_status(repo)
            count = 0 if success or not technical else current['consecutive_errors']+1
            sequence = max((r.get('sequence',0) for r in self._rows(repo) if r['kind']=='EXECUTION_STATUS'),default=0)+1
            payload = {'kind':'EXECUTION_STATUS','sequence':sequence,'activation_id':self.policy(repo).get('activation_id','legacy'),
                       'consecutive_errors':count,'paused':count>=3,'message':message,'at':self.clock().isoformat()}
            self._write(repo,conn,'multiple-execution:'+str(sequence)+':'+str(uuid5(NAMESPACE_URL,self.clock().isoformat())),payload)

    async def collect_verified(self):
        if self.policy().get('workflow_version') == 2 and not self.policy()['enabled']:
            activation = self.policy().get('activation_id')
            if not self.policy().get('preparation_enabled') or self.execution_status().get('paused'):
                return {'enabled': False, 'created': [], 'diagnostics': ['Preparazione automatica disattivata.']}
            preview = self.preview()
            if hasattr(self.runtime, 'analyze') and not self.prepared_group():
                from better.services.candidate_refresh import CandidateRefresh
                try:
                    refresh = await CandidateRefresh(self).run(preview)
                except Exception:
                    self.record_execution('ANALYSIS_REFRESH_FAILED', technical=True)
                    return {'enabled': True, 'created': [], 'diagnostics': ['ANALYSIS_REFRESH_FAILED: nessuna schedina creata.']}
                if any(refresh.values()):
                    if refresh['failed']:
                        self.record_execution('ANALYSIS_REFRESH_FAILED', technical=True)
                    return {'enabled': True, 'created': [], 'analysis_refresh': refresh,
                            'diagnostics': ['Aggiornamento delle candidate: preparazione nel prossimo ciclo, senza addebiti.']}
            if self.prepared_group() or not preview['tickets']:
                return {'enabled': True, 'created': [], 'diagnostics': [preview.get('message') or 'Gruppo già preparato; conferma manuale.']}
            try:
                report = self.update(MultipleSwitch(request_id=uuid5(NAMESPACE_URL, 'prepare-only:' + preview['plan_id']),
                    enabled=True, plan_id=preview['plan_id']), auto_prepare_activation=activation)
                return {'enabled': True, 'created': [], 'prepared': [i for r in report['preparations'] for i in r['slips']],
                        'diagnostics': ['Schedine preparate; Banca invariata e conferma manuale.']}
            except Exception as error:
                self.record_execution(str(error) if isinstance(error, ValueError) else 'PREPARATION_FAILED',
                                      technical=not isinstance(error, ValueError))
                return {'enabled': True, 'created': [], 'diagnostics': [str(error) if isinstance(error, ValueError) else 'PREPARATION_FAILED']}
        if not self.policy()['enabled'] or self.execution_status().get('paused'):
            return {'enabled':False,'created':[],'diagnostics':['Automazione disattivata o sospesa.']}
        from better.services.scheduled_multiples import ScheduledMultiples
        schedule = ScheduledMultiples(self)
        if self.policy().get('workflow_version') == 2 and not schedule.pending():
            group = self.prepared_group()
            if group:
                with self._transaction() as conn:
                    repo = copy(self.repository)
                    repo.engine = _BoundEngine(conn)
                    if not schedule.pending(repo):
                        schedule.save(repo, conn, group['slips'], group['preview'])
        if schedule.pending():
            return await schedule.collect_due()
        preview = self.preview()
        policy = self.policy()
        if hasattr(self.runtime,'providers') and (not policy.get('preview_id') or not policy.get('approved_budget_eur') or Decimal(policy['budget_eur'])>Decimal(policy['approved_budget_eur']) or (policy.get('approval_mode')!='FUTURE_PROPOSALS' and policy.get('preview_id')!=preview['preview_id'])):
            return {'enabled':True,'created':[],'diagnostics':['APPROVAL_REQUIRED: approva l’anteprima aggiornata.']}
        if hasattr(self.runtime, 'analyze'):
            from better.services.candidate_refresh import CandidateRefresh
            try:
                refresh = await CandidateRefresh(self).run(preview)
            except Exception:
                message = 'ANALYSIS_REFRESH_FAILED: aggiornamento non completato; nessuna giocata confermata.'
                self.record_execution(message, technical=True)
                return {'enabled': self.policy()['enabled'] and not self.execution_status()['paused'], 'created': [],
                        'diagnostics': [message], 'error': message}
            if not any(refresh.values()):
                refresh = None
            if refresh is not None and refresh['failed']:
                message = 'ANALYSIS_REFRESH_FAILED: aggiornamento non riuscito; nessuna giocata confermata.'
            elif refresh is not None and refresh['refreshed']:
                message = ('ANALYSIS_REFRESHED: candidate aggiornate e riclassificate; '
                           'nuova verifica nel prossimo ciclo. Le approvazioni puntuali vanno riconfermate.')
            elif refresh is not None:
                message = 'ANALYSIS_REFRESH_WAIT: aggiornamento in corso o tentativi sospesi; nessun addebito.'
            if refresh is not None:
                # Waiting is not a successful attempt: preserve the technical-error circuit breaker.
                if refresh['failed'] or refresh['refreshed']:
                    self.record_execution(message, technical=bool(refresh['failed']))
                return {'enabled': self.policy()['enabled'] and not self.execution_status()['paused'], 'created': [],
                        'diagnostics': [message], 'analysis_refresh': refresh,
                        **({'error': message} if refresh['failed'] else {})}
        if not preview['tickets']:
            return self.collect()
        # Prepare now without moving money. Verification and confirmation belong
        # to the first kickoff minus fifteen minutes of each individual ticket.
        if hasattr(self.runtime, 'providers'):
            return schedule.prepare(preview)
        try:
            await self.final_check(preview)
        except PlacementBlocked as error:
            message = str(error)
            self.record_execution(message)
            return {'enabled':True,'created':[],'diagnostics':[message]}
        except Exception:
            message = 'API_PROVIDER_ERROR: verifica finale calendario/quote non riuscita.'
            self.record_execution(message,technical=True)
            return {'enabled':not self.execution_status()['paused'],'created':[],'diagnostics':[message], 'error':message}
        self._verified_preview = preview['preview_id']
        self._verified_activation = policy.get('activation_id','legacy')
        try:
            result = self.collect()
        except Exception:
            message = 'REGISTRATION_ERROR: registrazione non verificata; controllare Banca e schedine.'
            self.record_execution(message,technical=True)
            return {'enabled':not self.execution_status()['paused'],'created':[],'diagnostics':[message],'error':message}
        finally:
            self._verified_preview = None
            self._verified_activation = None
        if result.get('error'):
            self.record_execution(result['error'])
        if result.get('created'):
            self.record_execution('Giocate registrate; Banca aggiornata.',success=True)
            if policy.get('approval_mode')=='CURRENT_PROPOSAL' and hasattr(self.runtime,'providers'):
                self.update_policy(MultipleAutoSwitch(request_id=uuid5(NAMESPACE_URL,'completed:'+preview['preview_id']),enabled=False))
        return result

    async def final_check(self, preview, *, scheduled=False):
        if self.policy().get('workflow_version') == 2 and any(
                r.get('status') != 'BET' for t in preview['tickets'] for r in t['selections']):
            raise PlacementBlocked('INDICATIVE_NOT_ADMITTED: proposta indicativa; conferma automatica bloccata.')
        if not hasattr(self.runtime,'providers'):
            return  # Pure service tests supply no network runtime.
        now = self.clock()
        checked = {}
        deadlines = []
        runs = {}
        for ticket in preview['tickets']:
            for row in ticket['selections']:
                if self.policy().get('workflow_version') == 2 and row.get('status') != 'BET':
                    raise PlacementBlocked('INDICATIVE_NOT_ADMITTED: proposta indicativa; conferma automatica bloccata.')
                match_id = row['match_id']
                run = self.repository.get_run(row['analysis_id'])
                if run is None or not 0 <= (now-run.as_of).total_seconds() <= self.settings.stats_ttl_seconds:
                    raise PlacementBlocked('ANALYSIS_STALE: aggiorna l’analisi prima di confermare.')
                runs[match_id] = run
                newer = [r for r in self.repository.all_records('analysis_runs',match_id=match_id) if moment(r['as_of'])>run.as_of and moment(r['created_at'])<=now]
                if newer:
                    raise PlacementBlocked('ANALYSIS_CHANGED: esiste un’analisi più recente; rivedi la proposta.')
                if match_id not in checked:
                    fixture = await asyncio.wait_for(self.runtime.providers.fixtures.get_match(match_id),self.settings.provider_timeout_seconds)
                    odds = await asyncio.wait_for(self.runtime.providers.odds.get_odds(match_id, self.clock()),self.settings.provider_timeout_seconds)
                    checked[match_id] = fixture, odds
                fixture, odds = checked[match_id]
                observed_now = self.clock()
                if odds.match_id != match_id:
                    raise PlacementBlocked('QUOTE_IDENTITY_MISMATCH: quote riferite a un’altra partita.')
                original = run.snapshot.match
                identity_fields = ('match_id','competition','season','kickoff','home_team_id','away_team_id')
                if any(getattr(fixture,k)!=getattr(original,k) for k in identity_fields) or fixture.kickoff <= observed_now:
                    raise PlacementBlocked('FIXTURE_CHANGED: calendario o identità cambiati; rivedi la proposta.')
                quotes = [q for q in odds.quotes if q.match_id==match_id and q.market.type.value==row['market'] and q.market.selection==row['selection'] and q.bookmaker==row['bookmaker'] and not q.is_opening and 0 <= (observed_now-q.captured_at).total_seconds() <= self.settings.odds_ttl_seconds and q.provenance.available_at(observed_now)]
                if not quotes:
                    raise PlacementBlocked('QUOTE_STALE: quota aggiornata non disponibile; nessun addebito.')
                latest = max(quotes,key=lambda q:q.captured_at)
                deadlines.extend([latest.captured_at+timedelta(seconds=self.settings.odds_ttl_seconds),fixture.kickoff,run.as_of+timedelta(seconds=self.settings.stats_ttl_seconds)])
                if Decimal(str(row['odds'])) != Decimal(str(latest.odds)):
                    raise PlacementBlocked('QUOTE_CHANGED: quota cambiata; aggiorna analisi e anteprima.')
        end = self.clock()
        if deadlines and end >= min(deadlines):
            raise PlacementBlocked('FINAL_CHECK_EXPIRED: dati scaduti durante il controllo; riprovare.')
        for match_id, run in runs.items():
            if any(moment(r['as_of'])>run.as_of and moment(r['created_at'])<=end for r in self.repository.all_records('analysis_runs',match_id=match_id)):
                raise PlacementBlocked('ANALYSIS_CHANGED: analisi aggiornata durante il controllo.')
        self._verified_until = min(deadlines) if deadlines else end
        if not scheduled and self.preview()['preview_id'] != preview['preview_id']:
            raise PlacementBlocked('PREVIEW_CHANGED: la composizione è cambiata durante i controlli.')

    def report(self):
        plan = self.proposal()
        rows = [r for r in self._rows() if r['kind'] == 'PREPARED' and r['turn'] == self._scope()]
        spent = self._spent(self.repository)
        policy = self.policy()
        execution = self.execution_status()
        success_at = max((r.get('at', '') for r in rows), default='')
        errors = sorted((r for r in self._rows() if r['kind']=='ERROR' and r.get('turn')==self._scope() and r.get('at','')>success_at), key=lambda r:r.get('at',''), reverse=True)[:5]
        remaining = max(Decimal('0'), Decimal(policy['budget_eur'])-spent)
        readiness = execution.get('message') or (errors[0]['reason'] if errors else ('Budget del turno gia assegnato.' if remaining<=0 else 'In attesa di almeno tre partite utilizzabili entro 24 ore.' if len(plan['selected'])<3 else 'Pronta per il prossimo ciclo del collector.'))
        preview = self.preview()
        from better.services.scheduled_multiples import ScheduledMultiples
        scheduled = ScheduledMultiples(self).report()
        approval_required = bool(policy['enabled'] and (not policy.get('preview_id') or not policy.get('approved_budget_eur') or Decimal(policy['budget_eur'])>Decimal(policy['approved_budget_eur']) or (policy.get('approval_mode')!='FUTURE_PROPOSALS' and policy.get('preview_id')!=preview['preview_id'])))
        if policy['enabled'] and scheduled and policy.get('preview_id') and policy.get('approved_budget_eur') and Decimal(policy['budget_eur']) <= Decimal(policy['approved_budget_eur']):
            approval_required = any(r['approval_required'] for r in scheduled)
            readiness = 'Multiple preparate: conferma automatica a T\u221224 dal primo kickoff del gruppo, se i controlli passano.'
        if approval_required:
            readiness = 'APPROVAL_REQUIRED: rivedi e approva l’anteprima aggiornata.'
        refresh_rows = self.repository.all_records('annotations', entity_id='candidate-refresh:v1')
        refresh_status = sorted(refresh_rows, key=lambda r: (r['at'], r['kind'] != 'CLAIM'), reverse=True)[:6]
        return {'workflow_version': policy.get('workflow_version', 1),
                'preparation_enabled': policy.get('preparation_enabled', False),
                'style': policy.get('style', 'MULTIPLE'),
                'confirmation_enabled': policy['enabled'] and not execution.get('paused'),
                'approval_required': approval_required, 'enabled': policy['enabled'] and not execution.get('paused'), 'paused': execution.get('paused',False), 'execution_status': execution, 'preview': preview, 'budget_eur': policy['budget_eur'], 'control': policy, 'proposal': plan, 'approved_plan': None,
                'analysis_refresh_status': refresh_status,
                'scheduled_confirmations': scheduled,
                'allocated_eur': f'{spent:.2f}', 'unallocated_eur': f'{max(Decimal("0"), Decimal(policy['budget_eur'])-spent):.2f}',
                'preparations': rows, 'groups': [], 'errors': errors, 'readiness_reason': readiness,
                'note': 'Budget totale del turno settimanale Europe/Rome. Preparazione manuale senza addebito; automazione separata delle giocate registrate con addebito Banca. Nessuna puntata al bookmaker.'}

    def update(self, request, *, auto_place=False, auto_schedule=None, auto_prepare_activation=None):
        if not request.enabled:
            return self.report()
        with self._transaction() as conn:
            repo = copy(self.repository)
            repo.engine = _BoundEngine(conn)
            worker = AutomaticMultiples(SimpleNamespace(repository=repo, settings=self.settings), clock=self.clock)
            if auto_prepare_activation is not None and (not worker.policy().get('preparation_enabled')
                    or worker.policy().get('activation_id') != auto_prepare_activation or worker.execution_status(repo).get('paused')):
                raise ValueError('PREPARATION_DISABLED: autorizzazione cambiata; nessuna schedina creata')
            if auto_place and hasattr(self.runtime,'providers') and (getattr(self,'_verified_preview',None) != worker.preview()['preview_id'] or getattr(self,'_verified_activation',None)!=worker.policy().get('activation_id','legacy') or self.clock()>=getattr(self,'_verified_until',self.clock())):
                raise ValueError('FINAL_CHECK_REQUIRED: rivedi l’anteprima e verifica quote e calendario')
            if auto_place and hasattr(self.runtime,'providers') and (not worker.policy().get('approved_budget_eur') or Decimal(worker.policy()['budget_eur'])>Decimal(worker.policy()['approved_budget_eur'])):
                raise ValueError('APPROVAL_REQUIRED: aumento del budget da autorizzare')
            if auto_place and not worker.policy()['enabled']:
                return worker.report()
            prior = next((r for r in self._rows(repo) if r['kind'] == 'PREPARED'
                          and (r['plan_id'] == request.plan_id or r.get('request_id') == str(request.request_id))), None)
            if prior:
                if prior['plan_id'] != request.plan_id:
                    raise ValueError('Richiesta gia utilizzata per una proposta diversa')
            else:
                plan = worker.proposal()
                if plan['plan_id'] != request.plan_id:
                    raise ValueError('Proposta cambiata: aggiorna Da valutare e verifica le sei selezioni prima di approvare')
                version2 = worker.policy().get('workflow_version') == 2
                minimum = {'MULTIPLE': 2, 'SYSTEM': 6, 'SINGLES': 1}[worker.policy()['style']] if version2 else 3
                if len(plan['selected']) < minimum:
                    raise ValueError('Servono almeno tre partite utilizzabili in Da valutare con kickoff nelle prossime 24 ore')
                if Decimal(plan['budget_eur']) <= 0:
                    raise ValueError('Budget di preparazione del turno gia assegnato: nessuna nuova schedina creata')
                desk = SalaBetService(repo, clock=self.clock)
                style_preview = worker.preview(repo) if version2 else None
                if version2 and not style_preview.get('ready'):
                    raise ValueError(style_preview.get('reason_code', 'PREPARATION_BLOCKED') + ': ' + style_preview.get('message', 'Proposta non disponibile'))
                combinations = [t['selections'] for t in style_preview['tickets']] if version2 else worker.compose(plan['alternatives'])
                existing = {tuple(sorted((s.match_id, s.market.value, s.selection) for s in slip.selections)) for slip in desk.list_slips()}
                if version2 and any(tuple(sorted((c['match_id'], c['market'], c['selection']) for c in rows)) in existing for rows in combinations):
                    raise ValueError('DUPLICATE_GROUP: una schedina della proposta esiste già; nessun gruppo parziale creato')
                combinations = [rows for rows in combinations if tuple(sorted((c['match_id'], c['market'], c['selection']) for c in rows)) not in existing]
                if not combinations:
                    raise ValueError('Nessuna nuova multipla valida: selezioni incompatibili o schedine gia presenti')
                cents = int(Decimal(plan['budget_eur'])*100)
                if cents < len(combinations):
                    raise ValueError('Budget residuo troppo basso per tutte le multiple')
                if auto_place:
                    bank = desk.bank.status()
                    if not bank['initialized']:
                        raise ValueError('BANK_NOT_INITIALIZED: inizializza la Banca prima di attivare le giocate registrate')
                    if Decimal(plan['budget_eur']) > Decimal(bank['available']):
                        raise ValueError('INSUFFICIENT_BANKROLL: bankroll disponibile insufficiente per il budget del turno')
                ids = []
                for index, rows in enumerate(combinations):
                    identifier = uuid5(NAMESPACE_URL, f"candidate-preparation:{plan['plan_id']}:{index}")
                    candidate_ids = []
                    for position, row in enumerate(rows, 1):
                        if not self.clock() < moment(row['kickoff']) <= (moment(plan['window_end']) if plan.get('window_end') else self.clock() + timedelta(hours=24)):
                            raise ValueError('MATCH_ALREADY_STARTED: finestra delle prossime 24 ore non piu valida')
                        candidate = desk.create_batch_candidate(BetCandidateCreate(analysis_id=row['analysis_id'], prediction_id=row['prediction_id'], market=row['market'], selection=row['selection']),
                            {'batch_id': identifier, 'ranking_position': position, 'ranking_score': row['ranking_score'],
                             'ranking_reasons': row['reasons'], 'batch_status': row['status'], 'data_quality': row['data_quality'],
                             'sample_size': row['sample_size'], 'score_breakdown': row['score_breakdown'], 'blockers': row['blockers']})
                        if candidate.removed:
                            raise ValueError('Candidata rimossa: aggiorna prima della preparazione')
                        candidate_ids.append(candidate.id)
                    slip = desk.create_slip(BetSlipCreate(request_id=identifier, candidate_ids=tuple(candidate_ids)))
                    amount = Decimal(style_preview['tickets'][index]['amount_eur']) if version2 else Decimal(cents//len(combinations) + (index < cents % len(combinations))) / 100
                    desk.update_slip(slip.id, BetSlipUpdate(request_id=uuid5(identifier, 'details'), stake=amount,
                        bookmaker=rows[0]['bookmaker'], odds_at_placement={c: Decimal(str(r['odds'])) for c,r in zip(candidate_ids, rows, strict=True)}))
                    indicative = version2 and any(r['status'] != 'BET' for r in rows)
                    if not indicative:
                        desk.confirm_slip(slip.id, BetSlipAction(request_id=uuid5(identifier, 'ready')))
                    if auto_place and indicative:
                        raise ValueError('INDICATIVE_NOT_ADMITTED: conferma automatica bloccata')
                    if auto_place:
                        desk.place_slip(slip.id, BetSlipAction(request_id=uuid5(identifier, 'automatic-test-place')))
                    metadata = {'origin': MODE, 'turn': plan['turn'], 'plan_id': plan['plan_id'], 'amount_eur': str(amount),
                                'status': 'OPEN' if auto_place else 'DRAFT' if indicative else 'READY', 'automatic_test_placement': auto_place, 'selections': rows, 'reason': (('Giocata confermata automaticamente; Banca addebitata, nessun bookmaker contattato. ' if auto_place else 'Preparata senza addebito; INDICATIVE resta DRAFT e non è ammessa alla conferma automatica. ' if indicative else 'Pronta come schedina, non piazzata; conferma manuale e quote da verificare. ') + 'INDICATIVE non diventa BET. Mercati diversi della stessa partita restano correlati.')}
                    if auto_schedule is not None:
                        metadata['reason'] = 'Preparata automaticamente senza addebito; verifica e conferma a T-24 dal primo kickoff del gruppo di 24 ore, con consenso valido. INDICATIVE resta tale.'
                    self._write(repo, conn, 'auto-multiple-slip:'+str(identifier), metadata, entity='auto-multiple-slip:'+str(identifier))
                    if version2:
                        self._write(repo, conn, 'style-slip:' + str(identifier), {
                            'style': worker.policy()['style'], 'group_id': plan['plan_id'],
                            'index': index + 1, 'count': len(combinations), 'budget_eur': plan['budget_eur'],
                            'origin': 'AUTOMATION', 'reason': 'INDICATIVE_NOT_ADMITTED' if indicative else None},
                            entity='salabet-style-slip:' + str(identifier))
                    ids.append(str(identifier))
                self._write(repo, conn, 'candidate-prepared:'+plan['plan_id'], {'kind': 'PREPARED', 'plan_id': plan['plan_id'],
                    'request_id': str(request.request_id), 'turn': plan['turn'], 'slips': ids, 'allocated_eur': plan['budget_eur'],
                    'selected': plan['selected'], 'automatic_test_placement': auto_place, 'at': self.clock().isoformat(),
                    **({'preview': style_preview, 'style': worker.policy()['style']} if version2 else {})})
                if auto_schedule is not None:
                    from better.services.scheduled_multiples import ScheduledMultiples
                    ScheduledMultiples(self).save(repo, conn, ids, auto_schedule)
        return self.report()

    def collect(self):
        if not self.policy()['enabled'] or self.execution_status().get('paused'):
            return {'enabled': False, 'created': [], 'diagnostics': []}
        plan = self.proposal()
        if Decimal(plan['budget_eur']) <= 0 or len(plan['selected']) < 3:
            return {'enabled': True, 'created': [], 'diagnostics': ['Budget gia assegnato o meno di tre partite utilizzabili entro 24 ore.']}
        try:
            report = self.update(MultipleSwitch(request_id=uuid5(NAMESPACE_URL, 'auto-test:'+plan['plan_id']),
                                               enabled=True, plan_id=plan['plan_id']), auto_place=True)
            rows = [p for p in report['preparations'] if p['plan_id'] == plan['plan_id'] and p.get('automatic_test_placement')]
            return {'enabled': report['enabled'], 'created': [s for p in rows for s in p['slips']], 'diagnostics': []}
        except ValueError as error:
            reason = str(error)
            with self._transaction() as conn:
                repo = copy(self.repository)
                repo.engine = _BoundEngine(conn)
                identifier = 'multiple-error:'+canonical_hash({'plan': plan['plan_id'], 'reason': reason})
                if not any(r.get('diagnostic_id') == identifier for r in self._rows(repo)):
                    self._write(repo, conn, identifier, {'kind': 'ERROR', 'diagnostic_id': identifier,
                        'reason': reason, 'at': self.clock().isoformat(), 'turn': self._scope()})
            return {'enabled': True, 'created': [], 'diagnostics': [reason], 'error': reason}
