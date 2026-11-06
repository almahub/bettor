"""UTC-aware freshness and strict historical-availability checks."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


def utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("naive timestamps are forbidden")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class FreshnessResult:
    age_seconds: float
    fresh: bool
    score: float


def freshness(observed_at: datetime, as_of: datetime, max_age: timedelta) -> FreshnessResult:
    observed_at, as_of = utc(observed_at), utc(as_of)
    seconds = max_age.total_seconds()
    if seconds <= 0:
        raise ValueError("freshness limit must be positive")
    age = (as_of - observed_at).total_seconds()
    if age < 0:
        raise ValueError("future data cannot be used as historical evidence")
    # Boundary is fresh. Score is a conservative step function, never misleadingly >0 for stale data.
    return FreshnessResult(age, age <= seconds, 1.0 if age <= seconds else 0.0)


def available_as_of(
    as_of: datetime,
    *,
    retrieved_at: datetime,
    published_at: datetime | None = None,
    observed_at: datetime | None = None,
) -> bool:
    cutoff = utc(as_of)
    return all(
        utc(value) <= cutoff for value in (retrieved_at, published_at, observed_at) if value is not None
    )


def require_as_of(
    as_of: datetime,
    *,
    retrieved_at: datetime,
    published_at: datetime | None = None,
    observed_at: datetime | None = None,
) -> None:
    if not available_as_of(
        as_of, retrieved_at=retrieved_at, published_at=published_at, observed_at=observed_at
    ):
        raise ValueError("record was not available at analysis as_of")
