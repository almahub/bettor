from better.domain.enums import ValidationStatus
from better.domain.schemas import DataReport, MarketReport, OddsReport, PredictiveReport, ValidationReport

from .base import BaseAgent


class MarketSelectionAgent(BaseAgent):
    agent_id = "market_selection"
    dependencies = ("football_data_analyst", "football_odds_analyst", "predictive_model", "model_validator")
    allowed_tools = frozenset({"league_capabilities", "market_rules"})

    async def _run(self, context):
        predictions = self.payload(context, "predictive_model", PredictiveReport)
        odds = self.payload(context, "football_odds_analyst", OddsReport)
        validation = self.payload(context, "model_validator", ValidationReport)
        data = self.payload(context, "football_data_analyst", DataReport)
        eligible = []
        supported = []
        rejections = {}
        capabilities = self.settings.league_capabilities.get(
            context.match.competition, self.settings.supported_markets
        )
        for market in context.requested_markets:
            reason = None
            configured = market.value in self.settings.supported_markets and market.value in capabilities
            has_analysis = bool(
                predictions
                and any(market in prediction.probabilities for prediction in predictions.model_predictions)
            )
            if configured and has_analysis:
                supported.append(market)
            quotes = [q for q in odds.quotes if q.market.type == market] if odds else []
            valid = (
                [
                    v
                    for v in validation.validations
                    if v.market == market
                    and v.validation_status
                    in {ValidationStatus.NORMAL_WEIGHT, ValidationStatus.REDUCED_WEIGHT}
                ]
                if validation
                else []
            )
            if not configured:
                reason = "MARKET_UNSUPPORTED"
            elif not data or any(
                t.sample_size < self.settings.min_training_sample for t in data.team_profiles.values()
            ):
                reason = "MODEL_INPUT_INSUFFICIENT"
            elif not predictions or market not in predictions.ensemble or not valid:
                reason = "MODEL_VALIDATION_INSUFFICIENT"
            elif not quotes:
                reason = "QUOTE_MISSING"
            elif not any(
                q.liquidity is not None and float(q.liquidity) >= self.settings.min_liquidity for q in quotes
            ):
                reason = "LIQUIDITY_INSUFFICIENT"
            if reason:
                rejections[market.value] = reason
            else:
                eligible.append(market)
        source = self.model_source(context, "market_eligibility")
        return self.output(
            context,
            MarketReport(
                eligible_markets=tuple(eligible),
                supported_markets=tuple(supported),
                rejections=rejections,
            ),
            summary="Eligible markets" if eligible else "NO_SUITABLE_MARKET",
            provenance=(source,),
            quality=self.upstream_quality(context, "football_data_analyst", "football_odds_analyst"),
            analysis_available=bool(supported),
            operationally_eligible=bool(eligible),
        )
