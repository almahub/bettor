"""Auditable decimal-odds arithmetic. Edge uses probability points, not ROI."""

import math
from collections.abc import Sequence
from decimal import ROUND_DOWN, Decimal


def finite(value: float, name: str = "value") -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number, not boolean")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def probability(value: float) -> float:
    value = finite(value, "probability")
    if not 0 <= value <= 1:
        raise ValueError("probability must be in [0, 1]")
    return value


def decimal_odds(value: float) -> float:
    value = finite(value, "decimal odds")
    if value <= 1:
        raise ValueError("decimal odds must exceed 1")
    return value


def implied_probability(odds: float) -> float:
    return 1 / decimal_odds(odds)


def bookmaker_margin(odds: Sequence[float]) -> float:
    if len(odds) < 2:
        raise ValueError("a complete market needs at least two outcomes")
    return math.fsum(implied_probability(value) for value in odds) - 1


def devig(odds: Sequence[float]) -> list[float]:
    """Proportional normalization of a COMPLETE mutually exclusive market.

    Caller must establish completeness and matching bookmaker/timestamp.
    Negative overround is permitted, but never interpreted as certain profit.
    """
    if len(odds) < 2:
        raise ValueError("de-vigging requires a complete market")
    implied = [implied_probability(value) for value in odds]
    total = math.fsum(implied)
    return [value / total for value in implied]


def fair_odds(p: float) -> float:
    p = probability(p)
    if p == 0:
        raise ValueError("zero probability has no finite fair odds")
    return finite(1 / p, "fair odds")


def expected_value(p: float, odds: float) -> float:
    """Expected net profit per unit staked, for a binary no-push selection."""
    return probability(p) * decimal_odds(odds) - 1


def edge(p: float, no_vig_p: float) -> float:
    return probability(p) - probability(no_vig_p)


def minimum_odds(p: float, min_ev: float = 0.0) -> float:
    p, min_ev = probability(p), finite(min_ev, "minimum EV")
    if p == 0 or min_ev < 0:
        raise ValueError("minimum odds requires positive probability and nonnegative EV")
    return finite((1 + min_ev) / p, "minimum odds")


def kelly(p: float, odds: float) -> float:
    p, odds = probability(p), decimal_odds(odds)
    return max(0.0, min(1.0, (p * odds - 1) / (odds - 1)))


def fractional_kelly(p: float, odds: float, fraction: float = 0.25) -> float:
    return kelly(p, odds) * probability(fraction)


def money(value: Decimal | str | int) -> Decimal:
    if isinstance(value, (float, bool)):
        raise ValueError("money must be Decimal, string or integer, never float")
    result = Decimal(value)
    if not result.is_finite() or result < 0:
        raise ValueError("money must be finite and nonnegative")
    return result


def stake_amount(bankroll: Decimal, fraction: float, quantum: Decimal = Decimal("0.01")) -> Decimal:
    bankroll, fraction = money(bankroll), probability(fraction)
    if not quantum.is_finite() or quantum <= 0:
        raise ValueError("rounding quantum must be positive and finite")
    units = (bankroll * Decimal(str(fraction)) / quantum).to_integral_value(rounding=ROUND_DOWN)
    return units * quantum


def push_expected_value(win: float, loss: float, odds: float) -> float:
    """For DNB/integer handicap: pushes return stake, contributing zero profit."""
    win, loss = probability(win), probability(loss)
    if win + loss > 1 + 1e-12:
        raise ValueError("win and loss probabilities overlap")
    return win * (decimal_odds(odds) - 1) - loss


def push_fair_odds(win: float, loss: float) -> float:
    win, loss = probability(win), probability(loss)
    if win == 0 or win + loss > 1 + 1e-12:
        raise ValueError("invalid win/loss distribution")
    return finite(1 + loss / win, "push fair odds")


def matched_back_lay(
    back_stake: Decimal,
    back_odds: float,
    lay_odds: float,
    commission: float = 0.0,
    *,
    stake_returned: bool = True,
) -> dict[str, Decimal]:
    """Equal-profit back/lay hedge; excludes promotion eligibility and rollover."""
    stake = money(back_stake)
    back, lay = Decimal(str(decimal_odds(back_odds))), Decimal(str(decimal_odds(lay_odds)))
    fee = Decimal(str(probability(commission)))
    if fee == 1:
        raise ValueError("exchange commission must be below 1")
    effective = back if stake_returned else back - 1
    lay_stake = stake * effective / (lay - fee)
    liability = lay_stake * (lay - 1)
    win_profit = stake * (back - 1) - liability
    lose_profit = lay_stake * (1 - fee) - (stake if stake_returned else 0)
    return {
        "lay_stake": lay_stake,
        "liability": liability,
        "win_profit": win_profit,
        "lose_profit": lose_profit,
    }
