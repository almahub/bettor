from .base import BaseAgent
from .betting_journal import BettingJournal
from .data_analyst import FootballDataAnalyst
from .devils_advocate import DevilsAdvocate
from .lineup_player_impact import LineupPlayerImpactAgent
from .market_selection import MarketSelectionAgent
from .matched_betting import MatchedBettingAgent
from .model_validator import ModelValidator
from .news_intelligence import NewsIntelligenceAgent
from .odds_analyst import FootballOddsAnalyst
from .predictive_model import PredictiveModelAgent
from .professional_bettor import ProfessionalBettor
from .risk_manager import RiskManager
from .tactical_analyst import TacticalAnalyst
from .value_bettor import ValueBettor


class AgentRegistry:
    def __init__(self, agents=(), enabled: dict[str, bool] | None = None):
        self._agents: dict[str, BaseAgent] = {}
        self.enabled_flags = dict(enabled or {})
        for agent in agents:
            self.register(agent)

    def register(self, agent: BaseAgent):
        if agent.agent_id in self._agents:
            raise ValueError(f"Duplicate agent: {agent.agent_id}")
        self._agents[agent.agent_id] = agent

    def get(self, agent_id: str) -> BaseAgent:
        return self._agents[agent_id]

    def is_enabled(self, agent_id: str) -> bool:
        return agent_id in self._agents and self.enabled_flags.get(agent_id, True)

    def ids(self) -> tuple[str, ...]:
        return tuple(self._agents)

    def __iter__(self):
        return iter(self._agents.values())

    def __len__(self):
        return len(self._agents)

    def __getitem__(self, key):
        return self.get(key)


AGENT_REGISTRY = {
    cls.agent_id: cls
    for cls in (
        FootballDataAnalyst,
        FootballOddsAnalyst,
        NewsIntelligenceAgent,
        LineupPlayerImpactAgent,
        TacticalAnalyst,
        PredictiveModelAgent,
        ValueBettor,
        MarketSelectionAgent,
        MatchedBettingAgent,
        ModelValidator,
        RiskManager,
        DevilsAdvocate,
        BettingJournal,
        ProfessionalBettor,
    )
}


def create_registry(providers, settings, journal=None) -> AgentRegistry:
    agents = [
        FootballDataAnalyst(providers.stats, settings),
        FootballOddsAnalyst(providers.odds, settings),
        NewsIntelligenceAgent(providers.news, settings),
        LineupPlayerImpactAgent(providers.lineups, settings),
        TacticalAnalyst(settings, getattr(providers, "system_two", None)),
        PredictiveModelAgent(settings),
        ValueBettor(settings),
        MarketSelectionAgent(settings),
        MatchedBettingAgent(settings),
        ModelValidator(settings),
        RiskManager(settings),
        DevilsAdvocate(settings),
        BettingJournal(settings, journal),
        ProfessionalBettor(settings),
    ]
    return AgentRegistry(agents, settings.agents_enabled)
