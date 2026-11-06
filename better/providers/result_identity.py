"""Deterministic, append-only identity resolution for post-match result providers."""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from enum import StrEnum
from typing import Literal, Protocol

from pydantic import Field, HttpUrl

from better.domain.common import Contract, Probability, UTCDateTime
from better.domain.models import Match
from better.storage.repository import canonical_hash

NORMALIZATION_VERSION = "team-name-v3"
_CLUB_TOKENS = frozenset({"fc", "fbc", "ac", "acf", "as", "bc", "cfc", "acd", "asd", "ssd", "ss", "ssc", "us", "usd", "calcio"})
_SERIE_C = {
    "888it:competition:2000079607": ("football-data:competition:2123", "GROUP_A"),
    "888it:competition:2000079608": ("football-data:competition:2123", "GROUP_B"),
    "888it:competition:2000079609": ("football-data:competition:2123", "GROUP_C"),
}


def normalize_team_name(value: str) -> str:
    """Normalize legal club prefixes and founding-year suffixes, never reserve/age labels."""
    text = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii").lower()
    for pattern, replacement in (
        (r"\ba\s*\.\s*s\s*\.\s*d\s*\.?", "asd"),
        (r"\bs\s*\.\s*s\s*\.\s*d\s*\.?", "ssd"),
        (r"\ba\s*\.\s*c\s*\.?", "ac"),
        (r"\bu\s*\.\s*s\s*\.?", "us"),
        (r"\bs\s*\.\s*s\s*\.?", "ss"),
    ):
        text = re.sub(pattern, replacement, text)
    text = re.sub(r"\bunder\s*([0-9]{2})\b", r"u\1", text)
    tokens = re.sub(r"[^a-z0-9]+", " ", text).split()
    while tokens and tokens[0] in _CLUB_TOKENS:
        tokens.pop(0)
    while tokens and (tokens[-1] in _CLUB_TOKENS or re.fullmatch(r"(?:18|19|20)\d{2}", tokens[-1])):
        tokens.pop()
    if tokens[:2] == ["citta", "di"]:
        tokens = tokens[2:]
    return " ".join(tokens)


class ResultIdentityStatus(StrEnum):
    VERIFIED_EXACT = "VERIFIED_EXACT"
    VERIFIED_MAPPED = "VERIFIED_MAPPED"
    PROBABLE = "PROBABLE"
    AMBIGUOUS = "AMBIGUOUS"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"


class ResultIdentityCandidate(Contract):
    provider: str
    source_tier: Literal["A", "B"]
    provider_match_id: str
    competition_id: str
    competition_name: str
    competition_group: str | None = None
    home_team_id: str
    away_team_id: str
    home_team_name: str
    away_team_name: str
    kickoff: UTCDateTime
    source_url: HttpUrl
    evidence: dict = Field(default_factory=dict)


class ResultProviderIdentity(Contract):
    source_match_id: str
    provider_match_id: str
    competition_id: str
    home_team_id: str
    away_team_id: str
    kickoff: UTCDateTime
    provider: str
    source_competition_id: str
    source_home_team_id: str
    source_away_team_id: str
    home_team_name: str
    away_team_name: str
    resolution_method: str
    confidence: Probability
    verified_at: UTCDateTime
    source_url: HttpUrl

    @property
    def match_id(self) -> str:
        """Compatibility with result providers that previously accepted Match."""
        return self.provider_match_id


class ResultIdentityResolution(Contract):
    source_match_id: str
    status: ResultIdentityStatus
    identity: ResultProviderIdentity | None = None
    resolution_method: str
    confidence: Probability = 0
    verified: bool = False
    verified_at: UTCDateTime | None = None
    source_evidence: tuple[dict, ...] = ()
    ambiguity_status: str | None = None
    candidate_count: int = Field(default=0, ge=0)


class ResultIdentityResolver(Protocol):
    async def resolve(self, match_identity: Match, as_of: datetime) -> ResultIdentityResolution: ...


class ResultIdentityCandidateProvider(Protocol):
    async def find_candidates(
        self, match_identity: Match, as_of: datetime
    ) -> tuple[ResultIdentityCandidate, ...]: ...


