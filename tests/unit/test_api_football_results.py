from datetime import UTC, datetime

import httpx
import pytest
from pydantic import SecretStr

from better.domain.models import Match
from better.providers.api_football import APIFootballProvider, APIFootballSettings
from better.providers.api_football_results import APIFootballResultProvider
from better.providers.result_identity import (
    DeterministicResultIdentityResolver,
    ResultIdentityStatus,
    ResultProviderIdentity,
)
from better.storage.repository import Repository

NOW = datetime(2026, 10, 3, 12, tzinfo=UTC)
MATCH = Match(
    match_id="888it:match:generic",
    competition="888it:competition:generic",
    season="2026/27",
    kickoff=datetime(2026, 10, 3, 18, 30, tzinfo=UTC),
    home_team="Source Home label",
    away_team="Source Away label",
    home_team_id="888it:team:home",
    away_team_id="888it:team:away",
)


def native_fixture(status="NS", *, fulltime=(None, None), kickoff=MATCH.kickoff):
    return {
        "fixture": {
            "id": 700001,
            "date": kickoff.isoformat(),
            "status": {"short": status},
        },
        "league": {"id": 777, "season": 2026, "name": "Example League", "round": "Round 8"},
        "teams": {
            "home": {"id": 101, "name": "Provider Home"},
            "away": {"id": 202, "name": "Provider Away"},
        },
        "score": {
            "fulltime": {"home": fulltime[0], "away": fulltime[1]},
            "extratime": {"home": None, "away": None},
            "penalty": {"home": None, "away": None},
        },
    }


def make_provider(row=None):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={"errors": [], "response": [row or native_fixture()]})

    transport = APIFootballProvider(
        APIFootballSettings(
            competitions={MATCH.competition: {"league": 777, "season": 2026}},
            teams={MATCH.home_team_id: 101, MATCH.away_team_id: 202},
        ),
        key=SecretStr("secret"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
        clock=lambda: NOW,
    )
    transport.executor.interval = 0
    return APIFootballResultProvider(transport), calls


def identity(**changes):
    values = {
        "source_match_id": MATCH.match_id,
        "provider_match_id": "api-football:fixture:700001",
        "competition_id": "api-football:league:777:season:2026",
        "home_team_id": "api-football:team:101",
        "away_team_id": "api-football:team:202",
        "kickoff": MATCH.kickoff,
        "provider": "api_football",
        "source_competition_id": MATCH.competition,
        "source_home_team_id": MATCH.home_team_id,
        "source_away_team_id": MATCH.away_team_id,
        "home_team_name": "Provider Home",
        "away_team_name": "Provider Away",
        "resolution_method": "VERIFIED_TEAM_MAPPING",
        "confidence": 1,
        "verified_at": NOW,
        "source_url": "https://v3.football.api-sports.io/fixtures?id=700001",
    }
    values.update(changes)
    return ResultProviderIdentity(**values)


async def test_configured_competition_and_team_mappings_resolve_and_persist():
    provider, calls = make_provider()
    repository = Repository("sqlite:///:memory:")
    resolver = DeterministicResultIdentityResolver(repository, provider)
    result = await resolver.resolve(MATCH, NOW)
    assert result.status == ResultIdentityStatus.VERIFIED_MAPPED
    assert result.identity == identity(source_url=result.identity.source_url)
    assert {row["kind"] for row in repository.list_records("identity_mappings", last=20)} == {
        "competition",
        "team",
        "match",
    }
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("NS", "PENDING"),
        ("TBD", "PENDING"),
        ("1H", "PENDING"),
        ("HT", "PENDING"),
        ("2H", "PENDING"),
        ("ET", "PENDING"),
        ("BT", "PENDING"),
        ("P", "PENDING"),
        ("SUSP", "PENDING"),
        ("INT", "PENDING"),
        ("PST", "PENDING"),
        ("CANC", "UNVERIFIED"),
        ("ABD", "UNVERIFIED"),
        ("AWD", "UNVERIFIED"),
        ("WO", "UNVERIFIED"),
    ],
)
async def test_non_final_statuses_never_auto_settle(status, expected):
    provider, _ = make_provider(native_fixture(status))
    result = await provider.get_result(identity(), NOW)
    assert result.final_status == expected
    assert result.home_score is None and result.away_score is None


@pytest.mark.parametrize("status", ["FT", "AET", "PEN"])
async def test_final_statuses_use_regulation_score(status):
    row = native_fixture(status, fulltime=(1, 1))
    row["score"]["extratime"] = {"home": 2, "away": 1}
    row["score"]["penalty"] = {"home": 5, "away": 4}
    provider, _ = make_provider(row)
    result = await provider.get_result(identity(), NOW)
    assert result.final_status == "FINAL"
    assert (result.home_score, result.away_score) == (1, 1)
    assert (result.extra_time_home, result.extra_time_away) == (2, 1)
    assert (result.penalties_home, result.penalties_away) == (5, 4)


async def test_final_without_regulation_score_is_unverified():
    provider, _ = make_provider(native_fixture("AET"))
    result = await provider.get_result(identity(), NOW)
    assert result.final_status == "UNVERIFIED"


@pytest.mark.parametrize(
    "changed_identity",
    [
        {"competition_id": "api-football:league:138:season:2026"},
        {"home_team_id": "api-football:team:999"},
        {"away_team_id": "api-football:team:999"},
        {"kickoff": datetime(2026, 10, 3, 19, tzinfo=UTC)},
    ],
)
async def test_result_identity_mismatch_is_unverified(changed_identity):
    provider, _ = make_provider(native_fixture("FT", fulltime=(2, 0)))
    result = await provider.get_result(identity(**changed_identity), NOW)
    assert result.final_status == "UNVERIFIED"
    assert result.provider_status == "IDENTITY_MISMATCH"


async def test_result_request_uses_transport_cache_and_counter():
    provider, calls = make_provider(native_fixture("FT", fulltime=(2, 0)))
    first = await provider.get_result(identity(), NOW)
    second = await provider.get_result(identity(), NOW)
    assert first == second
    assert len(calls) == 1
    assert provider.transport.describe()["requests_made_today"] == 1
