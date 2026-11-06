from datetime import datetime, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from better.config import Settings
from better.domain.enums import AgentStatus, MarketType
from better.domain.models import DataQuality, Market, ModelPrediction, ProbabilityEstimate
from better.domain.schemas import AgentResult
from better.orchestration.dependency_graph import topological_layers
from better.orchestration.workflow import SPORTS_LAYERS


def test_quality_is_decomposable_and_round_trips():
    quality = DataQuality(
        completeness=0.9, freshness=0.8, source_reliability=0.95, cross_source_consistency=1
    )
    assert quality.score == 0.8
    assert DataQuality.model_validate_json(quality.model_dump_json()) == quality
    with pytest.raises(ValidationError):
        DataQuality(completeness=0.9, freshness=0.8, score=1)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -0.1, 1.1])
def test_invalid_probabilities(value):
    with pytest.raises(ValidationError):
        ProbabilityEstimate(point_estimate=value, lower=0, upper=1, method="test")


def test_invalid_interval_and_market():
    with pytest.raises(ValidationError):
        ProbabilityEstimate(point_estimate=0.2, lower=0.3, upper=0.5, method="test")
    with pytest.raises(ValidationError):
        Market(type=MarketType.BTTS, selection="HOME")
    with pytest.raises(ValidationError):
        Market(type=MarketType.ASIAN_HANDICAP, selection="HOME")


def test_prediction_distribution_and_timezone():
    values = dict(
        model="poisson",
        model_version="1",
        sample_size=30,
        data_cutoff=datetime.now(timezone.utc),
        probabilities={"1X2": {"HOME": 0.4, "DRAW": 0.3, "AWAY": 0.3}},
    )
    ModelPrediction(**values)
    values["probabilities"] = {"1X2": {"HOME": 0.8, "DRAW": 0.3, "AWAY": 0.3}}
    with pytest.raises(ValidationError):
        ModelPrediction(**values)
    values["data_cutoff"] = datetime(2026, 1, 1)
    with pytest.raises(ValidationError):
        ModelPrediction(**values)


def test_result_requires_typed_payload_when_successful():
    with pytest.raises(ValidationError):
        AgentResult(
            analysis_id=uuid4(),
            agent_id="test",
            status=AgentStatus.OK,
            summary="unsupported success",
            generated_at=datetime.now(timezone.utc),
        )


def test_graph_rejects_cycles_and_unknown_nodes():
    assert len(SPORTS_LAYERS) >= 5
    with pytest.raises(ValueError):
        topological_layers({"a": ("b",), "b": ("a",)})
    with pytest.raises(ValueError):
        topological_layers({"a": ("missing",)})


def test_configuration_is_validated_and_versioned():
    settings = Settings.load()
    assert settings.minimum_edge == 0.04
    assert settings.version != Settings(minimum_edge=0.05).version
    with pytest.raises(ValidationError):
        Settings(kelly_fraction=2)
