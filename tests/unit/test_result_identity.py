from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from better.config import Settings
from better.domain.models import Match
from better.providers.result_identity import (
    DeterministicResultIdentityResolver,
    ManualResultIdentityMapping,
    ResultIdentityCandidate,
    ResultIdentityStatus,
    normalize_team_name,
)
from better.providers.results import CompositeResultProvider, MatchResult
from better.services.data_pipeline import DataPipelineCollector
from better.storage.repository import Repository
from tests.unit.test_validation_cohort import runtime

NOW = datetime(2026, 10, 2, 18, tzinfo=timezone.utc)
COMPETITIONS = {
    "A": "888it:competition:2000079607",
    "B": "888it:competition:2000079608",
    "C": "888it:competition:2000079609",
}


def source_match(group="A", **changes):
    values = {
        "match_id": "888it:match:123",
        "competition": COMPETITIONS[group],
        "season": "2026/27",
        "kickoff": NOW,
        "home_team": "A.C. Trento Calcio",
        "away_team": "U.S. Pergolettese",
        "home_team_id": "888it:team:10",
        "away_team_id": "888it:team:20",
    }
    values.update(changes)
    return Match(**values)


def candidate(group="A", **changes):
    values = {
        "provider": "football_data_org",
        "source_tier": "B",
        "provider_match_id": "football-data:match:999",
        "competition_id": "football-data:competition:2123",
        "competition_name": "Serie C",
        "competition_group": f"GROUP_{group}",
        "home_team_id": "football-data:team:110",
        "away_team_id": "football-data:team:120",
        "home_team_name": "Trento FC",
        "away_team_name": "Pergolettese Calcio",
        "kickoff": NOW,
        "source_url": "https://api.football-data.org/v4/matches/999",
        "evidence": {"raw_hash": "abc"},
    }
    values.update(changes)
    return ResultIdentityCandidate(**values)


class CandidateProvider:
    provider_id = "football_data_org"

    def __init__(self, candidates=(), *, score=(2, 1), status="FINAL"):
        self.candidates = tuple(candidates)
        self.score = score
        self.status = status
        self.find_calls = 0
        self.fetch_calls = 0
        self.received = None

    async def find_candidates(self, match_identity, as_of):
        self.find_calls += 1
        return self.candidates

    async def get_result(self, identity, as_of):
        self.fetch_calls += 1
        self.received = identity
        if self.status == "NONE":
            return None
        return MatchResult(
            match_id=identity.source_match_id,
            final_status=self.status,
            home_score=self.score[0] if self.status == "FINAL" else None,
            away_score=self.score[1] if self.status == "FINAL" else None,
            source="football-data.org match resource",
            source_tier="B",
            source_url=identity.source_url,
            published_at=as_of,
            retrieved_at=as_of,
            provider=self.provider_id,
            verified_at=as_of,
            provider_status="FINISHED" if self.status == "FINAL" else "IN_PLAY",
        )


def resolver(provider, repository=None, **changes):
    repository = repository or Repository("sqlite:///:memory:")
    return (
        DeterministicResultIdentityResolver(
            repository,
            provider,
            kickoff_tolerance_minutes=changes.get("tolerance", 15),
            require_competition=True,
        ),
        repository,
    )


async def test_exact_match_persists_competition_team_and_match_mappings():
    service, repository = resolver(CandidateProvider([candidate()]))
    result = await service.resolve(source_match(), NOW + timedelta(hours=3))
    assert result.status == ResultIdentityStatus.VERIFIED_EXACT
    assert result.identity.provider_match_id == "football-data:match:999"
    rows = repository.list_records("identity_mappings", last=1000)
    assert {row["kind"] for row in rows} == {"competition", "team", "match"}
    assert len(rows) == 4
    assert all(row["normalization_version"] == "team-name-v3" for row in rows)


async def test_persisted_match_mapping_is_reused_without_catalog_scan():
    provider = CandidateProvider([candidate()])
    service, repository = resolver(provider)
    await service.resolve(source_match(), NOW + timedelta(hours=3))
    provider.candidates = ()
    second = await service.resolve(source_match(), NOW + timedelta(hours=4))
    assert second.status == ResultIdentityStatus.VERIFIED_MAPPED
    assert second.resolution_method == "PERSISTED_MATCH_MAPPING"
    assert provider.find_calls == 1
    assert len(repository.list_records("identity_mappings", last=1000)) == 4