class DeterministicResultIdentityResolver:
    """Resolve by verified competition, kickoff and exact normalized orientation."""

    def __init__(
        self,
        repository,
        candidate_provider,
        *,
        kickoff_tolerance_minutes: int = 15,
        require_competition: bool = True,
        provider_priority: tuple[str, ...] = (
            "serie_c_official",
            "api_football",
            "football_data_org",
            "manual",
        ),
    ):
        self.repository = repository
        self.candidate_provider = candidate_provider
        self.tolerance_seconds = kickoff_tolerance_minutes * 60
        self.require_competition = require_competition
        self.provider_priority = provider_priority

    async def resolve(self, match_identity: Match, as_of: datetime) -> ResultIdentityResolution:
        supports_provider = getattr(self.candidate_provider, "supports_provider", None)
        direct_provider_id = getattr(self.candidate_provider, "provider_id", None)

        def available(provider):
            if supports_provider is not None:
                return supports_provider(provider)
            return direct_provider_id in {None, provider}

        persisted = next(
            (
                mapping
                for provider in self.provider_priority
                if available(provider)
                if (
                    mapping := self.repository.result_identity_mapping(
                        "match", match_identity.match_id, provider=provider
                    )
                )
            ),
            None,
        )
        if persisted is None and direct_provider_id is None and supports_provider is None:
            persisted = self.repository.result_identity_mapping("match", match_identity.match_id)
        incomplete_mapping = persisted if persisted and not persisted.get("identity") else None
        if persisted and persisted.get("identity"):
            identity = ResultProviderIdentity.model_validate(persisted["identity"])
            if (
                identity.source_competition_id != match_identity.competition
                or identity.source_home_team_id != match_identity.home_team_id
                or identity.source_away_team_id != match_identity.away_team_id
                or abs((identity.kickoff - match_identity.kickoff).total_seconds()) > self.tolerance_seconds
            ):
                return self._record_unresolved(match_identity, ResultIdentityStatus.CONFLICT,
                                               "PERSISTED_SOURCE_IDENTITY_CHANGED", as_of, ())
            resolution = self._resolution(
                match_identity,
                ResultIdentityStatus.VERIFIED_MAPPED,
                "PERSISTED_MATCH_MAPPING",
                1.0,
                as_of,
                identity=identity,
                evidence=({"mapping_id": persisted["mapping_id"], "reused": True},),
                count=1,
            )
            self._record_attempt(resolution, (), as_of)
            return resolution

        finder = getattr(self.candidate_provider, "find_candidates", None)
        if finder is None or getattr(self.candidate_provider, "supports_identity_discovery", True) is False:
            # A provider explicitly wired to the source namespace can consume the source ID directly.
            provider = getattr(self.candidate_provider, "provider_id", "direct_result_provider")
            identity = ResultProviderIdentity(
                source_match_id=match_identity.match_id,
                provider_match_id=match_identity.match_id,
                competition_id=match_identity.competition,
                home_team_id=match_identity.home_team_id,
                away_team_id=match_identity.away_team_id,
                kickoff=match_identity.kickoff,
                provider=provider,
                source_competition_id=match_identity.competition,
                source_home_team_id=match_identity.home_team_id,
                source_away_team_id=match_identity.away_team_id,
                home_team_name=match_identity.home_team,
                away_team_name=match_identity.away_team,
                resolution_method="DIRECT_PROVIDER_NAMESPACE",
                confidence=1,
                verified_at=as_of,
                source_url="https://better.local/result-provider/direct",
            )
            resolution = self._resolution(
                match_identity,
                ResultIdentityStatus.VERIFIED_EXACT,
                "DIRECT_PROVIDER_NAMESPACE",
                1,
                as_of,
                identity=identity,
                count=1,
            )
            self._record_attempt(resolution, (), as_of)
            return resolution

        candidates = tuple(await finder(match_identity, as_of))
        matches: list[tuple[ResultIdentityCandidate, bool]] = []
        probable = []
        for candidate in candidates:
            competition_ok = self._competition_matches(match_identity.competition, candidate)
            kickoff_ok = (
                abs((candidate.kickoff - match_identity.kickoff).total_seconds()) <= self.tolerance_seconds
            )
            home_mapped = self.repository.result_identity_mapping(
                "team", match_identity.home_team_id, provider=candidate.provider
            )
            away_mapped = self.repository.result_identity_mapping(
                "team", match_identity.away_team_id, provider=candidate.provider
            )
            configured_home = self._configured_target(
                "team_target", match_identity.home_team_id, candidate.provider
            )
            configured_away = self._configured_target(
                "team_target", match_identity.away_team_id, candidate.provider
            )
            home_ok = (
                home_mapped["target_id"] == candidate.home_team_id
                if home_mapped
                else (
                    configured_home == candidate.home_team_id
                    if configured_home
                    else normalize_team_name(match_identity.home_team)
                    == normalize_team_name(candidate.home_team_name)
                )
            )
            away_ok = (
                away_mapped["target_id"] == candidate.away_team_id
                if away_mapped
                else (
                    configured_away == candidate.away_team_id
                    if configured_away
                    else normalize_team_name(match_identity.away_team)
                    == normalize_team_name(candidate.away_team_name)
                )
            )
            if competition_ok and kickoff_ok and home_ok and away_ok:
                matches.append(
                    (candidate, bool(home_mapped or away_mapped or configured_home or configured_away))
                )
            elif competition_ok and kickoff_ok:
                probable.append(candidate)

        if len(matches) > 1:
            ranks = {provider: index for index, provider in enumerate(self.provider_priority)}
            best_rank = min(ranks.get(item[0].provider, len(ranks)) for item in matches)
            preferred = [item for item in matches if ranks.get(item[0].provider, len(ranks)) == best_rank]
            if len(preferred) > 1:
                return self._record_unresolved(
                    match_identity,
                    ResultIdentityStatus.AMBIGUOUS,
                    "MULTIPLE_EXACT_CANDIDATES",
                    as_of,
                    candidates,
                )
            matches = preferred
        if not matches:
            status = (
                ResultIdentityStatus.PROBABLE
                if len(probable) == 1
                else (ResultIdentityStatus.AMBIGUOUS if len(probable) > 1 else ResultIdentityStatus.NOT_FOUND)
            )
            reason = "TEAM_IDENTITY_UNVERIFIED" if probable else "NO_DETERMINISTIC_MATCH"
            return self._record_unresolved(match_identity, status, reason, as_of, candidates)

        candidate, used_mapping = matches[0]
        if (
            incomplete_mapping
            and incomplete_mapping["target_provider"] == candidate.provider
            and incomplete_mapping["target_id"] != candidate.provider_match_id
        ):
            return self._record_unresolved(
                match_identity,
                ResultIdentityStatus.CONFLICT,
                "PERSISTED_MATCH_MAPPING_CONFLICT",
                as_of,
                candidates,
            )
        status = ResultIdentityStatus.VERIFIED_MAPPED if used_mapping else ResultIdentityStatus.VERIFIED_EXACT
        method = "VERIFIED_TEAM_MAPPING" if used_mapping else f"EXACT_NORMALIZED_{NORMALIZATION_VERSION}"
        identity = self._identity(match_identity, candidate, method, as_of)
        self._persist_verified(match_identity, candidate, identity, status, as_of)
        resolution = self._resolution(
            match_identity,
            status,
            method,
            1.0,
            as_of,
            identity=identity,
            evidence=(candidate.model_dump(mode="json"),),
            count=len(candidates),
        )
        self._record_attempt(resolution, candidates, as_of)
        return resolution

    def manual_verify(self, request, verified_at: datetime) -> ResultIdentityResolution:
        match = self.repository.get_match_record(request.source_match_id)
        if match is None:
            raise KeyError("match not found")
        candidate = ResultIdentityCandidate(
            provider=request.provider,
            source_tier=request.source_tier,
            provider_match_id=request.provider_match_id,
            competition_id=request.provider_competition_id,
            competition_name=request.provider_competition_name,
            competition_group=request.competition_group,
            home_team_id=request.provider_home_team_id,
            away_team_id=request.provider_away_team_id,
            home_team_name=request.provider_home_team_name,
            away_team_name=request.provider_away_team_name,
            kickoff=request.provider_kickoff,
            source_url=request.source_url,
            evidence={"verified_by": request.verified_by, "notes": request.notes},
        )
        identity = self._identity(match, candidate, "MANUAL_VERIFIED", verified_at)
        self._persist_verified(
            match,
            candidate,
            identity,
            ResultIdentityStatus.VERIFIED_MAPPED,
            verified_at,
            provenance={"verified_by": request.verified_by, "notes": request.notes, "confirmed": True},
        )
        return self._resolution(
            match,
            ResultIdentityStatus.VERIFIED_MAPPED,
            "MANUAL_VERIFIED",
            1,
            verified_at,
            identity=identity,
            evidence=(candidate.model_dump(mode="json"),),
            count=1,
        )

    def _competition_matches(self, source_id: str, candidate: ResultIdentityCandidate) -> bool:
        mapping = self.repository.result_identity_mapping(
            "competition", source_id, provider=candidate.provider
        )
        if mapping:
            return mapping["target_id"] == candidate.competition_id
        configured = self._configured_target("competition_target", source_id, candidate.provider)
        if configured:
            return configured == candidate.competition_id
        known = _SERIE_C.get(source_id)
        if known and candidate.provider == "football_data_org":
            return candidate.competition_id == known[0] and candidate.competition_group == known[1]
        return not self.require_competition and source_id == candidate.competition_id

    def _configured_target(self, method_name, source_id, provider):
        method = getattr(self.candidate_provider, method_name, None)
        return method(source_id, provider) if method is not None else None

    @staticmethod
    def _identity(match: Match, candidate: ResultIdentityCandidate, method: str, at: datetime):
        return ResultProviderIdentity(
            source_match_id=match.match_id,
            provider_match_id=candidate.provider_match_id,
            competition_id=candidate.competition_id,
            home_team_id=candidate.home_team_id,
            away_team_id=candidate.away_team_id,
            kickoff=candidate.kickoff,
            provider=candidate.provider,
            source_competition_id=match.competition,
            source_home_team_id=match.home_team_id,
            source_away_team_id=match.away_team_id,
            home_team_name=candidate.home_team_name,
            away_team_name=candidate.away_team_name,
            resolution_method=method,
            confidence=1,
            verified_at=at,
            source_url=candidate.source_url,
        )

    def _persist_verified(self, match, candidate, identity, status, at, provenance=None):
        shared = {
            "provider": candidate.provider,
            "status": status.value,
            "method": identity.resolution_method,
            "confidence": 1.0,
            "verified_at": at,
            "source_url": str(candidate.source_url),
            "normalization_version": NORMALIZATION_VERSION,
            "provenance": provenance or candidate.evidence,
        }
        known_competition = _SERIE_C.get(match.competition)
        source_competition_name = (
            f"Serie C Girone {known_competition[1].removeprefix('GROUP_')}"
            if known_competition
            else match.competition
        )
        self.repository.save_result_identity_mapping(
            kind="competition",
            source_id=match.competition,
            target_id=candidate.competition_id,
            source_name=source_competition_name,
            target_name=candidate.competition_name,
            identity=None,
            **shared,
        )
        for source_id, target_id, source_name, target_name in (
            (match.home_team_id, candidate.home_team_id, match.home_team, candidate.home_team_name),
            (match.away_team_id, candidate.away_team_id, match.away_team, candidate.away_team_name),
        ):
            self.repository.save_result_identity_mapping(
                kind="team",
                source_id=source_id,
                target_id=target_id,
                source_name=source_name,
                target_name=target_name,
                identity=None,
                **shared,
            )
        self.repository.save_result_identity_mapping(
            kind="match",
            source_id=match.match_id,
            target_id=candidate.provider_match_id,
            source_name=f"{match.home_team} - {match.away_team}",
            target_name=f"{candidate.home_team_name} - {candidate.away_team_name}",
            identity=identity.model_dump(mode="json"),
            **shared,
        )

    def _record_unresolved(self, match, status, reason, at, candidates):
        resolution = self._resolution(
            match,
            status,
            reason,
            0.75 if status == ResultIdentityStatus.PROBABLE else 0,
            at,
            evidence=tuple(item.model_dump(mode="json") for item in candidates),
            ambiguity=reason if status == ResultIdentityStatus.AMBIGUOUS else None,
            count=len(candidates),
        )
        self._record_attempt(resolution, candidates, at)
        return resolution

    def _record_attempt(self, resolution, candidates, at):
        document = {
            "scope": "result_identity",
            "match_id": resolution.source_match_id,
            "attempted_at": at.isoformat(),
            "result": resolution.status.value,
            "reason": resolution.resolution_method,
            "selected": resolution.identity.model_dump(mode="json") if resolution.identity else None,
            "candidates": [item.model_dump(mode="json") for item in candidates],
        }
        identifier = "result-identity-attempt:" + canonical_hash(document)
        with self.repository.engine.begin() as connection:
            self.repository._append(
                connection,
                "identity_resolution_attempts",
                identifier=identifier,
                entity_id=resolution.source_match_id,
                match_id=resolution.source_match_id,
                payload=document,
            )

    @staticmethod
    def _resolution(
        match, status, method, confidence, at, *, identity=None, evidence=(), ambiguity=None, count=0
    ):
        return ResultIdentityResolution(
            source_match_id=match.match_id,
            status=status,
            identity=identity,
            resolution_method=method,
            confidence=confidence,
            verified=status in {ResultIdentityStatus.VERIFIED_EXACT, ResultIdentityStatus.VERIFIED_MAPPED},
            verified_at=at
            if status in {ResultIdentityStatus.VERIFIED_EXACT, ResultIdentityStatus.VERIFIED_MAPPED}
            else None,
            source_evidence=evidence,
            ambiguity_status=ambiguity,
            candidate_count=count,
        )


class ManualResultIdentityMapping(Contract):
    source_match_id: str
    provider: str
    provider_match_id: str
    provider_competition_id: str
    provider_competition_name: str
    competition_group: str | None = None
    provider_home_team_id: str
    provider_away_team_id: str
    provider_home_team_name: str
    provider_away_team_name: str
    provider_kickoff: UTCDateTime
    source_tier: Literal["A", "B"]
    source_url: HttpUrl
    verified_by: str = Field(min_length=2, max_length=200)
    notes: str = Field(min_length=2, max_length=2000)
    confirmed: Literal[True]
