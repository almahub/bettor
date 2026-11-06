"""Small deterministic binary logistic model; training must pass explicit cutoffs."""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from better.system_zero.freshness import utc
from better.system_zero.math import finite


def sigmoid(value: float) -> float:
    value = finite(value)
    return 1 / (1 + math.exp(-value)) if value >= 0 else math.exp(value) / (1 + math.exp(value))


@dataclass(frozen=True)
class LogisticModel:
    coefficients: tuple[float, ...]
    intercept: float
    trained_as_of: datetime

    def __post_init__(self) -> None:
        if not self.coefficients:
            raise ValueError("model requires at least one feature")
        for value in (*self.coefficients, self.intercept):
            finite(value)
        utc(self.trained_as_of)

    def predict(self, features: Sequence[float]) -> float:
        if len(features) != len(self.coefficients):
            raise ValueError("feature count differs from fitted model")
        return sigmoid(
            self.intercept
            + math.fsum(
                weight * finite(feature) for weight, feature in zip(self.coefficients, features, strict=True)
            )
        )


def fit(
    features: Sequence[Sequence[float]],
    labels: Sequence[int],
    *,
    available_at: Sequence[datetime],
    as_of: datetime,
    epochs: int = 500,
    learning_rate: float = 0.1,
    l2: float = 0.01,
) -> LogisticModel:
    cutoff = utc(as_of)
    if not features or len(features) != len(labels) or len(features) != len(available_at):
        raise ValueError("training rows, outcomes and timestamps must align")
    if any(utc(value) > cutoff for value in available_at):
        raise ValueError("training data contains unavailable future outcomes/features")
    if any(label not in (0, 1) for label in labels):
        raise ValueError("binary labels are required")
    columns = len(features[0])
    if columns < 1 or any(len(row) != columns for row in features):
        raise ValueError("feature dimensions are inconsistent")
    rows = [[finite(value) for value in row] for row in features]
    rate, regularization = finite(learning_rate), finite(l2)
    if (
        not isinstance(epochs, int)
        or isinstance(epochs, bool)
        or epochs < 1
        or rate <= 0
        or regularization < 0
    ):
        raise ValueError("invalid optimizer settings")
    coefficients, intercept = [0.0] * columns, 0.0
    for _ in range(epochs):
        residuals = [
            sigmoid(intercept + math.fsum(c * x for c, x in zip(coefficients, row, strict=True))) - label
            for row, label in zip(rows, labels, strict=True)
        ]
        for column in range(columns):
            gradient = math.fsum(
                error * row[column] for error, row in zip(residuals, rows, strict=True)
            ) / len(rows)
            coefficients[column] -= rate * (gradient + regularization * coefficients[column])
        intercept -= rate * math.fsum(residuals) / len(rows)
    return LogisticModel(tuple(coefficients), intercept, cutoff)
