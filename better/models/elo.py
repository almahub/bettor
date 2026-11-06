"""Configurable Elo baseline; draw probability is an explicit fitted input."""

import math

from better.system_zero.math import finite, probability


def probabilities(
    home_rating: float,
    away_rating: float,
    *,
    home_advantage: float = 60,
    draw_probability: float = 0.25,
    scale: float = 400,
) -> dict[str, float]:
    difference = finite(home_rating) + finite(home_advantage) - finite(away_rating)
    scale, draw = finite(scale), probability(draw_probability)
    if scale <= 0:
        raise ValueError("Elo scale must be positive")
    logit = math.log(10) * difference / scale
    home_share = 1 / (1 + math.exp(-logit)) if logit >= 0 else math.exp(logit) / (1 + math.exp(logit))
    return {"HOME": (1 - draw) * home_share, "DRAW": draw, "AWAY": (1 - draw) * (1 - home_share)}


def update(
    home_rating: float, away_rating: float, outcome: float, *, k: float = 20, home_advantage: float = 60
) -> tuple[float, float]:
    home, away, k = finite(home_rating), finite(away_rating), finite(k)
    outcome = probability(outcome)
    if k < 0:
        raise ValueError("K must be nonnegative")
    expected = probabilities(home, away, home_advantage=home_advantage, draw_probability=0)["HOME"]
    change = k * (outcome - expected)
    return home + change, away - change
