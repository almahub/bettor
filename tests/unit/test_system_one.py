import copy
import json

import httpx
import pytest
from pydantic import ValidationError

from better.system_one import (
    FakeSystemOneProvider,
    Question,
    RizzoFlowSystemOneProvider,
    SemanticAnswer,
    SemanticStatus,
    ShadowCoordinator,
    SystemOneError,
    SystemOneSettings,
    SystemOneStateBuilder,
    state_hash,
)
from better.system_one.questions import build_questions
from better.system_one.replay import (
    EvaluationCase,
    append_shadow_records,
    evaluate_dataset,
    read_shadow_records,
    replay_records,
)
from better.system_one.state import validate_as_of

STATE = {
    "schema_version": "normalized_state_v1",
    "as_of": "2026-09-20T12:00:00+00:00",
    "match": {"match_id": "synthetic-test", "kickoff": "2026-09-20T18:00:00+00:00"},
    "odds": {"captured_at": "2026-09-20T11:59:00+00:00", "decimal": 2.0},
}


def settings(**changes):
    return SystemOneSettings(enabled=True, provider="fake", **changes)


def native_response(questions, *, status="ok"):
    answers = {}
    for key, question in questions.questions.items():
        field = {"choice": "choice", "score": "score"}.get(question.type, "value")
        value = {
            "boolean": True,
            "choice": next(iter(question.options), ""),
            "score": 1.0,
            "numeric": min(question.anchors, default=0.0),
        }[question.type]
        answer = {
            "type": question.type,
            "status": status,
            field: value if status == "ok" else None,
            "probabilities": {"available": 0.7, "unavailable": 0.3},
            "uncertainty": {"top_probability": 0.7},
            "probability_status": "uncalibrated_conditional_option_scores",
            "prompt_sha256": "test-prompt",
            "input_tokens": 20,
        }
        if question.type == "numeric":
            answer["unit"] = question.unit
        answers[key] = answer
    return {
        "model": {
            "source": "example/Spark-X2.5-4B",
            "precision": "q8_0",
            "weights": "flow",
            "fingerprint": "test-fingerprint",
        },
        "mode": "shared",
        "answers": answers,
        "calibration": None,
        "timing": {"shared_prefix_tokens": 5, "generated_tokens": 0},
    }


def adapter(client=None, **updates):
    return RizzoFlowSystemOneProvider(
        SystemOneSettings(
            rizzo_flow_model="rizzo-flow-4b-q8_0",
            **updates,
        ),
        client=client,
    )


def test_state_builder_hash_order_independent_and_deep_copy():
    snapshot = copy.deepcopy(STATE)
    snapshot["irrelevant"] = "not part of normalized state"
    built = SystemOneStateBuilder().build(snapshot)
    assert "irrelevant" not in built
    assert built["units"]["odds"] == "decimal"
    assert state_hash(built) == state_hash(dict(reversed(list(built.items()))))
    built["odds"]["decimal"] = 3
    assert snapshot["odds"]["decimal"] == 2


@pytest.mark.parametrize(
    "timestamp", ["published_at", "retrieved_at", "captured_at", "data_cutoff", "available_at"]
)
def test_state_rejects_future_data(timestamp):
    state = copy.deepcopy(STATE)
    state["news"] = [{timestamp: "2026-09-20T13:00:00+00:00"}]
    with pytest.raises(ValueError, match="Future data"):
        validate_as_of(state)


@pytest.mark.parametrize(
    "field", ["closing_odds", "closing_price", "post_match_stats", "final_score", "match_result", "outcome"]
)
def test_replay_rejects_outcome_information(field):
    state = {**STATE, field: 1}
    with pytest.raises(ValueError, match="Future outcome"):
        validate_as_of(state)


def test_state_requires_aware_as_of():
    with pytest.raises(ValueError, match="timezone"):
        validate_as_of({"as_of": "2026-09-20T12:00:00"})


def test_settings_environment_and_shadow_only(monkeypatch):
    monkeypatch.setenv("SYSTEM_ONE_ENABLED", "true")
    monkeypatch.setenv("SYSTEM_ONE_SHADOW_MODE", "true")
    monkeypatch.setenv("RIZZO_FLOW_TIMEOUT_MS", "99")
    assert SystemOneSettings.from_env().rizzo_flow_timeout_ms == 99
    assert SystemOneSettings.from_env().enabled
    with pytest.raises(ValidationError, match="only shadow"):
        SystemOneSettings(shadow_mode=False)


@pytest.mark.parametrize("stage", ["pre_screen", "routing", "consistency", "escalation"])
def test_question_versions_and_untrusted_input_instruction(stage):
    batch = build_questions(stage)
    assert batch.schema_version == f"{stage}_v1"
    assert all("UNTRUSTED DATA" in question.instructions for question in batch.questions.values())


