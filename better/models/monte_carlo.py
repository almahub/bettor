"""Seeded sampling of an explicitly supplied score distribution."""

from collections import Counter
from collections.abc import Mapping
from random import Random

from .poisson import ScoreDistribution, validate_distribution


def simulate(
    distribution: Mapping[tuple[int, int], float], samples: int = 10000, seed: int = 42
) -> ScoreDistribution:
    validate_distribution(distribution)
    if not isinstance(samples, int) or isinstance(samples, bool) or samples < 1:
        raise ValueError("samples must be a positive integer")
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise ValueError("an explicit integer seed is required")
    scores = sorted(distribution)
    observations = Counter(
        Random(seed).choices(scores, weights=[distribution[score] for score in scores], k=samples)
    )
    return {score: observations.get(score, 0) / samples for score in scores}
