"""Bounded pre-placement refresh; never records a T24 capture or touches money/slips."""
from copy import copy
from datetime import timedelta
from uuid import NAMESPACE_URL, uuid5

from better.domain.salabet import BetCandidateCreate, BetSlipAction
from better.domain.schemas import AnalysisRequest
from better.services.automation_control import AutomationControl
from better.services.batch_analysis import BatchAnalysisService
from better.services.salabet_service import SalaBetService
from better.services.timing_comparison import moment
from better.storage.repository import canonical_hash

ENTITY = 'candidate-refresh:v1'
RETRY_DELAY = timedelta(minutes=15)


class CandidateRefresh:
    def __init__(self, owner):
        self.owner = owner
        self.repo, self.runtime = owner.repository, owner.runtime
        self.clock, self.settings = owner.clock, owner.settings

    def _rows(self, repo=None):
        return (repo or self.repo).all_records('annotations', entity_id=ENTITY)

    def _claim(self, key, match_id):
        from better.services.automatic_multiples import _BoundEngine
        with self.owner._transaction() as conn:
            repo = copy(self.repo)
            repo.engine = _BoundEngine(conn)
            rows = [r for r in self._rows(repo) if r['key'] == key]
            claims = [r for r in rows if r['kind'] == 'CLAIM']
            if any(r['kind'] == 'DONE' for r in rows):
                return None
            if claims and (len(claims) >= 3 or self.clock() < max(moment(r['at']) for r in claims) + RETRY_DELAY):
                return None
            attempt = len(claims) + 1
            self.owner._write(repo, conn, f'refresh:{key}:claim:{attempt}', {
                'kind': 'CLAIM', 'key': key, 'match_id': match_id,
                'attempt': attempt, 'at': self.clock().isoformat()}, entity=ENTITY)
            return attempt

    def _fresh_run(self, match, original):
        rows = self.repo.all_records('analysis_runs', match_id=match.match_id)
        fresh = [r for r in rows if original.as_of < moment(r['as_of']) <= self.clock()
                 and moment(r['created_at']) <= self.clock()
                 and 0 <= (self.clock() - moment(r['as_of'])).total_seconds() <= self.settings.stats_ttl_seconds]
        for row in sorted(fresh, key=lambda r: r['as_of'], reverse=True):
            run = self.repo.get_run(row['analysis_id'])
            fields = ('match_id', 'competition', 'season', 'kickoff', 'home_team_id', 'away_team_id')
            if run and all(getattr(run.snapshot.match, k) == getattr(match, k) for k in fields):
                return run
        return None

    def _verify(self, match, run):
        if not self.clock() < match.kickoff or not run.as_of <= run.created_at <= self.clock():
            raise ValueError('REFRESH_OUTSIDE_PREMATCH')
        if not 0 <= (self.clock() - run.as_of).total_seconds() <= self.settings.stats_ttl_seconds:
            raise ValueError('REFRESH_NOT_FRESH')
        fields = ('match_id', 'competition', 'season', 'kickoff', 'home_team_id', 'away_team_id')
        if run.match_id != match.match_id or any(getattr(match, k) != getattr(run.snapshot.match, k) for k in fields):
            raise ValueError('REFRESH_IDENTITY_CONFLICT')
        snapshots = (run.snapshot.stats, run.snapshot.news, run.snapshot.lineups)
        if any(s.captured_at > run.as_of or any(not p.available_at(run.as_of) for p in s.provenance)
               for s in snapshots if s is not None):
            raise ValueError('REFRESH_LEAKAGE_REJECTED')
        if any(q.captured_at > run.as_of or not q.provenance.available_at(run.as_of)
               for q in (run.snapshot.odds.quotes if run.snapshot.odds else ())):
            raise ValueError('REFRESH_LEAKAGE_REJECTED')
        result = run.results.get('predictive_model')
        if any(p.data_cutoff > run.as_of for p in getattr(getattr(result, 'payload', None), 'model_predictions', ())):
            raise ValueError('REFRESH_LEAKAGE_REJECTED')

    async def run(self, preview):
        import asyncio

        from better.services.automatic_multiples import _BoundEngine

        targets = {}
        for ticket in preview['tickets']:
            for selection in ticket['selections']:
                run = self.repo.get_run(selection['analysis_id'])
                if run and (self.clock() - run.as_of).total_seconds() > self.settings.stats_ttl_seconds:
                    targets.setdefault(run.match_id, run)
        report = {'refreshed': [], 'failed': [], 'waiting': []}
        for match_id, original in targets.items():
            if not self.owner.policy()['enabled'] or self.owner.execution_status().get('paused'):
                break
            match = original.snapshot.match
            if (match.competition not in self.settings.timing_comparison_competitions
                    or not self.clock() < match.kickoff <= self.clock() + timedelta(hours=24)):
                continue
            key = canonical_hash({'match': match_id, 'kickoff': match.kickoff.isoformat(),
                                  'source_analysis': str(original.analysis_id)})
            attempt = self._claim(key, match_id)
            if attempt is None:
                report['waiting'].append(match_id)
                continue
            try:
                fixture = await asyncio.wait_for(self.runtime.providers.fixtures.get_match(match_id),
                                                 self.settings.provider_timeout_seconds)
                fields = ('match_id', 'competition', 'season', 'kickoff', 'home_team_id', 'away_team_id')
                if any(getattr(fixture, k) != getattr(match, k) for k in fields) or fixture.kickoff <= self.clock():
                    raise ValueError('REFRESH_FIXTURE_CHANGED')
                run = self._fresh_run(match, original)
                if run is None:
                    control = AutomationControl(self.repo, self.settings, clock=self.clock)
                    policy = control.policy()
                    options = {'cache_only_research': True}
                    if policy.get('enabled') and policy.get('persisted'):
                        await control.ensure_fx()
                        options = {'research_budget': control.guard(match, 'PREPLACEMENT_REFRESH:' + key)}
                    # Research reservations are shared across retries of the same stale analysis.
                    run = await asyncio.wait_for(self.runtime.analyze(
                        AnalysisRequest(match_id=match_id, parent_analysis_id=original.analysis_id), **options),
                        timeout=RETRY_DELAY.total_seconds() - 60)
                self._verify(match, run)
                item, reason = BatchAnalysisService(self.runtime, clock=self.clock)._best_candidate(run)
                with self.owner._transaction() as conn:
                    repo = copy(self.repo)
                    repo.engine = _BoundEngine(conn)
                    desk = SalaBetService(repo, clock=self.clock)
                    old = [c for c in desk.list_candidates() if c.match_id == match_id and c.kickoff == match.kickoff
                           and (source := repo.get_run(c.analysis_id)) is not None and source.as_of <= original.as_of]
                    candidate = None
                    if old and self.clock() < match.kickoff and item:
                        candidate = desk.create_batch_candidate(BetCandidateCreate(
                            analysis_id=item['analysis_id'], prediction_id=item['prediction_id'],
                            market=item['market'], selection=item['selection']), {
                                'batch_id': str(uuid5(NAMESPACE_URL, 'refresh:' + key)),
                                'ranking_score': item['ranking_score'], 'ranking_reasons': [
                                    'Aggiornamento automatico pre-conferma', *item['reasons']],
                                'batch_status': item['status'], 'data_quality': item['data_quality'],
                                'sample_size': item['sample_size'], 'score_breakdown': item['score_breakdown'],
                                'blockers': item['blockers']})
                    # Visibility only: existing slips retain their original immutable selections.
                    for old_candidate in old:
                        desk.remove_candidate(old_candidate.id, BetSlipAction(request_id=uuid5(
                            NAMESPACE_URL, 'refresh-hide:' + key + ':' + str(old_candidate.id))))
                    self.owner._write(repo, conn, f'refresh:{key}:done', {
                        'kind': 'DONE', 'key': key, 'match_id': match_id, 'attempt': attempt,
                        'analysis_id': str(run.analysis_id), 'candidate_id': str(candidate.id) if candidate else None,
                        'reason': reason if item is None else 'ANALYSIS_REFRESHED',
                        'at': self.clock().isoformat()}, entity=ENTITY)
                report['refreshed'].append(match_id)
            except Exception as error:
                known = {'REFRESH_OUTSIDE_PREMATCH', 'REFRESH_NOT_FRESH', 'REFRESH_IDENTITY_CONFLICT',
                         'REFRESH_LEAKAGE_REJECTED', 'REFRESH_FIXTURE_CHANGED'}
                reason = str(error) if isinstance(error, ValueError) and str(error) in known else 'ANALYSIS_REFRESH_FAILED'
                with self.repo.engine.begin() as conn:
                    self.owner._write(self.repo, conn, f'refresh:{key}:failed:{attempt}', {
                        'kind': 'FAILED', 'key': key, 'match_id': match_id, 'attempt': attempt,
                        'reason': reason, 'at': self.clock().isoformat()}, entity=ENTITY)
                report['failed'].append(match_id)
        return report