@pytest.mark.parametrize("target_id, expected", [
    ("football-data:match:999", ResultIdentityStatus.VERIFIED_EXACT),
    ("football-data:match:other", ResultIdentityStatus.CONFLICT),
])
async def test_incomplete_prematch_mapping_is_rechecked_before_result_fetch(target_id, expected):
    provider = CandidateProvider([candidate()])
    service, repository = resolver(provider)
    match = source_match()
    repository.save_result_identity_mapping(
        kind="match",
        source_id=match.match_id,
        provider="football_data_org",
        target_id=target_id,
        source_name="A.C. Trento Calcio - U.S. Pergolettese",
        target_name="Trento FC - Pergolettese Calcio",
        status="VERIFIED_EXACT",
        method="PREMATCH_MAPPING",
        confidence=1,
        verified_at=NOW,
        source_url="https://api.football-data.org/v4/matches/999",
        normalization_version="team-name-v1",
        provenance={"source": "prematch"},
        identity=None,
    )
    resolution = await service.resolve(match, NOW + timedelta(hours=3))
    assert resolution.status == expected
    assert provider.find_calls == 1
    if expected == ResultIdentityStatus.VERIFIED_EXACT:
        assert resolution.identity.provider_match_id == target_id
        assert repository.result_identity_mapping("match", match.match_id)["identity"] is not None
    else:
        assert resolution.identity is None
        assert repository.result_identity_mapping("match", match.match_id)["identity"] is None


async def test_persisted_team_mapping_allows_different_provider_names():
    service, repository = resolver(
        CandidateProvider([candidate(home_team_name="Club X", away_team_name="Club Y")])
    )
    for source_id, target_id, source_name, target_name in (
        ("888it:team:10", "football-data:team:110", "Trento", "Club X"),
        ("888it:team:20", "football-data:team:120", "Pergolettese", "Club Y"),
    ):
        repository.save_result_identity_mapping(
            kind="team",
            source_id=source_id,
            provider="football_data_org",
            target_id=target_id,
            source_name=source_name,
            target_name=target_name,
            status="VERIFIED_MAPPED",
            method="MANUAL_VERIFIED",
            confidence=1,
            verified_at=NOW,
            source_url="https://www.seriec.com/",
            normalization_version="team-name-v1",
            provenance={"verified_by": "tester"},
            identity=None,
        )
    result = await service.resolve(source_match(), NOW)
    assert result.status == ResultIdentityStatus.VERIFIED_MAPPED
    assert result.resolution_method == "VERIFIED_TEAM_MAPPING"


@pytest.mark.parametrize(
    ("left", "right", "normalized"),
    [
        ("A.C. Trento Calcio", "Trento FC", "trento"),
        ("U.S. Pergolettese", "Pergolettese Calcio", "pergolettese"),
        ("A.S.D. Sorrento 1945", "Sorrento 1945 FC", "sorrento"),
        ("Inter Under 23", "Inter U23", "inter u23"),
    ],
)
def test_controlled_versioned_team_name_normalization(left, right, normalized):
    assert normalize_team_name(left) == normalize_team_name(right) == normalized


@pytest.mark.parametrize(("minutes", "expected"), [(15, "VERIFIED_EXACT"), (16, "NOT_FOUND")])
async def test_kickoff_tolerance_is_conservative(minutes, expected):
    service, _ = resolver(CandidateProvider([candidate(kickoff=NOW + timedelta(minutes=minutes))]))
    result = await service.resolve(source_match(), NOW + timedelta(hours=3))
    assert result.status.value == expected
    assert result.verified is (minutes == 15)


