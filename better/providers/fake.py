"""Explicitly synthetic, deterministic offline sources; never real match observations."""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from better.domain.enums import ErrorCode, MarketType, ReliabilityTier, SourceType
from better.domain.errors import BetterError
from better.domain.models import (
    HistoricalPrediction,
    LineupSnapshot,
    Market,
    Match,
    NewsItem,
    NewsSnapshot,
    OddsQuote,
    OddsSnapshot,
    Portfolio,
    Position,
    StatsSnapshot,
    TeamStats,
)
from better.domain.provenance import Provenance

from .base import ProviderBundle


@dataclass
class FakeProvider:
    match: Match
    stats: StatsSnapshot
    odds: OddsSnapshot
    news: NewsSnapshot
    lineups: LineupSnapshot
    failures: dict[str, ErrorCode] = field(default_factory=dict)
    delay: float = 0
    calls: dict[str, int] = field(default_factory=dict)

    async def _get(self, name: str, match_id: str):
        self.calls[name] = self.calls.get(name, 0) + 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if name in self.failures:
            raise BetterError(self.failures[name], f"Synthetic {name} provider failure", source="fake")
        if match_id != self.match.match_id:
            raise BetterError(ErrorCode.MISSING_DATA, "Unknown synthetic match", source="fake")
        return getattr(self, name)

    async def list_matches(self, as_of: datetime) -> tuple[Match, ...]:
        return (self.match,) if self.match.kickoff >= as_of else ()

    async def get_match(self, match_id: str) -> Match:
        return await self._get("match", match_id)

    async def get_stats(self, match_id: str, as_of: datetime) -> StatsSnapshot:
        return await self._get("stats", match_id)

    async def get_odds(self, match_id: str, as_of: datetime) -> OddsSnapshot:
        return await self._get("odds", match_id)

    async def get_news(self, match_id: str, as_of: datetime) -> NewsSnapshot:
        return await self._get("news", match_id)

    async def get_lineups(self, match_id: str, as_of: datetime) -> LineupSnapshot:
        return await self._get("lineups", match_id)


DEMO_CASES = (
    "bet",
    "no_bet",
    "missing_data",
    "stale_odds",
    "wait_lineups",
    "suspended_model",
    "source_conflict",
    "risk_limit",
    "wait_odds",
    "news_offline",
    "odds_offline",
    "watchlist",
)


