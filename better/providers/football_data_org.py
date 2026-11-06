"""football-data.org v4 fixtures/results; no inferred statistics, odds or lineups.

Official contract reviewed 2026-09-28. The v4 dateTo filter is exclusive.
Current API representations are never presented as historical as-of snapshots.
"""

import copy
import hashlib
import json
import os
import re
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal, Self
from urllib.parse import urlsplit

import httpx
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StrictInt,
    ValidationError,
    model_validator,
)

from better.domain.enums import ErrorCode, ReliabilityTier, SourceType
from better.domain.errors import BetterError
from better.domain.models import Match
from better.domain.provenance import Provenance
from better.providers.resilience import CacheEntry, ProviderExecutor, SnapshotCache

PROVIDER_ID = "football_data_org"
MATCH_ID_PREFIX = "football-data:match:"


class FootballDataOrgSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    api_key: SecretStr = SecretStr("")
    base_url: str = "https://api.football-data.org/v4"
    timeout_seconds: float = Field(default=5, gt=0)
    max_retries: int = Field(default=1, ge=0, le=5)
    requests_per_minute: float = Field(default=10, gt=0)
    cache_ttl_seconds: float = Field(default=60, ge=0)

    @model_validator(mode="after")
    def valid_url(self) -> Self:
        url = urlsplit(self.base_url)
        if (
            url.scheme not in ("http", "https")
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError("Provider URL must be HTTP(S), without credentials, query or fragment")
        return self

    @classmethod
    def from_env(cls) -> Self:
        from better.services.local_environment import environment_value
        values = {
                field: os.environ[f"FOOTBALL_DATA_ORG_{field.upper()}"]
                for field in cls.model_fields
                if f"FOOTBALL_DATA_ORG_{field.upper()}" in os.environ
            }
        if "api_key" not in values and environment_value("FOOTBALL_DATA_ORG_API_KEY"):
            values["api_key"] = environment_value("FOOTBALL_DATA_ORG_API_KEY")
        return cls.model_validate(values)


class _NativeModel(BaseModel):
    model_config = ConfigDict(extra="ignore", allow_inf_nan=False)


class _Team(_NativeModel):
    id: StrictInt | None
    name: str | None
    shortName: str | None = None


class _Competition(_NativeModel):
    id: StrictInt = Field(gt=0)
    name: str = Field(min_length=1)
    code: str | None = None


class _Season(_NativeModel):
    id: StrictInt = Field(gt=0)
    startDate: date
    endDate: date | None = None


class _ScorePair(_NativeModel):
    home: StrictInt | None = Field(default=None, ge=0)
    away: StrictInt | None = Field(default=None, ge=0)


class _Score(_NativeModel):
    duration: Literal["REGULAR", "EXTRA_TIME", "PENALTY_SHOOTOUT"] | None = None
    fullTime: _ScorePair | None = None
    regularTime: _ScorePair | None = None
    extraTime: _ScorePair | None = None
    penalties: _ScorePair | None = None


class _NativeMatch(_NativeModel):
    id: StrictInt = Field(gt=0)
    utcDate: AwareDatetime
    status: Literal[
        "SCHEDULED",
        "TIMED",
        "IN_PLAY",
        "PAUSED",
        "EXTRA_TIME",
        "PENALTY_SHOOTOUT",
        "FINISHED",
        "SUSPENDED",
        "POSTPONED",
        "CANCELLED",
        "AWARDED",
    ]
    lastUpdated: AwareDatetime
    competition: _Competition
    group: str | None = None
    season: _Season
    homeTeam: _Team
    awayTeam: _Team
    score: _Score | None = None


class FootballDataOrgFixtureRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    match: Match
    provider: Literal["football_data_org"] = "football_data_org"
    provider_match_id: int
    competition_id: str
    competition_name: str
    competition_code: str | None
    competition_group: str | None = None
    status: str
    last_updated: AwareDatetime
    retrieved_at: AwareDatetime
    provenance: Provenance
    response_headers: dict[str, str] = Field(default_factory=dict)
    raw_hash: str
    score_duration: str | None = None
    # Kept apart from Match and never supplied as pre-match evidence.
    full_time_home: int | None = None
    full_time_away: int | None = None
    regular_time_home: int | None = None
    regular_time_away: int | None = None
    extra_time_home: int | None = None
    extra_time_away: int | None = None
    penalties_home: int | None = None
    penalties_away: int | None = None


class FootballDataOrgFixtureProvider:
    def __init__(
        self,
        settings: FootballDataOrgSettings | None = None,
        *,
        client: httpx.AsyncClient | None = None,
        executor: ProviderExecutor | None = None,
        clock: Callable[[], datetime] | None = None,
        on_record: Callable[[FootballDataOrgFixtureRecord, dict[str, Any]], None] | None = None,
    ):
        self.settings = settings or FootballDataOrgSettings.from_env()
        self._client = client
        self._clock = clock or (lambda: datetime.now(UTC))
        self.executor = executor or ProviderExecutor(
            timeout=self.settings.timeout_seconds,
            retries=self.settings.max_retries,
            rate_per_second=self.settings.requests_per_minute / 60,
        )
        self._cache = SnapshotCache()
        self._records: dict[str, FootballDataOrgFixtureRecord] = {}
        self._raw: dict[str, dict[str, Any]] = {}
        self._on_record = on_record
        self._rate_limited_until: datetime | None = None
        self.skipped_unresolved_ids: list[int] = []

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None:
            raise BetterError(
                ErrorCode.CONFIGURATION_ERROR, "Provider clock must be timezone-aware", source=PROVIDER_ID
            )
        return now.astimezone(UTC)

    async def close(self):
        if self._client is not None:
            await self._client.aclose()

    @staticmethod
    def _external_id(match_id: str) -> int:
        if not re.fullmatch(r"football-data:match:[1-9][0-9]*", match_id):
            raise BetterError(
                ErrorCode.CONFIGURATION_ERROR,
                "Use a canonical football-data:match:<positive integer> ID",
                source=PROVIDER_ID,
            )
        return int(match_id.removeprefix(MATCH_ID_PREFIX))

    def latest_record(self, match_id: str) -> FootballDataOrgFixtureRecord | None:
        """Access original retrieval metadata, without changing freshness timestamps."""
        record = self._records.get(match_id)
        return record.model_copy(deep=True) if record else None

    def raw_record(self, raw_hash: str) -> dict[str, Any]:
        return copy.deepcopy(self._raw[raw_hash])

    async def get_match(self, match_id: str) -> Match:
        record = await self.get_fixture_record(match_id)
        if record.status not in ("SCHEDULED", "TIMED"):
            raise BetterError(
                ErrorCode.MISSING_DATA,
                "Match is not an eligible scheduled pre-match fixture",
                source=PROVIDER_ID,
            )
        return record.match

    async def get_fixture_record(self, match_id: str) -> FootballDataOrgFixtureRecord:
        external_id = self._external_id(match_id)
        cached = self._cache.get(match_id, self._now())
        if cached:
            return cached.payload.model_copy(deep=True)
        payload, retrieved_at, headers, url = await self._request(f"/matches/{external_id}")
        record = self._normalize(payload, retrieved_at=retrieved_at, headers=headers, url=url)
        if record.provider_match_id != external_id:
            raise BetterError(
                ErrorCode.SCHEMA_ERROR, "Fixture response has a different match ID", source=PROVIDER_ID
            )
        self._record(record, payload)
        return record.model_copy(deep=True)

    async def list_matches(self, as_of: datetime) -> tuple[Match, ...]:
        if as_of.tzinfo is None:
            raise BetterError(
                ErrorCode.CONFIGURATION_ERROR, "Fixture day requires an aware timestamp", source=PROVIDER_ID
            )
        day = as_of.astimezone(UTC).date()
        records = await self.list_fixture_records(day)
        return tuple(
            record.match.model_copy(deep=True)
            for record in records
            if record.status in ("SCHEDULED", "TIMED") and record.match.kickoff > as_of
        )

    async def list_fixture_records(self, day: date) -> tuple[FootballDataOrgFixtureRecord, ...]:
        """Return all states for a UTC date so identity/result resolution can run post-match."""
        key = f"fixture-records:{day.isoformat()}"
        cached = self._cache.get(key, self._now())
        if cached:
            records = cached.payload
        else:
            payload, retrieved_at, headers, url = await self._request(
                "/matches",
                params={
                    "dateFrom": day.isoformat(),
                    "dateTo": (day + timedelta(days=1)).isoformat(),
                },
            )
            if not isinstance(payload, dict) or not isinstance(payload.get("matches"), list):
                raise BetterError(ErrorCode.SCHEMA_ERROR, "Invalid matches list response", source=PROVIDER_ID)
            pairs = []
            seen = set()
            for item in payload["matches"]:
                try:
                    record = self._normalize(item, retrieved_at=retrieved_at, headers=headers, url=url)
                except BetterError as error:
                    if error.detail.code == ErrorCode.MISSING_DATA:
                        if item["id"] in seen:
                            raise BetterError(
                                ErrorCode.SCHEMA_ERROR,
                                "Duplicate fixture ID in provider list",
                                source=PROVIDER_ID,
                            ) from error
                        seen.add(item["id"])
                        self.skipped_unresolved_ids.append(item["id"])
                        continue
                    raise
                if record.provider_match_id in seen:
                    raise BetterError(
                        ErrorCode.SCHEMA_ERROR, "Duplicate fixture ID in provider list", source=PROVIDER_ID
                    )
                if record.match.kickoff.astimezone(UTC).date() != day:
                    raise BetterError(
                        ErrorCode.SCHEMA_ERROR,
                        "Provider returned fixture outside requested UTC day",
                        source=PROVIDER_ID,
                    )
                seen.add(record.provider_match_id)
                pairs.append((record, item))
            # Record/cache only after the complete list passes validation.
            for record, raw_item in pairs:
                self._record(record, raw_item)
            records = tuple(record for record, _ in pairs)
            self._put_cache(key, records, retrieved_at)
        return tuple(record.model_copy(deep=True) for record in records)

    def _put_cache(self, key: str, payload: Any, retrieved_at: datetime) -> None:
        if self.settings.cache_ttl_seconds:
            self._cache.put(
                key,
                CacheEntry(
                    payload,
                    retrieved_at,
                    retrieved_at + timedelta(seconds=self.settings.cache_ttl_seconds),
                    PROVIDER_ID,
                ),
            )

    def _record(self, record: FootballDataOrgFixtureRecord, payload: dict) -> None:
        if self._on_record:
            self._on_record(record.model_copy(deep=True), copy.deepcopy(payload))
        self._records[record.match.match_id] = record
        self._raw[record.raw_hash] = copy.deepcopy(payload)
        self._put_cache(record.match.match_id, record, record.retrieved_at)

    async def _request(self, path: str, *, params: dict[str, str] | None = None):
        if not self.settings.api_key.get_secret_value():
            raise BetterError(
                ErrorCode.CONFIGURATION_ERROR,
                "FOOTBALL_DATA_ORG_API_KEY is not configured",
                source=PROVIDER_ID,
            )
        url = self.settings.base_url.rstrip("/") + path

        async def fetch(client):
            if self._rate_limited_until and self._now() < self._rate_limited_until:
                raise BetterError(
                    ErrorCode.RATE_LIMIT, "Provider rate-limit cooldown is active", source=PROVIDER_ID
                )
            try:
                response = await client.get(
                    url,
                    params=params,
                    headers={"X-Auth-Token": self.settings.api_key.get_secret_value()},
                    timeout=self.settings.timeout_seconds,
                    follow_redirects=False,
                )
            except httpx.TimeoutException as error:
                raise BetterError(
                    ErrorCode.TIMEOUT, "Fixture provider timed out", source=PROVIDER_ID
                ) from error
            except httpx.TransportError as error:
                raise BetterError(
                    ErrorCode.TRANSIENT_PROVIDER_ERROR, "Fixture provider unavailable", source=PROVIDER_ID
                ) from error
            retrieved_at = self._now()
            headers = {
                name: response.headers[name]
                for name in ("x-api-version", "x-requestsavailable", "x-requestcounter-reset")
                if name in response.headers
            }
            if response.status_code == 429:
                reset = response.headers.get(
                    "retry-after", response.headers.get("x-requestcounter-reset", "0")
                )
                if reset.isdigit() and int(reset) > 0:
                    self._rate_limited_until = retrieved_at + timedelta(seconds=min(int(reset), 86400))
                raise BetterError(
                    ErrorCode.RATE_LIMIT, "Fixture provider rate limit reached", source=PROVIDER_ID
                )
            if response.status_code == 404:
                raise BetterError(ErrorCode.MISSING_DATA, "Fixture resource not found", source=PROVIDER_ID)
            if response.status_code in (408, 425) or response.status_code >= 500:
                raise BetterError(
                    ErrorCode.TRANSIENT_PROVIDER_ERROR,
                    "Fixture provider transient HTTP error",
                    source=PROVIDER_ID,
                )
            if response.status_code != 200:
                raise BetterError(
                    ErrorCode.PERMANENT_PROVIDER_ERROR,
                    f"Fixture provider returned HTTP {response.status_code}",
                    source=PROVIDER_ID,
                )
            if headers.get("x-api-version", "v4") != "v4":
                raise BetterError(
                    ErrorCode.SCHEMA_ERROR, "Unexpected football-data API version", source=PROVIDER_ID
                )
            try:
                payload = response.json()
            except ValueError as error:
                raise BetterError(
                    ErrorCode.SCHEMA_ERROR, "Fixture provider returned invalid JSON", source=PROVIDER_ID
                ) from error
            if not isinstance(payload, dict):
                raise BetterError(
                    ErrorCode.SCHEMA_ERROR, "Fixture provider response must be an object", source=PROVIDER_ID
                )
            return payload, retrieved_at, headers, str(response.request.url)

        if self._client:
            return await self.executor.call(lambda: fetch(self._client))
        async with httpx.AsyncClient() as client:
            return await self.executor.call(lambda: fetch(client))

    def _normalize(
        self, payload: dict, *, retrieved_at: datetime, headers: dict, url: str
    ) -> FootballDataOrgFixtureRecord:
        try:
            native = _NativeMatch.model_validate(payload)
            if native.lastUpdated > retrieved_at:
                raise ValueError("Provider lastUpdated is in the future")
            if any(
                team.id is None or team.id <= 0 or not team.name or not team.name.strip()
                for team in (native.homeTeam, native.awayTeam)
            ):
                raise BetterError(
                    ErrorCode.MISSING_DATA, "Fixture opponents are unresolved", source=PROVIDER_ID
                )
            competition_id = f"football-data:competition:{native.competition.id}"
            match = Match(
                match_id=f"{MATCH_ID_PREFIX}{native.id}",
                competition=competition_id,
                season=f"football-data:season:{native.season.id}",
                kickoff=native.utcDate,
                home_team=native.homeTeam.shortName or native.homeTeam.name,
                away_team=native.awayTeam.shortName or native.awayTeam.name,
                home_team_id=f"football-data:team:{native.homeTeam.id}",
                away_team_id=f"football-data:team:{native.awayTeam.id}",
            )
            digest = hashlib.sha256(
                json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
            ).hexdigest()
            provenance = Provenance(
                source_id=f"football-data:match:{native.id}",
                source_type=SourceType.API,
                url=url,
                retrieved_at=retrieved_at,
                published_at=native.lastUpdated,
                reliability_tier=ReliabilityTier.B,
                field="fixture",
                raw_reference=f"football-data-org:v4:sha256:{digest}",
                notes="lastUpdated is the provider update time; retrieved_at is actual retrieval, never historical as_of",
            )
            score = native.score if native.status == "FINISHED" else None
            full_time = score.fullTime if score else None
            regular_time = score.regularTime if score else None
            extra_time = score.extraTime if score else None
            penalties = score.penalties if score else None
            return FootballDataOrgFixtureRecord(
                match=match,
                provider_match_id=native.id,
                competition_id=competition_id,
                competition_name=native.competition.name,
                competition_code=native.competition.code,
                competition_group=native.group,
                status=native.status,
                last_updated=native.lastUpdated,
                retrieved_at=retrieved_at,
                provenance=provenance,
                response_headers=headers,
                raw_hash=digest,
                score_duration=score.duration if score else None,
                full_time_home=full_time.home if full_time else None,
                full_time_away=full_time.away if full_time else None,
                regular_time_home=regular_time.home if regular_time else None,
                regular_time_away=regular_time.away if regular_time else None,
                extra_time_home=extra_time.home if extra_time else None,
                extra_time_away=extra_time.away if extra_time else None,
                penalties_home=penalties.home if penalties else None,
                penalties_away=penalties.away if penalties else None,
            )
        except (ValidationError, ValueError, TypeError) as error:
            raise BetterError(
                ErrorCode.SCHEMA_ERROR, "Invalid football-data fixture schema", source=PROVIDER_ID
            ) from error
