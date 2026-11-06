from collections import defaultdict

from better.domain.enums import MarketType, PredictionStatus, ValidationStatus
from better.domain.schemas import (
    AgentResult,
    ForecastMarket,
    IndicativeForecast,
    OddsReport,
    PredictiveReport,
    ValidationReport,
)
from better.system_zero.math import devig

_EXPECTED = {
    MarketType.MATCH_WINNER: ("HOME", "DRAW", "AWAY"),
    MarketType.OVER_UNDER_2_5: ("OVER_2_5", "UNDER_2_5"),
    MarketType.BTTS: ("YES", "NO"),
    MarketType.DNB: ("HOME", "AWAY"),
}


class ForecastService:
    """Builds read-only forecasts without changing decision inputs or gates."""

    def build(self, results: dict[str, AgentResult], as_of, requested_markets) -> IndicativeForecast:
        forecasts: list[ForecastMarket] = []
        predictive = self._payload(results, "predictive_model", PredictiveReport)
        validation = self._payload(results, "model_validator", ValidationReport)
        if predictive:
            admitted = set(predictive.ensemble)
            for prediction in predictive.model_predictions:
                for market, probabilities in prediction.probabilities.items():
                    if market not in requested_markets:
                        continue
                    limitations = ["Model estimate; it does not guarantee the outcome."]
                    if market not in admitted:
                        limitations.append(
                            "Not admitted to edge, EV, stake or decision: validation/sample insufficient."
                        )
                    matching_validation = next(
                        (
                            item
                            for item in (validation.validations if validation else ())
                            if item.model_id == prediction.model
                            and item.model_version == prediction.model_version
                            and item.market == market
                        ),
                        None,
                    )
                    operational = market in admitted
                    status = (
                        PredictionStatus.VALIDATED
                        if operational
                        else (
                            prediction.prediction_status
                            if prediction.prediction_status != PredictionStatus.VALIDATED
                            else PredictionStatus.UNVALIDATED
                        )
                    )
                    reason = (
                        "ALL_OPERATIONAL_CHECKS_PASSED"
                        if operational
                        else (
                            "MODEL_VALIDATION_SUSPENDED"
                            if matching_validation
                            and matching_validation.validation_status == ValidationStatus.SUSPEND_MODEL
                            else prediction.reason
                            if prediction.reason.startswith("MODEL_INPUT_")
                            else "MODEL_VALIDATION_INSUFFICIENT"
                        )
                    )
                    forecasts.append(
                        ForecastMarket(
                            market=market,
                            probabilities=probabilities,
                            method="RAW_POISSON",
                            admitted_for_decision=operational,
                            source=f"{prediction.model}:{prediction.model_version}",
                            sample_size=prediction.sample_size,
                            limitations=tuple(limitations),
                            fair_odds=prediction.fair_odds.get(market, {}),
                            prediction_status=status,
                            operationally_eligible=operational,
                            reason=reason,
                            input_completeness=prediction.input_completeness,
                            missing_features=prediction.missing_features,
                            validation_sample_size=(
                                matching_validation.sample_size
                                if matching_validation
                                else prediction.validation_sample_size
                            ),
                            validation_minimum=prediction.validation_minimum,
                            validation_status=(
                                matching_validation.validation_status
                                if matching_validation
                                else prediction.validation_status
                            ),
                        )
                    )

        odds = self._payload(results, "football_odds_analyst", OddsReport)
        if odds:
            groups = defaultdict(dict)
            for quote in odds.quotes:
                if quote.market.type in requested_markets:
                    key = (quote.bookmaker, quote.market.type, quote.market.line)
                    groups[key][quote.market.selection] = quote
            for (bookmaker, market, _line), quotes in groups.items():
                selections = _EXPECTED.get(market)
                if not selections or set(quotes) != set(selections):
                    continue
                timestamps = {quotes[selection].captured_at for selection in selections}
                if len(timestamps) != 1:
                    continue
                probabilities = devig([float(quotes[selection].odds) for selection in selections])
                forecasts.append(
                    ForecastMarket(
                        market=market,
                        probabilities=dict(zip(selections, probabilities, strict=True)),
                        method="MARKET_NO_VIG",
                        admitted_for_decision=False,
                        source=bookmaker,
                        limitations=(
                            "Odds-implied probabilities, proportionally normalized to remove margin.",
                            "They describe the bookmaker price, not an independent predictive edge.",
                        ),
                        fair_odds={
                            selection: 1 / probability
                            for selection, probability in zip(selections, probabilities, strict=True)
                        },
                        prediction_status=PredictionStatus.INDICATIVE,
                        operationally_eligible=False,
                        reason="MARKET_BASELINE_NOT_MODEL",
                        input_completeness=1,
                        validation_sample_size=0,
                        validation_minimum=200,
                        validation_status=ValidationStatus.INSUFFICIENT_SAMPLE,
                    )
                )
        return IndicativeForecast(generated_at=as_of, markets=tuple(forecasts))

    @staticmethod
    def _payload(results, agent_id, expected):
        result = results.get(agent_id)
        return result.payload if result and isinstance(result.payload, expected) else None