@pytest.mark.parametrize(
    "changes",
    [
        {"home_team_name": "Pergolettese", "away_team_name": "Trento"},
        {"home_team_name": "Trento U19", "away_team_name": "Pergolettese U19"},
        {"home_team_name": "Trento Women", "away_team_name": "Pergolettese Women"},
    ],
)
async def test_reversed_youth_and_women_candidates_never_auto_verify(changes):
    service, _ = resolver(CandidateProvider([candidate(**changes)]))
    result = await service.resolve(source_match(), NOW + timedelta(hours=3))
    assert result.status == ResultIdentityStatus.PROBABLE
    assert result.verified is False


async def test_wrong_competition_is_not_found():
    service, _ = resolver(CandidateProvider([candidate(competition_id="football-data:competition:2019")]))
    result = await service.resolve(source_match(), NOW + timedelta(hours=3))
    assert result.status == ResultIdentityStatus.NOT_FOUND


async def test_duplicate_exact_events_are_ambiguous():
    other = candidate(provider_match_id="football-data:match:1000")
    service, _ = resolver(CandidateProvider([candidate(), other]))
    result = await service.resolve(source_match(), NOW + timedelta(hours=3))
    assert result.status == ResultIdentityStatus.AMBIGUOUS
    assert result.candidate_count == 2


async def test_duplicate_names_in_other_competition_are_disambiguated():
    cup = candidate(
        provider_match_id="football-data:match:1000",
        competition_id="football-data:competition:2122",
        competition_name="Coppa Italia",
        competition_group=None,
    )
    service, _ = resolver(CandidateProvider([cup, candidate()]))
    result = await service.resolve(source_match(), NOW + timedelta(hours=3))
    assert result.status == ResultIdentityStatus.VERIFIED_EXACT
    assert result.identity.provider_match_id == "football-data:match:999"


async def test_no_candidates_is_not_found():
    service, _ = resolver(CandidateProvider())
    result = await service.resolve(source_match(), NOW + timedelta(hours=3))
    assert result.status == ResultIdentityStatus.NOT_FOUND


@pytest.mark.parametrize("group", ["A", "B", "C"])
async def test_verified_serie_c_groups_resolve_to_football_data_isc(group):
    service, _ = resolver(CandidateProvider([candidate(group)]))
    result = await service.resolve(source_match(group), NOW + timedelta(hours=3))
    assert result.verified
    assert result.identity.competition_id == "football-data:competition:2123"


async def test_manual_verified_mapping_is_versioned_and_reused():
    repository = Repository("sqlite:///:memory:")
    match = source_match()
    repository.save_match(match)
    service, _ = resolver(CandidateProvider(), repository)
    request = ManualResultIdentityMapping(
        source_match_id=match.match_id,
        provider="football_data_org",
        provider_match_id="football-data:match:999",
        provider_competition_id="football-data:competition:2123",
        provider_competition_name="Serie C",
        competition_group="GROUP_A",
        provider_home_team_id="football-data:team:110",
        provider_away_team_id="football-data:team:120",
        provider_home_team_name="Trento FC",
        provider_away_team_name="Pergolettese Calcio",
        provider_kickoff=NOW,
        source_tier="B",
        source_url="https://www.seriec.com/calendario",
        verified_by="operatore",
        notes="Confrontato con calendario ufficiale",
        confirmed=True,
    )
    manual = service.manual_verify(request, NOW + timedelta(hours=1))
    reused = await service.resolve(match, NOW + timedelta(hours=2))
    assert manual.status == ResultIdentityStatus.VERIFIED_MAPPED
    assert reused.resolution_method == "PERSISTED_MATCH_MAPPING"
    assert (
        repository.result_identity_mapping("match", match.match_id)["provenance"]["verified_by"]
        == "operatore"
    )


def test_manual_mapping_requires_explicit_confirmation():
    with pytest.raises(ValidationError):
        ManualResultIdentityMapping(
            source_match_id="x",
            provider="football_data_org",
            provider_match_id="y",
            provider_competition_id="c",
            provider_competition_name="Serie C",
            provider_home_team_id="h",
            provider_away_team_id="a",
            provider_home_team_name="Home",
            provider_away_team_name="Away",
            provider_kickoff=NOW,
            source_tier="B",
            source_url="https://www.seriec.com/",
            verified_by="operator",
            notes="checked",
            confirmed=False,
        )


