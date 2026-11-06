"""Verified API-Football match identities and regulation-time results."""

from __future__ import annotations

import re
from datetime import datetime

from better.domain.enums import ErrorCode
from better.domain.models import Match
from better.providers.api_football import APIFootballProvider
from better.providers.result_identity import ResultIdentityCandidate, ResultProviderIdentity
from better.providers.results import MatchResult

PROVIDER_ID = "api_football"
_FINAL_STATUSES = frozenset({"FT", "AET", "PEN"})
_PENDING_STATUSES = frozenset({"NS", "TBD", "1H", "HT", "2H", "ET", "BT", "P", "SUSP", "INT", "PST"})
_UNVERIFIED_STATUSES = frozenset({"CANC", "ABD", "AWD", "WO"})


def competition_id(league: int, season: int) -> str:
    return f"api-football:league:{league}:season:{season}"


def team_id(identifier: int) -> str:
    return f"api-football:team:{identifier}"


def fixture_id(identifier: int) -> str:
    return f"api-football:fixture:{identifier}"


class APIFootballResultProvider:
    """Result adapter kept separate from pre-match statistics and lineup logic."""

    provider_id = PROVIDER_ID

    def __init__(self, transport: APIFootballProvider, *, owns_transport: bool = False):
        self.transport = transport
        self.settings = transport.settings
        self.owns_transport = owns_transport

    def competition_target(self, source_id: str, provider: str | None = None) -> str | None:
        if provider is not None and provider != self.provider_id:
            return None
        binding = self.settings.competitions.get(source_id)
        return competition_id(binding.league, binding.season) if binding else None

    def team_target(self, source_id: str, provider: str | None = None) -> str | None:
        if provider is not None and provider != self.provider_id:
            return None
        identifier = self.settings.teams.get(source_id)
        return team_id(identifier) if identifier else None

    async def close(self):
        if self.owns_transport:
            await self.transport.close()

    async def find_candidates(self, match_identity: Match, as_of: datetime):
        binding = self.settings.competitions.get(match_identity.competition)
        if binding is None:
            return ()
        response = await self.transport.request(
            "fixtures",
            {
                "league": binding.league,
                "season": binding.season,
                "date": match_identity.kickoff.date().isoformat(),
            },
            ttl=300,
        )
        try:
            candidates = tuple(
                self._candidate(row, response)
                for row in response["data"]
                if row["league"]["id"] == binding.league and row["league"]["season"] == binding.season
            )
        except (KeyError, TypeError, ValueError):
            raise self.transport._fail(
                ErrorCode.SCHEMA_ERROR, "Identita partita API-Football non valida"
            ) from None
        return candidates

    def _candidate(self, row, response):
        native_fixture = row["fixture"]
        league = row["league"]
        home, away = row["teams"]["home"], row["teams"]["away"]
        native_id = native_fixture["id"]
        if any(
            type(value) is not int
            for value in (native_id, league["id"], league["season"], home["id"], away["id"])
        ):
            raise ValueError()
        kickoff = datetime.fromisoformat(native_fixture["date"])
        round_name = str(league.get("round") or "")
        group_match = re.search(r"(?:Girone|Group)\s+([A-Z])\b", round_name, re.IGNORECASE)
        group = f"GROUP_{group_match.group(1).upper()}" if group_match else None
        return ResultIdentityCandidate(
            provider=self.provider_id,
            source_tier="B",
            provider_match_id=fixture_id(native_id),
            competition_id=competition_id(league["id"], league["season"]),
            competition_name=str(league.get("name") or league["id"]),
            competition_group=group,
            home_team_id=team_id(home["id"]),
            away_team_id=team_id(away["id"]),
            home_team_name=str(home["name"]),
            away_team_name=str(away["name"]),
            kickoff=kickoff,
            source_url=response["url"],
            evidence={
                "raw_hash": response["sha256"],
                "retrieved_at": response["retrieved_at"].isoformat(),
                "league": league["id"],
                "season": league["season"],
            },
        )

    async def get_result(self, match_identity: Match | ResultProviderIdentity, as_of: datetime):
        if isinstance(match_identity, ResultProviderIdentity):
            if match_identity.provider != self.provider_id:
                return None
            provider_match_id = match_identity.provider_match_id
            source_match_id = match_identity.source_match_id
        else:
            provider_match_id = match_identity.match_id
            source_match_id = match_identity.match_id
        native_id = self._native_fixture_id(provider_match_id)
        if native_id is None:
            return None
        response = await self.transport.request("fixtures", {"id": native_id}, ttl=60)
        data = response["data"]
        if not isinstance(data, list) or len(data) != 1:
            return self._unverified(source_match_id, response, "IDENTITY_NOT_UNIQUE", as_of)
        row = data[0]
        if not self._identity_matches(row, match_identity, native_id):
            return self._unverified(source_match_id, response, "IDENTITY_MISMATCH", as_of)
        try:
            status = row["fixture"]["status"]["short"]
        except (KeyError, TypeError):
            return self._unverified(source_match_id, response, "SCHEMA_INVALID", as_of)
        if status in _PENDING_STATUSES:
            return self._result(source_match_id, response, status, "PENDING", as_of)
        if status in _UNVERIFIED_STATUSES or status not in _FINAL_STATUSES:
            return self._unverified(source_match_id, response, status, as_of)
        try:
            score = row["score"]
            fulltime = score["fulltime"]
            home, away = fulltime["home"], fulltime["away"]
            if type(home) is not int or type(away) is not int or min(home, away) < 0:
                raise ValueError()
            extra_home, extra_away = self._score_pair(score.get("extratime"))
            penalties_home, penalties_away = self._score_pair(score.get("penalty"))
        except (KeyError, TypeError, ValueError):
            return self._unverified(source_match_id, response, f"{status}_SCORE_INVALID", as_of)
        return self._result(
            source_match_id,
            response,
            status,
            "FINAL",
            as_of,
            home_score=home,
            away_score=away,
            extra_time_home=extra_home,
            extra_time_away=extra_away,
            penalties_home=penalties_home,
            penalties_away=penalties_away,
        )

    @staticmethod
    def _native_fixture_id(value: str) -> int | None:
        prefix = "api-football:fixture:"
        if not value.startswith(prefix):
            return None
        suffix = value.removeprefix(prefix)
        return int(suffix) if suffix.isdigit() and int(suffix) > 0 else None

    @staticmethod
    def _score_pair(value):
        if value is None:
            return None, None
        home, away = value.get("home"), value.get("away")
        if home is None and away is None:
            return None, None
        if type(home) is not int or type(away) is not int or min(home, away) < 0:
            raise ValueError()
        return home, away

    @staticmethod
    def _identity_matches(row, identity, native_id):
        try:
            if row["fixture"]["id"] != native_id:
                return False
            if not isinstance(identity, ResultProviderIdentity):
                return True
            league = row["league"]
            teams = row["teams"]
            return (
                competition_id(league["id"], league["season"]) == identity.competition_id
                and team_id(teams["home"]["id"]) == identity.home_team_id
                and team_id(teams["away"]["id"]) == identity.away_team_id
                and datetime.fromisoformat(row["fixture"]["date"]) == identity.kickoff
            )
        except (KeyError, TypeError, ValueError):
            return False

    def _unverified(self, source_match_id, response, status, as_of):
        return self._result(source_match_id, response, status, "UNVERIFIED", as_of)

    def _result(self, source_match_id, response, provider_status, final_status, as_of, **score):
        return MatchResult(
            match_id=source_match_id,
            final_status=final_status,
            source="API-Football fixture resource",
            source_tier="B",
            source_url=response["url"],
            retrieved_at=response["retrieved_at"],
            provider=self.provider_id,
            verified_at=as_of,
            provider_status=provider_status,
            **score,
        )
