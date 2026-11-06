"""Offline native-shaped fixtures; no real credentials or network requests."""

import copy
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import SecretStr

from better.domain.errors import BetterError
from better.domain.models import Match
from better.providers.api_football import APIFootballProvider, APIFootballSettings, read_api_key
from better.storage.repository import Repository

NOW = datetime(2026, 9, 28, 10, tzinfo=UTC)
MATCH = Match(
    match_id="888it:match:1",
    competition="888it:competition:5",
    season="unknown",
    kickoff=NOW + timedelta(hours=8),
    home_team="Synthetic Home",
    away_team="Synthetic Away",
    home_team_id="888it:team:1",
    away_team_id="888it:team:2",
)


def fixture(identifier=10, home=1, away=2, date=None, goals=(2, 1)):
    return {
        "fixture": {"id": identifier, "date": (date or MATCH.kickoff).isoformat(), "status": {"short": "FT"}},
        "league": {"id": 5, "season": 2026},
        "teams": {
            "home": {"id": home, "name": f"Team {home}"},
            "away": {"id": away, "name": f"Team {away}"},
        },
        "score": {"fulltime": {"home": goals[0], "away": goals[1]}},
    }


def make_provider(handler=None):
    calls = []

    def respond(request):
        calls.append(request)
        assert request.headers["x-apisports-key"] == "test-secret"
        if handler:
            return handler(request)
        if request.url.path.endswith("status"):
            data = {
                "account": {"email": "must-not-leak@example.test"},
                "subscription": {"plan": "Free", "active": True},
                "requests": {"current": 2, "limit_day": 100},
            }
        elif request.url.path.endswith("lineups"):
            data = [
                {
                    "team": {"id": team},
                    "startXI": [{"player": {"name": f"Player {team}-{i}"}} for i in range(11)],
                }
                for team in (1, 2)
            ]
        elif request.url.params.get("team"):
            team = int(request.url.params["team"])
            data = [fixture(i, team, 3, NOW - timedelta(days=i + 1)) for i in range(3)]
        else:
            data = [fixture()]
        return httpx.Response(200, json={"errors": [], "response": data})

    provider = APIFootballProvider(
        APIFootballSettings(
            competitions={MATCH.competition: {"league": 5, "season": 2026}},
            teams={MATCH.home_team_id: 1, MATCH.away_team_id: 2},
        ),
        fixtures=AsyncMock(get_match=AsyncMock(return_value=MATCH)),
        key=SecretStr("test-secret"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
        clock=lambda: NOW,
    )
    provider.executor.interval = 0
    return provider, calls


def test_secret_file_read_does_not_execute_or_override_environment(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text('# comment\nAPI_FOOTBALL_API_KEY="file-key"\nOTHER=$(danger)\n', encoding="utf-8")
    monkeypatch.delenv("API_FOOTBALL_API_KEY", raising=False)
    assert read_api_key(path).get_secret_value() == "file-key"
    assert "file-key" not in repr(read_api_key(path))
    monkeypatch.setenv("API_FOOTBALL_API_KEY", "env-key")
    assert read_api_key(path).get_secret_value() == "env-key"
    monkeypatch.setenv("API_FOOTBALL_API_KEY", "")
    assert read_api_key(path).get_secret_value() == ""


async def test_live_access_check_does_not_mistake_cached_fixtures_for_plan_access():
    from better.services.football_archive import FootballArchive
    blocked = [False]

    def handler(request):
        if request.url.path.endswith('status'):
            return httpx.Response(200, json={'errors': [], 'response': {
                'subscription': {'plan': 'Free', 'active': True}, 'requests': {'current': 1, 'limit_day': 100}}})
        if blocked[0]:
            return httpx.Response(200, json={'errors': {'plan': 'Free plans do not have access to the 2026 season'}, 'response': []})
        return httpx.Response(200, json={'errors': [], 'response': [fixture()]})

    p, calls = make_provider(handler)
    repo = Repository('sqlite:///:memory:')
    p.settings = p.settings.model_copy(update={'archive_enabled': True})
    p.archive = FootballArchive(repo, (2026,), (5,))
    await p.request('fixtures', {'league': 5, 'season': 2026})
    blocked[0] = True
    report = await p.check_access(5, 2026)
    assert len(calls) == 3
    assert report['coverage']['accessible'] is False
    assert p.describe()['coverage']['accessible'] is False
    assert report['account']['active'] is True
    await p.client.aclose()
    repo.close()


async def test_access_check_excludes_account_identity_and_key():
    p, calls = make_provider()
    report = await p.check_access(5, 2026)
    assert report["coverage"]["accessible"] is True
    assert report["account"]["plan"] == "Free"
    assert p.describe()["coverage"]["accessible"] is True
    assert "email" not in str(report) and "test-secret" not in str(report)
    assert report["recent_requests"] == [
        {
            "endpoint": "status",
            "params": {},
            "http_status": 200,
            "result_count": 1,
            "cache_hit": False,
        },
        {
            "endpoint": "fixtures",
            "params": {"league": 5, "season": 2026},
            "http_status": 200,
            "result_count": 1,
            "cache_hit": False,
        },
    ]
    await p.check_access(5, 2026)
    assert len(calls) == 4, "An explicit access check must verify the current plan, not cached fixtures"
    assert [event["cache_hit"] for event in p.describe()["recent_requests"]] == [
        False,
        False,
        False,
        False,
    ]


async def test_plan_denial_is_cached_and_never_falls_back_to_older_season():
    def deny(request):
        return httpx.Response(
            200, json={"errors": {"plan": "test-secret arbitrary upstream text"}, "response": []}
        )

    p, calls = make_provider(deny)
    for method in (p.get_stats, p.get_lineups):
        with pytest.raises(BetterError, match="stagione 2026") as error:
            await method(MATCH.match_id, NOW)
        assert error.value.detail.code == "MISSING_DATA"
        assert "test-secret" not in str(error.value)
    assert len(calls) == 1
    assert p.describe()["blocked_seasons"] == [2026]


async def test_stats_goal_aggregates_provenance_and_cached_timestamp():
    p, calls = make_provider()
    stats = await p.get_stats(MATCH.match_id, NOW)
    assert stats.home.sample_size == 3
    assert stats.home.goals_for_per_match == 2
    assert stats.away.goals_against_per_match == 1
    assert stats.home.xg_per_match is None
    assert stats.home.team_id == MATCH.home_team_id
    assert len(stats.home.recent_matches) == 3
    assert stats.home.recent_matches[0].outcome == "W"
    assert stats.home.recent_matches[0].venue == "HOME"
    assert stats.home.recent_matches[0].opponent_id == "api-football:team:3"
    assert {"last_5", "last_10", "season", "home"} <= set(stats.home.segments)
    assert stats.home.segments["season"]["btts_rate"] == 1
    assert stats.home.segments["season"]["over_2_5_rate"] == 1
    assert all(s.source_id == "api_football" and s.retrieved_at == NOW for s in stats.provenance)
    assert stats.data_cutoff < NOW
    p.clock = lambda: NOW + timedelta(seconds=30)
    again = await p.get_stats(MATCH.match_id, NOW + timedelta(seconds=30))
    assert again.captured_at == NOW
    assert len(calls) == 3


@pytest.mark.parametrize("mutation", ["future", "duplicate", "null", "foreign", "extra_time"])
async def test_invalid_or_unusable_history_is_not_accepted(mutation):
    def handler(request):
        rows = [fixture()]
        if request.url.params.get("team"):
            team = int(request.url.params["team"])
            rows = [fixture(i, team, 3, NOW - timedelta(days=i + 1)) for i in range(2)]
            if mutation == "future":
                for row in rows:
                    row["fixture"]["date"] = (NOW + timedelta(days=1)).isoformat()
            if mutation == "duplicate":
                rows[1] = copy.deepcopy(rows[0])
            if mutation == "null":
                rows[0]["score"]["fulltime"]["home"] = None
            if mutation == "foreign":
                rows[0]["teams"]["home"]["id"] = 99
            if mutation == "extra_time":
                for row in rows:
                    row["fixture"]["status"]["short"] = "AET"
        return httpx.Response(200, json={"errors": [], "response": rows})

    p, _ = make_provider(handler)
    with pytest.raises(BetterError):
        await p.get_stats(MATCH.match_id, NOW)


async def test_identity_mismatch_blocks_stats():
    p, _ = make_provider(
        lambda request: httpx.Response(200, json={"errors": [], "response": [fixture(home=99)]})
    )
    with pytest.raises(BetterError, match="corrispondenza univoca"):
        await p.get_stats(MATCH.match_id, NOW)


async def test_stats_reuse_generic_persisted_competition_team_and_match_mappings():
    repository = Repository("sqlite:///:memory:")
    shared = {
        "provider": "api_football",
        "status": "VERIFIED_EXACT",
        "method": "EXACT_NORMALIZED_team-name-v1",
        "confidence": 1,
        "verified_at": NOW,
        "source_url": "https://v3.football.api-sports.io/fixtures",
        "normalization_version": "team-name-v1",
        "provenance": {"test": "generic"},
        "identity": None,
    }
    for kind, source_id, target_id in (
        ("competition", MATCH.competition, "api-football:league:5:season:2026"),
        ("team", MATCH.home_team_id, "api-football:team:1"),
        ("team", MATCH.away_team_id, "api-football:team:2"),
        ("match", MATCH.match_id, "api-football:fixture:10"),
    ):
        repository.save_result_identity_mapping(
            kind=kind,
            source_id=source_id,
            target_id=target_id,
            source_name=source_id,
            target_name=target_id,
            **shared,
        )
    calls = []

    def respond(request):
        calls.append(request)
        if request.url.params.get("team"):
            team = int(request.url.params["team"])
            rows = [fixture(i, team, 3, NOW - timedelta(days=i + 1)) for i in range(3)]
        else:
            rows = [fixture()]
        return httpx.Response(200, json={"errors": [], "response": rows})

    provider = APIFootballProvider(
        APIFootballSettings(),
        fixtures=AsyncMock(get_match=AsyncMock(return_value=MATCH)),
        repository=repository,
        key=SecretStr("test-secret"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
        clock=lambda: NOW,
    )
    provider.executor.interval = 0
    result = await provider.get_stats(MATCH.match_id, NOW)
    assert result.home.team_id == MATCH.home_team_id
    assert dict(calls[0].url.params) == {"id": "10"}
    assert len(calls) == 3


async def test_missing_team_mapping_is_resolved_and_persisted_from_unique_fixture():
    repository = Repository("sqlite:///:memory:")

    def respond(request):
        if request.url.params.get("team"):
            team = int(request.url.params["team"])
            return httpx.Response(
                200,
                json={
                    "errors": [],
                    "response": [fixture(i, team, 3, NOW - timedelta(days=i + 1)) for i in range(3)],
                },
            )
        row = fixture()
        row["league"]["name"] = "Test League"
        row["teams"]["home"]["name"] = "Synthetic Home FC"
        row["teams"]["away"]["name"] = "A.C. Synthetic Away"
        return httpx.Response(200, json={"errors": [], "response": [row]})

    provider = APIFootballProvider(
        APIFootballSettings(competitions={MATCH.competition: {"league": 5, "season": 2026}}),
        fixtures=AsyncMock(get_match=AsyncMock(return_value=MATCH)),
        repository=repository,
        key=SecretStr("test-secret"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
        clock=lambda: NOW,
    )
    provider.executor.interval = 0
    result = await provider.get_stats(MATCH.match_id, NOW)
    assert result.home.team_id == MATCH.home_team_id
    mappings = repository.list_records("identity_mappings", last=20)
    assert {row["kind"] for row in mappings} == {"competition", "team", "match"}
    assert repository.result_identity_mapping("team", MATCH.home_team_id, provider="api_football")[
        "target_id"
    ] == "api-football:team:1"
    assert repository.result_identity_mapping("match", MATCH.match_id, provider="api_football")[
        "target_id"
    ] == "api-football:fixture:10"
    attempt = repository.list_records(
        "identity_resolution_attempts", last=1, entity_id=MATCH.match_id
    )[0]
    assert attempt["result"] == "RESOLVED"


@pytest.mark.parametrize(
    ("source_home", "provider_home", "source_away", "provider_away"),
    [
        ("ASD Alcione Milano", "Alcione", "Trento", "Trento"),
        ("Potenza", "Potenza", "Picerno", "AZ Picerno"),
        ("Audace Cerignola", "Audace Cerignola", "Altamura", "Team Altamura"),
    ],
)
async def test_unique_name_variants_are_resolved_generically_and_persisted(
    source_home, provider_home, source_away, provider_away
):
    repository = Repository("sqlite:///:memory:")
    match = MATCH.model_copy(update={"home_team": source_home, "away_team": source_away})
    row = fixture()
    row["teams"]["home"]["name"] = provider_home
    row["teams"]["away"]["name"] = provider_away
    provider = APIFootballProvider(
        APIFootballSettings(competitions={MATCH.competition: {"league": 5, "season": 2026}}),
        fixtures=AsyncMock(get_match=AsyncMock(return_value=match)),
        repository=repository,
        key=SecretStr("test-secret"),
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"errors": [], "response": [row]})
            )
        ),
        clock=lambda: NOW,
    )
    provider.executor.interval = 0

    _, resolved, _ = await provider._resolve(match.match_id)

    assert resolved["fixture"]["id"] == row["fixture"]["id"]
    mapping = repository.result_identity_mapping("match", match.match_id, provider="api_football")
    assert mapping["verification_status"] == "VERIFIED_MAPPED"
    assert mapping["verification_method"].startswith("AUTO_VERIFIED_NAME_")
    repository.close()


