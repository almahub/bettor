from datetime import timedelta
from decimal import Decimal

from better.config import Settings
from better.domain.enums import (
    AgentStatus,
    ConcernSeverity,
    DecisionStatus,
    Direction,
    ErrorCode,
    ValidationStatus,
)
from better.domain.models import (
    DataQuality,
    DecisionDegradation,
    Degradation,
    GateTrace,
    Stake,
    WatchCondition,
)
from better.domain.schemas import (
    AgentContext,
    ContrarianReport,
    DataReport,
    Decision,
    LineupReport,
    MarketReport,
    NewsReport,
    OddsReport,
    PredictiveReport,
    RiskReport,
    ValidationReport,
    ValueReport,
)
from better.services.evidence_service import evidence_roots
from better.system_zero.markets import OUTCOMES
from better.system_zero.math import (
    bookmaker_margin,
    devig,
    expected_value,
    fair_odds,
    implied_probability,
    minimum_odds,
)


class DecisionService:
    """System 0 final authority. No signal counting and no semantic-provider inputs."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def evaluate(self, context: AgentContext) -> Decision:
        settings, results = self.settings, context.upstream_results

        def report(agent_id, report_type):
            result = results.get(agent_id)
            usable = result and result.status in {AgentStatus.OK, AgentStatus.PARTIAL}
            return result.payload if usable and isinstance(result.payload, report_type) else None

        value = report("value_bettor", ValueReport)
        validation = report("model_validator", ValidationReport)
        risk = report("risk_manager", RiskReport)
        lineup = report("lineup_player_impact", LineupReport)
        news = report("news_intelligence", NewsReport)
        contrarian = report("devils_advocate", ContrarianReport)
        markets = report("market_selection", MarketReport)
        predictive = report("predictive_model", PredictiveReport)
        data = report("football_data_analyst", DataReport)
        odds = report("football_odds_analyst", OddsReport)
        candidate = risk.candidate if risk else None
        if candidate is None and value and value.candidates:
            candidate = max(value.candidates, key=lambda c: c.expected_value)
        degradations = []
        for agent_id, result in sorted(results.items()):
            limitations = result.degradations
            if result.status == AgentStatus.PARTIAL and not limitations:
                limitations = (
                    Degradation(
                        code="UNCLASSIFIED_PARTIAL",
                        missing_checks=("explicit_partial_impact_assessment",),
                        critical=True,
                        affected_gates=("DATA_QUALITY",),
                        impact="BLOCK",
                        reason="PARTIAL without a structured explanation is not safe to admit. "
                        "Legacy snapshots remain readable but must be reanalysed with classified limitations.",
                    ),
                )
            for limitation in limitations:
                applies = (
                    not limitation.market_scope
                    or candidate is None
                    or candidate.market.type in limitation.market_scope
                )
                degradations.append(
                    DecisionDegradation(
                        **limitation.model_dump(), agent_id=agent_id, applies_to_decision=applies
                    )
                )
        blocking_degradations = [d for d in degradations if d.applies_to_decision and d.critical]
        trace = []

        def gate(name, passed, reason, wait=False):
            relevant = [d for d in degradations if d.applies_to_decision and name in d.affected_gates]
            if relevant:
                passed = passed and not any(d.critical for d in relevant)
                wait = wait and not any(d.critical for d in relevant)
                reason += "; limitations=" + ",".join(f"{d.agent_id}/{d.code}:{d.impact}" for d in relevant)
            trace.append(
                GateTrace(gate=name, status="PASS" if passed else "WAIT" if wait else "FAIL", reason=reason)
            )

        context_ok = all(
            result.analysis_id == context.analysis_id
            and result.agent_id == identifier
            and result.generated_at <= context.as_of
            and all(source.available_at(context.as_of) for source in result.provenance)
            for identifier, result in results.items()
        )
        context_ok = context_ok and (data is None or data.data_cutoff <= context.as_of)
        context_ok = context_ok and (
            predictive is None
            or all(prediction.data_cutoff <= context.as_of for prediction in predictive.model_predictions)
        )
        context_ok = context_ok and (
            odds is None or all(quote.match_id == context.match.match_id for quote in odds.quotes)
        )
        if data:
            home, away = data.team_profiles.get("home"), data.team_profiles.get("away")
            context_ok = context_ok and bool(
                home
                and away
                and home.team_id == context.match.home_team_id
                and away.team_id == context.match.away_team_id
            )
        gate(
            "CONTEXT_INTEGRITY",
            context_ok,
            "analysis, match and as-of identities verified"
            if context_ok
            else "foreign analysis/match, future source or invalid temporal context",
        )

        critical_ids = set(settings.critical_agents) - {"professional_bettor"}
        missing = sorted(
            agent
            for agent in critical_ids
            if agent not in results
            or results[agent].status
            in {
                AgentStatus.SOURCE_ERROR,
                AgentStatus.MODEL_ERROR,
                AgentStatus.VALIDATION_ERROR,
                AgentStatus.INSUFFICIENT_DATA,
            }
        )
        stale = sorted(
            agent
            for agent in critical_ids
            if agent in results and results[agent].status == AgentStatus.STALE_DATA
        )
        qualities = [
            results[agent].data_quality
            for agent in ("football_data_analyst", "football_odds_analyst")
            if agent in results
        ]
        quality = DataQuality(
            **{
                key: min((getattr(q, key) for q in qualities), default=0)
                for key in ("completeness", "freshness", "source_reliability", "cross_source_consistency")
            }
        )
        quality_ok = (
            not missing
            and not stale
            and quality.score >= settings.minimum_data_quality
            and quality.completeness >= settings.minimum_completeness
            and quality.freshness >= settings.minimum_freshness
        )
        gate("DATA_QUALITY", quality_ok, f"missing={missing}; stale={stale}; quality={quality.score:.3f}")

        all_evidence = tuple(e for result in results.values() for e in result.evidence)
        # The same evidence may be carried forward unchanged. Preserve identity once; reject conflicting copies.
        evidence_by_id = {}
        inconsistent_evidence = False
        for item in all_evidence:
            if item.evidence_id in evidence_by_id and evidence_by_id[item.evidence_id] != item:
                inconsistent_evidence = True
            evidence_by_id[item.evidence_id] = item
        # Async completion order and dependency-map insertion order are not evidence semantics.
        evidence = tuple(evidence_by_id[identifier] for identifier in sorted(evidence_by_id, key=str))
        predictions = {p.prediction_id for p in predictive.model_predictions} if predictive else set()
        ancestry = {}
        try:
            ancestry = evidence_roots(evidence, predictions)
            ancestry_ok = not inconsistent_evidence
        except ValueError:
            ancestry_ok = False
        future_sources = any(not p.available_at(context.as_of) for e in evidence for p in e.provenance)
        data_result = results.get("football_data_analyst")
        predictive_result = results.get("predictive_model")
        trusted_origins = {
            f"{p.primary_source_id or p.source_id}:{p.raw_reference}"
            for item in (data_result.evidence if data_result else ())
            for p in item.provenance
            if p.reliability_tier.value in {"A", "B"}
            and p.source_type.value != "MODEL"
            and 0 <= (context.as_of - p.retrieved_at).total_seconds() <= settings.stats_ttl_seconds
            and p.available_at(context.as_of)
        }
        trustworthy = bool(
            candidate
            and trusted_origins
            and predictive_result
            and any(
                (not item.market_scope or candidate.market.type in item.market_scope)
                and ancestry.get(item.evidence_id, frozenset()) & trusted_origins
                for item in predictive_result.evidence
            )
        )
        quote_trusted = bool(
            candidate
            and candidate.provenance.reliability_tier.value in {"A", "B"}
            and candidate.provenance.source_type.value != "MODEL"
        )
        provenance_ok = ancestry_ok and not future_sources and trustworthy and quote_trusted
        gate(
            "EVIDENCE_PROVENANCE",
            provenance_ok,
            f"ancestry_valid={ancestry_ok}; available_as_of={not future_sources}; trusted_model_support={trustworthy}; trusted_quote={quote_trusted}",
        )

        validations = (
            tuple(
                v
                for v in validation.validations
                if candidate is None
                or (
                    v.model_id == candidate.model_id
                    and v.model_version == candidate.model_version
                    and v.market == candidate.market.type
                )
            )
            if validation
            else ()
        )
        suspended = bool(validations) and any(
            v.validation_status == ValidationStatus.SUSPEND_MODEL for v in validations
        )
        model_ok = bool(validations) and all(
            v.validation_status in {ValidationStatus.NORMAL_WEIGHT, ValidationStatus.REDUCED_WEIGHT}
            and v.sample_size >= settings.min_sample
            and v.recommended_weight_multiplier > 0
            and v.brier_score is not None
            and v.brier_score <= settings.max_brier
            and v.log_loss is not None
            and v.calibration_error is not None
            and v.calibration_error <= settings.suspend_calibration_error
            for v in validations
        )
        reliability = min((v.recommended_weight_multiplier for v in validations), default=0)
        validation_sample = min((v.sample_size for v in validations), default=0)
        gate(
            "MODEL_VALIDITY",
            model_ok,
            "independent out-of-sample model checks passed"
            if model_ok
            else (
                f"MODEL_VALIDATION_INSUFFICIENT: sample={validation_sample}/{settings.min_sample}"
                if not suspended and validation_sample < settings.min_sample
                else "model suspended, missing validation or calibration degraded"
            ),
        )

        supported = bool(
            candidate
            and markets
            and candidate.market.type in markets.eligible_markets
            and candidate.market.type.value in settings.supported_markets
            and candidate.market.type.value in OUTCOMES
        )
        gate(
            "MARKET",
            supported,
            "market eligible and supported" if supported else "no suitable validated market",
        )
        numerical_consistency = False
        fresh_price = False
        if candidate and predictive and value and odds and candidate in value.candidates:
            predicted = predictive.ensemble.get(candidate.market.type, {}).get(candidate.market.selection)
            model = next(
                (
                    prediction
                    for prediction in predictive.model_predictions
                    if prediction.model == candidate.model_id
                    and prediction.model_version == candidate.model_version
                    and candidate.market.type in prediction.probabilities
                ),
                None,
            )
            group = tuple(
                quote
                for quote in odds.quotes
                if not quote.is_opening
                and quote.match_id == context.match.match_id
                and quote.bookmaker == candidate.bookmaker
                and quote.market.type == candidate.market.type
                and quote.market.line == candidate.market.line
                and quote.captured_at == candidate.captured_at
            )
            complete = (
                set(quote.market.selection for quote in group)
                == OUTCOMES.get(candidate.market.type.value, set())
                and len(group) == len(OUTCOMES.get(candidate.market.type.value, ()))
                and bool(group)
            )
            matched_quote = next(
                (
                    quote
                    for quote in group
                    if quote.market == candidate.market
                    and quote.odds == candidate.current_odds
                    and quote.provenance == candidate.provenance
                    and quote.liquidity == candidate.liquidity
                ),
                None,
            )
            de_vig_probability, margin = None, None
            if complete:
                fair_market = devig([quote.odds for quote in group])
                de_vig_probability = next(
                    (
                        p
                        for quote, p in zip(group, fair_market, strict=True)
                        if quote.market.selection == candidate.market.selection
                    ),
                    None,
                )
                margin = bookmaker_margin([quote.odds for quote in group])
            interval = (
                model.uncertainty.get(f"{candidate.market.type.value}:{candidate.market.selection}")
                if model
                else None
            )
            conservative = interval.lower if interval else predicted
            numerical_consistency = (
                complete
                and matched_quote is not None
                and model is not None
                and 0 < candidate.conservative_probability <= candidate.model_probability
                and predicted is not None
                and abs(predicted - candidate.model_probability) < 1e-8
                and conservative is not None
                and abs(conservative - candidate.conservative_probability) < 1e-8
                and abs(de_vig_probability - candidate.no_vig_probability) < 1e-8
                and abs(margin - candidate.bookmaker_margin) < 1e-8
                and abs(implied_probability(candidate.current_odds) - candidate.implied_probability) < 1e-8
                and abs(fair_odds(candidate.model_probability) - candidate.fair_odds) < 1e-8
                and abs(
                    expected_value(candidate.model_probability, candidate.current_odds)
                    - candidate.expected_value
                )
                < 1e-8
                and abs(candidate.model_probability - candidate.no_vig_probability - candidate.edge) < 1e-8
                and abs(
                    minimum_odds(candidate.conservative_probability, settings.minimum_ev)
                    - candidate.minimum_acceptable_odds
                )
                < 1e-8
            )
            fresh_price = (
                complete
                and matched_quote is not None
                and all(
                    0 <= (context.as_of - quote.captured_at).total_seconds() <= settings.odds_ttl_seconds
                    and quote.provenance.available_at(context.as_of)
                    for quote in group
                )
            )
        gate(
            "NUMERICAL_CONSISTENCY",
            numerical_consistency,
            "model probability and price formulas verified"
            if numerical_consistency
            else "missing or inconsistent model/value linkage",
        )
        gate(
            "PRICE_FRESHNESS",
            fresh_price,
            "quote current as-of analysis" if fresh_price else "quote unavailable, future or stale",
        )
        positive_value = bool(
            candidate
            and candidate.edge >= settings.minimum_edge
            and candidate.expected_value > 0
            and candidate.expected_value >= settings.minimum_ev
            and expected_value(candidate.conservative_probability, candidate.current_odds) > 0
        )
        price_ok = bool(candidate and candidate.current_odds >= candidate.minimum_acceptable_odds)
        gate(
            "VALUE",
            positive_value and price_ok,
            "edge, EV and minimum price passed"
            if positive_value and price_ok
            else "edge, conservative EV or current price below threshold",
        )

        resolved = set()
        for resolution in context.resolutions:
            if resolution.resolved_at <= context.as_of and all(
                identifier in evidence_by_id for identifier in resolution.evidence_ids
            ):
                supporting = [evidence_by_id[identifier] for identifier in resolution.evidence_ids]
                if all(
                    any(
                        p.reliability_tier.value in {"A", "B"}
                        and p.source_type.value != "MODEL"
                        and p.available_at(resolution.resolved_at)
                        for p in item.provenance
                    )
                    for item in supporting
                ):
                    resolved.add(resolution.concern_id)
        concerns = contrarian.concerns if contrarian else ()
        fatal = tuple(
            c
            for c in concerns
            if c.severity == ConcernSeverity.FATAL_CONCERN and c.concern_id not in resolved
        )
        conflicts = bool(news and news.conflicts)
        contrary_ok = contrarian is not None and not fatal and not conflicts
        gate(
            "CONTRARIAN",
            contrary_ok,
            "no unresolved material contradiction"
            if contrary_ok
            else "contrarian review missing, unresolved fatal concern or source conflict",
        )

        risk_ok = bool(
            risk
            and risk.accepted
            and risk.stake.bankroll_fraction > 0
            and risk.stake.amount > 0
            and risk.stake.bankroll_fraction <= settings.max_single_bet_fraction
            and risk.candidate == candidate
        )
        if context.portfolio and risk:
            portfolio = context.portfolio
            # Money is authoritative. Reconstructing cents from a float fraction can
            # wrongly turn a valid EUR 1.00 / EUR 3000 position into EUR 0.99.
            stake = risk.stake.amount / portfolio.bankroll
            positions = portfolio.positions

            def exposure_where(predicate):
                return sum(
                    (
                        Decimal(str(position.bankroll_fraction))
                        for position in positions
                        if predicate(position)
                    ),
                    Decimal(0),
                )

            exposure = exposure_where(lambda position: True)
            match_exposure = exposure_where(lambda position: position.match_id == context.match.match_id)
            team_exposure = max(
                (
                    exposure_where(lambda position, team=team: team in position.team_ids)
                    for team in (context.match.home_team_id, context.match.away_team_id)
                ),
                default=Decimal(0),
            )
            competition_exposure = exposure_where(
                lambda position: position.competition == context.match.competition
            )
            day_exposure = exposure_where(
                lambda position: position.placed_at >= context.as_of - timedelta(days=1)
            )
            week_exposure = exposure_where(
                lambda position: position.placed_at >= context.as_of - timedelta(days=7)
            )
            risk_ok = (
                risk_ok
                and all(
                    current + stake <= Decimal(str(limit))
                    for current, limit in (
                        (exposure, settings.max_portfolio_exposure),
                        (match_exposure, settings.max_match_exposure),
                        (team_exposure, settings.max_team_exposure),
                        (competition_exposure, settings.max_competition_exposure),
                        (day_exposure, settings.max_daily_exposure),
                        (week_exposure, settings.max_weekly_exposure),
                    )
                )
                and portfolio.drawdown < settings.max_drawdown
                and portfolio.daily_loss_fraction < settings.max_daily_exposure
                and portfolio.weekly_loss_fraction < settings.max_weekly_exposure
                and all(p.placed_at <= context.as_of for p in positions)
                and risk.stake.bankroll_fraction == float(stake)
                and risk.stake.amount == risk.stake.amount.quantize(Decimal("0.01"))
                and risk.stake.amount <= portfolio.bankroll * Decimal(str(settings.max_single_bet_fraction))
            )
        else:
            risk_ok = False
        gate(
            "RISK",
            risk_ok,
            "stake and cumulative portfolio limits checked"
            if risk_ok
            else "risk veto or portfolio unavailable",
        )

        lineup_ttl = (
            settings.official_lineups_ttl_seconds
            if lineup and lineup.official
            else settings.lineups_ttl_seconds
        )
        lineup_fresh = bool(
            lineup
            and lineup.lineup
            and lineup.lineup.match_id == context.match.match_id
            and 0 <= (context.as_of - lineup.lineup.captured_at).total_seconds() <= lineup_ttl
            and all(source.available_at(context.as_of) for source in lineup.lineup.provenance)
        )
        lineup_sensitive = not lineup_fresh or lineup.sensitivity >= settings.lineup_sensitivity_threshold
        timing_ok = context.as_of < context.match.kickoff and (
            not lineup_sensitive or bool(lineup_fresh and lineup.official)
        )
        gate(
            "TIMING",
            timing_ok,
            "prematch timing and lineup conditions met"
            if timing_ok
            else "kickoff passed or material official lineup still missing",
            wait=True,
        )
        liquidity = (
            0
            if candidate is None or candidate.liquidity is None
            else min(1, float(candidate.liquidity) / max(settings.min_liquidity, 1))
        )
        uncertainty = (
            0
            if candidate is None
            else max(0, 1 - 2 * (candidate.model_probability - candidate.conservative_probability))
        )
        severity_factor = (
            0 if fatal else 0.6 if any(c.severity == ConcernSeverity.MAJOR_CONCERN for c in concerns) else 1
        )
        components = dict(
            data_completeness=quality.completeness,
            model_reliability=reliability,
            source_consistency=quality.cross_source_consistency,
            model_uncertainty=uncertainty,
            lineup_certainty=1 if lineup_fresh and lineup.official else 0.8 if not lineup_sensitive else 0.3,
            market_liquidity=liquidity,
            contrary_severity=severity_factor,
        )
        for limitation in degradations:
            if limitation.applies_to_decision and limitation.impact != "WARNING_ONLY":
                components[f"degradation:{limitation.agent_id}:{limitation.code}"] = (
                    0 if limitation.critical else limitation.confidence_cap
                )
        confidence = min(components.values())
        confidence_ok = confidence >= settings.minimum_confidence
        gate("CONFIDENCE", confidence_ok, f"weakest confidence component={confidence:.3f}")

        watch_conditions = ()
        if not context_ok:
            status = DecisionStatus.NO_BET
        elif suspended:
            status = DecisionStatus.NO_BET
        elif missing:
            status = DecisionStatus.INSUFFICIENT_DATA
        elif blocking_degradations:
            status = DecisionStatus.NO_BET
        elif stale or not quality_ok or not provenance_ok or not fresh_price:
            status = DecisionStatus.NO_BET
        elif (
            not model_ok
            or not supported
            or not numerical_consistency
            or not contrary_ok
            or context.as_of >= context.match.kickoff
        ):
            status = DecisionStatus.NO_BET
        elif not timing_ok:
            status = DecisionStatus.WAIT_FOR_LINEUPS
            watch_conditions = (
                WatchCondition(type="LINEUP", operator="CONFIRMED", target="official starting XI"),
            )
        elif not price_ok:
            status = DecisionStatus.WAIT_FOR_BETTER_ODDS
            watch_conditions = (
                WatchCondition(type="ODDS", operator=">=", target=candidate.minimum_acceptable_odds),
            )
        elif not positive_value:
            status = DecisionStatus.NO_BET
        elif not risk_ok:
            status = DecisionStatus.NO_BET
        elif not confidence_ok:
            status = DecisionStatus.WATCHLIST
            watch_conditions = (
                WatchCondition(
                    type="LIQUIDITY" if liquidity < settings.minimum_confidence else "CONFIDENCE",
                    operator=">=",
                    target=settings.min_liquidity
                    if liquidity < settings.minimum_confidence
                    else settings.minimum_confidence,
                ),
            )
        else:
            status = DecisionStatus.BET
        reason_codes = []

        def add_reason(code):
            if code not in reason_codes:
                reason_codes.append(code)

        agent_errors = [
            (agent_id, error)
            for agent_id, result in results.items()
            for error in result.errors
        ]
        error_text = " ".join(error.message for _, error in agent_errors).casefold()
        identity_missing = "identity_mapping_" in error_text or "identita squadre" in error_text
        if identity_missing:
            add_reason("IDENTITY_MAPPING_MISSING")
        api_provider_error = any(
            error.source == "api_football"
            and error.code
            in {
                ErrorCode.TRANSIENT_PROVIDER_ERROR,
                ErrorCode.PERMANENT_PROVIDER_ERROR,
                ErrorCode.RATE_LIMIT,
                ErrorCode.TIMEOUT,
                ErrorCode.SCHEMA_ERROR,
            }
            for _, error in agent_errors
        )
        if api_provider_error:
            add_reason("API_PROVIDER_ERROR")
        if "openai non disponibile: autenticazione fallita" in error_text:
            add_reason("OPENAI_AUTH_ERROR")
        if "openai_key_expired" in error_text:
            add_reason("OPENAI_KEY_EXPIRED")
        if "openai_permission_error" in error_text:
            add_reason("OPENAI_PERMISSION_ERROR")
        if "no news provider configured" in error_text:
            add_reason("NEWS_PROVIDER_NOT_CONFIGURED")
        if "lineup_not_published_yet" in error_text:
            add_reason("LINEUP_NOT_PUBLISHED_YET")
        if data is None and not (identity_missing or api_provider_error):
            add_reason("MODEL_INPUT_INSUFFICIENT")
        data_result = results.get("football_data_analyst")
        if data_result is not None and data_result.status == AgentStatus.PARTIAL:
            add_reason("STATS_PARTIAL")
        if predictive is None or any(
            prediction.reason.startswith("MODEL_INPUT_")
            for prediction in (predictive.model_predictions if predictive else ())
        ):
            add_reason("MODEL_INPUT_INSUFFICIENT")
        if not model_ok:
            add_reason("MODEL_VALIDATION_INSUFFICIENT")
        if (
            odds is None
            or not odds.quotes
            or (markets and any(reason == "QUOTE_MISSING" for reason in markets.rejections.values()))
        ):
            add_reason("QUOTE_MISSING")
        if lineup_sensitive and not (lineup_fresh and lineup and lineup.official) and not any(
            code in reason_codes for code in ("LINEUP_NOT_PUBLISHED_YET", "IDENTITY_MAPPING_MISSING")
        ):
            add_reason("LINEUP_NOT_PUBLISHED_YET")
        research_failed = any(
            result.status == AgentStatus.SOURCE_ERROR
            for agent_id, result in results.items()
            if agent_id in {"news_intelligence", "external_research", "football_data_research"}
        )
        enriched = context.enriched_snapshot
        for section in (
            enriched.external_research if enriched else None,
            enriched.football_data_research if enriched else None,
        ):
            if section and str(section.get("status", "")).endswith("_ERROR"):
                research_failed = True
                if "OPENAI_API_KEY" in str(section.get("error", "")):
                    add_reason("OPENAI_AUTH_ERROR")
        if research_failed:
            add_reason("RESEARCH_FAILED")
        if markets is None or not markets.supported_markets:
            add_reason("MARKET_UNSUPPORTED")
        return Decision(
            analysis_id=context.analysis_id,
            match_id=context.match.match_id,
            status=status,
            market=candidate.market if candidate else None,
            current_odds=candidate.current_odds if candidate else None,
            minimum_acceptable_odds=candidate.minimum_acceptable_odds if candidate else None,
            estimated_probability=candidate.model_probability if candidate else None,
            fair_odds=candidate.fair_odds if candidate else None,
            estimated_edge=candidate.edge if candidate else None,
            expected_value=candidate.expected_value if candidate else None,
            confidence=confidence,
            confidence_components=components,
            degradations=tuple(degradations),
            risk_level="LOW" if status == DecisionStatus.BET else "HIGH" if fatal else "UNKNOWN",
            stake=risk.stake if status == DecisionStatus.BET else Stake(),
            supporting_evidence=tuple(
                e
                for e in evidence
                if e.direction in {Direction.POSITIVE, Direction.STRONG_POSITIVE, Direction.NEUTRAL}
            ),
            contrary_evidence=tuple(
                e for e in evidence if e.direction in {Direction.NEGATIVE, Direction.STRONG_NEGATIVE}
            ),
            fatal_concerns=fatal,
            invalidation_conditions=(
                "Refresh price after TTL or material market movement",
                "Re-run on lineup, injury, model validation or portfolio change",
                "Decision valid only for recorded as_of snapshot",
            ),
            watch_conditions=watch_conditions,
            data_quality=quality,
            decision_trace=tuple(trace),
            generated_at=context.as_of,
            reason_codes=tuple(reason_codes),
        )
