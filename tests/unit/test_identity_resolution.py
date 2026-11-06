from datetime import UTC, datetime, timedelta

from better.domain.models import Match
from better.services.identity_resolution import SportradarIdentityResolver, name_similarity

NOW = datetime(2026, 10, 1, 12, tzinfo=UTC)
MATCH = Match(
    match_id="888it:match:10",
    competition="888it:competition:20",
    season="unknown",
    kickoff=NOW + timedelta(hours=4),
    home_team="Internazionale Milano",
    away_team="Juventus FC",
    home_team_id="888it:team:1",
    away_team_id="888it:team:2",
)


def row(identifier, home="Inter Milan", away="Juventus", offset=0, competition="Serie A"):
    return {
        "sport_event": {
            "id": f"sr:sport_event:{identifier}",
            "start_time": (MATCH.kickoff + timedelta(minutes=offset)).isoformat(),
            "competitors": [
                {"id": "sr:competitor:1", "name": home, "qualifier": "home"},
                {"id": "sr:competitor:2", "name": away, "qualifier": "away"},
            ],
            "sport_event_context": {"competition": {"id": "sr:competition:23", "name": competition}},
        }
    }


def test_alias_normalization_and_high_confidence_resolution():
    assert name_similarity("Juventus FC", "Juventus") == 1
    resolver = SportradarIdentityResolver(minimum_name_score=0.7)
    ranked = resolver.rank(MATCH, [row(1)], competition_name="Serie A")
    selected, reason = resolver.decide(ranked)
    assert reason == "AUTO_VERIFIED"
    assert selected.event_id == "sr:sport_event:1"


def test_close_runner_up_is_fail_closed_and_exposes_reason():
    resolver = SportradarIdentityResolver(minimum_name_score=0.7)
    ranked = resolver.rank(MATCH, [row(1), row(2, offset=1)], competition_name="Serie A")
    selected, reason = resolver.decide(ranked)
    assert selected is None
    assert reason == "AMBIGUOUS_CANDIDATES"


def test_orientation_and_competition_mismatch_are_not_auto_verified():
    resolver = SportradarIdentityResolver(minimum_name_score=0.7)
    assert resolver.rank(MATCH, [row(1, home="Juventus", away="Inter Milan")]) == []
    ranked = resolver.rank(MATCH, [row(2, competition="Premier League")], competition_name="Serie A")
    selected, reason = resolver.decide(ranked)
    assert selected is None
    assert reason == "COMPETITION_MISMATCH"
