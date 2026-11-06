import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from better.system_zero.freshness import available_as_of, freshness, require_as_of
from better.system_zero.markets import canonical_market, equivalent_selection, validate_complete_market
from better.system_zero.math import (
    bookmaker_margin,
    devig,
    edge,
    expected_value,
    fair_odds,
    fractional_kelly,
    implied_probability,
    kelly,
    matched_back_lay,
    minimum_odds,
    money,
    push_expected_value,
    push_fair_odds,
    stake_amount,
)
from better.system_zero.risk import OpenPosition, RiskLimits, assess_stake, capped_stake


def test_auditable_value_arithmetic():
    assert implied_probability(2.5) == 0.4
    assert fair_odds(0.4) == 2.5
    assert expected_value(0.6, 2.0) == pytest.approx(0.2)
    assert edge(0.6, 0.5) == pytest.approx(0.1)
    assert minimum_odds(0.5, 0.04) == pytest.approx(2.08)
    assert kelly(0.6, 2.0) == pytest.approx(0.2)
    assert fractional_kelly(0.6, 2.0) == pytest.approx(0.05)
    assert kelly(0.1, 2) == 0
    assert kelly(1, 2) == 1


def test_proportional_devig_complete_market():
    assert bookmaker_margin([2, 3, 4]) == pytest.approx(1 / 12)
    probabilities = devig([2, 3, 4])
    assert probabilities == pytest.approx([6 / 13, 4 / 13, 3 / 13])
    assert sum(probabilities) == pytest.approx(1)
    assert sum(devig([3, 3])) == pytest.approx(1)


@pytest.mark.parametrize("odds", [0, 1, -2, math.nan, math.inf, True])
def test_invalid_odds_fail_closed(odds):
    with pytest.raises(ValueError):
        implied_probability(odds)


@pytest.mark.parametrize("p", [-0.1, 1.1, math.nan, math.inf, True])
def test_invalid_probability_fail_closed(p):
    with pytest.raises(ValueError):
        expected_value(p, 2)


def test_zero_and_empty_boundaries():
    assert expected_value(0, 2) == -1
    assert expected_value(1, 2) == 1
    for function, args in [
        (fair_odds, (0,)),
        (minimum_odds, (0,)),
        (devig, ([],)),
        (devig, ([2],)),
        (fractional_kelly, (0.6, 2, 1.01)),
    ]:
        with pytest.raises(ValueError):
            function(*args)


def test_unrepresentable_finite_odds_never_escape_as_infinity():
    for function, args in (
        (fair_odds, (5e-324,)),
        (minimum_odds, (5e-324,)),
        (push_fair_odds, (5e-324, 0.5)),
    ):
        with pytest.raises(ValueError):
            function(*args)


def test_money_floors_and_hedge_payoffs():
    assert stake_amount(Decimal("100.01"), 0.01) == Decimal("1.00")
    assert stake_amount(Decimal("101"), 0.01, Decimal("0.05")) == Decimal("1.00")
    assert capped_stake(0.8, 3, Decimal("1000")) == Decimal("10.00")
    with pytest.raises(ValueError):
        money(10.0)
    for stake_returned in (True, False):
        hedge = matched_back_lay(Decimal("10"), 3, 3.2, 0.05, stake_returned=stake_returned)
        assert hedge["win_profit"] == pytest.approx(hedge["lose_profit"])
        assert hedge["liability"] > 0


def test_push_markets_need_win_loss_refund_formula():
    assert push_expected_value(0.4, 0.3, 2.0) == pytest.approx(0.1)
    assert push_fair_odds(0.4, 0.3) == pytest.approx(1.75)
    with pytest.raises(ValueError):
        push_expected_value(0.8, 0.5, 2)


AS_OF = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)