def make_demo_bundle(case: str = "bet", as_of: datetime | None = None) -> ProviderBundle:
    if case not in DEMO_CASES:
        raise ValueError(f"Unknown demo case: {case}")
    now = as_of or datetime.now(timezone.utc)
    match = Match(
        match_id="demo-match",
        competition="SYNTHETIC_LEAGUE",
        season="demo",
        kickoff=now + timedelta(hours=2),
        home_team="Synthetic Home",
        away_team="Synthetic Away",
        home_team_id="synthetic-home",
        away_team_id="synthetic-away",
    )

    def source(field, age=timedelta(minutes=1), *, official=False, tier=ReliabilityTier.A):
        return Provenance(
            source_id="synthetic-fixture",
            source_type=SourceType.OFFICIAL if official else SourceType.API,
            retrieved_at=now - age,
            published_at=now - age,
            reliability_tier=tier,
            field=field,
            raw_reference=f"demo:{case}:{field}",
            notes="SYNTHETIC TEST FIXTURE: not real observations",
        )

    def team(team_id, attack, defense):
        return TeamStats(
            team_id=team_id,
            sample_size=10 if case == "watchlist" else 80,
            goals_for_per_match=attack,
            goals_against_per_match=defense,
            xg_per_match=attack,
            xga_per_match=defense,
            segments={
                key: {"goals_for_per_match": attack, "goals_against_per_match": defense}
                for key in ("last_5", "last_10", "season", "home", "away", "opponent_strength")
            },
            tactical_traits={
                "pressing": "high" if attack > 1.5 else "low",
                "build_up": "short" if attack > 1.5 else "direct",
            },
        )

    stats = StatsSnapshot(
        snapshot_id=f"demo-stats:{case}",
        match_id=match.match_id,
        home=team(match.home_team_id, 2.2, 0.8),
        away=team(match.away_team_id, 0.8, 2.0),
        data_cutoff=now - timedelta(days=1),
        captured_at=now - timedelta(hours=1),
        provenance=(source("stats", timedelta(hours=1)),),
    )
    quote_age = timedelta(minutes=20) if case == "stale_odds" else timedelta(minutes=1)
    home_odds = "1.20" if case == "wait_odds" else "3.50" if case == "watchlist" else "2.10"
    prices = {
        MarketType.MATCH_WINNER: {"HOME": home_odds, "DRAW": "3.30", "AWAY": "3.50"},
        MarketType.OVER_UNDER_2_5: {"OVER_2_5": "1.60", "UNDER_2_5": "2.20"},
        MarketType.BTTS: {"YES": "1.70", "NO": "2.00"},
    }
    quotes = tuple(
        OddsQuote(
            match_id=match.match_id,
            bookmaker="SYNTHETIC_BOOK",
            market=Market(type=market, selection=selection),
            odds=Decimal(odds),
            captured_at=now - quote_age,
            liquidity=Decimal("10000"),
            provenance=source(f"odds.{market}.{selection}", quote_age),
        )
        for market, selections in prices.items()
        for selection, odds in selections.items()
    )
    odds = OddsSnapshot(snapshot_id=f"demo-odds:{case}", match_id=match.match_id, quotes=quotes)
    items = ()
    if case == "source_conflict":
        items = (
            NewsItem(
                event_id="injury",
                headline="Synthetic conflicting injury report",
                category="INJURY",
                verified=True,
                material=True,
                contradicts_event_id="fit",
                provenance=source("news.injury"),
            ),
            NewsItem(
                event_id="fit",
                headline="Synthetic availability report",
                category="INJURY",
                verified=True,
                material=True,
                provenance=source("news.fit"),
            ),
        )
    news = NewsSnapshot(
        snapshot_id=f"demo-news:{case}",
        match_id=match.match_id,
        items=items,
        captured_at=now - timedelta(minutes=1),
        provenance=(source("news"),),
    )
    lineups = LineupSnapshot(
        snapshot_id=f"demo-lineups:{case}",
        match_id=match.match_id,
        status="PROBABLE_LINEUP" if case == "wait_lineups" else "CONFIRMED_LINEUP",
        home_players=tuple(f"synthetic-home-{i}" for i in range(11)),
        away_players=tuple(f"synthetic-away-{i}" for i in range(11)),
        sensitivity=0.9 if case == "wait_lineups" else 0.2,
        captured_at=now - timedelta(minutes=1),
        provenance=(source("lineups", official=True),),
    )
    failures = {}
    if case == "missing_data":
        failures["stats"] = ErrorCode.MISSING_DATA
    if case in {"news_offline", "odds_offline"}:
        failures[case.split("_")[0]] = ErrorCode.TRANSIENT_PROVIDER_ERROR
    provider = FakeProvider(match, stats, odds, news, lineups, failures)
    history = []
    for market in prices:
        for i in range(300):
            outcome = int(i % 10 < 7)
            probability = 0.1 if case == "suspended_model" else 0.7
            predicted = now - timedelta(days=302 - i)
            history.append(
                HistoricalPrediction(
                    model_id="poisson",
                    model_version="1.0.0",
                    competition=match.competition,
                    market=market,
                    probability=probability,
                    outcome=outcome,
                    predicted_at=predicted,
                    settled_at=predicted + timedelta(hours=3),
                    data_cutoff=predicted - timedelta(hours=1),
                    split="TEST",
                    source_type="LIVE_FORWARD",
                    odds=Decimal("1.50"),
                    closing_odds=Decimal("1.45"),
                    profit=Decimal("0.50" if outcome else "-1"),
                    season="synthetic",
                    bookmaker="SYNTHETIC_BOOK",
                )
            )
    positions = ()
    if case == "risk_limit":
        positions = (
            Position(
                match_id=match.match_id,
                team_ids=(match.home_team_id, match.away_team_id),
                competition=match.competition,
                market=Market(type=MarketType.MATCH_WINNER, selection="HOME"),
                bankroll_fraction=0.015,
                placed_at=now - timedelta(minutes=10),
            ),
        )
    return ProviderBundle(
        provider,
        provider,
        provider,
        provider,
        provider,
        tuple(history),
        Portfolio(positions=positions, drawdown=0.25 if case == "no_bet" else 0),
    )
