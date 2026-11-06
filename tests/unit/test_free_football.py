import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from better.config import Settings
from better.domain.errors import BetterError
from better.domain.models import Match
from better.providers.api_football import APIFootballProvider, APIFootballSettings
from better.providers.free_football import (
    FreeFootballProvider,
    FreeFootballSettings,
    parse_csv,
    parse_openfootball,
)
from better.providers.result_identity import DeterministicResultIdentityResolver
from better.providers.results import CompositeResultProvider
from better.storage.repository import Repository

NOW = datetime(2026, 11, 3, 10, tzinfo=UTC)
MATCH = Match(match_id="888:m", competition="888:a", season="2026", kickoff=NOW - timedelta(days=1),
              home_team="Home", away_team="Away", home_team_id="h", away_team_id="a")


@pytest.fixture
def repo():
    repository = Repository("sqlite:///:memory:")
    yield repository
    repository.close()


def row(**changes):
    return {"division": "I1", "season": 2026, "kickoff": MATCH.kickoff.isoformat(),
            "home": "Home FC", "away": "Away FC", "home_score": 2, "away_score": 1,
            "statistics": {}, **changes}


def make_provider(repo, *, score=(2, 1), published=None, article_home="Home", callback=None):
    calls = []
    url = "https://www.legaseriea.it/serie-a/news/home-away-2026-2027-2-1-cronaca-risultato-gol"

    def handle(request):
        calls.append(str(request.url))
        if callback:
            return callback(request)
        if request.url.path.endswith('/news'):
            return httpx.Response(200, text=f'<a href="{url}">result</a>')
        text = json.dumps({"@type": "NewsArticle", "headline": f"{article_home}-Away {score[0]}-{score[1]}, cronaca",
                           "datePublished": (published or MATCH.kickoff + timedelta(hours=3)).isoformat()})
        return httpx.Response(200, text='<script type="application/ld+json">'+text+'</script>')

    settings = FreeFootballSettings(enabled=True, competitions={"888:a": "I1"}, seasons=(2026,))
    provider = FreeFootballProvider(settings, repository=repo, clock=lambda: NOW,
        fixtures=SimpleNamespace(get_match=AsyncMock(return_value=MATCH)),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    return provider, calls


@pytest.mark.parametrize('score,expected', [((2, 1), 'FINAL'), ((1, 2), 'CONFLICT')])
async def test_official_result_requires_oriented_identity_and_checks_score_conflict(repo, score, expected):
    provider, calls = make_provider(repo, score=score)
    provider.save_rows([row()], 'https://open.test/season.json', NOW)
    resolver = DeterministicResultIdentityResolver(repo, provider, provider_priority=('free_football',))
    resolution = await resolver.resolve(MATCH, NOW)
    assert resolution.verified
    result = await provider.get_result(resolution.identity, NOW)
    assert result.final_status == expected and result.source_tier == 'A'
    await provider.get_result(resolution.identity, NOW)
    assert len(calls) == 2
    assert not repo.all_records('match_results')  # adapter alone never settles
    await provider.close()


async def test_pending_open_data_can_identify_fixture_but_only_official_report_proves_finish(repo):
    provider, _ = make_provider(repo)
    provider.save_rows([row(home_score=None, away_score=None)], 'https://open.test/season.json', NOW)
    identity = (await DeterministicResultIdentityResolver(repo, provider).resolve(MATCH, NOW)).identity
    assert (await provider.get_result(identity, NOW)).final_status == 'FINAL'
    await provider.close()


@pytest.mark.parametrize('published,home', [(NOW + timedelta(hours=1), 'Home'), (None, 'Different')])
async def test_future_or_wrong_team_report_cannot_settle(repo, published, home):
    provider, _ = make_provider(repo, published=published, article_home=home)
    provider.save_rows([row()], 'https://open.test/season.json', NOW)
    identity = (await DeterministicResultIdentityResolver(repo, provider).resolve(MATCH, NOW)).identity
    assert await provider.get_result(identity, NOW) is None
    await provider.close()


async def test_local_archive_dedup_known_at_and_ambiguity(repo):
    provider, calls = make_provider(repo)
    for _ in range(2):
        provider.save_rows([row()], 'https://open.test/season.json', NOW)
    assert len(repo.all_records('annotations')) == 2  # one content, one acquisition
    assert provider.rows(NOW - timedelta(seconds=1)) == []
    provider.save_rows([row(away_score=3)], 'https://other.test/season.csv', NOW)
    result = await DeterministicResultIdentityResolver(repo, provider).resolve(MATCH, NOW)
    assert result.status == 'AMBIGUOUS' and not result.verified
    assert not calls
    await provider.close()


def test_csv_is_explicit_and_retains_only_observed_statistics():
    header = 'division,season,kickoff,home,away,home_score,away_score,source_url,home_shots\n'
    body = 'I2,2026,2026-11-01T15:00:00+01:00,Home,Away,1,0,https://source.test/report,12\n'
    result = parse_csv(header + body)[0]
    assert result['kickoff'] == '2026-11-01T14:00:00+00:00'
    assert result['statistics'] == {'home_shots': 12}
    with pytest.raises(ValueError):
        parse_csv(header + body.replace('T15:00:00+01:00', 'T15:00:00'))
    with pytest.raises(ValueError):
        parse_csv(header + body.replace('I2,', 'I3,'))


def test_openfootball_dst_and_unknown_scores_not_fabricated():
    payload = {'name': 'Italian Serie A 2026/27', 'matches': [
        {'date': '2026-10-24', 'time': '18:30', 'team1': 'Home', 'team2': 'Away'},
        {'date': '2026-11-02', 'time': '18:30', 'team1': 'Away', 'team2': 'Home', 'score': {'ft': [2, 1]}},
    ]}
    rows = parse_openfootball(payload, 'I1', 2026)
    assert rows[0]['home_score'] is None
    assert rows[0]['kickoff'].endswith('16:30:00+00:00')
    assert rows[1]['kickoff'].endswith('17:30:00+00:00')
    payload['matches'][1]['score'] = [2, 1]
    assert parse_openfootball(payload, 'I1', 2026)[1]['home_score'] == 2


async def test_free_stats_are_local_only_and_exclude_future_results(repo):
    provider, calls = make_provider(repo)
    future_match = MATCH.model_copy(update={'kickoff': NOW + timedelta(days=1)})
    provider.fixtures.get_match.return_value = future_match
    rows = [row(kickoff=(NOW - timedelta(days=i+1)).isoformat()) for i in range(10)]
    rows += [row(kickoff=(NOW + timedelta(days=2)).isoformat(), home_score=40)]
    provider.save_rows(rows, 'https://open.test/season.json', NOW)
    stats = await provider.get_stats(MATCH.match_id, NOW)
    assert stats.home.sample_size == stats.away.sample_size == 10
    assert stats.home.goals_for_per_match == 2
    assert all(r.kickoff < NOW for r in stats.home.recent_matches)
    assert not calls
    await provider.close()


async def test_api_football_network_disabled_even_with_key(repo):
    from pydantic import SecretStr
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: pytest.fail('network called')))
    provider = APIFootballProvider(APIFootballSettings(network_enabled=False), repository=repo,
                                  key=SecretStr('test'), client=client)
    with pytest.raises(BetterError, match='Archivio locale'):
        await provider.request('fixtures', {'league': 135, 'season': 2026})
    assert (await provider.check_access(135, 2026))['network_enabled'] is False
    await client.aclose()


