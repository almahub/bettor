from better.domain.enums import AgentStatus
from better.domain.schemas import LineupReport

from .base import BaseAgent


class LineupPlayerImpactAgent(BaseAgent):
    agent_id = "lineup_player_impact"
    allowed_tools = frozenset({"lineup_provider", "player_impact_model"})

    def __init__(self, provider, settings):
        super().__init__(settings)
        self.provider = provider

    async def _run(self, context):
        snapshot = await self.provider.get_lineups(context.match.match_id, context.as_of)
        if snapshot.match_id != context.match.match_id:
            return self.missing(context, "Lineup match identity mismatch")
        official = snapshot.status == "CONFIRMED_LINEUP"
        ttl = self.settings.official_lineups_ttl_seconds if official else self.settings.lineups_ttl_seconds
        quality = self.quality(
            context,
            snapshot.provenance,
            snapshot.captured_at,
            ttl,
            completeness=1 if official else 0.8 if snapshot.status == "PROBABLE_LINEUP" else 0,
        )
        evidence = self.evidence(
            context,
            f"Lineup status: {snapshot.status}",
            snapshot.provenance,
            kind="LINEUP_STATUS",
            confidence=1 if official else 0.5,
        )
        return self.output(
            context,
            LineupReport(lineup=snapshot, official=official, sensitivity=snapshot.sensitivity),
            summary="Formation status preserved; no untrained numerical player adjustment",
            quality=quality,
            evidence=(evidence,),
            provenance=snapshot.provenance,
            unknowns=("Player impact model not fitted; probability delta unavailable",),
            confidence=quality.score if official else 0.5,
            status=AgentStatus.OK if quality.freshness else AgentStatus.STALE_DATA,
        )
