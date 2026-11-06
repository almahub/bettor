import asyncio
import hashlib
import json

from better.domain.enums import AgentStatus, ErrorCode
from better.domain.errors import BetterError
from better.domain.models import Degradation
from better.domain.schemas import DataReport, TacticalMatchup, TacticalReport
from better.providers.llm import ReasoningRequest

from .base import BaseAgent


class TacticalAnalyst(BaseAgent):
    agent_id = "tactical_analyst"
    dependencies = ("football_data_analyst", "lineup_player_impact", "news_intelligence")
    allowed_tools = frozenset({"event_data", "system_two_provider"})

    def __init__(self, settings, system_two=None):
        super().__init__(settings)
        self.system_two = system_two

    async def _run(self, context):
        data = self.payload(context, "football_data_analyst", DataReport)
        if not data:
            return self.missing(context, "No source-supported tactical traits")
        home, away = data.team_profiles.get("home"), data.team_profiles.get("away")
        if not home or not away or not home.tactical_traits or not away.tactical_traits:
            return self.missing(context, "Event/video-derived traits unavailable; no invented matchup")
        source_results = context.upstream_results["football_data_analyst"]
        parents = tuple(e.evidence_id for e in source_results.evidence)
        matchups = []
        for name in sorted(set(home.tactical_traits) & set(away.tactical_traits)):
            matchups.append(
                TacticalMatchup(
                    home_trait=f"{name}: {home.tactical_traits[name]}",
                    away_trait=f"{name}: {away.tactical_traits[name]}",
                    interaction="Compare recorded opposing traits in the same dimension",
                    possible_market_effect="UNQUANTIFIED; no probability adjustment without validated interaction model",
                    evidence_ids=parents,
                    limitations=("No verified video sequence or fitted tactical interaction effect",),
                )
            )
        evidence = self.evidence(
            context,
            "Source-supported style comparison without causal or numerical extrapolation",
            source_results.provenance,
            kind="TACTICAL_MATCHUP",
            derived_from=parents,
            confidence=0.5,
        )
        analysis_text = None
        unknowns = []
        deep_unavailable = False
        if self.settings.system_two_enabled and self.system_two is None:
            unknowns.append("System 2 enabled but no provider configured; deep reasoning not performed")
            deep_unavailable = True
        if self.settings.system_two_enabled and self.system_two is not None:
            supplied = json.dumps(
                {
                    "matchups": [m.model_dump(mode="json") for m in matchups],
                    "evidence": [e.model_dump(mode="json") for e in source_results.evidence],
                },
                sort_keys=True,
            )
            try:
                response = await asyncio.wait_for(
                    self.system_two.reason(
                        ReasoningRequest(
                            task="Explain opposing tactical traits and limitations",
                            trusted_system_prompt=self.system_prompt,
                            untrusted_evidence_json=supplied,
                            model=getattr(self.system_two, "model_id", "configured-system-two"),
                            input_hash=hashlib.sha256(supplied.encode()).hexdigest(),
                        )
                    ),
                    self.settings.provider_timeout_seconds,
                )
                if not set(response.evidence_ids) <= set(str(identifier) for identifier in parents):
                    raise BetterError(ErrorCode.SCHEMA_ERROR, "System 2 cited unavailable evidence")
                analysis_text = response.analysis
                unknowns.extend(response.limitations)
                if response.requires_review:
                    unknowns.append("System 2 interpretation requires review")
                    deep_unavailable = True
            except TimeoutError:
                unknowns.append("System 2 timed out; deep reasoning unavailable")
                deep_unavailable = True
            except BetterError as error:
                if error.detail.code == ErrorCode.SCHEMA_ERROR:
                    raise
                unknowns.append(f"System 2 unavailable: {error.detail.code.value}")
                deep_unavailable = True
        return self.output(
            context,
            TacticalReport(matchups=tuple(matchups), analysis_text=analysis_text),
            summary="Qualitative opposing traits with explicit uncertainty",
            quality=source_results.data_quality,
            provenance=source_results.provenance,
            evidence=(evidence,),
            confidence=0.5,
            status=AgentStatus.PARTIAL,
            degradations=(
                Degradation(
                    code="TACTICAL_EFFECT_UNVALIDATED",
                    missing_checks=("verified_video_sequence", "fitted_tactical_interaction_effect"),
                    critical=False,
                    affected_gates=("CONFIDENCE",),
                    impact="WARNING_ONLY",
                    reason="Source traits support qualitative comparison only (agent confidence 0.5). "
                    "No tactical probability or stake adjustment is used. Decision confidence measures "
                    "the independent quantitative inputs, not an average of agent confidence; no extra penalty.",
                ),
            )
            + (
                (
                    Degradation(
                        code="SYSTEM_TWO_INTERPRETATION_LIMITED",
                        missing_checks=tuple(unknowns),
                        critical=False,
                        affected_gates=("CONTRARIAN", "CONFIDENCE"),
                        impact="CONFIDENCE_CAP" if deep_unavailable else "WARNING_ONLY",
                        confidence_cap=0.5 if deep_unavailable else None,
                        reason="Requested deep interpretation is incomplete or requires review. "
                        "Cap operational confidence at the existing qualitative agent confidence (0.5); "
                        "this is a conservative policy, not an empirically calibrated probability."
                        if deep_unavailable
                        else "The completed interpretation declares qualitative limitations. "
                        "It does not adjust numerical probabilities or stake; no extra confidence penalty.",
                    ),
                )
                if unknowns
                else ()
            ),
            unknowns=unknowns,
        )
