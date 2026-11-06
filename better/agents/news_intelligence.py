from better.domain.enums import AgentStatus
from better.domain.models import Degradation
from better.domain.schemas import NewsReport

from .base import BaseAgent


class NewsIntelligenceAgent(BaseAgent):
    agent_id = "news_intelligence"
    allowed_tools = frozenset({"news_provider", "official_club_provider", "weather_provider"})

    def __init__(self, provider, settings):
        super().__init__(settings)
        self.provider = provider

    async def _run(self, context):
        snapshot = await self.provider.get_news(context.match.match_id, context.as_of)
        if snapshot.match_id != context.match.match_id:
            return self.missing(context, "News match identity mismatch")
        events = {}
        for item in snapshot.items:
            if not item.provenance.available_at(context.as_of):
                continue
            key = (item.event_id, item.provenance.primary_source_id or item.provenance.source_id)
            previous = events.get(key)
            if (
                previous is None
                or item.provenance.reliability_tier.value < previous.provenance.reliability_tier.value
            ):
                events[key] = item
        conflicts = tuple(sorted({item.event_id for item in events.values() if item.contradicts_event_id}))
        unverified = tuple(
            sorted(
                {
                    item.event_id
                    for item in events.values()
                    if item.material
                    and (not item.verified or item.provenance.reliability_tier.value in {"C", "D"})
                }
            )
        )
        quality = self.quality(
            context,
            snapshot.provenance,
            snapshot.captured_at,
            self.settings.news_ttl_seconds,
            consistency=0 if conflicts else 1,
        )
        evidence = tuple(
            self.evidence(
                context,
                item.headline,
                (item.provenance,),
                kind="NEWS_EVENT",
                confidence=0.8 if item.verified else 0.3,
            )
            for item in events.values()
        )
        return self.output(
            context,
            NewsReport(events=tuple(events.values()), conflicts=conflicts, unverified_material=unverified),
            summary="Deduplicated sourced events; retrieved text is untrusted data",
            quality=quality,
            evidence=evidence,
            provenance=snapshot.provenance,
            risks=tuple(f"SOURCE_CONFLICT:{e}" for e in conflicts),
            degradations=(
                Degradation(
                    code="UNRESOLVED_NEWS_CONFLICT",
                    missing_checks=tuple(f"resolve:{event}" for event in conflicts),
                    critical=True,
                    affected_gates=("CONTRARIAN",),
                    impact="BLOCK",
                    reason="Material source contradictions require resolution before BET.",
                ),
            )
            if conflicts
            else (),
            status=AgentStatus.STALE_DATA
            if not quality.freshness
            else AgentStatus.PARTIAL
            if conflicts
            else AgentStatus.OK,
        )
