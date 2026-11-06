import math
from uuid import NAMESPACE_URL, uuid5

from better.domain.enums import AgentStatus, PredictionStatus, ValidationStatus
from better.domain.models import Degradation, ModelPrediction, ProbabilityEstimate
from better.domain.schemas import DataReport, PredictiveReport, ValidationReport
from better.models.canonical import (
    FEATURE_SCHEMA_VERSION,
    MODEL_NAME,
    MODEL_VERSION,
    poisson_from_stats,
)

from .base import BaseAgent


class PredictiveModelAgent(BaseAgent):
    agent_id = "predictive_model"
    dependencies = ("football_data_analyst", "model_validator")
    allowed_tools = frozenset({"probability_models", "feature_store"})
    forbidden_tools = BaseAgent.forbidden_tools | {"odds_provider"}

    async def _run(self, context):
        data = self.payload(context, "football_data_analyst", DataReport)
        validation = self.payload(context, "model_validator", ValidationReport)
        if not data:
            return self.missing(context, "Statistics required for quantitative probabilities")
        home, away = data.team_profiles.get("home"), data.team_profiles.get("away")
        if home is None or away is None or min(home.sample_size, away.sample_size) <= 0:
            return self.missing(context, "Insufficient team observations")
        canonical = poisson_from_stats(home, away, context.requested_markets)
        distribution = canonical["score_distribution"]
        probabilities = canonical["probabilities"]
        size = canonical["sample_size"]
        training_sample_ok = size >= self.settings.min_training_sample
        matching_validations = [
            item
            for item in (validation.validations if validation else ())
            if item.model_id == MODEL_NAME
            and item.model_version == MODEL_VERSION
            and item.market in probabilities
        ]
        admitted_validations = [
            item
            for item in matching_validations
            if item.validation_status in {ValidationStatus.NORMAL_WEIGHT, ValidationStatus.REDUCED_WEIGHT}
            and item.recommended_weight_multiplier > 0
        ]
        allowed = {item.market for item in admitted_validations}
        ensemble = {
            market: p for market, p in probabilities.items() if training_sample_ok and market in allowed
        }
        if ensemble:
            prediction_status = PredictionStatus.VALIDATED
            operationally_eligible = True
            relevant_validation = [item for item in admitted_validations if item.market in ensemble]
            validation_sample_size = min(item.sample_size for item in relevant_validation)
            validation_status = min(
                (item.validation_status for item in relevant_validation),
                key=lambda status: 0 if status == ValidationStatus.REDUCED_WEIGHT else 1,
            )
            reason = "ALL_OPERATIONAL_CHECKS_PASSED"
        elif not training_sample_ok:
            prediction_status = PredictionStatus.INDICATIVE
            operationally_eligible = False
            validation_sample_size = max((item.sample_size for item in matching_validations), default=0)
            validation_status = next(
                (
                    item.validation_status
                    for item in matching_validations
                    if item.validation_status == ValidationStatus.SUSPEND_MODEL
                ),
                ValidationStatus.INSUFFICIENT_SAMPLE,
            )
            reason = "MODEL_INPUT_SAMPLE_BELOW_OPERATIONAL_MINIMUM"
        else:
            prediction_status = PredictionStatus.UNVALIDATED
            operationally_eligible = False
            validation_sample_size = max((item.sample_size for item in matching_validations), default=0)
            validation_status = next(
                (
                    item.validation_status
                    for item in matching_validations
                    if item.validation_status == ValidationStatus.SUSPEND_MODEL
                ),
                ValidationStatus.INSUFFICIENT_SAMPLE,
            )
            reason = (
                "MODEL_VALIDATION_SUSPENDED"
                if validation_status == ValidationStatus.SUSPEND_MODEL
                else "MODEL_VALIDATION_INSUFFICIENT"
            )
        uncertainty = {}
        for market, selections in probabilities.items():
            for selection, p in selections.items():
                # Sensitivity envelope, not a claimed fitted/calibrated confidence interval.
                width = min(0.5, 1.96 * math.sqrt(p * (1 - p) / size))
                uncertainty[f"{market.value}:{selection}"] = ProbabilityEstimate(
                    point_estimate=p,
                    lower=max(0, p - width),
                    upper=min(1, p + width),
                    method="binomial_standard_error_sensitivity_not_calibrated_interval",
                )
        prediction = ModelPrediction(
            prediction_id=uuid5(NAMESPACE_URL, f"{context.analysis_id}:{MODEL_NAME}:{MODEL_VERSION}"),
            model=MODEL_NAME,
            model_version=MODEL_VERSION,
            probabilities=probabilities,
            data_cutoff=data.data_cutoff,
            sample_size=size,
            feature_version=FEATURE_SCHEMA_VERSION,
            features=canonical["features"],
            uncertainty=uncertainty,
            fair_odds={
                market: {selection: 1 / probability for selection, probability in selections.items()}
                for market, selections in probabilities.items()
            },
            prediction_status=prediction_status,
            operationally_eligible=operationally_eligible,
            reason=reason,
            input_completeness=1,
            required_features=(
                "home_goal_rate",
                "away_goal_rate",
            ),
            missing_features=(),
            validation_sample_size=validation_sample_size,
            validation_minimum=self.settings.min_sample,
            validation_status=validation_status,
        )
        source = self.model_source(context, "poisson_probabilities")
        parents = tuple(e.evidence_id for e in context.upstream_results["football_data_analyst"].evidence)
        evidence = self.evidence(
            context,
            "Poisson score distribution from recorded pre-match attack/defense features",
            (source,),
            kind="MODEL_PROBABILITIES",
            derived_from=parents,
            markets=tuple(probabilities),
        )
        return self.output(
            context,
            PredictiveReport(
                model_predictions=(prediction,),
                ensemble=ensemble,
                score_distribution={f"{h}-{a}": p for (h, a), p in distribution.items()},
                method=(
                    "validated_single_model_ensemble_poisson"
                    if ensemble
                    else "indicative_raw_poisson_not_admitted"
                ),
                seed=context.seed,
                analysis_available=True,
                operationally_eligible=bool(ensemble),
                reason_codes=() if ensemble else (reason,),
            ),
            summary="Deterministic Poisson estimate; validation filters operational markets",
            quality=self.upstream_quality(context, "football_data_analyst"),
            provenance=(source,),
            evidence=(evidence,),
            unknowns=("Sensitivity envelope is not empirically calibrated",),
            status=AgentStatus.OK if ensemble else AgentStatus.PARTIAL,
            analysis_available=True,
            operationally_eligible=bool(ensemble),
            degradations=(
                Degradation(
                    code="INDICATIVE_PREDICTION_NOT_ADMITTED",
                    missing_checks=(
                        (f"minimum_training_sample:{size}/{self.settings.min_training_sample}",)
                        if not training_sample_ok
                        else ("independent_validation_for_requested_markets",)
                    ),
                    critical=True,
                    affected_gates=("MODEL_VALIDITY", "MARKET"),
                    impact="BLOCK",
                    reason=(
                        "Raw probabilities exist, but the observed team sample is below the operational "
                        "minimum. They are shown only as an indicative forecast."
                        if not training_sample_ok
                        else "Raw probabilities exist, but no requested market has an admitted model."
                    ),
                ),
            )
            if not ensemble
            else (),
        )
