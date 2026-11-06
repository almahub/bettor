"""Synthetic native-shaped fixtures, never represented as live bookmaker data."""

import copy
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path

import httpx
import pytest

from better.config import Settings
from better.domain.errors import BetterError
from better.domain.schemas import AnalysisRequest
from better.orchestration.orchestrator import Orchestrator
from better.providers.base import ProviderBundle
from better.providers.resilience import ProviderExecutor
from better.providers.sport_888 import CATALOG_PATH, Sport888Provider, Sport888Settings
from better.services.application import UnconfiguredProvider
from better.services.provider_factory import SourceConfiguration, build_providers
from better.storage.repository import Repository
from better.system_one import ShadowCoordinator, SystemOneSettings

NOW = datetime(2026, 10, 1, 12, tzinfo=UTC)
MATCH_ID = "888it:match:123"


def event():
    return {
        "id": 123,
        "sport": "FOOTBALL",
        "state": "NOT_STARTED",
        "tags": ["MATCH"],
        "start": (NOW + timedelta(hours=2)).isoformat(),
        "group": "Synthetic native fixture league",
        "groupId": 5,
        "homeName": "Fixture Home",
        "awayName": "Fixture Away",
        "participants": [
            {"participantId": 10, "name": "Fixture Home", "home": True, "participantType": "TEAM"},
            {"participantId": 20, "name": "Fixture Away", "home": False, "participantType": "TEAM"},
        ],
    }


def offer(criterion, kind, types, line=None):
    return {
        "id": criterion,
        "eventId": 123,
        "criterion": {"id": criterion},
        "betOfferType": {"id": kind},
        "tags": ["OFFERED_PREMATCH"],
        "outcomes": [
            {
                "id": 100 + i,
                "type": t,
                "odds": 2100 + i * 100,
                "status": "OPEN",
                "changedDate": (NOW - timedelta(minutes=20)).isoformat(),
                **({"line": line} if line else {}),
            }
            for i, t in enumerate(types)
        ],
    }


def payload():
    return {
        "events": [event()],
        "betOffers": [
            offer(1001159858, 2, ["OT_ONE", "OT_CROSS", "OT_TWO"]),
            offer(1001159926, 6, ["OT_OVER", "OT_UNDER"], 2500),
            offer(1001642858, 18, ["OT_YES", "OT_NO"]),
        ],
    }


