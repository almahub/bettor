from decimal import Decimal

from pydantic import Field

from better.domain.common import Contract, DecimalOdds, Probability
from better.domain.schemas import MatchedReport
from better.system_zero.math import matched_back_lay

from .base import BaseAgent


class MatchedBettingInput(Contract):
    promotion: str
    requirements: tuple[str, ...]
    back_stake: Decimal = Field(gt=0)
    back_odds: DecimalOdds
    lay_odds: DecimalOdds
    commission: Probability = 0
    stake_returned: bool = True
    rollover: Decimal = Field(default=Decimal("0"), ge=0)


class MatchedBettingAgent(BaseAgent):
    agent_id = "matched_betting"
    allowed_tools = frozenset({"back_lay_math"})
    forbidden_tools = BaseAgent.forbidden_tools | {
        "football_prediction",
        "tactical_signal",
        "final_sports_bet_decision",
    }

    def __init__(self, settings, proposal: MatchedBettingInput | None = None):
        super().__init__(settings)
        self.proposal = proposal

    def calculate(self, proposal: MatchedBettingInput) -> MatchedReport:
        result = matched_back_lay(
            proposal.back_stake,
            proposal.back_odds,
            proposal.lay_odds,
            proposal.commission,
            stake_returned=proposal.stake_returned,
        )
        return MatchedReport(
            lay_stake=str(result["lay_stake"]),
            liability=str(result["liability"]),
            profit_if_back_wins=str(result["win_profit"]),
            profit_if_lay_wins=str(result["lose_profit"]),
            assumptions=(
                "Both prices remain executable; equal settlement rules; full hedge filled",
                f"Rollover {proposal.rollover} is an outstanding constraint, not assumed completed",
                "Promotion eligibility and requirements require independent verification",
                *proposal.requirements,
            ),
        )

    async def _run(self, context):
        if self.proposal is None:
            return self.missing(context, "Matched betting requires a separate typed promotion proposal")
        return self.output(
            context,
            self.calculate(self.proposal),
            summary="Independent back/lay hedge arithmetic; no sports prediction",
        )
