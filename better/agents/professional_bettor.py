from better.domain.enums import DecisionStatus
from better.domain.schemas import DecisionReport

from .base import BaseAgent


class ProfessionalBettor(BaseAgent):
    agent_id = "professional_bettor"
    dependencies = (
        "football_data_analyst",
        "football_odds_analyst",
        "news_intelligence",
        "lineup_player_impact",
        "tactical_analyst",
        "predictive_model",
        "market_selection",
        "value_bettor",
        "model_validator",
        "risk_manager",
        "devils_advocate",
    )
    allowed_tools = frozenset({"deterministic_decision_service"})
    forbidden_tools = BaseAgent.forbidden_tools | {
        "football_stats_provider",
        "odds_provider",
        "news_provider",
    }
    needs_portfolio = True

    async def _run(self, context):
        from better.services.decision_service import DecisionService

        decision = DecisionService(self.settings).evaluate(context)
        return self.output(
            context,
            DecisionReport(decision=decision),
            summary=decision.status.value,
            quality=decision.data_quality,
            confidence=decision.confidence,
            analysis_available=True,
            operationally_eligible=decision.status == DecisionStatus.BET,
        )