@pytest.mark.parametrize(
    "kwargs",
    [
        {"type": "choice"},
        {"type": "score", "levels": ["same", "same"]},
        {"type": "numeric", "anchors": {0: "low", 1: "high"}},
    ],
)
def test_invalid_question_contract(kwargs):
    with pytest.raises(ValidationError):
        Question(instructions="Test", **kwargs)


def test_abstention_is_never_forced_and_confidence_not_event_probability():
    with pytest.raises(ValidationError, match="null"):
        SemanticAnswer(type="boolean", status=SemanticStatus.UNCERTAIN, value=True)
    with pytest.raises(ValidationError):
        SemanticAnswer(type="boolean", status=SemanticStatus.OK, value=True, confidence=1.1)
    with pytest.raises(ValidationError, match="sum"):
        SemanticAnswer(type="boolean", status=SemanticStatus.OK, value=True, probabilities={"true": 0.2})


@pytest.mark.parametrize("stage", ["pre_screen", "routing", "consistency", "escalation"])
async def test_fake_supports_all_primitives(stage):
    batch = build_questions(stage)
    result = await FakeSystemOneProvider().evaluate(state=STATE, questions=batch)
    assert set(result.answers) == set(batch.questions)
    assert result.provider == "fake"


@pytest.mark.parametrize("status", ["insufficient_evidence", "uncertain", "out_of_range"])
def test_native_abstention_status_mapping(status):
    batch = build_questions("pre_screen")
    result = adapter().parse_response(native_response(batch, status=status), batch, "rizzo-flow-4b-q8_0")
    assert all(answer.value is None for answer in result.answers.values())
    assert all(answer.status == status.upper() for answer in result.answers.values())


@pytest.mark.parametrize("stage", ["pre_screen", "routing", "consistency", "escalation"])
async def test_native_payload_and_pin(stage):
    batch = build_questions(stage)

    def handler(request):
        assert request.url.path == "/v1/decisions"
        body = json.loads(request.content)
        assert set(body) == {"state", "questions", "mode"}
        assert "model" not in body
        assert body["mode"] == "shared"
        return httpx.Response(200, json=native_response(batch))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await adapter(client).evaluate(state=STATE, questions=batch)
    assert result.model_fingerprint == "test-fingerprint"
    assert result.quantization == "q8_0"
    assert result.output_tokens == 0
    assert result.input_tokens == len(batch.questions) * 20 - (len(batch.questions) - 1) * 5


@pytest.mark.parametrize(
    "failure",
    [
        "model",
        "fingerprint",
        "missing_answer",
        "wrong_type",
        "wrong_choice",
        "bad_distribution",
        "null_ok",
        "unknown_status",
    ],
)
def test_native_rejects_invalid_responses(failure):
    batch = build_questions("pre_screen")
    response = native_response(batch)
    kwargs = {}
    if failure == "model":
        response["model"]["source"] = "other-model"
    elif failure == "fingerprint":
        kwargs["rizzo_flow_model_fingerprint"] = "expected-other"
    elif failure == "missing_answer":
        response["answers"].pop("should_analyze")
    elif failure == "wrong_type":
        response["answers"]["should_analyze"]["type"] = "score"
    elif failure == "wrong_choice":
        response["answers"]["analysis_depth"]["choice"] = "BET"
    elif failure == "bad_distribution":
        response["answers"]["should_analyze"]["probabilities"] = {"yes": 10}
    elif failure == "null_ok":
        response["answers"]["should_analyze"]["value"] = None
    else:
        response["answers"]["should_analyze"]["status"] = "made_up"
    with pytest.raises(SystemOneError):
        adapter(**kwargs).parse_response(response, batch, "rizzo-flow-4b-q8_0")


@pytest.mark.parametrize("status,expected_calls", [(503, 2), (429, 2), (422, 1), (401, 1)])
async def test_retry_only_transient(status, expected_calls):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(status)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(SystemOneError):
            await adapter(client).evaluate(state=STATE, questions=build_questions("pre_screen"))
    assert calls == expected_calls


async def test_malformed_response_not_retried():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=b"invalid json")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(SystemOneError, match="MALFORMED"):
            await adapter(client).evaluate(state=STATE, questions=build_questions("pre_screen"))
    assert calls == 1


