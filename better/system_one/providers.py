"""HTTP adapter for the verified native Rizzo contract, plus a fully offline fake."""

import asyncio
from collections.abc import Mapping
from time import perf_counter
from typing import Any

import httpx
from pydantic import ValidationError

from .config import SystemOneSettings
from .contracts import (
    Question,
    QuestionBatch,
    SemanticAnswer,
    SemanticStatus,
    SystemOneError,
    SystemOneResult,
)


def _validate_values(answers: Mapping[str, SemanticAnswer], questions: QuestionBatch) -> None:
    if set(answers) != set(questions.questions):
        raise SystemOneError("RESPONSE_QUESTION_MISMATCH")
    for name, question in questions.questions.items():
        answer = answers[name]
        if answer.type != question.type:
            raise SystemOneError("RESPONSE_TYPE_MISMATCH")
        if answer.status != SemanticStatus.OK:
            continue
        if question.type == "choice" and answer.value not in question.options:
            raise SystemOneError("RESPONSE_CHOICE_OUT_OF_RANGE")
        if question.type == "score" and not 0 <= answer.value <= len(question.levels) - 1:
            raise SystemOneError("RESPONSE_SCORE_OUT_OF_RANGE")
        if question.type == "numeric" and not min(question.anchors) <= answer.value <= max(question.anchors):
            raise SystemOneError("RESPONSE_NUMERIC_OUT_OF_RANGE")


