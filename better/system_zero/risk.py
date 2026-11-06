"""Conservative portfolio risk limits. No stake recovery or martingale logic."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from .freshness import utc
from .math import fractional_kelly, money, probability, stake_amount


@dataclass(frozen=True)
class RiskLimits:
    kelly_fraction: float = 0.25
    single: float = 0.01
    match: float = 0.015
    portfolio: float = 0.10
    daily: float = 0.05
    weekly: float = 0.10
    team: float = 0.03
    competition: float = 0.08
    correlated: float = 0.015
    max_drawdown: float = 0.20

    def __post_init__(self) -> None:
        for value in vars(self).values():
            probability(value)


@dataclass(frozen=True)
class OpenPosition:
    stake: Decimal
    match_id: str
    competition: str
    team_ids: tuple[str, ...]
    opened_at: datetime
    correlation_group: str = ""
    settled_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "stake", money(self.stake))
        utc(self.opened_at)
        if self.settled_at is not None and utc(self.settled_at) < utc(self.opened_at):
            raise ValueError("a position cannot settle before opening")


@dataclass(frozen=True)
class RiskAssessment:
    accepted: bool
    stake: Decimal
    bankroll_fraction: float
    capped: bool
    reasons: tuple[str, ...]
    exposures: dict[str, Decimal]


def assess_stake(
    p: float,
    odds: float,
    bankroll: Decimal,
    *,
    match_id: str,
    competition: str,
    team_ids: tuple[str, ...],
    as_of: datetime,
    positions: tuple[OpenPosition, ...] = (),
    limits: RiskLimits | None = None,
    confidence: float = 1.0,
    reliability_multiplier: float = 1.0,
    correlation_group: str = "",
    drawdown: float = 0.0,
) -> RiskAssessment:
    """Size within every remaining exposure budget; reject exhausted limits.

    ``positions`` must contain all current liabilities plus historical stakes needed
    for daily/weekly turnover limits. Settled positions release portfolio exposure,
    but still consume the daily/weekly amount staked.
    No independence assumption or covariance discount reduces exposure.
    """
    bankroll, as_of = money(bankroll), utc(as_of)
    limits = limits or RiskLimits()
    confidence, reliability_multiplier = probability(confidence), probability(reliability_multiplier)
    drawdown = probability(drawdown)
    fraction = fractional_kelly(p, odds, limits.kelly_fraction) * confidence * reliability_multiplier
    zero = Decimal(0)
    if bankroll == 0:
        return RiskAssessment(False, zero, 0.0, False, ("ZERO_BANKROLL",), {})
    if any(utc(position.opened_at) > as_of for position in positions):
        raise ValueError("future positions cannot enter risk evaluation")
    active = tuple(
        position for position in positions if position.settled_at is None or utc(position.settled_at) > as_of
    )
    exposures = {
        "portfolio": sum((position.stake for position in active), zero),
        "match": sum((position.stake for position in active if position.match_id == match_id), zero),
        "daily": sum(
            (position.stake for position in positions if utc(position.opened_at).date() == as_of.date()), zero
        ),
        "weekly": sum(
            (
                position.stake
                for position in positions
                if utc(position.opened_at).isocalendar()[:2] == as_of.isocalendar()[:2]
            ),
            zero,
        ),
        "competition": sum(
            (position.stake for position in active if position.competition == competition), zero
        ),
        "correlated": sum(
            (
                position.stake
                for position in active
                if correlation_group and position.correlation_group == correlation_group
            ),
            zero,
        ),
    }
    # Each team has a separate concentration limit; taking the maximum enforces both.
    exposures["team"] = max(
        (
            sum((position.stake for position in active if team_id in position.team_ids), zero)
            for team_id in team_ids
        ),
        default=zero,
    )
    budgets = {
        name: bankroll * Decimal(str(getattr(limits, name))) - amount for name, amount in exposures.items()
    }
    reasons = [f"{name.upper()}_EXPOSURE_LIMIT" for name, amount in budgets.items() if amount <= 0]
    if drawdown >= limits.max_drawdown:
        reasons.append("DRAWDOWN_LIMIT")
    if fraction == 0:
        reasons.append("NO_POSITIVE_KELLY")
    if reasons:
        return RiskAssessment(False, zero, 0.0, False, tuple(reasons), exposures)
    raw_stake = bankroll * Decimal(str(fraction))
    allowed = min(raw_stake, bankroll * Decimal(str(limits.single)), *budgets.values())
    # Exact decimal floor: never round up through a hard limit.
    units = (allowed // Decimal("0.01")) * Decimal("0.01")
    if units <= 0:
        return RiskAssessment(False, zero, 0.0, allowed < raw_stake, ("BELOW_MINIMUM_UNIT",), exposures)
    return RiskAssessment(True, units, float(units / bankroll), allowed < raw_stake, (), exposures)


def capped_stake(
    p: float, odds: float, bankroll: Decimal, *, fraction: float = 0.25, max_fraction: float = 0.01
) -> Decimal:
    """Single-position convenience function; use assess_stake for portfolio decisions."""
    return stake_amount(bankroll, min(fractional_kelly(p, odds, fraction), probability(max_fraction)))
