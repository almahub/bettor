from datetime import UTC, datetime, timedelta

import pytest

from better.domain.models import Match
from better.storage.repository import Repository

NOW = datetime(2026, 10, 1, 12, tzinfo=UTC)
MATCH = Match(
    match_id="888it:match:1",
    competition="888it:competition:1",
    season="unknown",
    kickoff=NOW + timedelta(hours=2),
    home_team="Home",
    away_team="Away",
    home_team_id="888it:team:1",
    away_team_id="888it:team:2",
)


def repository():
    return Repository("sqlite:///:memory:")


def test_verified_mapping_is_persisted_and_conflicts_fail_closed():
    repo = repository()
    repo.save_identity_mapping(
        kind="team",
        canonical_id=MATCH.home_team_id,
        target_id="sr:competitor:1",
        confidence=0.98,
        method="AUTO_HIGH_CONFIDENCE",
        verified_at=NOW,
        evidence={"hash": "a"},
    )
    assert repo.identity_target("team", MATCH.home_team_id) == "sr:competitor:1"
    repo.save_identity_mapping(
        kind="team",
        canonical_id=MATCH.home_team_id,
        target_id="sr:competitor:9",
        confidence=0.99,
        method="MANUAL",
        verified_at=NOW,
        evidence={"hash": "b"},
    )
    with pytest.raises(ValueError, match="Conflicting verified"):
        repo.identity_target("team", MATCH.home_team_id)


def test_coverage_reasons_and_point_in_time_archive_are_auditable():
    repo = repository()
    repo.record_identity_attempt(
        match_id=MATCH.match_id,
        attempted_at=NOW,
        result="FAILED",
        reason="NO_CANDIDATE",
        candidates=[],
    )
    coverage = repo.identity_coverage()
    assert coverage["coverage"] == 0
    assert coverage["failure_reasons"] == {"NO_CANDIDATE": 1}
    repo.archive_pre_match_observation(
        match=MATCH,
        as_of=NOW,
        collected_at=NOW + timedelta(seconds=2),
        snapshots={},
        failures={},
        purpose="TEST",
    )
    status = repo.historical_collection_status()
    assert status["observations"] == 1
    assert status["eligible_for_model_validation"] == 0
    with pytest.raises(ValueError, match="pre-match cutoff"):
        repo.archive_pre_match_observation(
            match=MATCH,
            as_of=NOW,
            collected_at=MATCH.kickoff,
            snapshots={},
            failures={},
            purpose="INVALID",
        )
