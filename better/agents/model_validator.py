from collections import defaultdict
from decimal import Decimal

from better.domain.enums import AgentStatus
from better.domain.models import Degradation, ModelValidation
from better.domain.schemas import ValidationReport
from better.system_zero.validation import (
    closing_line_value,
    performance,
    validate_model,
    validate_multiclass_model,
)

from .base import BaseAgent


class ModelValidator(BaseAgent):
    agent_id = "model_validator"
    allowed_tools = frozenset({"validation_store", "validation_metrics"})
    forbidden_tools = BaseAgent.forbidden_tools | {"current_model_prediction"}
    needs_history = True

    async def _run(self, context):
        groups = defaultdict(list)
        for entry in context.validation_history:
            if (
                entry.settled_at <= context.as_of
                and entry.market in context.requested_markets
                and entry.source_type == "LIVE_FORWARD"
            ):
                groups[
                    (
                        entry.model_id,
                        entry.model_version,
                        entry.market,
                        entry.feature_schema_version,
                        entry.config_hash,
                    )
                ].append(entry)
        validations = []
        for (model, version, market, _feature_version, _config_hash), entries in sorted(groups.items()):
            entries.sort(key=lambda entry: entry.settled_at)
            if all(e.probabilities and e.actual_selection for e in entries):
                result = validate_multiclass_model(
                    [e.probabilities for e in entries],
                    [e.actual_selection for e in entries],
                    min_sample=self.settings.min_sample,
                    max_brier=self.settings.max_brier,
                    max_calibration_error=self.settings.suspend_calibration_error,
                    degraded_calibration_error=self.settings.max_calibration_error,
                )
            else:
                result = validate_model(
                    [e.probability for e in entries],
                    [e.outcome for e in entries],
                    min_sample=self.settings.min_sample,
                    max_brier=self.settings.max_brier,
                    max_calibration_error=self.settings.suspend_calibration_error,
                    degraded_calibration_error=self.settings.max_calibration_error,
                )
            # Sum of observed stakes supplies a sufficient arithmetic capital bound only.
            # Actual historical bankroll is absent, so do not publish ROI or bankroll drawdown.
            metrics = performance(
                [e.profit for e in entries],
                [e.stake for e in entries],
                sum((e.stake for e in entries), Decimal(0)),
            )
            metrics.pop("roi")
            metrics.pop("max_drawdown")
            metrics = {key: float(value) for key, value in metrics.items()}
            closing = [
                closing_line_value(e.odds, e.closing_odds) for e in entries if e.closing_odds is not None
            ]
            if closing:
                metrics["mean_clv"] = sum(closing) / len(closing)
            validations.append(
                ModelValidation(
                    model_id=model,
                    model_version=version,
                    market=market,
                    validation_status=result.status,
                    recommended_weight_multiplier=result.recommended_weight_multiplier,
                    sample_size=result.sample_size,
                    brier_score=result.brier,
                    log_loss=result.log_loss,
                    calibration_error=result.calibration_error,
                    reason=result.reason,
                    metrics=metrics,
                )
            )
        if not validations:
            return self.missing(context, "No settled out-of-sample history available at as_of")
        source = self.model_source(context, "out_of_sample_validation")
        evidence = self.evidence(
            context,
            "Independent historical calibration and risk metrics; no current prediction input",
            (source,),
            kind="MODEL_VALIDATION",
        )
        return self.output(
            context,
            ValidationReport(validations=tuple(validations)),
            summary="Independent out-of-sample model validation",
            provenance=(source,),
            evidence=(evidence,),
            status=AgentStatus.PARTIAL,
            degradations=(
                Degradation(
                    code="HISTORICAL_BANKROLL_UNAVAILABLE",
                    missing_checks=("bankroll_roi", "bankroll_drawdown"),
                    critical=False,
                    affected_gates=("MODEL_VALIDITY",),
                    impact="WARNING_ONLY",
                    reason="Historical starting bankroll is absent. Bankroll ROI/drawdown are not reported "
                    "or used for model admission; sample size, Brier, log loss and calibration are evaluated "
                    "on settled out-of-sample records for this competition. No extra confidence penalty.",
                ),
            ),
            unknowns=("Historical initial bankroll unavailable: bankroll ROI and drawdown are not reported",),
        )
