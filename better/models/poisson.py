"""Independent-goals Poisson baseline with explicit truncation checks."""

import math
from collections.abc import Mapping

from better.system_zero.math import finite, probability

ScoreDistribution = dict[tuple[int, int], float]


def poisson_pmf(rate: float, goals: int) -> float:
    rate = finite(rate, "goal rate")
    if rate < 0 or not isinstance(goals, int) or isinstance(goals, bool) or goals < 0:
        raise ValueError("goal rate and integer goal count must be nonnegative")
    if rate == 0:
        return float(goals == 0)
    return math.exp(-rate + goals * math.log(rate) - math.lgamma(goals + 1))


def truncated_mass(home_rate: float, away_rate: float, max_goals: int = 12) -> float:
    if not isinstance(max_goals, int) or isinstance(max_goals, bool) or max_goals < 1:
        raise ValueError("max_goals must be a positive integer")
    home = math.fsum(poisson_pmf(home_rate, goals) for goals in range(max_goals + 1))
    away = math.fsum(poisson_pmf(away_rate, goals) for goals in range(max_goals + 1))
    return home * away


def score_distribution(home_rate: float, away_rate: float, max_goals: int = 12) -> ScoreDistribution:
    mass = truncated_mass(home_rate, away_rate, max_goals)
    if mass < 0.999:
        raise ValueError("score cutoff loses >0.1% mass; increase max_goals")
    return {
        (home, away): poisson_pmf(home_rate, home) * poisson_pmf(away_rate, away) / mass
        for home in range(max_goals + 1)
        for away in range(max_goals + 1)
    }


def validate_distribution(distribution: Mapping[tuple[int, int], float]) -> None:
    if not distribution:
        raise ValueError("score distribution cannot be empty")
    for score, value in distribution.items():
        if len(score) != 2 or any(
            not isinstance(goal, int) or isinstance(goal, bool) or goal < 0 for goal in score
        ):
            raise ValueError("score keys require two nonnegative integers")
        probability(value)
    if not math.isclose(math.fsum(distribution.values()), 1, abs_tol=1e-9):
        raise ValueError("score distribution must sum to one")


def market_probabilities(distribution: Mapping[tuple[int, int], float]) -> dict[str, dict[str, float]]:
    validate_distribution(distribution)
    home = math.fsum(p for (h, a), p in distribution.items() if h > a)
    draw = math.fsum(p for (h, a), p in distribution.items() if h == a)
    away = math.fsum(p for (h, a), p in distribution.items() if h < a)
    over = math.fsum(p for (h, a), p in distribution.items() if h + a >= 3)
    under = math.fsum(p for (h, a), p in distribution.items() if h + a < 3)
    yes = math.fsum(p for (h, a), p in distribution.items() if h > 0 and a > 0)
    no = math.fsum(p for (h, a), p in distribution.items() if h == 0 or a == 0)
    return {
        "1X2": {"HOME": home, "DRAW": draw, "AWAY": away},
        "OVER_UNDER_2_5": {"OVER_2_5": over, "UNDER_2_5": under},
        "BTTS": {"YES": yes, "NO": no},
    }
