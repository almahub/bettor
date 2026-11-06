"""Low-score Dixon–Coles correction to the independent Poisson baseline."""

import math

from better.system_zero.math import finite

from .poisson import ScoreDistribution
from .poisson import score_distribution as poisson_distribution


def score_distribution(
    home_rate: float, away_rate: float, max_goals: int = 12, rho: float = -0.1
) -> ScoreDistribution:
    rho = finite(rho, "rho")
    distribution = poisson_distribution(home_rate, away_rate, max_goals)
    factors = {
        (0, 0): 1 - home_rate * away_rate * rho,
        (0, 1): 1 + home_rate * rho,
        (1, 0): 1 + away_rate * rho,
        (1, 1): 1 - rho,
    }
    if min(factors.values()) < 0:
        raise ValueError("rho creates negative low-score probabilities")
    adjusted = {score: value * factors.get(score, 1) for score, value in distribution.items()}
    total = math.fsum(adjusted.values())
    if total <= 0:
        raise ValueError("Dixon-Coles distribution has no probability mass")
    return {score: value / total for score, value in adjusted.items()}
