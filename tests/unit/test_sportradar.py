from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import SecretStr

from better.domain.errors import BetterError
from better.domain.models import Match
from better.providers.sportradar import SportradarSettings, SportradarSoccerProvider, read_api_key

NOW = datetime(2026, 9, 29, 10, tzinfo=UTC)
MATCH = Match(
    match_id="888it:match:1",
    competition="888it:competition:5",
    season="unknown",
    kickoff=NOW + timedelta(hours=8),
    home_team="Home FC",
    away_team="Away FC",
    home_team_id="888it:team:1",
    away_team_id="888it:team:2",
)


def event(identifier="99", start=None, home="1", away="2", coverage=None):
    return {
        "id": f"sr:sport_event:{identifier}",
        "start_time": (start or MATCH.kickoff).isoformat(),
        "competitors": [
            {"id": f"sr:competitor:{home}", "name": "Home FC", "qualifier": "home"},
            {"id": f"sr:competitor:{away}", "name": "Away FC", "qualifier": "away"},
        ],
        "sport_event_context": {"competition": {"id": "sr:competition:5", "name": "League"}},
        "coverage": {"sport_event_properties": coverage or {}},
    }


def provider(handler=None):
    calls = []

    def respond(request):
        calls.append(request)
        assert request.headers["x-api-key"] == "secret"
        if handler:
            return handler(request)
        path = request.url.path
        if path.endswith("competitions.json"):
            body = {"competitions": [{"id": "sr:competition:5", "name": "League"}]}
        elif path.endswith("lineups.json"):
            body = {
                "sport_event": {"sport_event_conditions": {"lineups": {"confirmed": True}}},
                "lineups": [
                    {
                        "competitor": {"id": f"sr:competitor:{team}"},
                        "players": [{"name": f"P{team}-{i}", "starter": True} for i in range(11)],
                    }
                    for team in (1, 2)
                ],
            }
        elif "/competitors/" in path:
            team = "1" if "competitor:1" in path else "2"
            summaries = []
            for index in range(10):
                row = event(str(index), NOW - timedelta(days=index + 1), home=team, away="3")
                summaries.append(
                    {
                        "sport_event": row,
                        "sport_event_status": {
                            "status": "closed",
                            "home_normaltime_score": 2,
                            "away_normaltime_score": 1,
                        },
                    }
                )
            body = {"summaries": summaries}
        else:
            body = {
                "summaries": [
                    {
                        "sport_event": event(coverage={"lineups": True, "lineups_availability": "pre"}),
                        "sport_event_status": {"status": "not_started"},
                    }
                ]
            }
        return httpx.Response(200, json=body)

    p = SportradarSoccerProvider(
        SportradarSettings(monthly_call_budget=20),
        fixtures=AsyncMock(get_match=AsyncMock(return_value=MATCH)),
        key=SecretStr("secret"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
        clock=lambda: NOW,
    )
    p.executor.interval = 0
    return p, calls


async def test_regulation_scores_can_be_derived_from_two_regular_periods():
    def periods(request):
        if "/competitors/" not in request.url.path:
            return httpx.Response(
                200, json={"summaries": [{"sport_event": event(), "sport_event_status": {}}]}
            )
        rows = []
        for index in range(3):
            rows.append(
                {
                    "sport_event": event(
                        str(index),
                        NOW - timedelta(days=index + 1),
                        home="1" if "competitor:1" in request.url.path else "2",
                        away="3",
                    ),
                    "sport_event_status": {
                        "status": "closed",
                        "home_score": 9,
                        "away_score": 8,
                        "period_scores": [
                            {"type": "regular_period", "number": 1, "home_score": 1, "away_score": 0},
                            {"type": "regular_period", "number": 2, "home_score": 1, "away_score": 1},
                            {"type": "overtime", "number": 3, "home_score": 7, "away_score": 7},
                        ],
                    },
                }
            )
        return httpx.Response(200, json={"summaries": rows})

    p, _ = provider(periods)
    stats = await p.get_stats(MATCH.match_id, NOW)
    assert stats.home.goals_for_per_match == 2
    assert stats.home.goals_against_per_match == 1


def test_key_reader_is_secret_and_does_not_interpolate(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text('SPORTRADAR_API_KEY="file-key"\nOTHER=$(danger)\n', encoding="utf-8")
    monkeypatch.delenv("SPORTRADAR_API_KEY", raising=False)
    assert read_api_key(path).get_secret_value() == "file-key"
    assert "file-key" not in repr(read_api_key(path))


async def test_access_cache_and_stats_are_typed():
    p, calls = provider()
    report = await p.check_access()
    assert report["coverage"] == {"accessible": True, "competition_count": 1}
    await p.check_access()
    assert len(calls) == 1
    stats = await p.get_stats(MATCH.match_id, NOW)
    assert stats.home.sample_size == 10 and stats.home.goals_for_per_match == 2
    assert stats.away.goals_against_per_match == 1
    assert "last_10" in stats.home.segments and "opponent_strength" not in stats.home.segments
    assert all(x.source_id == "sportradar_soccer" for x in stats.provenance)


async def test_lineups_require_declared_pre_match_coverage_and_remain_probable():
    p, _ = provider()
    lineup = await p.get_lineups(MATCH.match_id, NOW)
    assert len(lineup.home_players) == 11
    assert lineup.status == "PROBABLE_LINEUP"

    def no_coverage(request):
        return httpx.Response(200, json={"summaries": [{"sport_event": event(), "sport_event_status": {}}]})

    blocked, calls = provider(no_coverage)
    with pytest.raises(BetterError, match="non coperte"):
        await blocked.get_lineups(MATCH.match_id, NOW)
    assert len(calls) == 1


async def test_identity_mismatch_and_historical_as_of_fail_before_unsafe_use():
    def mismatch(request):
        wrong = event(home="8")
        wrong["competitors"][0]["name"] = "Different Home"
        return httpx.Response(200, json={"summaries": [{"sport_event": wrong, "sport_event_status": {}}]})

    p, _ = provider(mismatch)
    with pytest.raises(BetterError, match="univoca"):
        await p.get_stats(MATCH.match_id, NOW)
    p, calls = provider()
    with pytest.raises(BetterError, match="as-of"):
        await p.get_stats(MATCH.match_id, NOW - timedelta(days=2))
    assert calls == []


async def test_explicit_native_ids_allow_small_bookmaker_kickoff_shift():
    p, _ = provider()
    p.settings.teams.update({MATCH.home_team_id: "sr:competitor:1", MATCH.away_team_id: "sr:competitor:2"})
    p.settings.competitions[MATCH.competition] = "sr:competition:5"

    original = p.client

    def shifted(request):
        if "/schedules/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "summaries": [
                        {
                            "sport_event": event(start=MATCH.kickoff - timedelta(minutes=2)),
                            "sport_event_status": {},
                        }
                    ]
                },
            )
        return httpx.Response(200, json={"summaries": []})

    await original.aclose()
    p.client = httpx.AsyncClient(transport=httpx.MockTransport(shifted))
    _, row, _ = await p._resolve(MATCH.match_id)
    assert row["sport_event"]["id"] == "sr:sport_event:99"


@pytest.mark.parametrize("status", [401, 403, 429, 500])
async def test_http_errors_are_typed_without_body_or_key(status):
    p, _ = provider(lambda request: httpx.Response(status, text="secret upstream"))
    with pytest.raises(BetterError) as error:
        await p.check_access()
    assert "secret" not in str(error.value)
