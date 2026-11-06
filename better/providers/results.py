"""Verified result-provider contracts and deterministic fallback handling."""

from datetime import datetime
from typing import Literal, Protocol

from pydantic import Field, HttpUrl, model_validator

from better.domain.common import Contract, UTCDateTime
from better.domain.models import Match
from better.providers.result_identity import ResultIdentityCandidate, ResultProviderIdentity


class MatchResult(Contract):
    match_id: str
    final_status: Literal["FINAL", "PENDING", "UNVERIFIED", "CONFLICT"]
    home_score: int | None = Field(default=None, ge=0, le=50)
    away_score: int | None = Field(default=None, ge=0, le=50)
    extra_time_home: int | None = Field(default=None, ge=0, le=50)
    extra_time_away: int | None = Field(default=None, ge=0, le=50)
    penalties_home: int | None = Field(default=None, ge=0, le=50)
    penalties_away: int | None = Field(default=None, ge=0, le=50)
    source: str
    source_tier: Literal["A", "B"]
    source_url: HttpUrl
    published_at: UTCDateTime | None = None
    retrieved_at: UTCDateTime
    provider: str
    verified_at: UTCDateTime
    provider_status: str | None = None

    @model_validator(mode="after")
    def final_score(self):
        if self.final_status == "FINAL" and (self.home_score is None or self.away_score is None):
            raise ValueError("verified final result requires the regulation-time score")
        return self


class MatchResultProvider(Protocol):
    async def get_result(
        self, match_identity: Match | ResultProviderIdentity, as_of: datetime
    ) -> MatchResult | None: ...


class CompositeResultProvider:
    """Query reliable providers in priority order and stop settlement on conflict."""

    def __init__(self, *providers: MatchResultProvider):
        self.providers = tuple(provider for provider in providers if provider is not None)
        self.supports_identity_discovery = any(
            getattr(provider, "find_candidates", None) is not None for provider in self.providers
        )

    provider_id = "composite_result_provider"

    def supports_provider(self, provider_id):
        return any(getattr(provider, "provider_id", None) == provider_id for provider in self.providers)

    async def find_candidates(self, match_identity, as_of):
        found = []
        errors = []
        for provider in self.providers:
            finder = getattr(provider, "find_candidates", None)
            if finder is not None:
                from better.domain.errors import BetterError
                try:
                    found.extend(await finder(match_identity, as_of))
                except BetterError as error:
                    errors.append(error)
        self.last_errors = [error.detail.model_dump(mode="json") for error in errors]
        if not found and errors:
            raise errors[0]
        return tuple(found)

    def competition_target(self, source_id, provider=None):
        return self._configured_target("competition_target", source_id, provider)

    def team_target(self, source_id, provider=None):
        return self._configured_target("team_target", source_id, provider)

    def _configured_target(self, method_name, source_id, provider):
        for candidate in self.providers:
            if provider is not None and getattr(candidate, "provider_id", None) != provider:
                continue
            method = getattr(candidate, method_name, None)
            if method is not None and (target := method(source_id, provider)) is not None:
                return target
        return None

    async def close(self):
        closed = set()
        for provider in self.providers:
            if id(provider) not in closed and hasattr(provider, "close"):
                await provider.close()
                closed.add(id(provider))

    async def get_claims(self, match_identity, as_of):
        claims = []
        for provider in self.providers:
            provider_id = getattr(provider, "provider_id", None)
            if isinstance(match_identity, ResultProviderIdentity) and provider_id is not None:
                if provider_id != match_identity.provider:
                    continue
            result = await provider.get_result(match_identity, as_of)
            if result is not None:
                claims.append(result)
        return tuple(claims)

    async def get_result(self, match_identity, as_of):
        claims = [
            claim for claim in await self.get_claims(match_identity, as_of) if claim.final_status == "FINAL"
        ]
        if not claims:
            return None
        scores = {(claim.home_score, claim.away_score) for claim in claims}
        if len(scores) > 1:
            first = claims[0]
            return first.model_copy(
                update={"final_status": "CONFLICT", "home_score": None, "away_score": None}
            )
        return claims[0]


