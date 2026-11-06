"""Explicit reliability-weighted ensemble; suspended models have no influence."""

import math
from collections.abc import Mapping

from better.system_zero.math import finite, probability


def combine(
    predictions: Mapping[str, Mapping[str, float]],
    weights: Mapping[str, float],
    *,
    suspended: frozenset[str] = frozenset(),
) -> dict[str, float]:
    active = {
        name: finite(weights.get(name, 0), "model weight") for name in predictions if name not in suspended
    }
    if any(weight < 0 for weight in active.values()):
        raise ValueError("model weights cannot be negative")
    active = {name: weight for name, weight in active.items() if weight > 0}
    if not active:
        raise ValueError("no admitted models with positive reliability weight")
    outcomes = set(predictions[next(iter(active))])
    if len(outcomes) < 2:
        raise ValueError("a complete probability market is required")
    for name in active:
        if set(predictions[name]) != outcomes:
            raise ValueError("ensemble outcome sets differ")
        for value in predictions[name].values():
            probability(value)
        if not math.isclose(math.fsum(predictions[name].values()), 1, abs_tol=1e-9):
            raise ValueError("each model must supply a normalized distribution")
    total = math.fsum(active.values())
    return {
        outcome: math.fsum(predictions[name][outcome] * weight for name, weight in active.items()) / total
        for outcome in sorted(outcomes)
    }
