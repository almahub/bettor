"""Offline HTTP integration: failures never escape the shadow observer."""

import copy

import httpx
import pytest

from better.system_one import (
    FakeSystemOneProvider,
    RizzoFlowSystemOneProvider,
    ShadowCoordinator,
    SystemOneSettings,
)

STATE = {
    "schema_version": "normalized_state_v1",
    "as_of": "2026-09-20T12:00:00Z",
    "match": {"match_id": "synthetic-test", "kickoff": "2026-09-20T18:00:00Z"},
    "odds": {"captured_at": "2026-09-20T11:59:00Z", "decimal": 2.0},
}


@pytest.mark.parametrize("failure", ["connection", "timeout", "malformed", "unloaded_model", "unauthorized"])
async def test_rizzo_failure_records_diagnostic_and_preserves_actual_pipeline(failure):
    def handler(request):
        if failure == "connection":
            raise httpx.ConnectError("unavailable", request=request)
        if failure == "timeout":
            raise httpx.ReadTimeout("timeout", request=request)
        if failure == "malformed":
            return httpx.Response(200, json={"not_answers": True})
        if failure == "unauthorized":
            return httpx.Response(401)
        return httpx.Response(503)

    settings = SystemOneSettings(
        enabled=True, rizzo_flow_model="example-pinned-model", rizzo_flow_max_retries=0
    )
    state = copy.deepcopy(STATE)
    actual = {"agents": ["football_data_analyst"], "decision": "NO_BET", "stake": 0}
    baseline = copy.deepcopy(actual)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        observer = ShadowCoordinator(settings, RizzoFlowSystemOneProvider(settings, client=client))
        record = await observer.observe("analysis-a", "pre_screen", state, actual)
    assert record.error
    assert record.pipeline_actual_action == baseline == actual
    assert record.system_one_suggested_action == {}
    assert state == STATE
    assert observer.health()["status"] == "degraded"


async def test_circuit_recovers_after_cooldown_without_resetting_pipeline():
    fake = FakeSystemOneProvider(mode="error")
    settings = SystemOneSettings(enabled=True, provider="fake", circuit_failure_threshold=1)
    observer = ShadowCoordinator(settings, fake)
    assert (await observer.observe("a", "pre_screen", STATE, {})).error
    fake.mode = "ok"
    observer._opened_at -= settings.circuit_reset_seconds + 1
    assert (await observer.observe("b", "pre_screen", STATE, {})).error is None
    assert observer.health()["status"] == "healthy"


async def test_stage_suggestions_remain_observations_and_telemetry_is_honest():
    fake = FakeSystemOneProvider(
        values={
            "should_analyze": False,
            "needs_stats_agent": False,
            "consistency_action": "REANALYSE",
            "requires_deep_reasoning": True,
        }
    )
    observer = ShadowCoordinator(SystemOneSettings(enabled=True, provider="fake"), fake)
    actual = {"agents": ["football_data_analyst"], "decision": "BET", "system2_calls": 0}
    for stage in ("pre_screen", "routing", "consistency", "escalation"):
        record = await observer.observe("a", stage, STATE, actual)
        assert record.pipeline_actual_action == actual
        assert record.error is None
    assert actual == {"agents": ["football_data_analyst"], "decision": "BET", "system2_calls": 0}
    telemetry = observer.telemetry()
    assert telemetry["potential_false_negative_opportunities"] == 1
    assert telemetry["agents_would_skip"] == 1
    assert telemetry["outputs_reanalysed"] == 0
    assert telemetry["actual_system2_calls"] == 0
    assert telemetry["potential_cost_saved"] is None
    assert telemetry["missed_escalations"] is None


async def test_kill_switch_provider_and_secrets_not_exposed():
    fake = FakeSystemOneProvider(mode="error")
    settings = SystemOneSettings(rizzo_flow_api_key="synthetic-secret")
    observer = ShadowCoordinator(settings, fake)
    for stage in ("pre_screen", "routing", "consistency", "escalation"):
        assert await observer.observe("a", stage, {"invalid": object()}, {}) is None
    assert fake.calls == 0
    assert observer.records == []
    assert "synthetic-secret" not in str(observer.health())
    assert "synthetic-secret" not in str(settings)