async def test_name_similarity_does_not_resolve_ambiguous_fixture_candidates():
    repository = Repository("sqlite:///:memory:")
    match = MATCH.model_copy(update={"home_team": "ASD Alcione Milano", "away_team": "Trento"})
    rows = [fixture(identifier=10), fixture(identifier=11)]
    for row in rows:
        row["teams"]["home"]["name"] = "Alcione"
        row["teams"]["away"]["name"] = "Trento"
    provider = APIFootballProvider(
        APIFootballSettings(competitions={MATCH.competition: {"league": 5, "season": 2026}}),
        fixtures=AsyncMock(get_match=AsyncMock(return_value=match)),
        repository=repository,
        key=SecretStr("test-secret"),
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"errors": [], "response": rows})
            )
        ),
        clock=lambda: NOW,
    )
    provider.executor.interval = 0

    with pytest.raises(BetterError, match="IDENTITY_MAPPING_AMBIGUOUS"):
        await provider._resolve(match.match_id)
    assert repository.list_records("identity_mappings", last=20) == []
    repository.close()


async def test_unverified_fixture_identity_is_recorded_as_not_found():
    repository = Repository("sqlite:///:memory:")
    provider = APIFootballProvider(
        APIFootballSettings(competitions={MATCH.competition: {"league": 5, "season": 2026}}),
        fixtures=AsyncMock(get_match=AsyncMock(return_value=MATCH)),
        repository=repository,
        key=SecretStr("test-secret"),
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, json={"errors": [], "response": [fixture(home=99, away=98)]}
                )
            )
        ),
        clock=lambda: NOW,
    )
    provider.executor.interval = 0
    with pytest.raises(BetterError, match="IDENTITY_MAPPING_NOT_FOUND"):
        await provider.get_stats(MATCH.match_id, NOW)
    attempt = repository.list_records(
        "identity_resolution_attempts", last=1, entity_id=MATCH.match_id
    )[0]
    assert attempt["result"] == "NOT_FOUND"
    assert repository.list_records("identity_mappings", last=20) == []


