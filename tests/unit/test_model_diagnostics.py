from copy import deepcopy
from types import SimpleNamespace

from better.services.model_diagnostics import ModelDiagnostics


def observation(index, correct=True, source="LIVE_FORWARD", probability=0.6):
    return {
        "prediction_id": str(index),
        "match_id": "match-" + str(index),
        "model_name": "poisson",
        "model_version": "1.0",
        "market": "BTTS",
        "probabilities": {"YES": probability, "NO": 1 - probability},
        "actual_selection": "YES" if correct else "NO",
        "source_type": source,
        "inclusion_status": "VALIDATION_INCLUDED",
        "input_completeness": 0.85,
        "data_cutoff": "2026-01-01T08:00:00+00:00",
        "as_of": "2026-01-01T09:00:00+00:00",
        "created_at": "2026-01-01T09:00:00+00:00",
        "kickoff": "2026-01-01T10:00:00+00:00",
        "settled_at": "2026-01-01T12:00:00+00:00",
    }


def report(rows):
    return ModelDiagnostics(SimpleNamespace(all_records=lambda table: rows)).report()


def test_calibration_sixty_percent_matches_sixty_of_one_hundred():
    rows = [observation(i, correct=i < 60) for i in range(100)]
    result = report(rows)["sources"]["LIVE_FORWARD"]
    assert result["summary"]["count"] == 100
    assert abs(result["summary"]["accuracy"] - 0.6) < 1e-10
    assert abs(result["summary"]["calibration_gap"]) < 1e-10
    assert abs(result["summary"]["brier_score"] - 0.24) < 1e-10
    assert result["dimensions"]["probability_band"][0]["label"] == "60–70%"
    assert result["dimensions"]["quality"][0]["label"] == "Completezza input: 75–100%"


def test_duplicate_predictions_do_not_multiply_and_history_stays_separate():
    row = observation(1)
    original = deepcopy(row)
    data = [row, deepcopy(row), observation(1, source="HISTORICAL_BACKTEST")]
    result = report(data)
    assert result["duplicates_ignored"] == 1
    assert result["sources"]["LIVE_FORWARD"]["summary"]["count"] == 1
    assert result["sources"]["HISTORICAL_BACKTEST"]["summary"]["count"] == 1
    assert row == original


def test_leakage_invalid_probabilities_and_unfinished_observations_are_excluded():
    late = observation(1)
    late["created_at"] = late["kickoff"]
    invalid = observation(2)
    invalid["probabilities"]["YES"] = float("nan")
    unfinished = observation(3)
    unfinished["inclusion_status"] = "RESULT_PENDING"
    result = report([late, invalid, unfinished])
    assert result["excluded"] == 3 and result["sources"] == {}


def test_quality_does_not_get_confused_with_completeness_and_versions_stay_separate():
    first = observation(1)
    first["data_quality"] = {"score": 0.5}
    second = observation(2)
    second["model_version"] = "2.0"
    second.pop("input_completeness")
    result = report([first, second])["sources"]["LIVE_FORWARD"]["dimensions"]
    assert len(result["version"]) == 2
    assert {r["label"] for r in result["quality"]} == {"Qualità dati: 50–75%", "Non registrata"}
