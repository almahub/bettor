from better.domain.enums import AgentStatus
from better.domain.models import Degradation
from better.domain.schemas import DataReport

from .base import BaseAgent

REQUIRED_SEGMENTS = frozenset({"last_5", "last_10", "season", "home", "away", "opponent_strength"})


class FootballDataAnalyst(BaseAgent):
    agent_id = "football_data_analyst"
    allowed_tools = frozenset(
        {
            "football_stats_provider",
            "fixture_provider",
            "team_provider",
            "competition_provider",
            "local_feature_store",
        }
    )
    forbidden_tools = BaseAgent.forbidden_tools | {"odds_provider", "bankroll_manager"}

    def __init__(self, provider, settings):
        super().__init__(settings)
        self.provider = provider

    async def _run(self, context):
        snapshot = await self.provider.get_stats(context.match.match_id, context.as_of)
        if snapshot.match_id != context.match.match_id or snapshot.data_cutoff > context.as_of:
            return self.missing(context, "Stats match/cutoff mismatch")
        if (
            snapshot.home.team_id != context.match.home_team_id
            or snapshot.away.team_id != context.match.away_team_id
        ):
            return self.missing(context, "Stats team identity mismatch")
        warnings = []
        required = REQUIRED_SEGMENTS
        coverage = []
        for team in (snapshot.home, snapshot.away):
            absent = required - set(team.segments)
            if absent:
                warnings.append(f"{team.team_id}: missing segments {', '.join(sorted(absent))}")
            if team.sample_size < self.settings.min_training_sample:
                warnings.append(f"{team.team_id}: SMALL_SAMPLE")
            coverage.append((len(required) - len(absent)) / len(required))
        quality = self.quality(
            context, snapshot.provenance, snapshot.captured_at, self.settings.stats_ttl_seconds, min(coverage)
        )
        evidence = self.evidence(
            context,
            "Observed home/away goal rates and source-supplied segments",
            snapshot.provenance,
            kind="STATISTICS",
        )
        payload = DataReport(
            team_profiles={"home": snapshot.home, "away": snapshot.away},
            comparisons={
                "attack_difference": snapshot.home.goals_for_per_match - snapshot.away.goals_for_per_match
            },
            sample_warnings=tuple(warnings),
            data_cutoff=snapshot.data_cutoff,
        )
        return self.output(
            context,
            payload,
            summary="Normalized source-supplied statistics",
            quality=quality,
            provenance=snapshot.provenance,
            evidence=(evidence,),
            unknowns=warnings,
            degradations=(
                Degradation(
                    code="STATISTICAL_COVERAGE_LIMITED",
                    missing_checks=tuple(warnings),
                    critical=False,
                    affected_gates=("DATA_QUALITY", "CONFIDENCE"),
                    impact="WARNING_ONLY",
                    reason="Missing segments already reduce data completeness. Training sample size is "
                    "checked by the predictive agent; its uncertainty enters final confidence. "
                    "No duplicate fixed penalty is applied.",
                ),
            )
            if warnings
            else (),
            status=AgentStatus.STALE_DATA
            if not quality.freshness
            else AgentStatus.PARTIAL
            if warnings
            else AgentStatus.OK,
        )
