from datetime import timedelta
from decimal import Decimal

from better.domain.models import Stake
from better.domain.schemas import RiskReport, ValidationReport, ValueReport
from better.system_zero.risk import OpenPosition, RiskLimits, assess_stake

from .base import BaseAgent


class RiskManager(BaseAgent):
    agent_id = "risk_manager"
    dependencies = ("value_bettor", "model_validator")
    allowed_tools = frozenset({"portfolio_repository", "deterministic_risk_math"})
    needs_portfolio = True

    async def _run(self, context):
        value = self.payload(context, "value_bettor", ValueReport)
        validation = self.payload(context, "model_validator", ValidationReport)
        portfolio = context.portfolio
        if portfolio is None:
            return self.missing(context, "Verified portfolio required for risk assessment")
        if portfolio.bankroll == 0:
            return self.output(
                context,
                RiskReport(accepted=False, stake=Stake(), reasons=("ZERO_BANKROLL",), candidate=None),
                summary="Bankroll esaurito: nessuna nuova puntata ammessa",
                operationally_eligible=False,
            )
        candidate = (
            max(value.candidates, key=lambda c: c.expected_value) if value and value.candidates else None
        )
        if candidate is None:
            indicative = (
                max(value.indicative_candidates, key=lambda c: c.expected_value)
                if value and value.indicative_candidates
                else None
            )
            return self.output(
                context,
                RiskReport(
                    accepted=False,
                    stake=Stake(),
                    reasons=((indicative.reason if indicative else "NO_CANDIDATE"),),
                    candidate=indicative,
                    operationally_eligible=False,
                ),
                summary=(
                    "Indicative value only: stake fixed at zero" if indicative else "No proposal to size"
                ),
                analysis_available=indicative is not None,
                operationally_eligible=False,
            )
        s = self.settings
        reliability = (
            min(
                (
                    v.recommended_weight_multiplier
                    for v in validation.validations
                    if (v.model_id, v.model_version, v.market)
                    == (candidate.model_id, candidate.model_version, candidate.market.type)
                ),
                default=0,
            )
            if validation
            else 0
        )
        positions = tuple(
            OpenPosition(
                stake=p.stake_amount
                if p.stake_amount is not None
                else portfolio.bankroll * Decimal(str(p.bankroll_fraction)),
                match_id=p.match_id,
                competition=p.competition,
                team_ids=p.team_ids,
                opened_at=p.placed_at,
                settled_at=p.settled_at,
                correlation_group=p.match_id,
            )
            for p in portfolio.positions
        )
        limits = RiskLimits(
            kelly_fraction=s.kelly_fraction,
            single=s.max_single_bet_fraction,
            match=s.max_match_exposure,
            portfolio=s.max_portfolio_exposure,
            daily=s.max_daily_exposure,
            weekly=s.max_weekly_exposure,
            team=s.max_team_exposure,
            competition=s.max_competition_exposure,
            correlated=s.max_match_exposure,
            max_drawdown=s.max_drawdown,
        )
        if any(p.placed_at > context.as_of for p in portfolio.positions):
            return self.missing(context, "Portfolio includes future positions")
        assessment = assess_stake(
            candidate.conservative_probability,
            candidate.current_odds,
            portfolio.bankroll,
            match_id=context.match.match_id,
            competition=context.match.competition,
            team_ids=(context.match.home_team_id, context.match.away_team_id),
            as_of=context.as_of,
            positions=positions,
            limits=limits,
            reliability_multiplier=reliability,
            correlation_group=context.match.match_id,
            drawdown=portfolio.drawdown,
        )
        exposures = {k: float(v / portfolio.bankroll) for k, v in assessment.exposures.items()}
        # The project gates use rolling 24h/7d, additionally enforce those conservative windows.
        exposures["daily"] = sum(
            float(p.stake_amount / portfolio.bankroll) if p.stake_amount is not None else p.bankroll_fraction
            for p in portfolio.positions
            if p.placed_at >= context.as_of - timedelta(days=1)
        )
        exposures["weekly"] = sum(
            float(p.stake_amount / portfolio.bankroll) if p.stake_amount is not None else p.bankroll_fraction
            for p in portfolio.positions
            if p.placed_at >= context.as_of - timedelta(days=7)
        )
        reasons = list(assessment.reasons)
        if portfolio.daily_loss_fraction >= s.max_daily_exposure:
            reasons.append("DAILY_LOSS_LIMIT")
        if portfolio.weekly_loss_fraction >= s.max_weekly_exposure:
            reasons.append("WEEKLY_LOSS_LIMIT")
        remaining = min(
            s.max_daily_exposure - exposures["daily"], s.max_weekly_exposure - exposures["weekly"]
        )
        if remaining <= 0:
            reasons.append("ROLLING_EXPOSURE_LIMIT")
        amount = min(assessment.stake, max(Decimal(0), portfolio.bankroll * Decimal(str(remaining))))
        amount = (amount // Decimal("0.01")) * Decimal("0.01")
        accepted = assessment.accepted and not reasons and amount > 0
        stake = (
            Stake(
                bankroll_fraction=float(amount / portfolio.bankroll),
                amount=amount,
                capped=assessment.capped or amount < assessment.stake,
            )
            if accepted
            else Stake()
        )
        source = self.model_source(context, "portfolio_risk")
        return self.output(
            context,
            RiskReport(
                accepted=accepted,
                stake=stake,
                reasons=tuple(reasons),
                candidate=candidate,
                exposures=exposures,
                operationally_eligible=accepted,
            ),
            summary="Conservative fractional Kelly with cumulative and correlated exposure limits",
            provenance=(source,),
            operationally_eligible=accepted,
        )