async def test_conflicting_persisted_team_identity_is_explicit_and_not_overwritten():
    repository = Repository("sqlite:///:memory:")
    row = fixture()
    row["teams"]["home"]["name"] = MATCH.home_team
    row["teams"]["away"]["name"] = MATCH.away_team
    provider = APIFootballProvider(
        APIFootballSettings(
            competitions={MATCH.competition: {"league": 5, "season": 2026}},
            teams={MATCH.home_team_id: 99, MATCH.away_team_id: 2},
        ),
        fixtures=AsyncMock(get_match=AsyncMock(return_value=MATCH)),
        repository=repository,
        key=SecretStr("test-secret"),
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"errors": [], "response": [row]})
            )
        ),
        clock=lambda: NOW,
    )
    provider.executor.interval = 0
    with pytest.raises(BetterError, match="IDENTITY_MAPPING_CONFLICT"):
        await provider.get_stats(MATCH.match_id, NOW)
    attempt = repository.list_records(
        "identity_resolution_attempts", last=1, entity_id=MATCH.match_id
    )[0]
    assert attempt["result"] == "CONFLICT"
    assert repository.list_records("identity_mappings", last=20) == []


async def test_historical_as_of_does_not_issue_network_request():
    p, calls = make_provider()
    with pytest.raises(BetterError, match="as-of"):
        await p.get_stats(MATCH.match_id, NOW - timedelta(days=10))
    assert calls == []