class FootballDataOrgResultProvider:
    """Tier-B structured results using football-data.org regulation-time fields."""

    provider_id = "football_data_org"

    def __init__(self, fixture_provider, *, owns_fixture_provider=False, competitions=None):
        self.fixture_provider = fixture_provider
        self.owns_fixture_provider = owns_fixture_provider
        self.competitions = competitions or {}

    def competition_target(self, source_id, provider=None):
        return self.competitions.get(source_id)

    async def refresh_standings(self, repository):
        for competition in set(self.competitions.values()):
            native = competition.removeprefix("football-data:competition:")
            payload, retrieved, _, url = await self.fixture_provider._request(f"/competitions/{native}/standings")
            if not isinstance(payload.get("standings"), list):
                raise ValueError("Invalid football-data.org standings")
            from better.storage.repository import canonical_hash
            stable = {"source_url": url, "data": payload}
            entity = "football-data:standings:" + competition
            with repository.engine.begin() as connection:
                content_id = "football-data:standings-content:" + canonical_hash(stable)
                repository._append(connection, "annotations", identifier=content_id, entity_id=entity,
                                   match_id=None, payload=stable)
                check = {"content_id": content_id, "acquired_at": retrieved.isoformat(), "source_url": url}
                repository._append(connection, "annotations", identifier="standings-check:" + canonical_hash(check),
                                   entity_id=entity + ":checks", match_id=None, payload=check)

    async def close(self):
        if self.owns_fixture_provider:
            await self.fixture_provider.close()

    async def find_candidates(self, match_identity: Match, as_of: datetime):
        if self.competitions and match_identity.competition not in self.competitions:
            return ()
        records = await self.fixture_provider.list_fixture_records(match_identity.kickoff.date())
        candidates = []
        for record in records:
            candidates.append(
                ResultIdentityCandidate(
                    provider=self.provider_id,
                    source_tier="B",
                    provider_match_id=record.match.match_id,
                    competition_id=record.competition_id,
                    competition_name=record.competition_name,
                    competition_group=record.competition_group,
                    home_team_id=record.match.home_team_id,
                    away_team_id=record.match.away_team_id,
                    home_team_name=record.match.home_team,
                    away_team_name=record.match.away_team,
                    kickoff=record.match.kickoff,
                    source_url=record.provenance.url,
                    evidence={
                        "competition_code": record.competition_code,
                        "raw_hash": record.raw_hash,
                        "retrieved_at": record.retrieved_at.isoformat(),
                    },
                )
            )
        return tuple(candidates)

    async def get_result(self, match_identity, as_of):
        provider_match_id = (
            match_identity.provider_match_id
            if isinstance(match_identity, ResultProviderIdentity)
            else match_identity.match_id
        )
        source_match_id = (
            match_identity.source_match_id
            if isinstance(match_identity, ResultProviderIdentity)
            else match_identity.match_id
        )
        if not provider_match_id.startswith("football-data:match:"):
            return None
        record = await self.fixture_provider.get_fixture_record(provider_match_id)
        if record.status != "FINISHED":
            return None
        home = record.regular_time_home
        away = record.regular_time_away
        if home is None or away is None:
            if record.score_duration != "REGULAR":
                return None
            home, away = record.full_time_home, record.full_time_away
        if home is None or away is None:
            return None
        return MatchResult(
            match_id=source_match_id,
            final_status="FINAL",
            home_score=home,
            away_score=away,
            extra_time_home=record.extra_time_home,
            extra_time_away=record.extra_time_away,
            penalties_home=record.penalties_home,
            penalties_away=record.penalties_away,
            source="football-data.org match resource",
            source_tier="B",
            source_url=record.provenance.url,
            published_at=record.last_updated,
            retrieved_at=record.retrieved_at,
            provider=self.provider_id,
            verified_at=as_of,
            provider_status=record.status,
        )