async def test_failed_provider_does_not_hide_independently_verifiable_candidate(repo):
    from better.domain.enums import ErrorCode
    provider, _ = make_provider(repo)
    provider.save_rows([row()], 'https://open.test/season.json', NOW)
    failed = SimpleNamespace(find_candidates=AsyncMock(side_effect=BetterError(ErrorCode.PERMANENT_PROVIDER_ERROR, 'HTTP 403')))
    composite = CompositeResultProvider(failed, provider)
    assert len(await composite.find_candidates(MATCH, NOW)) == 1
    assert composite.last_errors
    await provider.close()


def test_operational_competitions_do_not_change_decision_config_hash():
    assert Settings().decision_configuration() == Settings(active_competitions=('A', 'B')).decision_configuration()


def test_targeted_research_skips_fresh_sources_and_waits_for_official_lineup_window():
    from better.services.external_research import ExternalResearchConfiguration, missing_qualitative_fields
    config = ExternalResearchConfiguration(enabled=True)
    snapshots = {'news': SimpleNamespace(captured_at=NOW), 'lineups': SimpleNamespace(captured_at=NOW, status='CONFIRMED_LINEUP')}
    assert missing_qualitative_fields(snapshots, NOW, config) == ()
    far = missing_qualitative_fields({}, NOW, config, kickoff=NOW + timedelta(days=1))
    near = missing_qualitative_fields({}, NOW, config, kickoff=NOW + timedelta(hours=1))
    assert 'probable_lineups' in far and 'confirmed_lineups' not in far
    assert 'confirmed_lineups' in near and 'probable_lineups' not in near