def test_tier_c_result_claim_is_rejected():
    with pytest.raises(ValidationError):
        MatchResult(
            match_id="x",
            final_status="FINAL",
            home_score=1,
            away_score=0,
            source="unverified",
            source_tier="C",
            source_url="https://example.com/result",
            retrieved_at=NOW,
            provider="tier-c",
            verified_at=NOW,
        )


def test_result_claim_is_append_only_and_idempotent():
    repository = Repository("sqlite:///:memory:")
    result = MatchResult(
        match_id="888it:match:123",
        final_status="FINAL",
        home_score=2,
        away_score=1,
        extra_time_home=3,
        extra_time_away=1,
        penalties_home=4,
        penalties_away=2,
        source="structured",
        source_tier="B",
        source_url="https://api.football-data.org/v4/matches/999",
        published_at=NOW,
        retrieved_at=NOW,
        provider="football_data_org",
        verified_at=NOW,
        provider_status="FINISHED",
    )
    first = repository.save_result_claim(
        match_id=result.match_id, provider_match_id="football-data:match:999", result=result
    )
    second = repository.save_result_claim(
        match_id=result.match_id,
        provider_match_id="football-data:match:999",
        result=result.model_copy(update={"retrieved_at": NOW + timedelta(minutes=5)}),
    )
    rows = repository.list_records("match_result_claims", last=1000)
    assert first == second
    assert len(rows) == 1
    assert rows[0]["regulation_score"] == [2, 1]
    assert rows[0]["extra_time_score"] == [3, 1]
    assert rows[0]["penalties"] == [4, 2]


async def test_composite_conflict_keeps_both_claims_available():
    class DirectProvider:
        def __init__(self, provider_id, score):
            self.provider_id = provider_id
            self.score = score

        async def get_result(self, identity, as_of):
            return MatchResult(
                match_id=identity.match_id,
                final_status="FINAL",
                home_score=self.score[0],
                away_score=self.score[1],
                source=self.provider_id,
                source_tier="A" if self.provider_id == "official" else "B",
                source_url=f"https://example.com/{self.provider_id}",
                retrieved_at=as_of,
                provider=self.provider_id,
                verified_at=as_of,
            )

    composite = CompositeResultProvider(DirectProvider("official", (2, 1)), DirectProvider("tier-b", (1, 1)))
    claims = await composite.get_claims(source_match(), NOW)
    conflict = await composite.get_result(source_match(), NOW)
    assert len(claims) == 2
    assert conflict.final_status == "CONFLICT"


async def test_888_resolution_result_settlement_and_restart_are_idempotent():
    kickoff = NOW + timedelta(hours=2)
    match = source_match(
        match_id="demo-match",
        kickoff=kickoff,
        home_team="Synthetic Home FC",
        away_team="Synthetic Away Calcio",
        home_team_id="888it:team:home",
        away_team_id="888it:team:away",
    )
    resolved = candidate(
        home_team_name="Synthetic Home",
        away_team_name="Synthetic Away",
        home_team_id="football-data:team:home",
        away_team_id="football-data:team:away",
        kickoff=kickoff,
    )
    provider = CandidateProvider([resolved])
    configured = Settings(validation_verified_competitions=(COMPETITIONS["A"],))
    app, repository = runtime(result_provider=provider, configured=configured)
    app.providers.fixtures.match = match
    clock = [NOW]
    await DataPipelineCollector(app, clock=lambda: clock[0]).collect_due()
    clock[0] = kickoff + timedelta(hours=2)
    first = await DataPipelineCollector(app, clock=lambda: clock[0]).collect_due()
    second = await DataPipelineCollector(app, clock=lambda: clock[0]).collect_due()
    assert first["results"]["verified"] == ["demo-match"]
    assert len(first["results"]["settled"]) == 3
    assert second["results"]["settled"] == []
    assert provider.find_calls == 1
    assert provider.fetch_calls == 1
    assert len(repository.list_records("match_result_claims", last=1000)) == 1
    assert len(repository.list_records("match_results", last=1000)) == 1
    assert len(repository.list_records("validation_observations", last=1000)) == 3
    assert len(repository.list_records("settled_predictions", last=1000)) == 3
