from collections import defaultdict

from better.domain.enums import AgentStatus
from better.domain.schemas import OddsReport

from .base import BaseAgent


class FootballOddsAnalyst(BaseAgent):
    agent_id = "football_odds_analyst"
    allowed_tools = frozenset({"odds_provider"})
    forbidden_tools = BaseAgent.forbidden_tools | {"probability_model", "bankroll_manager"}

    def __init__(self, provider, settings):
        super().__init__(settings)
        self.provider = provider

    async def _run(self, context):
        snapshot = await self.provider.get_odds(context.match.match_id, context.as_of)
        quotes = tuple(
            q
            for q in snapshot.quotes
            if q.captured_at <= context.as_of and q.provenance.available_at(context.as_of)
        )
        if snapshot.match_id != context.match.match_id or any(
            q.match_id != context.match.match_id for q in quotes
        ):
            return self.missing(context, "Odds match identity mismatch")
        if not quotes:
            return self.missing(context, "No odds available at as_of")
        by_selection = defaultdict(list)
        for quote in quotes:
            by_selection[
                (quote.bookmaker, quote.market.type, quote.market.selection, quote.market.line)
            ].append(quote)
        current = []
        movements = {}
        for key, history in by_selection.items():
            history.sort(key=lambda q: q.captured_at)
            latest = history[-1]
            current.append(latest)
            openings = [q for q in history if q.is_opening]
            if openings:
                movements["|".join(map(str, key))] = latest.odds / openings[0].odds - 1
        quality = self.quality(
            context,
            tuple(q.provenance for q in current),
            min(q.captured_at for q in current),
            self.settings.odds_ttl_seconds,
        )
        anomalies = (
            ("MARKET_MOVEMENT", "UNKNOWN_CAUSE")
            if any(abs(v) > 0.05 for v in movements.values())
            else ("NO_SIGNIFICANT_MOVE",)
        )
        evidence = self.evidence(
            context,
            "Current market quotes; movements do not establish betting value",
            tuple(q.provenance for q in current),
            kind="MARKET_OBSERVATION",
        )
        return self.output(
            context,
            OddsReport(quotes=tuple(current), movements=movements, anomalies=anomalies),
            summary="Market prices without value judgement",
            quality=quality,
            evidence=(evidence,),
            provenance=tuple(q.provenance for q in current),
            status=AgentStatus.OK if quality.freshness else AgentStatus.STALE_DATA,
        )