class FakeSystemOneProvider:
    """Synthetic semantic responses, clearly identified and never sports probabilities."""

    def __init__(
        self,
        *,
        mode: str = "ok",
        values: Mapping[str, bool | str | float] | None = None,
        model_id: str = "fake-system-one-v1",
        delay_seconds: float = 60,
    ):
        self.mode = mode
        self.values = dict(values or {})
        self.model_id = model_id
        self.delay_seconds = delay_seconds
        self.calls = 0

    async def evaluate(
        self,
        *,
        state: dict[str, Any],
        questions: QuestionBatch,
        model: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> SystemOneResult:
        self.calls += 1
        if self.mode == "error":
            raise SystemOneError("FAKE_PROVIDER_ERROR", transient=True)
        if self.mode == "timeout":
            await asyncio.sleep(self.delay_seconds)
        if self.mode == "malformed":
            raise SystemOneError("MALFORMED_RESPONSE")
        status = {
            "abstain": SemanticStatus.INSUFFICIENT_EVIDENCE,
            "uncertain": SemanticStatus.UNCERTAIN,
            "out_of_range": SemanticStatus.OUT_OF_RANGE,
        }.get(self.mode, SemanticStatus.OK)
        answers = {}
        for name, question in questions.questions.items():
            defaults = {
                "boolean": True,
                "choice": next(iter(question.options), ""),
                "score": float(max(0, len(question.levels) - 1)),
                "numeric": min(question.anchors, default=0.0),
            }
            value = self.values.get(name, defaults[question.type]) if status == SemanticStatus.OK else None
            answers[name] = SemanticAnswer(
                type=question.type,
                status=status,
                value=value,
                confidence=1.0 if value is not None else None,
                probability_status="synthetic_fake_not_calibrated",
            )
        _validate_values(answers, questions)
        return SystemOneResult(
            provider="fake",
            model_id=self.model_id,
            model_fingerprint="fake-deterministic-v1",
            quantization="not_applicable",
            answers=answers,
        )


class RizzoFlowSystemOneProvider:
    """Native /v1/decisions. The served model is pinned by response metadata.

    The native request deliberately has no model field: the current native API serves
    one resident model and rejects unknown request fields. API evolution is isolated here.
    """

    def __init__(self, settings: SystemOneSettings, *, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self._client = client

    @staticmethod
    def _question(question: Question) -> dict[str, Any]:
        result = {
            "type": question.type,
            "instructions": question.instructions,
            "policy": {"allow_abstain": question.allow_abstain},
        }
        if question.type == "choice":
            result["options"] = [{"id": key, "description": value} for key, value in question.options.items()]
        elif question.type == "score":
            result["levels"] = question.levels
        elif question.type == "numeric":
            result["unit"] = question.unit
            result["anchors"] = [
                {"value": value, "description": description}
                for value, description in sorted(question.anchors.items())
            ]
        return result

    async def evaluate(
        self,
        *,
        state: dict[str, Any],
        questions: QuestionBatch,
        model: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> SystemOneResult:
        expected_model = model or self.settings.rizzo_flow_model
        if not expected_model or expected_model.endswith("latest"):
            raise SystemOneError("EXPLICIT_MODEL_ID_REQUIRED")
        payload = {
            "state": state,
            "questions": {name: self._question(question) for name, question in questions.questions.items()},
            "mode": "shared",
        }
        if self._client is not None:
            return await self._evaluate(self._client, payload, questions, expected_model)
        async with httpx.AsyncClient(follow_redirects=False) as client:
            return await self._evaluate(client, payload, questions, expected_model)

    async def _evaluate(
        self,
        client: httpx.AsyncClient,
        payload: dict,
        questions: QuestionBatch,
        expected_model: str,
    ) -> SystemOneResult:
        started = perf_counter()
        headers = {"Content-Type": "application/json"}
        secret = self.settings.rizzo_flow_api_key.get_secret_value()
        if secret:
            headers["Authorization"] = f"Bearer {secret}"
        for attempt in range(self.settings.rizzo_flow_max_retries + 1):
            try:
                response = await client.post(
                    self.settings.rizzo_flow_base_url.rstrip("/") + "/v1/decisions",
                    json=payload,
                    headers=headers,
                    timeout=self.settings.rizzo_flow_timeout_ms / 1000,
                )
                if response.status_code in (408, 429) or response.status_code >= 500:
                    raise SystemOneError("TRANSIENT_HTTP_ERROR", transient=True)
                if not 200 <= response.status_code < 300:
                    raise SystemOneError(f"PROVIDER_HTTP_{response.status_code}")
                result = self.parse_response(response.json(), questions, expected_model)
                return result.model_copy(update={"latency_ms": (perf_counter() - started) * 1000})
            except httpx.TimeoutException as exc:
                error = SystemOneError("PROVIDER_TIMEOUT", transient=True)
                error.__cause__ = exc
            except httpx.TransportError as exc:
                error = SystemOneError("PROVIDER_UNAVAILABLE", transient=True)
                error.__cause__ = exc
            except SystemOneError as exc:
                error = exc
            except (ValueError, TypeError, KeyError, ValidationError) as exc:
                raise SystemOneError("MALFORMED_RESPONSE") from exc
            if not error.transient or attempt == self.settings.rizzo_flow_max_retries:
                raise error
            await asyncio.sleep(min(0.05 * (2**attempt), 0.5))
        raise SystemOneError("PROVIDER_ERROR")  # defensive: retries are nonnegative

    def parse_response(self, payload: dict, questions: QuestionBatch, expected_model: str) -> SystemOneResult:
        try:
            metadata = payload["model"]
            if not isinstance(metadata, dict):
                raise TypeError("Native model metadata must be an object")
            source, precision, fingerprint = (
                metadata["source"],
                metadata["precision"],
                metadata["fingerprint"],
            )
            if not all(isinstance(value, str) and value for value in (source, precision, fingerprint)):
                raise ValueError("Missing model identity")
            checkpoint = source.rsplit("/", 1)[-1].lower()
            model_id = (
                f"rizzo-flow-{checkpoint.rsplit('-', 1)[-1]}-{precision}"
                if metadata.get("weights") == "flow"
                else f"rizzo-{checkpoint}-{precision}"
            )
            if expected_model != model_id:
                raise SystemOneError("MODEL_MISMATCH")
            if (
                self.settings.rizzo_flow_model_fingerprint
                and fingerprint != self.settings.rizzo_flow_model_fingerprint
            ):
                raise SystemOneError("MODEL_FINGERPRINT_MISMATCH")
            if payload["mode"] not in ("shared", "direct"):
                raise ValueError("Invalid execution mode")
            native_answers = payload["answers"]
            if set(native_answers) != set(questions.questions):
                raise SystemOneError("RESPONSE_QUESTION_MISMATCH")
            answers = {}
            logical_tokens = 0
            for name, question in questions.questions.items():
                native = native_answers[name]
                status = SemanticStatus(native["status"].upper())
                value_field = {"choice": "choice", "score": "score"}.get(question.type, "value")
                value = native[value_field]
                if question.type == "boolean" and value is not None and not isinstance(value, bool):
                    raise ValueError("Invalid native boolean")
                if (
                    question.type in ("score", "numeric")
                    and value is not None
                    and (isinstance(value, bool) or not isinstance(value, (int, float)))
                ):
                    raise ValueError("Invalid native numeric value")
                if question.type == "numeric" and native["unit"] != question.unit:
                    raise SystemOneError("RESPONSE_UNIT_MISMATCH")
                answers[name] = SemanticAnswer(
                    type=native["type"],
                    status=status,
                    value=value,
                    confidence=native["uncertainty"]["top_probability"],
                    probabilities=native["probabilities"],
                    probability_status=native["probability_status"],
                    prompt_hash=native["prompt_sha256"],
                )
                input_tokens = native["input_tokens"]
                if isinstance(input_tokens, bool) or not isinstance(input_tokens, int) or input_tokens < 0:
                    raise ValueError("Invalid token count")
                logical_tokens += input_tokens
            _validate_values(answers, questions)
            shared = payload["timing"].get("shared_prefix_tokens", 0)
            input_tokens = logical_tokens - shared * (len(native_answers) - 1)
            output_tokens = payload["timing"].get("generated_tokens", 0)
            return SystemOneResult(
                provider="rizzo_flow",
                model_id=model_id,
                model_fingerprint=fingerprint,
                quantization=precision,
                answers=answers,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
        except SystemOneError:
            raise
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise SystemOneError("MALFORMED_RESPONSE") from exc
