"""Independent model validity and performance calculations."""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from better.models.calibration import brier_score, expected_calibration_error, log_loss

from .math import decimal_odds, money, probability


def validate_multiclass_model(
    predictions,
    actuals,
    *,
    min_sample=200,
    max_brier=0.25,
    max_calibration_error=0.15,
    degraded_calibration_error=0.08,
):
    """Assess complete categorical distributions, one fixture per observation."""
    if len(predictions) != len(actuals):
        raise ValueError("predictions and outcomes must align")
    if not predictions:
        return ValidationAssessment("INSUFFICIENT_SAMPLE", 0, 0, None, None, None, "No historical outcomes")
    classes = tuple(predictions[0])
    if len(classes) < 2 or any(tuple(row) != classes for row in predictions):
        raise ValueError("multiclass predictions require one stable complete class set")
    for row, actual in zip(predictions, actuals, strict=True):
        if actual not in row or abs(sum(row.values()) - 1) > 1e-8:
            raise ValueError("invalid multiclass observation")
        for value in row.values():
            probability(value)
    # Mean per-class Brier remains in [0, 1], matching the existing threshold contract.
    brier = math.fsum(
        math.fsum((row[label] - int(label == actual)) ** 2 for label in classes) / len(classes)
        for row, actual in zip(predictions, actuals, strict=True)
    ) / len(predictions)
    loss = -math.fsum(
        math.log(min(1 - 1e-15, max(1e-15, row[actual])))
        for row, actual in zip(predictions, actuals, strict=True)
    ) / len(predictions)
    confidences = [max(row.values()) for row in predictions]
    correct = [int(max(row, key=row.get) == actual) for row, actual in zip(predictions, actuals, strict=True)]
    from better.models.calibration import expected_calibration_error

    calibration = expected_calibration_error(confidences, correct)
    return _assessment_from_metrics(
        len(predictions),
        brier,
        loss,
        calibration,
        min_sample=min_sample,
        max_brier=max_brier,
        max_calibration_error=max_calibration_error,
        degraded_calibration_error=degraded_calibration_error,
    )


def _assessment_from_metrics(
    sample_size,
    brier,
    loss,
    calibration,
    *,
    min_sample,
    max_brier,
    max_calibration_error,
    degraded_calibration_error,
):
    if sample_size < min_sample:
        return ValidationAssessment(
            "INSUFFICIENT_SAMPLE", 0, sample_size, brier, loss, calibration, "Sample below configured minimum"
        )
    if brier > max_brier or calibration > max_calibration_error:
        return ValidationAssessment(
            "SUSPEND_MODEL",
            0,
            sample_size,
            brier,
            loss,
            calibration,
            "Predictive quality or calibration exceeds configured limit",
        )
    if calibration > degraded_calibration_error:
        return ValidationAssessment(
            "REDUCED_WEIGHT",
            0.5,
            sample_size,
            brier,
            loss,
            calibration,
            "Calibration degraded; reduce operational weight",
        )
    return ValidationAssessment(
        "NORMAL_WEIGHT", 1, sample_size, brier, loss, calibration, "Configured validation checks passed"
    )


@dataclass(frozen=True)
class ValidationAssessment:
    status: str
    recommended_weight_multiplier: float
    sample_size: int
    brier: float | None
    log_loss: float | None
    calibration_error: float | None
    reason: str


def validate_model(
    predictions: Sequence[float],
    outcomes: Sequence[int],
    *,
    min_sample: int = 200,
    max_brier: float = 0.25,
    max_calibration_error: float = 0.15,
    degraded_calibration_error: float = 0.08,
) -> ValidationAssessment:
    if not isinstance(min_sample, int) or isinstance(min_sample, bool) or min_sample < 1:
        raise ValueError("minimum sample must be a positive integer")
    for threshold in (max_brier, max_calibration_error, degraded_calibration_error):
        probability(threshold)
    if degraded_calibration_error > max_calibration_error:
        raise ValueError("degraded calibration threshold exceeds suspension threshold")
    if len(predictions) != len(outcomes):
        raise ValueError("predictions and outcomes must align")
    if not predictions:
        return ValidationAssessment("INSUFFICIENT_SAMPLE", 0, 0, None, None, None, "No historical outcomes")
    brier, loss = brier_score(predictions, outcomes), log_loss(predictions, outcomes)
    calibration = expected_calibration_error(predictions, outcomes)
    if len(predictions) < min_sample:
        return ValidationAssessment(
            "INSUFFICIENT_SAMPLE",
            0,
            len(predictions),
            brier,
            loss,
            calibration,
            "Sample below configured minimum",
        )
    if brier > max_brier or calibration > max_calibration_error:
        return ValidationAssessment(
            "SUSPEND_MODEL",
            0,
            len(predictions),
            brier,
            loss,
            calibration,
            "Predictive quality or calibration exceeds configured limit",
        )
    if calibration > degraded_calibration_error:
        return ValidationAssessment(
            "REDUCED_WEIGHT",
            0.5,
            len(predictions),
            brier,
            loss,
            calibration,
            "Calibration degraded; reduce operational weight",
        )
    return ValidationAssessment(
        "NORMAL_WEIGHT", 1, len(predictions), brier, loss, calibration, "Configured validation checks passed"
    )


def closing_line_value(entry_odds: float, closing_odds: float) -> float:
    """Relative price improvement: entry / closing − 1; not realized profit."""
    return decimal_odds(entry_odds) / decimal_odds(closing_odds) - 1


def performance(
    profits: Sequence[Decimal], stakes: Sequence[Decimal], initial_bankroll: Decimal
) -> dict[str, float | int | Decimal]:
    initial = money(initial_bankroll)
    if initial == 0 or not profits or len(profits) != len(stakes):
        raise ValueError("positive initial bankroll and nonempty aligned settled bets required")
    clean_stakes = [money(stake) for stake in stakes]
    if any(not isinstance(profit, Decimal) or not profit.is_finite() for profit in profits):
        raise ValueError("profits must be finite Decimal values")
    turnover = sum(clean_stakes, Decimal(0))
    if turnover == 0:
        raise ValueError("turnover must be positive")
    if any(stake == 0 or profit < -stake for stake, profit in zip(clean_stakes, profits, strict=True)):
        raise ValueError("settled back bets require positive stake and loss no greater than stake")
    equity, peak, max_drawdown = initial, initial, Decimal(0)
    for profit in profits:
        equity += profit
        if equity < 0:
            raise ValueError("settlements exceed available bankroll")
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, (peak - equity) / peak)
    total_profit = sum(profits, Decimal(0))
    returns = [float(profit / stake) for profit, stake in zip(profits, clean_stakes, strict=True)]
    mean = math.fsum(returns) / len(returns)
    volatility = math.sqrt(math.fsum((value - mean) ** 2 for value in returns) / len(returns))
    return {
        "sample_size": len(profits),
        "profit": total_profit,
        "turnover": turnover,
        "roi": float(total_profit / initial),
        "yield": float(total_profit / turnover),
        "max_drawdown": float(max_drawdown),
        "volatility": volatility,
    }