def test_freshness_boundary_and_future_information():
    assert freshness(AS_OF - timedelta(minutes=5), AS_OF, timedelta(minutes=5)).fresh
    assert not freshness(AS_OF - timedelta(minutes=5, microseconds=1), AS_OF, timedelta(minutes=5)).fresh
    assert not available_as_of(AS_OF, retrieved_at=AS_OF, published_at=AS_OF + timedelta(seconds=1))
    with pytest.raises(ValueError):
        require_as_of(AS_OF, retrieved_at=AS_OF + timedelta(seconds=1))
    with pytest.raises(ValueError):
        freshness(AS_OF + timedelta(seconds=1), AS_OF, timedelta(minutes=5))
    with pytest.raises(ValueError):
        freshness(AS_OF.replace(tzinfo=None), AS_OF, timedelta(minutes=5))


def test_market_canonicalization_and_complete_outcomes():
    assert canonical_market("match_winner") == "1X2"
    validate_complete_market("1X2", {"HOME": 2, "DRAW": 3, "AWAY": 4})
    with pytest.raises(ValueError):
        validate_complete_market("1X2", {"HOME": 2, "AWAY": 4})
    with pytest.raises(ValueError):
        canonical_market("player cards")
    assert equivalent_selection("ASIAN_HANDICAP", "HOME", -0.5) == "1X2:HOME"


def _risk(**kwargs):
    return assess_stake(
        0.7,
        2.5,
        Decimal("1000"),
        match_id="match-a",
        competition="demo",
        team_ids=("home", "away"),
        as_of=AS_OF,
        **kwargs,
    )


def test_risk_caps_single_stake_and_match_correlation():
    result = _risk()
    assert result.accepted and result.capped
    assert result.stake == Decimal("10.00")
    position = OpenPosition(Decimal("14.00"), "match-a", "demo", ("home", "away"), AS_OF)
    result = _risk(positions=(position,))
    assert result.accepted
    assert result.stake == Decimal("1.00")
    exhausted = OpenPosition(Decimal("15.00"), "match-a", "demo", ("home", "away"), AS_OF)
    result = _risk(positions=(exhausted,))
    assert not result.accepted and result.stake == 0
    assert "MATCH_EXPOSURE_LIMIT" in result.reasons


def test_risk_no_recovery_sizing_and_drawdown_veto():
    baseline = _risk()
    cautious = _risk(confidence=0.05)
    assert cautious.stake < baseline.stake
    assert not _risk(drawdown=0.2).accepted
    assert not _risk(reliability_multiplier=0).accepted
    assert not _risk(limits=RiskLimits(single=0)).accepted


def test_correlated_positions_different_matches_share_limit():
    position = OpenPosition(Decimal("15.00"), "match-b", "other", ("other1", "other2"), AS_OF, "same-cause")
    assessment = _risk(positions=(position,), correlation_group="same-cause")
    assert not assessment.accepted
    assert "CORRELATED_EXPOSURE_LIMIT" in assessment.reasons


def test_future_position_is_not_silently_counted():
    position = OpenPosition(Decimal("5"), "match-b", "demo", ("home",), AS_OF + timedelta(seconds=1))
    with pytest.raises(ValueError):
        _risk(positions=(position,))


def test_settled_positions_release_exposure_but_not_daily_turnover():
    position = OpenPosition(
        Decimal("15"),
        "match-a",
        "demo",
        ("home", "away"),
        AS_OF - timedelta(hours=2),
        settled_at=AS_OF - timedelta(hours=1),
    )
    result = _risk(positions=(position,))
    assert result.accepted
    assert result.exposures["match"] == 0
    assert result.exposures["portfolio"] == 0
    assert result.exposures["daily"] == Decimal("15")
    exhausted = OpenPosition(
        Decimal("50"),
        "match-a",
        "demo",
        ("home", "away"),
        AS_OF - timedelta(hours=2),
        settled_at=AS_OF - timedelta(hours=1),
    )
    result = _risk(positions=(exhausted,))
    assert not result.accepted
    assert "DAILY_EXPOSURE_LIMIT" in result.reasons
