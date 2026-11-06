"""Chronological data partitioning and cutoff-safe forecast evaluation."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from better.models.calibration import brier_score, log_loss, reliability_curve

from .freshness import utc
from .math import probability


@dataclass(frozen=True)
class HistoricalPrediction:
    event_id: str
    predicted_at: datetime
    kickoff: datetime
    features_available_at: tuple[datetime, ...]
    outcome_available_at: datetime
    predicted_probability: float
    outcome: int
    model_version: str
    feature_version: str
    competition: str
    market: str
    odds_source: str
    execution_assumption: str
    training_cutoff: datetime

    def __post_init__(self) -> None:
        prediction, kickoff = utc(self.predicted_at), utc(self.kickoff)
        if prediction >= kickoff:
            raise ValueError("a pre-match prediction must precede kickoff")
        if utc(self.training_cutoff) > prediction:
            raise ValueError("training cutoff is after prediction")
        if not self.features_available_at or any(
            utc(value) > prediction for value in self.features_available_at
        ):
            raise ValueError("feature availability violates prediction cutoff")
        if utc(self.outcome_available_at) < kickoff:
            raise ValueError("outcome predates kickoff")
        probability(self.predicted_probability)
        if self.outcome not in (0, 1):
            raise ValueError("outcome must be binary")
        if not all(
            (
                self.event_id,
                self.model_version,
                self.feature_version,
                self.competition,
                self.market,
                self.odds_source,
                self.execution_assumption,
            )
        ):
            raise ValueError("backtest provenance metadata is mandatory")


def temporal_split(
    records: Sequence[HistoricalPrediction],
    *,
    train_end: datetime,
    validation_end: datetime,
    test_end: datetime,
) -> dict[str, list[HistoricalPrediction]]:
    train_end, validation_end, test_end = utc(train_end), utc(validation_end), utc(test_end)
    if not train_end < validation_end < test_end:
        raise ValueError("TRAIN, VALIDATION and TEST boundaries must be ordered")
    result: dict[str, list[HistoricalPrediction]] = {"TRAIN": [], "VALIDATION": [], "TEST": [], "LIVE": []}
    seen: set[str] = set()
    for record in sorted(records, key=lambda row: utc(row.predicted_at)):
        if record.event_id in seen:
            raise ValueError("duplicate event could leak across dataset splits")
        seen.add(record.event_id)
        timestamp = utc(record.outcome_available_at)
        split = (
            "TRAIN"
            if timestamp <= train_end
            else "VALIDATION"
            if timestamp <= validation_end
            else "TEST"
            if timestamp <= test_end
            else "LIVE"
        )
        result[split].append(record)
    return result


def evaluate(records: Sequence[HistoricalPrediction], *, as_of: datetime) -> dict[str, object]:
    cutoff = utc(as_of)
    if not records:
        raise ValueError("no historical predictions")
    if any(utc(record.outcome_available_at) > cutoff for record in records):
        raise ValueError("future outcomes cannot be used for historical validation")
    predictions = [record.predicted_probability for record in records]
    outcomes = [record.outcome for record in records]
    return {
        "data_cutoff": cutoff.isoformat(),
        "sample_size": len(records),
        "brier_score": brier_score(predictions, outcomes),
        "log_loss": log_loss(predictions, outcomes),
        "reliability_curve": reliability_curve(predictions, outcomes),
        "model_versions": sorted({record.model_version for record in records}),
        "feature_versions": sorted({record.feature_version for record in records}),
    }
