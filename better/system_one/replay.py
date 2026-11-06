"""As-of replay and task-specific evaluation; no invented calibration data or targets."""

import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from .config import SystemOneSettings
from .contracts import Contract, ShadowRecord, Stage, SystemOneProvider
from .questions import build_questions
from .shadow import ShadowCoordinator
from .state import state_hash, validate_as_of


class ReplayComparison(Contract):
    analysis_id: str
    stage: Stage
    original_model: str
    replay_model: str
    state_hash: str
    actual_action: dict[str, Any]
    original_suggestion: dict[str, Any]
    replay_suggestion: dict[str, Any]
    changed_questions: list[str]
    would_skip_valid_opportunity: bool
    error: str | None = None


class EvaluationCase(Contract):
    case_id: str
    stage: Stage
    split: Literal["CALIBRATION", "TEST"]
    state: dict[str, Any]
    expected_answers: dict[str, bool] = Field(min_length=1)
    label_source: str = Field(min_length=1)


def read_shadow_records(path: str | Path) -> list[ShadowRecord]:
    with Path(path).open(encoding="utf-8") as stream:
        return [ShadowRecord.model_validate_json(line) for line in stream if line.strip()]


def append_shadow_records(path: str | Path, records: list[ShadowRecord]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as stream:
        for record in records:
            stream.write(record.model_dump_json() + "\n")


async def replay_records(
    records: list[ShadowRecord],
    provider: SystemOneProvider,
    settings: SystemOneSettings | None = None,
) -> list[ReplayComparison]:
    settings = settings or SystemOneSettings(enabled=True, provider="fake")
    if not settings.enabled:
        raise ValueError("Replay requires System One enabled in shadow mode")
    coordinator = ShadowCoordinator(settings, provider)
    comparisons = []
    for record in records:
        validate_as_of(record.normalized_state)
        if state_hash(record.normalized_state) != record.state_hash:
            raise ValueError("Stored state hash does not match; replay refused")
        if record.question_schema_version != build_questions(record.stage).schema_version:
            raise ValueError("Unsupported historical question schema version")
        replayed = await coordinator.observe(
            record.analysis_id, record.stage, record.normalized_state, record.pipeline_actual_action
        )
        if replayed is None:
            raise ValueError("Replay stage is disabled or state is invalid")
        old, new = record.system_one_suggested_action, replayed.system_one_suggested_action
        comparisons.append(
            ReplayComparison(
                analysis_id=record.analysis_id,
                stage=record.stage,
                original_model=record.model_id,
                replay_model=replayed.model_id,
                state_hash=record.state_hash,
                actual_action=record.pipeline_actual_action,
                original_suggestion=old,
                replay_suggestion=new,
                changed_questions=sorted(
                    key for key in old.keys() | new.keys() if old.get(key) != new.get(key)
                ),
                would_skip_valid_opportunity=(
                    new.get("should_analyze") is False
                    and record.pipeline_actual_action.get("decision") == "BET"
                ),
                error=replayed.error,
            )
        )
    return comparisons


async def evaluate_dataset(
    cases: list[EvaluationCase],
    provider: SystemOneProvider,
    settings: SystemOneSettings | None = None,
) -> dict[str, Any]:
    if not cases:
        raise ValueError("A labeled task dataset is required; no synthetic calibration is assumed")
    identifiers = [case.case_id for case in cases]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("Duplicate case IDs would inflate evaluation")
    settings = settings or SystemOneSettings(enabled=True, provider="fake")
    if not settings.enabled:
        raise ValueError("Benchmark requires enabled shadow mode")
    seen_states: dict[str, str] = {}
    totals: dict[str, dict[str, int]] = defaultdict(
        lambda: {"tp": 0, "fp": 0, "tn": 0, "fn": 0, "abstentions": 0, "errors": 0}
    )
    coordinator = ShadowCoordinator(settings, provider)
    for case in cases:
        validate_as_of(case.state)
        digest = state_hash(case.state)
        if digest in seen_states and seen_states[digest] != case.split:
            raise ValueError("Calibration/test state overlap is forbidden")
        seen_states[digest] = case.split
        questions = build_questions(case.stage)
        for key in case.expected_answers:
            if key not in questions.questions or questions.questions[key].type != "boolean":
                raise ValueError("Labels must reference a boolean task question")
        record = await coordinator.observe(case.case_id, case.stage, case.state, {})
        if record is None:
            raise ValueError("Benchmark task is disabled or invalid")
        for key, expected in case.expected_answers.items():
            counts = totals[f"{case.split}:{case.stage}:{key}"]
            predicted = record.system_one_suggested_action.get(key)
            if record.error:
                counts["errors"] += 1
            if predicted is None:
                counts["abstentions"] += 1
            if expected:
                counts["tp" if predicted is True else "fn"] += 1
            elif predicted is True:
                counts["fp"] += 1
            else:
                counts["tn"] += 1
    metrics = {}
    for task, counts in totals.items():
        positives = counts["tp"] + counts["fn"]
        predicted_positives = counts["tp"] + counts["fp"]
        metrics[task] = {
            **counts,
            "recall": counts["tp"] / positives if positives else None,
            "false_negative_rate": counts["fn"] / positives if positives else None,
            "precision": counts["tp"] / predicted_positives if predicted_positives else None,
        }
    return {
        "case_count": len(cases),
        "tasks": metrics,
        "versions": sorted(
            {
                (
                    record.model_id,
                    record.model_fingerprint or "unavailable",
                    record.question_schema_version,
                    record.threshold_version,
                )
                for record in coordinator.records
            }
        ),
        "telemetry": coordinator.telemetry(),
        "active_routing_approved": False,
        "calibration_status": "MEASURED_NOT_PROMOTED",
        "interpretation": "Task labels define correctness; match outcomes alone do not. Abstention on a positive label counts as a missed positive.",
    }


def read_evaluation_cases(path: str | Path) -> list[EvaluationCase]:
    with Path(path).open(encoding="utf-8") as stream:
        return [EvaluationCase.model_validate(json.loads(line)) for line in stream if line.strip()]
