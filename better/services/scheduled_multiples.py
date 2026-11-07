"""Persisted, opt-in confirmation at each ticket's first kickoff minus 15m."""

from copy import copy
from datetime import timedelta
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid5

from better.domain.salabet import BetSlipAction
from better.services.salabet_service import SalaBetService
from better.services.timing_comparison import moment
from better.storage.repository import canonical_hash


class ScheduledMultiples:
    def __init__(self, owner):
        self.owner = owner

    @staticmethod
    def signature(slip):
        return canonical_hash({
            'selections': [s.model_dump(mode='json') for s in slip.selections],
            'bookmaker': slip.bookmaker, 'stake': str(slip.stake),
        })

    def authorized(self, row, repo=None):
        p = self.owner.policy(repo)
        return bool(p['enabled'] and p.get('activation_id') == row['activation_id']
                    and p.get('approved_budget_eur')
                    and Decimal(p['budget_eur']) <= Decimal(p['approved_budget_eur'])
                    and (p.get('approval_mode') == 'FUTURE_PROPOSALS'
                         or p.get('preview_id') == row['preview_id']))

    def pending(self, repo=None):
        rows = self.owner._rows(repo)
        done = {r['slip_id'] for r in rows if r['kind'] == 'SCHEDULE_FINISHED'}
        return [r for r in rows if r['kind'] == 'SCHEDULED' and r['slip_id'] not in done]

    def report(self):
        return [{'slip_id': r['slip_id'], 'confirm_at': r['confirm_at'],
                 'first_kickoff': r['first_kickoff'],
                 'approval_required': not self.authorized(r)} for r in self.pending()]

    def save(self, repo, conn, ids, preview):
        desk = SalaBetService(repo, clock=self.owner.clock)
        policy = self.owner.policy(repo)
        for identifier, ticket in zip(ids, preview['tickets'], strict=True):
            slip = desk.get_slip(UUID(identifier))
            kickoff = min(s.kickoff for s in slip.selections)
            row = {'kind': 'SCHEDULED', 'slip_id': identifier,
                   'activation_id': policy.get('activation_id'),
                   'preview_id': preview['preview_id'], 'ticket': ticket,
                   'signature': self.signature(slip), 'first_kickoff': kickoff.isoformat(),
                   'confirm_at': (kickoff - timedelta(minutes=15)).isoformat(),
                   'at': self.owner.clock().isoformat()}
            if not self.authorized(row, repo):
                raise ValueError('APPROVAL_REQUIRED: autorizzazione cambiata durante la preparazione.')
            self.owner._write(repo, conn, 'scheduled-multiple:' + identifier, row)

    def prepare(self, preview):
        from better.services.automatic_multiples import MultipleSwitch
        report = self.owner.update(MultipleSwitch(
            request_id=uuid5(NAMESPACE_URL, 'scheduled:' + preview['plan_id']),
            enabled=True, plan_id=preview['plan_id']), auto_schedule=preview)
        return {'enabled': report['enabled'], 'created': [],
                'prepared': [r['slip_id'] for r in self.pending()],
                'diagnostics': ['AWAITING_T15: multiple READY; nessun addebito prima del controllo finale.']}

    def finish(self, repo, conn, row, reason):
        self.owner._write(repo, conn, 'scheduled-finished:' + row['slip_id'], {
            'kind': 'SCHEDULE_FINISHED', 'slip_id': row['slip_id'],
            'reason': reason, 'at': self.owner.clock().isoformat()})

    async def collect_due(self):
        from better.services.automatic_multiples import PlacementBlocked, _BoundEngine
        owner = self.owner
        created, diagnostics = [], []
        for row in self.pending():
            if not self.authorized(row) or owner.execution_status().get('paused'):
                diagnostics.append('APPROVAL_REQUIRED: conferma programmata non autorizzata.')
                continue
            now = owner.clock()
            if now < moment(row['confirm_at']):
                diagnostics.append('AWAITING_T15: verifica finale prevista ' + row['confirm_at'])
                continue
            # A restart must not execute a missed confirmation close to kickoff.
            # Allow only the first minute after the scheduled trigger.
            if now >= moment(row['confirm_at']) + timedelta(minutes=1):
                with owner._transaction() as conn:
                    repo = copy(owner.repository)
                    repo.engine = _BoundEngine(conn)
                    if any(r['slip_id'] == row['slip_id'] for r in self.pending(repo)):
                        self.finish(repo, conn, row, 'T15_MISSED')
                diagnostics.append('T15_MISSED: controllo non eseguito nella finestra; conferma manuale necessaria.')
                continue
            try:
                preview = {'tickets': [row['ticket']], 'preview_id': row['preview_id']}
                try:
                    await owner.final_check(preview, scheduled=True)
                except PlacementBlocked:
                    raise
                except Exception as error:
                    raise RuntimeError('T15 provider verification failed') from error
                with owner._transaction() as conn:
                    repo = copy(owner.repository)
                    repo.engine = _BoundEngine(conn)
                    if not any(r['slip_id'] == row['slip_id'] for r in self.pending(repo)):
                        continue
                    if not self.authorized(row, repo) or owner.execution_status(repo).get('paused'):
                        raise PlacementBlocked('APPROVAL_REQUIRED: automazione disattivata o consenso cambiato.')
                    if owner.clock() >= moment(row['confirm_at']) + timedelta(minutes=1):
                        raise PlacementBlocked('T15_MISSED: controllo concluso oltre la finestra consentita.')
                    if owner.clock() >= owner._verified_until:
                        raise PlacementBlocked('FINAL_CHECK_EXPIRED: dati scaduti durante il controllo.')
                    desk = SalaBetService(repo, clock=owner.clock)
                    slip = desk.get_slip(UUID(row['slip_id']))
                    if slip.status != 'READY' or self.signature(slip) != row['signature']:
                        raise PlacementBlocked('SCHEDULED_SLIP_CHANGED: schedina modificata o gia confermata; nessun addebito automatico.')
                    # Recheck newer analyses under the same transaction as the debit.
                    for selection in slip.selections:
                        run = repo.get_run(selection.analysis_id)
                        if any(moment(r['as_of']) > run.as_of and moment(r['created_at']) <= owner.clock()
                               for r in repo.all_records('analysis_runs', match_id=selection.match_id)):
                            raise PlacementBlocked('ANALYSIS_CHANGED: analisi aggiornata dopo il controllo.')
                    desk.place_slip(slip.id, BetSlipAction(request_id=uuid5(slip.id, 'scheduled-t15-place')))
                    self.finish(repo, conn, row, 'PLACED')
                    created.append(row['slip_id'])
                owner.record_execution('T15_COMPLETED: giocata confermata; Banca aggiornata.', success=True)
            except (PlacementBlocked, ValueError) as error:
                # SalaBet uses known business errors, never raw provider exceptions.
                message = str(error) if isinstance(error, PlacementBlocked) else 'PLACEMENT_BLOCKED: verifica saldo e stato della schedina in SalaBet.'
                diagnostics.append(message)
                owner.record_execution(message)
            except Exception:
                message = 'API_PROVIDER_ERROR: controllo T15 non riuscito; nessuna conferma automatica.'
                owner.record_execution(message, technical=True)
                diagnostics.append(message)
        if not self.pending() and owner.policy().get('approval_mode') == 'CURRENT_PROPOSAL':
            from better.services.automatic_multiples import MultipleAutoSwitch
            owner.update_policy(MultipleAutoSwitch(request_id=uuid5(NAMESPACE_URL,
                'schedule-completed:' + str(owner.policy().get('activation_id'))), enabled=False))
        return {'enabled': owner.policy()['enabled'] and not owner.execution_status().get('paused'),
                'created': created, 'diagnostics': diagnostics}
