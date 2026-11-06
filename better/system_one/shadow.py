"""A one-way observer: semantic suggestions cannot write back into the pipeline."""

import asyncio
import json
from collections import Counter, OrderedDict
from copy import deepcopy
from time import monotonic, perf_counter
from typing import Any

from .config import SystemOneSettings
from .contracts import SemanticStatus, ShadowRecord, SystemOneError, SystemOneProvider, SystemOneResult
from .providers import FakeSystemOneProvider, RizzoFlowSystemOneProvider
from .questions import ROUTING_AGENT_IDS, build_questions
from .state import canonical_json, state_hash, validate_as_of


class ShadowCoordinator:
    def __init__(self, settings: SystemOneSettings, provider: SystemOneProvider | None = None):
        self.settings = settings
        self.provider = provider
        if self.provider is None and settings.enabled:
            if settings.provider == "rizzo_flow":
                self.provider = RizzoFlowSystemOneProvider(settings)
            elif settings.provider == "fake":
                self.provider = FakeSystemOneProvider()
        self.records: list[ShadowRecord] = []
        self._counters: Counter = Counter()
        self._cache: OrderedDict[tuple, tuple[float, SystemOneResult]] = OrderedDict()
        self._consecutive_failures = 0
        self._opened_at: float | None = None
        self._last_error: str | None = None

    async def observe(
        self,
        analysis_id: str,
        stage: str,
        state: dict[str, Any],
        pipeline_actual_action: dict[str, Any],
    ) -> ShadowRecord | None:
        """Record only. Errors, invalid provider output and timeouts never escape."""
        if not self.settings.enabled:
            return None
        if stage not in ("pre_screen", "routing", "consistency", "escalation"):
            self._counters["observer_errors"] += 1
            return None
        if not getattr(self.settings, f"{stage}_enabled"):
            return None
        try:
            isolated = json.loads(canonical_json(state))
            actual = json.loads(canonical_json(pipeline_actual_action))
            record = ShadowRecord(
                analysis_id=str(analysis_id),
                stage=stage,
                system_one_provider=self.settings.provider,
                model_id=self.settings.rizzo_flow_model or getattr(self.provider, "model_id", "unconfigured"),
                state_hash=state_hash(isolated),
                normalized_state=isolated,
                state_schema_version=isolated.get("schema_version", "normalized_state_v1"),
                question_schema_version=f"{stage}_v1",
                threshold_version=self.settings.threshold_version,
                pipeline_actual_action=actual,
            )
        except Exception:  # noqa: BLE001 - optional observer input must never break the real pipeline
            # Even malformed caller input must not make an optional observer block analysis.
            self._counters["observer_errors"] += 1
            self._last_error = "INVALID_OBSERVER_INPUT"
            return None
        started = perf_counter()
        try:
            validate_as_of(isolated)
            questions = build_questions(stage)
            cache_key = (
                record.state_hash,
                questions.schema_version,
                state_hash(questions.model_dump(mode="json")),
                self.settings.provider,
                record.model_id,
                self.settings.rizzo_flow_model_fingerprint,
                self.settings.threshold_version,
            )
            cached = self._cache.get(cache_key)
            if cached and monotonic() - cached[0] < self.settings.cache_ttl_seconds:
                result = cached[1].model_copy(deep=True)
                record.cache_hit = True
                self._counters["cache_hits"] += 1
            else:
                if self._opened_at is not None:
                    if monotonic() - self._opened_at < self.settings.circuit_reset_seconds:
                        raise SystemOneError("SYSTEM_ONE_BYPASSED")
                    self._opened_at = None
                    self._consecutive_failures = 0
                if self.provider is None:
                    raise SystemOneError("PROVIDER_NOT_CONFIGURED")
                self._counters["calls"] += 1
                timeout = (
                    self.settings.rizzo_flow_timeout_ms / 1000 * (self.settings.rizzo_flow_max_retries + 1)
                    + 0.1
                )
                async with asyncio.timeout(timeout):
                    result = await self.provider.evaluate(
                        state=deepcopy(isolated),
                        questions=questions,
                        model=self.settings.rizzo_flow_model or None,
                        context={"analysis_id": str(analysis_id), "stage": stage},
                    )
                # Revalidate provider objects too: injected adapters must obey the same contract.
                result = SystemOneResult.model_validate(result.model_dump())
                from .providers import _validate_values

                _validate_values(result.answers, questions)
                self._consecutive_failures = 0
                self._last_error = None
                if self.settings.cache_ttl_seconds:
                    self._cache[cache_key] = (monotonic(), result.model_copy(deep=True))
                    while len(self._cache) > self.settings.cache_max_entries:
                        self._cache.popitem(last=False)
            record.system_one_provider = result.provider
            record.model_id = result.model_id
            record.model_fingerprint = result.model_fingerprint
            record.quantization = result.quantization
            record.decisions = result.answers
            record.input_tokens = 0 if record.cache_hit else result.input_tokens
            record.output_tokens = 0 if record.cache_hit else result.output_tokens
            record.system_one_suggested_action = {
                name: answer.value
                for name, answer in result.answers.items()
                if answer.status == SemanticStatus.OK
            }
            self._count_result(record)
        except Exception as exc:  # noqa: BLE001 - provider plugins are an intentional failure boundary
            error = (
                exc.code
                if isinstance(exc, SystemOneError)
                else ("PROVIDER_TIMEOUT" if isinstance(exc, TimeoutError) else "INVALID_STATE_OR_RESPONSE")
            )
            record.error = error
            self._last_error = error
            if error == "SYSTEM_ONE_BYPASSED":
                self._counters["bypassed"] += 1
            else:
                self._counters["errors"] += 1
                self._consecutive_failures += 1
                if self._consecutive_failures >= self.settings.circuit_failure_threshold:
                    self._opened_at = monotonic()
            if error == "PROVIDER_TIMEOUT":
                self._counters["timeouts"] += 1
        record.latency_ms = (perf_counter() - started) * 1000
        self._counters["latency_ms"] += record.latency_ms
        self._counters["observations"] += 1
        self.records.append(record.model_copy(deep=True))
        return record

    def _count_result(self, record: ShadowRecord) -> None:
        suggested = record.system_one_suggested_action
        self._counters["input_tokens"] += record.input_tokens
        self._counters["output_tokens"] += record.output_tokens
        self._counters["abstentions"] += sum(a.status != SemanticStatus.OK for a in record.decisions.values())
        if record.stage == "routing":
            actual_agents = set(record.pipeline_actual_action.get("agents", []))
            would_skip = {
                agent
                for name, agent in ROUTING_AGENT_IDS.items()
                if suggested.get(f"needs_{name}_agent") is False and agent in actual_agents
            }
            would_add = {
                agent
                for name, agent in ROUTING_AGENT_IDS.items()
                if suggested.get(f"needs_{name}_agent") is True and agent not in actual_agents
            }
            self._counters["agents_actual"] += len(actual_agents)
            self._counters["agents_suggested"] += len(actual_agents - would_skip | would_add)
            self._counters["agents_would_skip"] += len(would_skip)
            self._counters["agents_would_add"] += len(would_add)
        if record.stage == "consistency":
            self._counters["outputs_flagged"] += suggested.get("material_conflict") is True
            self._counters["outputs_would_retry"] += suggested.get("consistency_action") == "RETRY_AGENT"
            self._counters["outputs_would_reanalyse"] += suggested.get("requires_reanalysis") is True
            self._counters["outputs_would_escalate"] += (
                suggested.get("consistency_action") == "ESCALATE_SYSTEM_2"
            )
        if record.stage == "escalation":
            suggested_escalation = suggested.get("requires_deep_reasoning") is True
            actual_calls = record.pipeline_actual_action.get("system2_calls", 0)
            self._counters["system_one_escalations"] += suggested_escalation
            self._counters["actual_system2_calls"] += actual_calls
            # These are disagreements with actual behavior, not labeled ground truth errors.
            self._counters["escalation_disagreements"] += suggested_escalation != bool(actual_calls)
        if record.stage == "pre_screen":
            self._counters["potential_false_negative_opportunities"] += (
                suggested.get("should_analyze") is False
                and record.pipeline_actual_action.get("decision") == "BET"
            )

    def health(self) -> dict[str, Any]:
        if not self.settings.enabled:
            status = "disabled"
        elif self._opened_at is not None:
            status = "SYSTEM_ONE_BYPASSED"
        elif self._last_error:
            status = "degraded"
        else:
            status = "healthy" if self._counters["calls"] else "not_checked"
        return {
            "enabled": self.settings.enabled,
            "provider": self.settings.provider,
            "mode": "shadow",
            "status": status,
        }

    def telemetry(self) -> dict[str, Any]:
        counts = dict(self._counters)
        calls = self._counters["calls"]
        observations = self._counters["observations"]
        return {
            **counts,
            "error_rate": self._counters["errors"] / calls if calls else 0,
            "timeout_rate": self._counters["timeouts"] / calls if calls else 0,
            "mean_latency_ms": self._counters["latency_ms"] / observations if observations else 0,
            "model_id": self.records[-1].model_id if self.records else None,
            "model_fingerprint": self.records[-1].model_fingerprint if self.records else None,
            "agents_would_add": self._counters["agents_would_add"],
            "outputs_retried": 0,
            "outputs_reanalysed": 0,
            "outputs_escalated": 0,
            "missed_escalations": None,
            "unnecessary_escalations": None,
            "potential_llm_calls_saved": None,
            "potential_tokens_saved": None,
            "potential_latency_saved": None,
            "potential_cost_saved": None,
            "unmeasured_reason": "Savings and ground-truth error metrics require labeled replay/cost data; shadow takes no actions.",
        }