async def test_qualitative_cache_subset_survives_restart_without_research(repo):
    from better.domain.research import ResearchResult
    from better.observability.metrics import Metrics
    from better.providers.research import FakeResearchProvider
    from better.services.external_research import ExternalResearchConfiguration, ExternalResearchService
    from tests.unit.test_external_research import NOW as RESEARCH_NOW
    from tests.unit.test_external_research import finding
    match = MATCH.model_copy(update={'kickoff': RESEARCH_NOW + timedelta(hours=5)})
    calls = []

    def respond(request):
        calls.append(request)
        return ResearchResult(request_id=request.request_id, match_id=match.match_id, as_of=request.as_of,
            findings=tuple(finding(field=f) for f in request.missing_fields), searched_fields=request.missing_fields,
            status='OK', completed_at=RESEARCH_NOW, provider='synthetic-test', model='test')

    provider = FakeResearchProvider(result=respond)
    config = ExternalResearchConfiguration(enabled=True)
    first = ExternalResearchService(config, provider, repository=repo, metrics=Metrics(), clock=lambda: RESEARCH_NOW)
    await first.research_missing(match, as_of=RESEARCH_NOW, missing_fields=('news', 'injuries'), source_state_version='same')
    restarted = ExternalResearchService(config, provider, repository=repo, metrics=Metrics(), clock=lambda: RESEARCH_NOW + timedelta(seconds=5))
    result = await restarted.research_missing(match, as_of=RESEARCH_NOW + timedelta(seconds=5), missing_fields=('news',), source_state_version='same')
    assert result.cached and len(calls) == 1
    assert [f.field for f in result.findings] == ['news']


async def test_disabled_competition_cannot_start_new_analysis_or_enrollment(repo):
    from better.domain.schemas import AnalysisRequest
    from better.providers.fake import make_demo_bundle
    from better.services.application import make_application
    from better.services.validation_cohort import ValidationCohortService
    settings = Settings(active_competitions=('different-league',))
    runtime = make_application(settings=settings, repository=repo, providers=make_demo_bundle('bet', as_of=NOW))
    with pytest.raises(BetterError, match='COMPETITION_DISABLED'):
        await runtime.analyze(AnalysisRequest(match_id='demo-match', as_of=NOW))
    match = runtime.providers.fixtures.match
    result = ValidationCohortService(runtime).enroll(match, now=NOW)
    assert result['reason'] == 'COMPETITION_DISABLED'
    assert not repo.all_records('analysis_runs')
    await runtime.close()
