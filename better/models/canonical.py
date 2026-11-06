"""Canonical deterministic Poisson feature and probability construction."""

from better.domain.enums import MarketType
from better.models.poisson import market_probabilities, score_distribution

MODEL_NAME = "poisson"
MODEL_VERSION = "1.0.0"
FEATURE_SCHEMA_VERSION = "1.0.0"


def poisson_from_stats(home, away, requested_markets):
    """Return the exact V1 Poisson inputs and complete market distributions."""
    home_rate = (
        (home.xg_per_match if home.xg_per_match is not None else home.goals_for_per_match)
        + (away.xga_per_match if away.xga_per_match is not None else away.goals_against_per_match)
    ) / 2
    away_rate = (
        (away.xg_per_match if away.xg_per_match is not None else away.goals_for_per_match)
        + (home.xga_per_match if home.xga_per_match is not None else home.goals_against_per_match)
    ) / 2
    distribution = score_distribution(home_rate, away_rate)
    requested = {MarketType(value) for value in requested_markets}
    probabilities = {
        MarketType(key): values
        for key, values in market_probabilities(distribution).items()
        if MarketType(key) in requested
    }
    return {
        "features": {"home_goal_rate": home_rate, "away_goal_rate": away_rate},
        "probabilities": probabilities,
        "score_distribution": distribution,
        "sample_size": min(home.sample_size, away.sample_size),
    }