async def test_lineups_do_not_claim_official_confirmation():
    p, _ = make_provider()
    result = await p.get_lineups(MATCH.match_id, NOW)
    assert len(result.home_players) == 11
    assert result.status == "PROBABLE_LINEUP"
    assert result.provenance[0].source_type == "API"


async def test_lineup_not_published_is_a_precise_missing_state():
    p, _ = make_provider(
        lambda request: httpx.Response(
            200,
            json={"errors": [], "response": [] if request.url.path.endswith("lineups") else [fixture()]},
        )
    )
    with pytest.raises(BetterError, match="LINEUP_NOT_PUBLISHED_YET") as error:
        await p.get_lineups(MATCH.match_id, NOW)
    assert error.value.detail.code == "MISSING_DATA"


@pytest.mark.parametrize("status", [401, 403, 429, 500])
async def test_http_errors_are_typed_and_do_not_expose_body(status):
    p, calls = make_provider(lambda request: httpx.Response(status, text="test-secret"))
    with pytest.raises(BetterError) as error:
        await p.request("status")
    assert "test-secret" not in str(error.value)
    assert len(calls) == 1


async def test_missing_key_and_budget_stop_before_network():
    p, calls = make_provider()
    p._key = SecretStr("")
    with pytest.raises(BetterError, match="assente"):
        await p.request("status")
    p._key = SecretStr("test-secret")
    p._daily_calls = 90
    with pytest.raises(BetterError, match="Budget"):
        await p.request("status")
    assert calls == []


