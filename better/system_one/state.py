"""Reproducible, as-of state snapshots with no future information."""

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def state_hash(state: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(dict(state)).encode("utf-8")).hexdigest()


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("State timestamps must include a timezone")
    return parsed


def validate_as_of(state: Mapping[str, Any]) -> None:
    if "as_of" not in state:
        raise ValueError("State requires an explicit as_of timestamp")
    as_of = _timestamp(state["as_of"])

    def check(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {
                    "closing_odds",
                    "closing_price",
                    "post_match_stats",
                    "final_score",
                    "match_result",
                    "outcome",
                    "settlement",
                }:
                    raise ValueError(f"Future outcome data is forbidden in replay state: {key}")
                if (
                    key
                    in {
                        "published_at",
                        "observed_at",
                        "captured_at",
                        "retrieved_at",
                        "available_at",
                        "data_cutoff",
                    }
                    and child
                    and _timestamp(child) > as_of
                ):
                    raise ValueError(f"Future data beyond as_of: {key}")
                check(child)
        elif isinstance(value, list):
            for child in value:
                check(child)

    check(dict(state))


class SystemOneStateBuilder:
    """Select normalized fields, keep evidence/dependencies, omit irrelevant narratives."""

    fields = (
        "event",
        "match",
        "fixture",
        "competition",
        "teams",
        "market",
        "markets",
        "odds",
        "form",
        "stats",
        "advanced_stats",
        "lineups",
        "injuries",
        "news",
        "news_signals",
        "data_quality",
        "deterministic_features",
        "provenance",
        "freshness",
        "completeness",
        "errors",
        "snapshot_id",
        "source_failures",
        "statistics",
        "odds_snapshots",
        "match_id",
        "config_version",
        "model_versions",
        "input_hash",
    )
    output_fields = (
        "agent_id",
        "status",
        "summary",
        "signal",
        "confidence",
        "evidence",
        "warnings",
        "provenance",
        "data_quality",
        "payload",
        "result",
        "data",
        "limitations",
        "risks",
        "unknowns",
        "errors",
    )

    def build(self, snapshot: Mapping[str, Any], outputs: Mapping[str, Any] | None = None) -> dict[str, Any]:
        raw = dict(snapshot)
        state = {"schema_version": "normalized_state_v1", "as_of": raw["as_of"]}
        state.update({key: raw[key] for key in self.fields if key in raw})
        state["units"] = {
            "odds": "decimal",
            "probability": "0_to_1",
            "freshness": "0_to_1",
            "timestamps": "ISO8601_UTC",
        }
        if outputs is not None:
            state["agent_outputs"] = {
                name: {key: output[key] for key in self.output_fields if key in output}
                for name, output in sorted(outputs.items())
            }
        # The JSON round-trip gives the observer its own deep snapshot; no alias into the pipeline.
        normalized = json.loads(canonical_json(state))
        validate_as_of(normalized)
        return normalized
