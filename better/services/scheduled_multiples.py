"""One final confirmation for the entire anchored 24-hour group."""

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
        anchors = {}
        for r in rows:
            if r['kind'] == 'SCHEDULED':
                anchors[r['preview_id']] = min(anchors.get(r['preview_id'], moment(r['first_kickoff'])), moment(r['first_kickoff']))
        return [{**r, 'first_kickoff': anchors[r['preview_id']].isoformat(),
                 'confirm_at': (anchors[r['preview_id']] - timedelta(hours=24)).isoformat()}
                for r in rows if r['kind'] == 'SCHEDULED' and r['slip_id'] not in done]

    def report(self):
        return [{'slip_id': r['slip_id'], 'confirm_at': r['confirm_at'],
                 'first_kickoff': r['first_kickoff'],
                 'approval_required': not self.authorized(r)} for r in self.pending()]

    def save(self, repo, conn, ids, preview):
        desk = SalaBetService(repo, clock=self.owner.clock)
        policy = self.owner.policy(repo)
        kickoff = moment(preview['window_start']) if preview.get('window_start') else min(
            moment(s['kickoff']) for t in preview['tickets'] for s in t['selections'])
        for identifier, ticket in zip(ids, preview['tickets'], strict=True):
            slip = desk.get_slip(UUID(identifier))
            row = {'kind': 'SCHEDULED', 'slip_id': identifier,
                   'activation_id': policy.get('activation_id'),
                   'preview_id': preview['preview_id'], 'ticket': ticket,
                   'signature': self.signature(slip), 'first_kickoff': kickoff.isoformat(),
                   'window_end': (kickoff + timedelta(hours=24)).isoformat(),
                   'confirm_at': (kickoff - timedelta(hours=24)).isoformat(),
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
                'diagnostics': ['AWAITING_T24: multiple READY; nessun addebito prima del controllo finale.']}

    def finish(self, repo, conn, row, reason):
        self.owner._write(repo, conn, 'scheduled-finished:' + row['slip_id'], {
            'kind': 'SCHEDULE_FINISHED', 'slip_id': row['slip_id'],
            'reason': reason, 'at': self.owner.clock().isoformat()})

    async def collect_due(self):
        from better.services.automatic_multiples import PlacementBlocked, _BoundEngine
        owner = self.owner
        created, diagnostics = [], []
        groups = {}
        for item in self.pending():
            groups.setdefault(item['preview_id'], []).append(item)
        for group in groups.values():
            row = min(group, key=lambda r: moment(r['confirm_at']))
            due = moment(row['confirm_at'])
            if any(not self.authorized(r) for r in group) or owner.execution_status().get('paused'):
                diagnostics.append('APPROVAL_REQUIRED: gruppo non autorizzato.')
                continue
            now = owner.clock()
            if now < due:
                diagnostics.append('AWAITING_T24: controllo unico del gruppo previsto ' + due.isoformat())
                continue
            if now >= moment(row['first_kickoff']):
                with owner._transaction() as conn:
                    repo = copy(owner.repository)
                    repo.engine = _BoundEngine(conn)
                    pending = {r['slip_id'] for r in self.pending(repo)}
                    for item in group:
                        if item['slip_id'] in pending:
                            self.finish(repo, conn, item, 'T24_MISSED')
                diagnostics.append('T24_MISSED: finestra del gruppo perduta; nessun recupero tardivo.')
                continue
            try:
                preview = {'tickets': [r['ticket'] for r in group], 'preview_id': row['preview_id']}
                try:
                    await owner.final_check(preview, scheduled=True)
                except PlacementBlocked:
                    raise
                except Exception as error:
                    raise RuntimeError('Group T24 provider verification failed') from error
                placed = []
                with owner._transaction() as conn:
                    repo = copy(owner.repository)
                    repo.engine = _BoundEngine(conn)
                    pending = {r['slip_id'] for r in self.pending(repo)}
                    if not all(r['slip_id'] in pending for r in group):
                        continue
                    if any(not self.authorized(r, repo) for r in group) or owner.execution_status(repo).get('paused'):
                        raise PlacementBlocked('APPROVAL_REQUIRED: automazione disattivata o consenso cambiato.')
                    if owner.clock() >= moment(row['first_kickoff']):
                        raise PlacementBlocked('MATCH_ALREADY_STARTED: controllo concluso dopo il primo kickoff del gruppo.')
                    if owner.clock() >= owner._verified_until:
                        raise PlacementBlocked('FINAL_CHECK_EXPIRED: dati scaduti durante il controllo.')
                    desk = SalaBetService(repo, clock=owner.clock)
                    slips = []
                    for item in group:
                        slip = desk.get_slip(UUID(item['slip_id']))
                        if slip.status != 'READY' or self.signature(slip) != item['signature']:
                            raise PlacementBlocked('SCHEDULED_SLIP_CHANGED: una schedina del gruppo e cambiata; nessuna conferma automatica.')
                        for selection in slip.selections:
                            run = repo.get_run(selection.analysis_id)
                            if any(moment(r['as_of']) > run.as_of and moment(r['created_at']) <= owner.clock()
                                   for r in repo.all_records('analysis_runs', match_id=selection.match_id)):
                                raise PlacementBlocked('ANALYSIS_CHANGED: analisi aggiornata dopo il controllo.')
                        slips.append(slip)
                    total = sum((s.stake for s in slips), Decimal('0'))
                    if total > Decimal(desk.bank.status()['available']):
                        raise PlacementBlocked('INSUFFICIENT_BANKROLL: saldo insufficiente per tutto il gruppo.')
                    for item, slip in zip(group, slips, strict=True):
                        desk.place_slip(slip.id, BetSlipAction(request_id=uuid5(slip.id, 'scheduled-place')))
                        self.finish(repo, conn, item, 'PLACED')
                        placed.append(item['slip_id'])
                created.extend(placed)
                owner.record_execution('T24_COMPLETED: gruppo confermato; Banca aggiornata.', success=True)
            except (PlacementBlocked, ValueError) as error:
                message = str(error) if isinstance(error, PlacementBlocked) else 'PLACEMENT_BLOCKED: verifica saldo e schedine; gruppo non confermato.'
                diagnostics.append(message)
                owner.record_execution(message)
            except Exception:
                message = 'API_PROVIDER_ERROR: controllo del gruppo T24 fallito; nessuna conferma automatica.'
                owner.record_execution(message, technical=True)
                diagnostics.append(message)
        if not self.pending() and owner.policy().get('approval_mode') == 'CURRENT_PROPOSAL':
            from better.services.automatic_multiples import MultipleAutoSwitch
            owner.update_policy(MultipleAutoSwitch(request_id=uuid5(NAMESPACE_URL,
                'schedule-completed:' + str(owner.policy().get('activation_id'))), enabled=False))
        return {'enabled': owner.policy()['enabled'] and not owner.execution_status().get('paused'),
                'created': created, 'diagnostics': diagnostics}
