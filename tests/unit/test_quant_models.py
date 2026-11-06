import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from better.models.calibration import brier_score, expected_calibration_error, log_loss, reliability_curve
from better.models.dixon_coles import score_distribution as dixon_coles
from better.models.elo import probabilities as elo_probabilities
from better.models.elo import update as elo_update
from better.models.ensemble import combine
from better.models.logistic import fit
from better.models.monte_carlo import simulate
from better.models.poisson import market_probabilities, poisson_pmf, score_distribution, truncated_mass
from better.system_zero.backtest import HistoricalPrediction, evaluate, temporal_split
from better.system_zero.validation import closing_line_value, performance, validate_model


def test_poisson_distribution_and_markets():
    distribution = score_distribution(1.5, 1.0)
    assert sum(distribution.values()) == pytest.approx(1)
    assert poisson_pmf(1.5, 0) == pytest.approx(math.exp(-1.5))
    assert truncated_mass(1.5, 1.0) > 0.999
    markets = market_probabilities(distribution)
    for probabilities in markets.values():
        assert sum(probabilities.values()) == pytest.approx(1)
        assert all(0 <= p <= 1 for p in probabilities.values())
    assert markets["1X2"]["HOME"] > markets["1X2"]["AWAY"]
    assert markets["BTTS"]["YES"] == pytest.approx((1 - math.exp(-1.5)) * (1 - math.exp(-1)), abs=1e-7)


def test_poisson_zero_rate_and_unsafe_truncation():
    distribution = score_distribution(0, 0)
    assert distribution[(0, 0)] == 1
    assert market_probabilities(distribution)["1X2"]["DRAW"] == 1
    with pytest.raises(ValueError):
        score_distribution(20, 20)
    with pytest.raises(ValueError):
        score_distribution(-1, 1)
    with pytest.raises(ValueError):
        market_probabilities({(0, 0): 0.5})


def test_dixon_coles_correction_is_normalized_and_rho_validated():
    independent = score_distribution(1.5, 1)
    adjusted = dixon_coles(1.5, 1, rho=-0.1)
    assert sum(adjusted.values()) == pytest.approx(1)
    assert adjusted[(0, 0)] > independent[(0, 0)]
    assert adjusted[(1, 1)] > independent[(1, 1)]
    assert dixon_coles(1.5, 1, rho=0) == pytest.approx(independent)
    with pytest.raises(ValueError):
        dixon_coles(2, 2, rho=2)


def test_elo_and_seeded_simulation():
    equal = elo_probabilities(1500, 1500, home_advantage=0)
    assert equal["HOME"] == equal["AWAY"]
    assert sum(equal.values()) == pytest.approx(1)
    home, away = elo_update(1500, 1500, 1, home_advantage=0)
    assert home > 1500 and away < 1500 and home + away == 3000
    extreme = elo_probabilities(1e6, -1e6)
    assert sum(extreme.values()) == pytest.approx(1)
    source = score_distribution(1.5, 1)
    assert simulate(source, 5000, 27) == simulate(source, 5000, 27)
    sampled = market_probabilities(simulate(source, 50000, 27))
    actual = market_probabilities(source)
    assert sampled["1X2"] == pytest.approx(actual["1X2"], abs=0.01)


def test_ensemble_uses_validator_weights_and_ignores_suspended():
    predictions = {"a": {"Y": 0.7, "N": 0.3}, "b": {"Y": 0.3, "N": 0.7}}
    assert combine(predictions, {"a": 3, "b": 1})["Y"] == pytest.approx(0.6)
    assert combine(predictions, {"a": 1, "b": 100}, suspended=frozenset({"b"}))["Y"] == 0.7
    with pytest.raises(ValueError):
        combine(predictions, {"a": 0, "b": 0})
    with pytest.raises(ValueError):
        combine({"a": {"Y": 0.8, "N": 0.3}}, {"a": 1})


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_logistic_learns_binary_baseline_without_future_rows():
    features, labels = [[-2], [-1], [1], [2]], [0, 0, 1, 1]
    model = fit(features, labels, available_at=[NOW] * 4, as_of=NOW)
    assert model.predict([-2]) < 0.1
    assert model.predict([2]) > 0.9
    with pytest.raises(ValueError):
        fit(features, labels, available_at=[NOW + timedelta(seconds=1)] * 4, as_of=NOW)
    with pytest.raises(ValueError):
        model.predict([1, 2])


def test_calibration_metrics_are_probability_aware():
    assert brier_score([0.5, 0.5], [0, 1]) == 0.25
    assert log_loss([0.5, 0.5], [0, 1]) == pytest.approx(math.log(2))
    assert brier_score([0, 1], [0, 1]) == 0
    assert math.isfinite(log_loss([0, 1], [1, 0]))
    curve = reliability_curve([0, 1], [0, 1])
    assert curve[0]["count"] == 1 and curve[-1]["count"] == 1
    assert curve[1]["mean_prediction"] is None
    assert expected_calibration_error([0.5, 0.5], [0, 1]) == 0


def test_independent_validator_blocks_unreliable_or_small_sample():
    assert validate_model([], []).status == "INSUFFICIENT_SAMPLE"
    assert validate_model([0.5] * 10, [0, 1] * 5).recommended_weight_multiplier == 0
    good = validate_model([0.5] * 200, [0, 1] * 100)
    assert good.status == "NORMAL_WEIGHT"
    bad = validate_model([0.9] * 200, [0] * 200)
    assert bad.status == "SUSPEND_MODEL"
    assert bad.recommended_weight_multiplier == 0
    degraded = validate_model([0.4] * 200, [0, 1] * 100, max_brier=0.3)
    assert degraded.status == "REDUCED_WEIGHT"


def test_performance_clv_and_drawdown():
    result = performance([Decimal("10"), Decimal("-10")], [Decimal("10")] * 2, Decimal("100"))
    assert result["roi"] == 0 and result["yield"] == 0
    assert result["max_drawdown"] == pytest.approx(10 / 110)
    assert result["volatility"] == 1
    assert closing_line_value(2.2, 2.0) == pytest.approx(0.1)


def _historical(**overrides):
    values = dict(
        event_id="synthetic-1",
        predicted_at=NOW,
        kickoff=NOW + timedelta(hours=1),
        features_available_at=(NOW,),
        outcome_available_at=NOW + timedelta(hours=3),
        predicted_probability=0.5,
        outcome=1,
        model_version="demo-1",
        feature_version="v1",
        competition="fictional",
        market="BTTS",
        odds_source="synthetic",
        execution_assumption="paper only",
        training_cutoff=NOW - timedelta(days=1),
    )
    values.update(overrides)
    return HistoricalPrediction(**values)


def test_historical_backtest_enforces_feature_training_and_outcome_cutoffs():
    record = _historical()
    result = evaluate([record], as_of=NOW + timedelta(hours=4))
    assert result["brier_score"] == 0.25
    with pytest.raises(ValueError):
        evaluate([record], as_of=NOW)
    with pytest.raises(ValueError):
        _historical(features_available_at=(NOW + timedelta(seconds=1),))
    with pytest.raises(ValueError):
        _historical(training_cutoff=NOW + timedelta(seconds=1))
    partitions = temporal_split(
        [record],
        train_end=NOW + timedelta(hours=4),
        validation_end=NOW + timedelta(days=1),
        test_end=NOW + timedelta(days=2),
    )
    assert partitions["TRAIN"] == [record]
    with pytest.raises(ValueError):
        temporal_split(
            [record, record],
            train_end=NOW + timedelta(hours=4),
            validation_end=NOW + timedelta(days=1),
            test_end=NOW + timedelta(days=2),
        )
