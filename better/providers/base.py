from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from better.domain.models import (
    HistoricalPrediction,
    LineupSnapshot,
    Match,
    NewsSnapshot,
    OddsSnapshot,
    Portfolio,
    StatsSnapshot,
)

from .llm import SystemTwoProvider


class FixtureProvider(Protocol):
    async def list_matches(self, as_of: datetime) -> tuple[Match, ...]: ...
    async def get_match(self, match_id: str) -> Match: ...


class FootballDataProvider(Protocol):
    async def get_stats(self, match_id: str, as_of: datetime) -> StatsSnapshot: ...


class OddsProvider(Protocol):
    async def get_odds(self, match_id: str, as_of: datetime) -> OddsSnapshot: ...


class NewsProvider(Protocol):
    async def get_news(self, match_id: str, as_of: datetime) -> NewsSnapshot: ...


class LineupProvider(Protocol):
    async def get_lineups(self, match_id: str, as_of: datetime) -> LineupSnapshot: ...


@dataclass(frozen=True)
class ProviderBundle:
    fixtures: FixtureProvider
    stats: FootballDataProvider
    odds: OddsProvider
    news: NewsProvider
    lineups: LineupProvider
    validation_history: tuple[HistoricalPrediction, ...] = ()
    portfolio: Portfolio | None = None
    system_two: SystemTwoProvider | None = None
    results: object | None = None
