"""Binary forecast scoring and reliability buckets, independent of prediction code."""

import math
from collections.abc import Sequence

from better.system_zero.math import probability


def _pairs(predictions: Sequence[float], outcomes: Sequence[int]) -> list[tuple[float, int]]:
    if not predictions or len(predictions) != len(outcomes):
        raise ValueError("nonempty aligned predictions and outcomes required")
    if any(outcome not in (0, 1) for outcome in outcomes):
        raise ValueError("binary outcomes required")
    return [(probability(p), int(y)) for p, y in zip(predictions, outcomes, strict=True)]


def brier_score(predictions: Sequence[float], outcomes: Sequence[int]) -> float:
    pairs = _pairs(predictions, outcomes)
    return math.fsum((p - y) ** 2 for p, y in pairs) / len(pairs)


def log_loss(predictions: Sequence[float], outcomes: Sequence[int], *, epsilon: float = 1e-15) -> float:
    pairs = _pairs(predictions, outcomes)
    epsilon = probability(epsilon)
    if epsilon == 0 or epsilon >= 0.5:
        raise ValueError("epsilon must be in (0, 0.5)")
    return -math.fsum(
        y * math.log(min(1 - epsilon, max(epsilon, p)))
        + (1 - y) * math.log(min(1 - epsilon, max(epsilon, 1 - p)))
        for p, y in pairs
    ) / len(pairs)


def reliability_curve(
    predictions: Sequence[float], outcomes: Sequence[int], bins: int = 10
) -> list[dict[str, float | int | None]]:
    pairs = _pairs(predictions, outcomes)
    if not isinstance(bins, int) or isinstance(bins, bool) or bins < 1:
        raise ValueError("bins must be a positive integer")
    result = []
    for bucket in range(bins):
        selected = [(p, y) for p, y in pairs if min(int(p * bins), bins - 1) == bucket]
        count = len(selected)
        result.append(
            {
                "lower": bucket / bins,
                "upper": (bucket + 1) / bins,
                "count": count,
                "mean_prediction": math.fsum(p for p, _ in selected) / count if count else None,
                "observed_frequency": math.fsum(y for _, y in selected) / count if count else None,
            }
        )
    return result


def expected_calibration_error(
    predictions: Sequence[float], outcomes: Sequence[int], bins: int = 10
) -> float:
    buckets = reliability_curve(predictions, outcomes, bins)
    return math.fsum(
        bucket["count"] * abs(bucket["mean_prediction"] - bucket["observed_frequency"])
        for bucket in buckets
        if bucket["count"]
    ) / len(predictions)
