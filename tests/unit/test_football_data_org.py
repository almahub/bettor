import copy
import hashlib
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import ValidationError

from better.domain.enums import ErrorCode
from better.domain.errors import BetterError
from better.providers.football_data_org import FootballDataOrgFixtureProvider, FootballDataOrgSettings

NOW = datetime(2026, 9, 28, 12, tzinfo=UTC)
MATCH_ID = "football-data:match:123"


async def test_provider_closes_safely_with_or_without_client():
    await FootballDataOrgFixtureProvider().close()
    client = httpx.AsyncClient()
    provider = FootballDataOrgFixtureProvider(client=client)
    await provider.close()
    await provider.close()
    assert client.is_closed


def native_match(**changes):
    """Entirely synthetic data shaped like the documented native v4 response."""
    return {
        "id": 123,
        "utcDate": "2026-09-28T19:00:00Z",
        "lastUpdated": "2026-09-28T10:00:00Z",
        "status": "TIMED",
        "competition": {"id": 42, "name": "Synthetic Cup", "code": "SYN"},
        "season": {"id": 77, "startDate": "2026-08-01", "endDate": "2027-05-31"},
        "homeTeam": {"id": 10, "name": "Synthetic Home"},
        "awayTeam": {"id": 20, "name": "Synthetic Away"},
        "score": {"duration": "REGULAR", "fullTime": {"home": None, "away": None}},
        **changes,
    }


def settings(**changes):
    return FootballDataOrgSettings(api_key="synthetic-test-key", requests_per_minute=60000, **changes)


async def test_native_fixture_mapping_auth_and_provenance_are_real_contract():
    stored = []
    payload = native_match()

    def handler(request):
        assert str(request.url) == "https://api.football-data.org/v4/matches/123"
        assert request.headers["X-Auth-Token"] == "synthetic-test-key"
        return httpx.Response(
            200,
            json=payload,
            headers={
                "X-API-Version": "v4",
                "X-RequestsAvailable": "9",
                "X-Authenticated-Client": "private-client-name",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = FootballDataOrgFixtureProvider(
            settings(),
            client=client,
            clock=lambda: NOW,
            on_record=lambda record, raw: stored.append((record, raw)),
        )
        match = await provider.get_match(MATCH_ID)
        record = provider.latest_record(MATCH_ID)
        assert match.match_id == MATCH_ID
        assert match.competition == "football-data:competition:42"
        assert match.season == "football-data:season:77"
        assert match.home_team_id == "football-data:team:10"
        assert match.away_team_id == "football-data:team:20"
        assert match.kickoff.hour == 19
        assert record.provenance.retrieved_at == NOW
        assert record.provenance.published_at == datetime(2026, 9, 28, 10, tzinfo=UTC)
        assert record.response_headers == {"x-api-version": "v4", "x-requestsavailable": "9"}
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        assert record.raw_hash == digest
        assert record.provenance.raw_reference.endswith(digest)
        assert stored[0][1] == payload
        raw = provider.raw_record(digest)
        raw["homeTeam"]["name"] = "mutated"
        assert provider.raw_record(digest)["homeTeam"]["name"] == "Synthetic Home"
        assert "synthetic-test-key" not in record.model_dump_json()
        assert "private-client-name" not in record.model_dump_json()


async def test_daily_filter_has_exclusive_date_to_and_utc_conversion():
    as_of = datetime.fromisoformat("2026-09-29T00:30:00+02:00")
    payload = native_match(utcDate="2026-09-28T23:00:00Z")
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.params["dateFrom"] == "2026-09-28"
        assert request.url.params["dateTo"] == "2026-09-29"
        return httpx.Response(200, json={"matches": [payload]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = FootballDataOrgFixtureProvider(settings(), client=client, clock=lambda: NOW)
        result = await provider.list_matches(as_of)
        assert len(result) == 1
        assert await provider.get_match(MATCH_ID) == result[0]
        assert len(requests) == 1  # listing populates the same timestamp-preserving fixture cache
        assert (
            provider.latest_record(MATCH_ID).retrieved_at == NOW
        )  # never uses caller as_of as retrieval time


async def test_cache_does_not_renew_original_timestamp_and_expires():
    clock = [NOW]
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=native_match())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = FootballDataOrgFixtureProvider(
            settings(cache_ttl_seconds=60), client=client, clock=lambda: clock[0]
        )
        original = await provider.get_fixture_record(MATCH_ID)
        clock[0] += timedelta(seconds=30)
        cached = await provider.get_fixture_record(MATCH_ID)
        assert cached.retrieved_at == original.retrieved_at == NOW
        assert calls == 1
        clock[0] += timedelta(seconds=31)
        refreshed = await provider.get_fixture_record(MATCH_ID)
        assert refreshed.retrieved_at == clock[0]
        assert calls == 2


@pytest.mark.parametrize(
    "match_id",
    [
        "123",
        "football-data:match:0",
        "football-data:match:../secret",
        "other:match:123",
        "football-data:match:12?token=x",
    ],
)
async def test_noncanonical_or_unsafe_ids_rejected_before_network(match_id):
    provider = FootballDataOrgFixtureProvider(settings(), clock=lambda: NOW)
    with pytest.raises(BetterError) as error:
        await provider.get_match(match_id)
    assert error.value.detail.code == ErrorCode.CONFIGURATION_ERROR


async def test_missing_api_key_is_explicit_and_never_uses_network(monkeypatch):
    monkeypatch.delenv("FOOTBALL_DATA_ORG_API_KEY", raising=False)
    provider = FootballDataOrgFixtureProvider(FootballDataOrgSettings(api_key=""), clock=lambda: NOW)
    with pytest.raises(BetterError, match="API_KEY"):
        await provider.get_match(MATCH_ID)


def test_environment_secrets_and_url_validation(monkeypatch):
    monkeypatch.setenv("FOOTBALL_DATA_ORG_API_KEY", "example-secret")
    monkeypatch.setenv("FOOTBALL_DATA_ORG_MAX_RETRIES", "2")
    config = FootballDataOrgSettings.from_env()
    assert config.max_retries == 2
    assert "example-secret" not in repr(config)
    with pytest.raises(ValidationError):
        FootballDataOrgSettings(base_url="https://user:password@example.com/v4")


@pytest.mark.parametrize(
    "status,expected",
    [
        (401, ErrorCode.PERMANENT_PROVIDER_ERROR),
        (403, ErrorCode.PERMANENT_PROVIDER_ERROR),
        (404, ErrorCode.MISSING_DATA),
        (429, ErrorCode.RATE_LIMIT),
        (503, ErrorCode.TRANSIENT_PROVIDER_ERROR),
    ],
)
async def test_http_errors_are_typed_and_do_not_expose_body(status, expected):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(status, text="private response body synthetic-test-key")
        )
    ) as client:
        provider = FootballDataOrgFixtureProvider(settings(max_retries=0), client=client, clock=lambda: NOW)
        with pytest.raises(BetterError) as caught:
            await provider.get_match(MATCH_ID)
    assert caught.value.detail.code == expected
    assert "private" not in str(caught.value)
    assert "synthetic-test-key" not in str(caught.value)


@pytest.mark.parametrize(
    "error_class,code",
    [(httpx.ConnectError, ErrorCode.TRANSIENT_PROVIDER_ERROR), (httpx.ReadTimeout, ErrorCode.TIMEOUT)],
)
async def test_network_failures_use_shared_executor(error_class, code):
    def handler(request):
        raise error_class("unavailable", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = FootballDataOrgFixtureProvider(settings(max_retries=0), client=client, clock=lambda: NOW)
        with pytest.raises(BetterError) as caught:
            await provider.get_match(MATCH_ID)
        assert caught.value.detail.code == code


async def test_transient_retry_and_rate_limit_cooldown():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(503) if calls == 1 else httpx.Response(200, json=native_match())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = FootballDataOrgFixtureProvider(settings(), client=client, clock=lambda: NOW)
        assert (await provider.get_match(MATCH_ID)).match_id == MATCH_ID
        assert calls == 2
    calls = 0

    def limited(request):
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"X-RequestCounter-Reset": "30"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(limited)) as client:
        provider = FootballDataOrgFixtureProvider(settings(), client=client, clock=lambda: NOW)
        with pytest.raises(BetterError) as caught:
            await provider.get_match(MATCH_ID)
        assert caught.value.detail.code == ErrorCode.RATE_LIMIT
        assert calls == 1  # retries respect upstream reset instead of hitting the server again


@pytest.mark.parametrize(
    "changes",
    [
        {"id": "123"},
        {"id": 999},
        {"utcDate": "2026-09-28T19:00:00"},
        {"lastUpdated": "2026-09-28T13:00:00Z"},
        {"status": "UNRECOGNIZED"},
        {"homeTeam": {"id": True, "name": "Bad ID"}},
    ],
)
async def test_invalid_native_contract_is_not_cached(changes):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=native_match(**changes)))
    ) as client:
        provider = FootballDataOrgFixtureProvider(settings(), client=client, clock=lambda: NOW)
        with pytest.raises(BetterError) as caught:
            await provider.get_match(MATCH_ID)
        assert caught.value.detail.code == ErrorCode.SCHEMA_ERROR
        assert provider.latest_record(MATCH_ID) is None