async def test_paginated_results_are_not_silently_truncated():
    p, _ = make_provider(
        lambda request: httpx.Response(
            200, json={"errors": [], "response": [fixture()], "paging": {"total": 2, "current": 1}}
        )
    )
    with pytest.raises(BetterError, match="campione incompleto"):
        await p.request("fixtures", {"league": 5, "season": 2026})


async def test_factory_reuses_one_provider_and_binds_bookmaker_fixtures(tmp_path, monkeypatch):
    from better.config import Settings
    from better.services.application import UnconfiguredProvider
    from better.services.provider_factory import build_providers

    monkeypatch.setenv("API_FOOTBALL_API_KEY", "test-secret")
    path = tmp_path / "sources.yaml"
    path.write_text(
        "providers:\n  fixture: {primary: sport_888}\n  football_stats: {primary: api_football}\n  lineups: {primary: api_football}\n",
        encoding="utf-8",
    )
    bundle = build_providers(Settings(sources_config=str(path)), UnconfiguredProvider())
    try:
        assert bundle.stats is bundle.lineups
        assert bundle.stats.fixtures is bundle.fixtures
        assert bundle.stats.describe()["configured"] is True
        assert bundle.results.providers[0].provider_id == "api_football"
        assert bundle.results.providers[0].transport is bundle.stats
    finally:
        await bundle.stats.close()
        await bundle.fixtures.close()


def test_dashboard_probe_is_explicit_and_reports_plan_block():
    from fastapi.testclient import TestClient

    from better.api.app import create_app
    from better.config import Settings
    from better.providers.base import ProviderBundle
    from better.services.application import UnconfiguredProvider, make_application
    from better.storage.repository import Repository
    from better.system_one import SystemOneSettings

    provider, calls = make_provider()
    missing = UnconfiguredProvider()
    engine = make_application(
        settings=Settings(),
        providers=ProviderBundle(missing, provider, missing, missing, provider),
        repository=Repository("sqlite:///:memory:"),
        system_one_settings=SystemOneSettings(enabled=False),
    )
    with TestClient(create_app(engine)) as client:
        response = client.get("/workspace")
        assert response.json()["api_football"]["configured"] is True
        assert calls == []
        response = client.post("/sources/api-football/check")
        assert response.status_code == 200
        assert response.json()["coverage"]["accessible"] is True
        assert "test-secret" not in response.text
        assert len(calls) == 2
