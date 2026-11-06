from collections import defaultdict

from better.domain.enums import Direction
from better.domain.models import ValueCandidate
from better.domain.schemas import MarketReport, OddsReport, PredictiveReport, ValueReport
from better.system_zero.math import (
    bookmaker_margin,
    devig,
    edge,
    expected_value,
    fair_odds,
    implied_probability,
    minimum_odds,
)

from .base import BaseAgent


class ValueBettor(BaseAgent):
    agent_id = "value_bettor"
    dependencies = ("football_odds_analyst", "predictive_model", "market_selection", "model_validator")
    allowed_tools = frozenset({"deterministic_value_math"})

    async def _run(self, context):
        predictions = self.payload(context, "predictive_model", PredictiveReport)
        odds = self.payload(context, "football_odds_analyst", OddsReport)
        markets = self.payload(context, "market_selection", MarketReport)
        if not predictions or not odds or not markets:
            return self.missing(context, "Model probabilities, prices and market eligibility required")
        groups = defaultdict(list)
        for quote in odds.quotes:
            if quote.market.type in markets.supported_markets:
                groups[(quote.bookmaker, quote.market.type, quote.market.line, quote.captured_at)].append(
                    quote
                )
        candidates = []
        indicative_candidates = []
        unknowns = []
        for (_, market, _, _), quotes in groups.items():
            operational = market in markets.eligible_markets and market in predictions.ensemble
            model = next((p for p in predictions.model_predictions if market in p.probabilities), None)
            probabilities = (
                predictions.ensemble.get(market, {})
                if operational
                else (model.probabilities.get(market, {}) if model else {})
            )
            if set(q.market.selection for q in quotes) != set(probabilities) or len(quotes) != len(
                probabilities
            ):
                unknowns.append(f"Incomplete simultaneous bookmaker market: {market}")
                continue
            prices = [q.odds for q in quotes]
            fair_market = devig(prices)
            if model is None:
                continue
            for quote, no_vig in zip(quotes, fair_market, strict=True):
                p = probabilities[quote.market.selection]
                if p == 0:
                    continue
                interval = model.uncertainty.get(f"{market.value}:{quote.market.selection}")
                conservative = interval.lower if interval else p
                if conservative == 0:
                    continue
                candidate = ValueCandidate(
                    market=quote.market,
                    bookmaker=quote.bookmaker,
                    current_odds=quote.odds,
                    model_probability=p,
                    conservative_probability=conservative,
                    implied_probability=implied_probability(quote.odds),
                    no_vig_probability=no_vig,
                    fair_odds=fair_odds(p),
                    edge=edge(p, no_vig),
                    expected_value=expected_value(p, quote.odds),
                    minimum_acceptable_odds=minimum_odds(conservative, self.settings.minimum_ev),
                    bookmaker_margin=bookmaker_margin(prices),
                    model_id=model.model,
                    model_version=model.model_version,
                    liquidity=quote.liquidity,
                    captured_at=quote.captured_at,
                    provenance=quote.provenance,
                    operationally_eligible=operational,
                    status="OPERATIONAL" if operational else "INDICATIVE",
                    reason=(
                        "ALL_OPERATIONAL_CHECKS_PASSED"
                        if operational
                        else markets.rejections.get(market.value, model.reason)
                    ),
                )
                (candidates if operational else indicative_candidates).append(candidate)
        candidates.sort(key=lambda candidate: candidate.expected_value, reverse=True)
        indicative_candidates.sort(key=lambda candidate: candidate.expected_value, reverse=True)
        source = self.model_source(context, "deterministic_value")
        parents = tuple(
            e.evidence_id
            for key in ("predictive_model", "football_odds_analyst")
            for e in context.upstream_results[key].evidence
        )
        evidence = self.evidence(
            context,
            "Price comparison preserves model probabilities and independent evidence ancestry",
            (source,),
            kind="VALUE_ANALYSIS",
            derived_from=parents,
            direction=Direction.NEUTRAL,
        )
        return self.output(
            context,
            ValueReport(
                candidates=tuple(candidates),
                indicative_candidates=tuple(indicative_candidates),
                analysis_available=bool(candidates or indicative_candidates),
                operationally_eligible=bool(candidates),
            ),
            summary="Deterministic proportional de-vig, edge and EV",
            quality=self.upstream_quality(context, "predictive_model", "football_odds_analyst"),
            provenance=(source,),
            evidence=(evidence,),
            unknowns=unknowns,
            analysis_available=bool(candidates or indicative_candidates),
            operationally_eligible=bool(candidates),
        )