def provider(data=None, *, status=200, age=0):
    data = data or payload()
    calls, clock = [], [NOW]

    def handle(request):
        calls.append(request)
        assert request.method == "GET"
        assert request.url.host == "eu-offering-api.kambicdn.com"
        body = {"events": data["events"]} if request.url.path.endswith(CATALOG_PATH) else data
        return httpx.Response(
            status,
            json=copy.deepcopy(body),
            headers={
                "date": format_datetime(NOW, usegmt=True),
                "age": str(age),
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    result = Sport888Provider(client=client, clock=lambda: clock[0])
    result.executor = ProviderExecutor(retries=0, rate_per_second=10000)
    return result, calls, clock


async def test_catalogue_is_light_and_only_selected_event_requests_odds():
    p, calls, _ = provider()
    matches = await p.list_matches(NOW)
    assert len(matches) == 1
    assert matches[0].competition == "888it:competition:5"
    assert matches[0].home_team_id == "888it:team:10"
    assert matches[0].season == "unknown"
    assert p.competition_names() == {"888it:competition:5": "Synthetic native fixture league"}
    assert len(calls) == 1 and "/betoffer/" not in str(calls[0].url)
    assert await p.get_match(MATCH_ID) == matches[0]
    assert len(calls) == 1
    odds = await p.get_odds(MATCH_ID, NOW)
    assert len(calls) == 2
    assert calls[-1].url.path.endswith("/betoffer/event/123")
    assert len(odds.quotes) == 7
    assert odds.quotes[0].odds == 2.1
    assert all(q.liquidity is None and q.bookmaker == "888sport.it" for q in odds.quotes)
    assert all(
        q.provenance.source_id == "888sport_it" and "sha256:" in q.provenance.raw_reference
        for q in odds.quotes
    )
    assert {q.market.type for q in odds.quotes} == {"1X2", "OVER_UNDER_2_5", "BTTS"}


async def test_catalogue_can_enforce_italian_league_allowlist():
    data = payload()
    italian = data["events"][0]
    italian["group"] = "Serie C - Girone B"
    foreign = copy.deepcopy(italian)
    foreign["id"] = 124
    foreign["group"] = "Premier League"
    data["events"].append(foreign)
    p, _, _ = provider(data)
    p.settings = Sport888Settings(
        allowed_competition_patterns=(
            r"^Serie A$",
            r"^Serie B$",
            r"^Serie C(?:\s*[-–—:]?\s*)Girone\s+[A-C]$",
        )
    )
    matches = await p.list_matches(NOW)
    assert [match.match_id for match in matches] == [MATCH_ID]
    assert p.competition_names() == {"888it:competition:5": "Serie C - Girone B"}


async def test_catalogue_rejects_foreign_fixture_with_misleading_italian_label():
    data = payload()
    italian = data["events"][0]
    italian["group"] = "Serie C - Girone B"
    italian["groupId"] = 2000079608
    foreign = copy.deepcopy(italian)
    foreign["id"] = 124
    foreign["group"] = "Serie B"
    foreign["groupId"] = 2000063156
    foreign["homeName"] = "Chattogram City FC"
    foreign["awayName"] = "Bangladesh Police"
    foreign["participants"][0].update({"participantId": 30, "name": "Chattogram City FC"})
    foreign["participants"][1].update({"participantId": 40, "name": "Bangladesh Police"})
    data["events"].append(foreign)
    p, _, _ = provider(data)
    p.settings = Sport888Settings(
        allowed_competition_patterns=(r"^Serie B$", r"^Serie C.*Girone B$"),
        allowed_competitions={2000079608: r"^Serie C.*Girone B$"},
    )

    matches = await p.list_matches(NOW)

    assert [match.match_id for match in matches] == [MATCH_ID]
    assert matches[0].competition == "888it:competition:2000079608"
    assert p.catalog_metadata["competition_bindings"] == {2000079608: r"^Serie C.*Girone B$"}


async def test_operational_catalogue_admits_verified_serie_a_b_groups_only():
    config = SourceConfiguration.load(Path(__file__).parents[2] / "config/888/sources.yaml")
    source = event()
    fixtures = []
    for identifier, group_id, group in (
        (123, 1000095001, "Serie A"),
        (124, 1000095002, "Serie B"),
        (125, 2000063156, "Serie B"),
    ):
        row = copy.deepcopy(source)
        row.update(id=identifier, groupId=group_id, group=group)
        fixtures.append(row)
    p, _, _ = provider({"events": fixtures})
    p.settings = config.sport_888

    matches = await p.list_matches(NOW)

    assert {match.match_id for match in matches} == {
        "888it:match:123",
        "888it:match:124",
    }
    assert config.api_football.competitions["888it:competition:1000095001"].league == 135
    assert config.api_football.competitions["888it:competition:1000095002"].league == 136


async def test_http_age_and_cache_never_rejuvenate_quotes():
    p, calls, clock = provider(age=200)
    first = await p.get_odds(MATCH_ID, NOW)
    assert first.quotes[0].captured_at == NOW - timedelta(seconds=200)
    clock[0] += timedelta(seconds=10)
    second = await p.get_odds(MATCH_ID, clock[0])
    assert first == second and len(calls) == 1
    clock[0] += timedelta(seconds=6)
    refreshed = await p.get_odds(MATCH_ID, clock[0])
    assert len(calls) == 2
    assert refreshed.quotes[0].captured_at < NOW


@pytest.mark.parametrize("mutation", ["live", "past", "outright", "no_team_ids", "wrong_side"])
async def test_ineligible_or_unresolved_events_are_not_selectable(mutation):
    d = payload()
    e = d["events"][0]
    if mutation == "live":
        e["state"] = "STARTED"
    if mutation == "past":
        e["start"] = (NOW - timedelta(minutes=1)).isoformat()
    if mutation == "outright":
        e["tags"] = ["OUTRIGHT"]
    if mutation == "no_team_ids":
        e["participants"] = []
    if mutation == "wrong_side":
        e["participants"][0]["name"] = "Another team"
    p, _, _ = provider(d)
    assert await p.list_matches(NOW) == ()
    with pytest.raises(BetterError):
        await p.get_match(MATCH_ID)


@pytest.mark.parametrize("mutation", ["suspended", "partial", "half", "other_line"])
async def test_unsupported_or_incomplete_market_is_not_mapped(mutation):
    d = payload()
    target = d["betOffers"][1]
    if mutation == "suspended":
        target["outcomes"][0]["status"] = "SUSPENDED"
    if mutation == "partial":
        target["outcomes"].pop()
    if mutation == "half":
        target["criterion"]["id"] = 1001159532
    if mutation == "other_line":
        for o in target["outcomes"]:
            o["line"] = 3500
    p, _, _ = provider(d)
    odds = await p.get_odds(MATCH_ID, NOW)
    assert {q.market.type for q in odds.quotes} == {"1X2", "BTTS"}


@pytest.mark.parametrize("status", [403, 429, 500])
async def test_http_failure_is_explicit_without_fake_fallback(status):
    p, calls, _ = provider(status=status)
    with pytest.raises(BetterError, match=f"HTTP {status}"):
        await p.list_matches(NOW)
    assert len(calls) == 1


async def test_duplicate_markets_and_stale_catalogue_fail_closed():
    d = payload()
    d["betOffers"].append(copy.deepcopy(d["betOffers"][0]))
    p, _, _ = provider(d)
    with pytest.raises(BetterError, match="ambiguous"):
        await p.get_odds(MATCH_ID, NOW)
    p, _, _ = provider(age=601)
    with pytest.raises(BetterError, match="stale"):
        await p.list_matches(NOW)


async def test_only_fixture_and_odds_do_not_authorize_bet():
    p, calls, _ = provider()
    missing = UnconfiguredProvider()
    engine = Orchestrator(
        ProviderBundle(p, missing, p, missing, missing),
        Settings(),
        Repository("sqlite:///:memory:"),
        shadow=ShadowCoordinator(SystemOneSettings(enabled=False)),
    )
    run = await engine.analyze(AnalysisRequest(match_id=MATCH_ID, as_of=NOW))
    assert run.decision.status == "INSUFFICIENT_DATA"
    assert run.decision.stake.amount == 0
    assert len(calls) == 2
    assert run.results["football_odds_analyst"].status == "OK"
    assert run.input_manifest["fixture_record"]["provider"] == "888sport_it"
    assert not any("synthetic-fixture" in p.source_id for r in run.results.values() for p in r.provenance)


def test_cannot_configure_bookmaker_as_statistics_source(tmp_path):
    path = tmp_path / "sources.yaml"
    path.write_text("providers:\n  football_stats: {primary: sport_888}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="fixtures and odds only"):
        build_providers(Settings(sources_config=str(path)), UnconfiguredProvider())


async def test_selected_fixture_identity_cannot_change_during_quote_collection():
    data = payload()
    p, _, _ = provider(data)
    await p.list_matches(NOW)
    data["events"][0]["participants"][0]["participantId"] = 99
    with pytest.raises(BetterError, match="ambiguous"):
        await p.get_odds(MATCH_ID, NOW)


async def test_foreign_outcome_participant_and_future_change_time_are_rejected():
    for update in ({"participantId": 999}, {"changedDate": (NOW + timedelta(seconds=5)).isoformat()}):
        data = payload()
        data["betOffers"][0]["outcomes"][0].update(update)
        p, _, _ = provider(data)
        with pytest.raises(BetterError, match="ambiguous"):
            await p.get_odds(MATCH_ID, NOW)