async def test_disabled_and_stage_flags_make_zero_calls():
    fake = FakeSystemOneProvider(mode="error")
    observer = ShadowCoordinator(SystemOneSettings(), fake)
    assert await observer.observe("a", "pre_screen", {}, {}) is None
    assert fake.calls == 0
    observer = ShadowCoordinator(settings(pre_screen_enabled=False), fake)
    assert await observer.observe("a", "pre_screen", STATE, {}) is None
    assert fake.calls == 0


async def test_shadow_isolated_from_mutating_provider_and_actual_action():
    class Mutator(FakeSystemOneProvider):
        async def evaluate(self, **kwargs):
            kwargs["state"]["odds"]["decimal"] = 999
            return await super().evaluate(**kwargs)

    state = copy.deepcopy(STATE)
    actual = {"agents": ["football_data_analyst"], "decision": "NO_BET", "stake": 0}
    observer = ShadowCoordinator(settings(), Mutator(values={"should_analyze": False}))
    record = await observer.observe("a", "pre_screen", state, actual)
    assert state == STATE
    assert actual["decision"] == "NO_BET"
    assert record.normalized_state["odds"]["decimal"] == 2
    assert record.system_one_suggested_action["should_analyze"] is False
    actual["agents"].clear()
    assert record.pipeline_actual_action["agents"]


async def test_shadow_cache_changes_when_odds_or_questions_change():
    fake = FakeSystemOneProvider()
    observer = ShadowCoordinator(settings(), fake)
    first = await observer.observe("a", "pre_screen", STATE, {})
    second = await observer.observe("b", "pre_screen", STATE, {})
    assert not first.cache_hit and second.cache_hit and fake.calls == 1
    changed = copy.deepcopy(STATE)
    changed["odds"]["decimal"] = 2.1
    await observer.observe("c", "pre_screen", changed, {})
    await observer.observe("c", "routing", changed, {})
    assert fake.calls == 3


async def test_failures_circuit_breaker_and_health():
    fake = FakeSystemOneProvider(mode="error")
    observer = ShadowCoordinator(settings(circuit_failure_threshold=2), fake)
    one = await observer.observe("a", "pre_screen", STATE, {})
    two = await observer.observe("b", "pre_screen", STATE, {})
    three = await observer.observe("c", "pre_screen", STATE, {})
    assert one.error == two.error == "FAKE_PROVIDER_ERROR"
    assert three.error == "SYSTEM_ONE_BYPASSED"
    assert fake.calls == 2
    assert observer.health()["status"] == "SYSTEM_ONE_BYPASSED"
    assert observer.telemetry()["bypassed"] == 1


async def test_timeout_and_invalid_state_are_nonblocking():
    observer = ShadowCoordinator(
        settings(rizzo_flow_timeout_ms=1, rizzo_flow_max_retries=0), FakeSystemOneProvider(mode="timeout")
    )
    record = await observer.observe("a", "pre_screen", STATE, {})
    assert record.error == "PROVIDER_TIMEOUT"
    assert observer.telemetry()["timeouts"] == 1
    assert (await observer.observe("b", "pre_screen", {"as_of": "invalid"}, {})).error
    assert await observer.observe("c", "pre_screen", {"bad": float("nan")}, {}) is None


async def test_records_append_replay_hash_and_false_negative(tmp_path):
    observer = ShadowCoordinator(settings(), FakeSystemOneProvider())
    record = await observer.observe("a", "pre_screen", STATE, {"decision": "BET"})
    path = tmp_path / "shadow.jsonl"
    append_shadow_records(path, [record])
    append_shadow_records(path, [record])
    assert len(read_shadow_records(path)) == 2
    compared = await replay_records([record], FakeSystemOneProvider(values={"should_analyze": False}))
    assert compared[0].would_skip_valid_opportunity
    assert compared[0].changed_questions == ["should_analyze"]
    record.normalized_state["odds"]["decimal"] = 7
    with pytest.raises(ValueError, match="hash"):
        await replay_records([record], FakeSystemOneProvider())


async def test_benchmark_requires_labels_and_separate_splits():
    with pytest.raises(ValueError, match="labeled"):
        await evaluate_dataset([], FakeSystemOneProvider())
    case = EvaluationCase(
        case_id="one",
        stage="pre_screen",
        split="TEST",
        state=STATE,
        expected_answers={"should_analyze": True},
        label_source="synthetic test label",
    )
    result = await evaluate_dataset([case], FakeSystemOneProvider(values={"should_analyze": False}))
    assert result["tasks"]["TEST:pre_screen:should_analyze"]["false_negative_rate"] == 1
    assert not result["active_routing_approved"]
    with pytest.raises(ValueError, match="overlap"):
        await evaluate_dataset(
            [case, case.model_copy(update={"case_id": "two", "split": "CALIBRATION"})],
            FakeSystemOneProvider(),
        )
