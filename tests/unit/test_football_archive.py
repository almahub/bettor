"""Persistent archive regression tests; synthetic data stays in temporary databases."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import SecretStr

from better.domain.errors import BetterError
from better.domain.models import Match
from better.providers.api_football import APIFootballProvider, APIFootballSettings
from better.services.football_archive import FootballArchive, collect_archive
from better.storage.repository import Repository

NOW = datetime(2026, 10, 15, 12, tzinfo=UTC)


async def test_targeted_archive_prioritizes_upcoming_league_and_team_history(repo):
    requested = []
    past = fixture(70)
    past["league"]["id"] = 136
    past["teams"]["home"]["name"] = "Home FC"
    past["teams"]["away"]["name"] = "Away FC"
    upcoming = {**past, "fixture": {"id": 80, "date": (NOW + timedelta(days=2)).isoformat(), "status": {"short": "NS"}}}
    async def handler(request):
        endpoint = request.url.path.strip("/")
        params = dict(request.url.params)
        requested.append((endpoint, params))
        if endpoint == "leagues":
            assert params["id"] == "136"
            data = [{"league": {"id": 136}, "seasons": [{"year": 2026, "current": True, "coverage": {}}]}]
        elif "ids" in params:
            assert params["ids"] == "70"
            data = [{**past, "events": [], "lineups": [], "statistics": [], "players": []}]
        else:
            assert params["league"] == "136"
            data = [upcoming, past]
        return httpx.Response(200, json={"response": data, "errors": [], "results": len(data), "paging": {"current": 1, "total": 1}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        config = APIFootballSettings(archive_enabled=True, archive_leagues=(135, 136), archive_seasons=(2026,),
                                    archive_batch_requests=3, competitions={"source-b": {"league": 136, "season": 2026}})
        provider = APIFootballProvider(config, repository=repo, client=client, key=SecretStr("test"), clock=lambda: NOW)
        match = Match(match_id="upcoming", competition="source-b", season="2026", kickoff=NOW + timedelta(days=2),
                      home_team="Home", away_team="Away", home_team_id="home", away_team_id="away")
        result = await collect_archive(provider, matches=[match])
        assert result["requests"] == len(requested) == 3
        assert provider.archive.latest("fixtures", {"id": 70})["detail_checked"]
        assert not repo.all_records("canonical_predictions")
        assert not repo.all_records("validation_observations")


def fixture(identifier=1):
    return {
        "fixture": {
            "id": identifier,
            "date": (NOW - timedelta(days=10)).isoformat(),
            "status": {"short": "FT"},
        },
        "league": {"id": 135, "season": 2026},
        "teams": {"home": {"id": 10}, "away": {"id": 20}},
        "score": {"fulltime": {"home": 2, "away": 1}},
    }


def evidence(data, at=NOW):
    return {"data": data, "retrieved_at": at, "url": "https://provider.test/fixtures", "sha256": "raw"}


@pytest.fixture
def repo(tmp_path):
    repository = Repository(f"sqlite:///{tmp_path / 'archive.db'}")
    yield repository
    repository.close()


def test_content_dedup_corrections_and_known_at(repo):
    archive = FootballArchive(repo)
    params = {"league": 135, "season": 2026}
    archive.save("fixtures", params, evidence([fixture()]), 3600)
    archive.save("fixtures", params, evidence([fixture()], NOW + timedelta(minutes=1)), 3600)
    assert len(repo.list_records("api_football_archive")) == 1
    changed = fixture()
    changed["score"]["fulltime"]["home"] = 3
    archive.save("fixtures", params, evidence([changed], NOW + timedelta(minutes=2)), 3600)
    assert len(repo.list_records("api_football_archive")) == 2
    assert archive.fixture(1, as_of=NOW - timedelta(seconds=1)) is None
    assert archive.fixture(1, as_of=NOW)["data"][0]["score"]["fulltime"]["home"] == 2
    assert archive.fixture(1)["data"][0]["score"]["fulltime"]["home"] == 3
    assert not repo.list_records("validation_observations")


async def test_restart_local_first_and_scope(repo):
    archive = FootballArchive(repo)
    archive.save("fixtures", {"league": 135, "season": 2026}, evidence([fixture()]), 3600)

    async def forbidden(request):
        pytest.fail("A fresh season catalog must avoid network requests")

    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
        provider = APIFootballProvider(
            APIFootballSettings(archive_enabled=True),
            repository=repo,
            key=SecretStr(""),
            client=client,
            clock=lambda: NOW,
        )
        response = await provider.request(
            "fixtures", {"league": 135, "season": 2026, "team": 10, "status": "FT"}
        )
        assert len(response["data"]) == 1
        assert provider._daily_calls == 0
    assert archive.lookup("fixtures", {"team": 10}, NOW, 3600) is None
    assert archive.lookup("fixtures", {"league": 136, "season": 2026}, NOW, 3600) is None
    assert archive.lookup("fixtures", {"league": 135, "season": 2026}, NOW + timedelta(hours=2), 3600) is None


async def test_local_only_miss_never_uses_network_or_volatile_cache(repo):
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={"errors": [], "response": []})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = APIFootballProvider(APIFootballSettings(archive_enabled=True), repository=repo,
                                      client=client, key=SecretStr("test"), clock=lambda: NOW)
        await provider.request("fixtures", {"league": 135, "season": 2026})
        with pytest.raises(BetterError, match="Archivio locale"):
            await provider.request("fixtures", {"league": 135, "season": 2026}, local_only=True)
        assert len(calls) == 1
        assert provider.last_error is None


@pytest.mark.parametrize("league", [135, 136, 138, 942, 943])
async def test_analysis_uses_only_local_stats_and_lineups(repo, league):
    from unittest.mock import AsyncMock

    from better.domain.models import Match

    match = Match(match_id="test:1", competition="serie-a", season="2026",
                  kickoff=NOW+timedelta(days=1), home_team="Home", away_team="Away",
                  home_team_id="home", away_team_id="away")
    rows = []
    for i in range(3):
        row = fixture(i+1)
        row["league"]["id"] = league
        row["teams"]["home"]["name"] = "Home"
        row["teams"]["away"]["name"] = "Away"
        rows.append(row)
    future = fixture(99)
    future["league"]["id"] = league
    future["fixture"].update(date=match.kickoff.isoformat(), status={"short": "NS"})
    future["teams"] = rows[0]["teams"]
    FootballArchive(repo, leagues=(league,)).save("fixtures", {"league": league, "season": 2026}, evidence(rows+[future]), 3600)
    def forbidden(request):
        pytest.fail("Analysis must not contact API-Football")
    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
        provider = APIFootballProvider(APIFootballSettings(archive_enabled=True, analysis_archive_only=True,
            archive_leagues=(league,), competitions={"serie-a": {"league": league, "season": 2026}}, teams={"home":10,"away":20}),
            fixtures=AsyncMock(get_match=AsyncMock(return_value=match)), repository=repo,
            client=client, clock=lambda: NOW)
        stats = await provider.get_stats(match.match_id, NOW)
        assert stats.home.sample_size == 3
        with pytest.raises(BetterError, match="Archivio locale"):
            await provider.get_lineups(match.match_id, NOW)
        provider.clock = lambda: NOW+timedelta(hours=2)
        with pytest.raises(BetterError, match="Archivio locale"):
            await provider.get_stats(match.match_id, provider.clock())
        assert provider._daily_calls == 0


def test_multiple_leagues_never_mix_team_history(repo):
    archive = FootballArchive(repo, leagues=(135,136))
    for league in (135,136):
        row = fixture(league)
        row["league"]["id"] = league
        archive.save("fixtures", {"league":league,"season":2026}, evidence([row]), 3600)
    response = archive.lookup("fixtures", {"league":136,"season":2026,"team":10}, NOW, 3600)
    assert [row["fixture"]["id"] for row in response["data"]] == [136]
    assert archive.fixture(136)["data"][0]["league"]["id"] == 136
    assert archive.report(NOW,136)["seasons"][0]["fixtures"] == 1


async def test_collector_rotates_leagues_after_restart(repo):
    calls = []
    def respond(request):
        calls.append(request.url)
        league = int(request.url.params.get("league", request.url.params.get("id", 135)))
        if request.url.path.endswith('leagues'):
            data = [{"league":{"id":league},"seasons":[{"year":2026,"current":True,"coverage":{"standings":False}}]}]
        elif 'ids' in request.url.params:
            league = int(request.url.params['ids'])
            row = fixture(league)
            row["league"]["id"] = league
            data = [{**row,"events":[],"lineups":[],"statistics":[],"players":[]}]
        else:
            row = fixture(league)
            row["league"]["id"] = league
            data = [row]
        return httpx.Response(200,json={"errors":[],"response":data})
    settings=APIFootballSettings(archive_enabled=True,archive_leagues=(135,136),archive_seasons=(2026,),archive_batch_requests=3)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        for _ in range(2):
            provider=APIFootballProvider(settings,repository=repo,client=client,key=SecretStr('test'),clock=lambda:NOW)
            provider.executor.interval=0
            assert (await collect_archive(provider))["requests"] == 3
    archive=FootballArchive(repo,leagues=(135,136))
    assert archive.report(NOW,135)["seasons"][0]["detailed"] == 1
    assert archive.report(NOW,136)["seasons"][0]["detailed"] == 1
    assert len(calls) == 6


def test_durable_budget(repo):
    assert FootballArchive(repo).reserve_request(NOW, 1, "fixtures", {})
    assert not FootballArchive(repo).reserve_request(NOW, 1, "fixtures", {})
    assert FootballArchive(repo).reserve_request(NOW + timedelta(days=1), 1, "fixtures", {})


async def test_stale_current_catalog_is_refreshed_before_backfill_cursor(repo):
    archive = FootballArchive(repo, seasons=(2026,), leagues=(135,136))
    old = NOW - timedelta(hours=2)
    coverage = [{"league":{"id":135},"seasons":[{"year":2026,"current":True,"coverage":{"standings":False}}]}]
    archive.save("leagues", {"id":135,"season":2026}, evidence(coverage,old), 7*86400)
    archive.save("fixtures", {"league":135,"season":2026}, evidence([fixture()],old), 3600)
    with repo.engine.begin() as connection:
        repo._append(connection,"annotations",identifier="cursor-test",entity_id="api-football:archive:run",match_id=None,payload={"next_pair":1})
    requests=[]
    def respond(request):
        requests.append(request)
        return httpx.Response(200,json={"errors":[],"response":[fixture()]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider=APIFootballProvider(APIFootballSettings(archive_enabled=True,archive_leagues=(135,136),archive_seasons=(2026,),archive_batch_requests=1),repository=repo,client=client,key=SecretStr("test"),clock=lambda:NOW)
        assert (await collect_archive(provider))["status"] == "OK"
    assert len(requests) == 1
    assert requests[0].url.path == "/fixtures"
    assert requests[0].url.params["league"] == "135"


def test_final_details_use_correction_window_not_live_ttl(repo):
    archive = FootballArchive(repo)
    archive.save("fixtures", {"ids": "1"}, evidence([{**fixture(), "players": [], "lineups": []}]), 7 * 86400)
    local = archive.lookup("fixtures/lineups", {"fixture": 1}, NOW + timedelta(days=1), 60)
    assert local is not None and local["data"] == []
    assert archive.lookup("fixtures/lineups", {"fixture": 1}, NOW + timedelta(days=8), 60) is None


async def test_bounded_resume_and_empty_details_do_not_loop(repo):
    calls = []

    def respond(request):
        calls.append(request.url)
        if request.url.path.endswith("leagues"):
            data = [
                {
                    "league": {"id": 135},
                    "seasons": [{"year": 2026, "current": True, "coverage": {"standings": True}}],
                }
            ]
        elif request.url.path.endswith("standings"):
            data = [{"league": {"id": 135, "standings": []}}]
        elif "ids" in request.url.params:
            data = [{**fixture(), "events": [], "lineups": [], "statistics": [], "players": []}]
        else:
            data = [fixture()]
        return httpx.Response(200, json={"errors": [], "response": data})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        settings = APIFootballSettings(
            archive_enabled=True, archive_seasons=(2026,), archive_batch_requests=2
        )
        provider = APIFootballProvider(
            settings, repository=repo, key=SecretStr("test"), client=client, clock=lambda: NOW
        )
        provider.executor.interval = 0
        assert (await collect_archive(provider))["requests"] == 2
        assert (await collect_archive(provider))["requests"] == 2
        restarted = APIFootballProvider(
            settings, repository=repo, key=SecretStr("test"), client=client, clock=lambda: NOW
        )
        restarted.executor.interval = 0
        assert (await collect_archive(restarted))["requests"] == 0
        assert len(calls) == 4
        assert (await restarted.request("fixtures/players", {"fixture": 1}))["data"] == []
        report = restarted.archive.report(NOW)
        assert report["seasons"][0]["detailed"] == 1
        assert report["seasons"][0]["populated"]["players"] == 0
    assert not repo.list_records("match_results")
    assert not repo.list_records("validation_observations")


@pytest.mark.parametrize(
    "body",
    [
        {"errors": {"plan": "unavailable"}, "response": []},
        {"errors": [], "response": [fixture()], "paging": {"total": 2}},
        {"errors": [], "response": [fixture()]},
    ],
)
async def test_failed_or_incomplete_bulk_never_archived(repo, body):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body))
    ) as client:
        provider = APIFootballProvider(
            APIFootballSettings(archive_enabled=True),
            repository=repo,
            key=SecretStr("test"),
            client=client,
            clock=lambda: NOW,
        )
        with pytest.raises(BetterError):
            await provider.request("fixtures", {"ids": "1-2"}, backfill=True)
    assert not repo.list_records("api_football_archive")
    assert len(repo.list_records("api_football_archive_attempts")) == 1