async def test_results_separate_from_prematch_and_regular_time_not_inferred():
    payload = native_match(
        status="FINISHED",
        score={
            "duration": "EXTRA_TIME",
            "fullTime": {"home": 2, "away": 1},
            "regularTime": None,
        },
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    ) as client:
        provider = FootballDataOrgFixtureProvider(settings(), client=client, clock=lambda: NOW)
        record = await provider.get_fixture_record(MATCH_ID)
        assert record.full_time_home == 2 and record.full_time_away == 1
        assert record.regular_time_home is None
        assert "score" not in record.match.model_dump()
        with pytest.raises(BetterError) as caught:
            await provider.get_match(MATCH_ID)
        assert caught.value.detail.code == ErrorCode.MISSING_DATA


async def test_unknown_opponents_not_invented_and_finished_excluded_from_fixture_list():
    unresolved = native_match(id=124, homeTeam={"id": None, "name": None})
    finished = native_match(id=125, status="FINISHED")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"matches": [native_match(), unresolved, finished]})
        )
    ) as client:
        provider = FootballDataOrgFixtureProvider(settings(), client=client, clock=lambda: NOW)
        matches = await provider.list_matches(NOW)
        assert [match.match_id for match in matches] == [MATCH_ID]
        assert provider.skipped_unresolved_ids == [124]


@pytest.mark.parametrize(
    "items",
    [
        [native_match(), native_match()],
        [native_match(), native_match(id=124, utcDate="2026-09-29T19:00:00Z")],
        [native_match(homeTeam={"id": None, "name": None}), native_match()],
    ],
)
async def test_bad_list_cannot_partially_populate_valid_cache(items):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"matches": copy.deepcopy(items)})
        )
    ) as client:
        provider = FootballDataOrgFixtureProvider(settings(), client=client, clock=lambda: NOW)
        with pytest.raises(BetterError) as caught:
            await provider.list_matches(NOW)
        assert caught.value.detail.code == ErrorCode.SCHEMA_ERROR
        assert provider.latest_record(MATCH_ID) is None
