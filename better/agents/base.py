import asyncio
import hashlib
import json
import logging
from abc import ABC, abstractmethod
from datetime import timedelta
from pathlib import Path
from time import perf_counter
from uuid import NAMESPACE_URL, uuid5

from pydantic import ValidationError

from better.domain.enums import AgentStatus, Direction, ErrorCode, ReliabilityTier, SourceType
from better.domain.errors import BetterError
from better.domain.models import DataQuality, Evidence, Signal
from better.domain.provenance import Provenance
from better.domain.schemas import AgentContext, AgentResult
from better.system_zero.freshness import freshness

logger = logging.getLogger("better.agents")


class BaseAgent(ABC):
    agent_id: str
    agent_version = "1.1.0"
    prompt_version = "1.0.0"
    dependencies: tuple[str, ...] = ()
    allowed_tools: frozenset[str] = frozenset()
    forbidden_tools: frozenset[str] = frozenset({"betting_execution"})
    needs_history = False
    needs_portfolio = False

    def __init__(self, settings):
        self.settings = settings
        self.timeout = settings.agent_timeout_seconds

    @property
    def system_prompt(self) -> str:
        filename = self.agent_id.removeprefix("football_")
        return (Path(__file__).parents[1] / "prompts" / f"{filename}.md").read_text(encoding="utf-8")

    def require_tool(self, tool: str):
        if tool not in self.allowed_tools or tool in self.forbidden_tools:
            raise BetterError(ErrorCode.CONFIGURATION_ERROR, f"{self.agent_id} cannot use {tool}")

    async def run(self, context: AgentContext) -> AgentResult:
        extra = set(context.upstream_results) - set(self.dependencies)
        if (
            extra
            or (context.validation_history and not self.needs_history)
            or (context.portfolio and not self.needs_portfolio)
        ):
            raise BetterError(
                ErrorCode.CONFIGURATION_ERROR, f"Unauthorized context for {self.agent_id}: {sorted(extra)}"
            )
        started = perf_counter()
        try:
            result = await asyncio.wait_for(self._run(context), timeout=self.timeout)
            if result.agent_id != self.agent_id or result.analysis_id != context.analysis_id:
                raise BetterError(ErrorCode.SCHEMA_ERROR, "Agent returned foreign identity")
            if result.status == AgentStatus.PARTIAL and not result.degradations:
                raise BetterError(ErrorCode.SCHEMA_ERROR, "PARTIAL requires explicit degradation assessment")
            return result
        except TimeoutError:
            return self.failure(
                context, BetterError(ErrorCode.TIMEOUT, "Agent timed out", source=self.agent_id)
            )
        except BetterError as exc:
            return self.failure(context, exc)
        except ValidationError:
            return self.failure(
                context,
                BetterError(ErrorCode.SCHEMA_ERROR, "Invalid provider/agent schema", source=self.agent_id),
            )
        finally:
            logger.debug(
                "agent_execution_finished",
                extra={
                    "agent": self.agent_id,
                    "analysis_id": str(context.analysis_id),
                    "run_id": str(context.run_id),
                    "duration_ms": (perf_counter() - started) * 1000,
                },
            )

    @abstractmethod
    async def _run(self, context: AgentContext) -> AgentResult:
        raise NotImplementedError

    def failure(self, context, error):
        statuses = {
            ErrorCode.MISSING_DATA: AgentStatus.INSUFFICIENT_DATA,
            ErrorCode.SCHEMA_ERROR: AgentStatus.VALIDATION_ERROR,
            ErrorCode.STALE_DATA: AgentStatus.STALE_DATA,
            ErrorCode.MODEL_FAILURE: AgentStatus.MODEL_ERROR,
        }
        return AgentResult(
            analysis_id=context.analysis_id,
            agent_id=self.agent_id,
            status=statuses.get(error.detail.code, AgentStatus.SOURCE_ERROR),
            summary=str(error),
            generated_at=context.as_of,
            errors=(error.detail,),
        )

    def missing(self, context, reason):
        return self.failure(context, BetterError(ErrorCode.MISSING_DATA, reason, source=self.agent_id))

    def output(
        self,
        context,
        payload,
        *,
        summary,
        quality=None,
        provenance=(),
        evidence=(),
        risks=(),
        unknowns=(),
        status=AgentStatus.OK,
        confidence=None,
        degradations=(),
        direction=Direction.NOT_APPLICABLE,
        analysis_available=True,
        operationally_eligible=None,
    ):
        quality = quality or DataQuality(
            completeness=1, freshness=1, source_reliability=1, cross_source_consistency=1
        )
        return AgentResult(
            analysis_id=context.analysis_id,
            agent_id=self.agent_id,
            status=status,
            summary=summary,
            payload=payload,
            generated_at=context.as_of,
            data_quality=quality,
            provenance=tuple(provenance),
            evidence=tuple(evidence),
            risks=tuple(risks),
            unknowns=tuple(unknowns),
            degradations=tuple(degradations),
            analysis_available=analysis_available,
            operationally_eligible=(
                status == AgentStatus.OK if operationally_eligible is None else operationally_eligible
            ),
            signal=Signal(
                direction=direction,
                strength=0 if direction == Direction.NOT_APPLICABLE else 0.5,
                confidence=quality.score if confidence is None else confidence,
            ),
        )

    def evidence(
        self,
        context,
        claim,
        provenance,
        *,
        kind,
        derived_from=(),
        confidence=0.8,
        direction=Direction.NEUTRAL,
        markets=(),
    ):
        provenance = tuple(provenance)
        identity = hashlib.sha256(
            json.dumps([p.model_dump(mode="json") for p in provenance], sort_keys=True).encode()
        ).hexdigest()
        return Evidence(
            evidence_id=uuid5(
                NAMESPACE_URL, f"{context.analysis_id}:{self.agent_id}:{kind}:{claim}:{identity}"
            ),
            agent_id=self.agent_id,
            type=kind,
            claim=claim,
            provenance=tuple(provenance),
            derived_from=tuple(derived_from),
            confidence=confidence,
            strength=0.5,
            direction=direction,
            market_scope=tuple(markets),
        )

    def model_source(self, context, field):
        return Provenance(
            source_id=self.agent_id,
            source_type=SourceType.MODEL,
            retrieved_at=context.as_of,
            reliability_tier=ReliabilityTier.B,
            field=field,
            raw_reference=f"{context.analysis_id}:{self.agent_version}",
            notes="Deterministic code-derived result; not an external observation",
        )

    def quality(self, context, provenance, observed_at, ttl, completeness=1, consistency=1):
        if not provenance or any(not p.available_at(context.as_of) for p in provenance):
            raise BetterError(ErrorCode.SCHEMA_ERROR, "Source unavailable at analysis as_of")
        if observed_at > context.as_of:
            raise BetterError(ErrorCode.SCHEMA_ERROR, "Snapshot timestamp is after analysis as_of")
        fresh = freshness(observed_at, context.as_of, timedelta(seconds=ttl))
        reliability = min(
            {"A": 1, "B": 0.85, "C": 0.45, "D": 0.2}[p.reliability_tier.value] for p in provenance
        )
        return DataQuality(
            completeness=completeness,
            freshness=fresh.score,
            source_reliability=reliability,
            cross_source_consistency=consistency,
        )

    def upstream_quality(self, context, *ids):
        values = [context.upstream_results[id].data_quality for id in ids if id in context.upstream_results]
        if not values:
            return DataQuality()
        return DataQuality(
            **{
                key: min(getattr(v, key) for v in values)
                for key in ("completeness", "freshness", "source_reliability", "cross_source_consistency")
            }
        )

    def payload(self, context, agent_id, expected_type):
        result = context.upstream_results.get(agent_id)
        return result.payload if result and isinstance(result.payload, expected_type) else None
