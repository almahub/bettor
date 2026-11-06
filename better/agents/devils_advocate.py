from uuid import NAMESPACE_URL, uuid5

from better.domain.enums import AgentStatus, ConcernSeverity
from better.domain.models import Concern
from better.domain.schemas import ContrarianReport, DataReport, NewsReport, PredictiveReport, ValueReport

from .base import BaseAgent


class DevilsAdvocate(BaseAgent):
    agent_id = "devils_advocate"
    dependencies = (
        "value_bettor",
        "football_data_analyst",
        "football_odds_analyst",
        "news_intelligence",
        "lineup_player_impact",
        "predictive_model",
        "model_validator",
        "tactical_analyst",
    )
    allowed_tools = frozenset({"evidence_audit", "system_two_provider"})

    async def _run(self, context):
        concerns = []

        def add(severity, reason, condition, agent_id=None):
            evidence = (
                tuple(e.evidence_id for e in context.upstream_results[agent_id].evidence)
                if agent_id in context.upstream_results
                else ()
            )
            concerns.append(
                Concern(
                    concern_id=uuid5(NAMESPACE_URL, f"{context.analysis_id}:concern:{reason}"),
                    severity=severity,
                    reason=reason,
                    condition=condition,
                    evidence_ids=evidence,
                )
            )

        value = self.payload(context, "value_bettor", ValueReport)
        if value and value.candidates:
            candidate = max(value.candidates, key=lambda c: c.expected_value)
            if candidate.model_probability - candidate.conservative_probability > 0.2:
                add(
                    ConcernSeverity.MAJOR_CONCERN,
                    "Large model uncertainty envelope",
                    "Obtain larger independent statistical sample",
                    "value_bettor",
                )
        data = self.payload(context, "football_data_analyst", DataReport)
        if data:
            if any(t.sample_size < self.settings.min_training_sample for t in data.team_profiles.values()):
                add(
                    ConcernSeverity.FATAL_CONCERN,
                    "Small training sample",
                    "Minimum training sample must be met",
                    "football_data_analyst",
                )
            if any("opponent_strength" not in t.segments for t in data.team_profiles.values()):
                add(
                    ConcernSeverity.MAJOR_CONCERN,
                    "Opponent strength not supplied",
                    "Add opponent-adjusted evidence",
                    "football_data_analyst",
                )
        news = self.payload(context, "news_intelligence", NewsReport)
        if news and news.conflicts:
            add(
                ConcernSeverity.FATAL_CONCERN,
                "Unresolved source conflict",
                "Resolve contradictory primary event sources",
                "news_intelligence",
            )
        if news and news.unverified_material:
            add(
                ConcernSeverity.MAJOR_CONCERN,
                "Material unverified news",
                "Obtain official or reliable independent confirmation",
                "news_intelligence",
            )
        for agent_id, result in context.upstream_results.items():
            if result.status == AgentStatus.STALE_DATA and agent_id in {
                "football_data_analyst",
                "football_odds_analyst",
            }:
                add(
                    ConcernSeverity.FATAL_CONCERN,
                    f"Stale critical information: {agent_id}",
                    "Refresh critical snapshot",
                    agent_id,
                )
            if any(not p.available_at(context.as_of) for p in result.provenance):
                add(
                    ConcernSeverity.FATAL_CONCERN,
                    f"Future leakage: {agent_id}",
                    "Rebuild analysis using only as-of evidence",
                    agent_id,
                )
        predictive = self.payload(context, "predictive_model", PredictiveReport)
        if predictive and any(
            not prediction.operationally_eligible for prediction in predictive.model_predictions
        ):
            add(
                ConcernSeverity.MAJOR_CONCERN,
                "Model estimate is indicative and lacks mature independent validation",
                "MODEL_VALIDATION_INSUFFICIENT",
                "predictive_model",
            )
        if predictive and any(p.data_cutoff > context.as_of for p in predictive.model_predictions):
            add(
                ConcernSeverity.FATAL_CONCERN,
                "Model cutoff after analysis timestamp",
                "Rebuild features before cutoff",
                "predictive_model",
            )
        source = self.model_source(context, "contrarian_audit")
        return self.output(
            context,
            ContrarianReport(concerns=tuple(concerns)),
            summary="Evidence-based attempts to invalidate preliminary proposal",
            provenance=(source,),
            risks=tuple(c.reason for c in concerns),
        )
