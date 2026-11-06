"""Targeted, budgeted external research orchestration."""

import os
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from pydantic import Field

from better.domain.common import Contract
from better.domain.research import (
    ResearchProviderFailure,
    ResearchRequest,
    ResearchResult,
    ResearchRunStatus,
    as_provenance,
)
from better.providers.research import OpenAIExternalResearchProvider
from better.services.local_environment import environment_value
from better.storage.repository import canonical_hash


def qualitative_source_state(match):
    """Version only the identity facts that can change qualitative research."""
    return canonical_hash({"match": match.model_dump(mode="json")})


def missing_qualitative_fields(snapshots, as_of, configuration, *, news_ttl=1800, kickoff=None):
    wanted = []
    news = snapshots.get("news")
    if news is None or (as_of - news.captured_at).total_seconds() > news_ttl:
        wanted.append("relevant_match_news" if "relevant_match_news" in configuration.fields else "news")
    lineup = snapshots.get("lineups")
    lineup_field = "confirmed_lineups" if kickoff and (kickoff - as_of).total_seconds() <= 7200 else "probable_lineups"
    if lineup is None or lineup.status == "UNKNOWN":
        wanted.extend((lineup_field, "unavailable_players", "injuries", "suspensions"))
    elif lineup.status != "CONFIRMED_LINEUP" and (as_of - lineup.captured_at).total_seconds() > 300:
        wanted.append(lineup_field)
    return tuple(name for name in wanted if (policy := configuration.fields.get(name)) is not None and policy.enabled)


def _request_budget_spend(row):
    """Do not lock a match after a pre-session authentication/configuration failure."""
    no_provider_work = not any(
        (
            row.get("session_id"),
            row.get("response_id"),
            row.get("search_calls", 0),
            row.get("tool_calls", 0),
            row.get("input_tokens", 0),
            row.get("output_tokens", 0),
            row.get("estimated_cost"),
        )
    )
    if row.get("status") == ResearchRunStatus.EXTERNAL_RESEARCH_ERROR.value and no_provider_work:
        return 0
    return int(row.get("request_count", 0))


class ResearchFieldPolicy(Contract):
    enabled: bool = False
    ttl_seconds: int = Field(default=900, ge=1, le=86400)


class ExternalResearchConfiguration(Contract):
    enabled: bool = False
    provider: Literal["openai"] = "openai"
    research_primary_source: bool = True
    fields: dict[str, ResearchFieldPolicy] = Field(
        default_factory=lambda: {
            name: ResearchFieldPolicy(enabled=True, ttl_seconds=300 if "lineups" in name else 1800)
            for name in (
                "news",
                "injuries",
                "suspensions",
                "unavailable_players",
                "doubtful_players",
                "returned_players",
                "probable_lineups",
                "confirmed_lineups",
                "squad_callups",
                "manager_press_conferences",
                "turnover",
                "manager_changes",
                "fixture_congestion",
                "relevant_match_news",
                "travel_context",
                "weather",
                "pitch_playing_conditions",
                "contextual_information",
            )
        }
    )
    max_requests_per_match: int = Field(default=1, ge=1, le=10)
    max_search_calls_per_match: int = Field(default=3, ge=1, le=3)
    max_tokens_per_match: int = Field(default=8000, ge=100, le=200000)
    max_cost_per_match: Decimal = Field(default=Decimal("0.25"), gt=0)

    @classmethod
    def from_env(cls, base=None):
        values = (base or cls()).model_dump()
        mapping = {
            "enabled": "EXTERNAL_RESEARCH_ENABLED",
            "research_primary_source": "RESEARCH_PRIMARY_SOURCE",
            "provider": "EXTERNAL_RESEARCH_PROVIDER",
            "max_requests_per_match": "OPENAI_RESEARCH_MAX_REQUESTS_PER_MATCH",
            "max_search_calls_per_match": "OPENAI_RESEARCH_MAX_SEARCH_CALLS",
            "max_tokens_per_match": "OPENAI_RESEARCH_MAX_TOKENS_PER_MATCH",
            "max_cost_per_match": "OPENAI_RESEARCH_MAX_COST_PER_MATCH",
        }
        for field, name in mapping.items():
            if name not in os.environ:
                continue
            raw = os.environ[name]
            if field in {"enabled", "research_primary_source"}:
                values[field] = raw.strip().lower() in {"1", "true", "yes", "on"}
            else:
                values[field] = raw
        return cls.model_validate(values)


def _decimal_env(name):
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    try:
        return Decimal(raw)
    except InvalidOperation as error:
        raise ValueError(f"{name} must be a decimal") from error


def _boolean_env(name, default=False):
    raw = os.environ.get(name)
    return default if raw is None else raw.strip().lower() in {"1", "true", "yes", "on"}


def build_external_research(configuration, repository, metrics, *, provider=None, clock=None):
    configuration = ExternalResearchConfiguration.from_env(configuration)
    if configuration.enabled and provider is None:
        if configuration.provider != "openai":
            raise ValueError(f"Unsupported external research provider: {configuration.provider}")
        api_key = environment_value("OPENAI_API_KEY")
        selected_transport = os.environ.get("OPENAI_RESEARCH_TRANSPORT")
        if not selected_transport:
            selected_transport = (
                "agents_api"
                if _boolean_env("OPENAI_RESEARCH_USE_AGENTS_API", default=True)
                else "responses_api"
            )
        response_model = os.environ.get("OPENAI_RESEARCH_MODEL") or (
            "gpt-5.6-luna" if selected_transport == "responses_api" else None
        )
        fallback_transport = os.environ.get("OPENAI_RESEARCH_FALLBACK_TRANSPORT") or None
        if api_key:
            prompt_path = os.path.join(
                os.path.dirname(os.path.dirname(__file__)),
                "providers",
                "external_research_prompt.md",
            )
            responses_instructions = (
                open(prompt_path, encoding="utf-8").read()
                if selected_transport == "responses_api" or fallback_transport == "responses_api"
                else None
            )
            provider = OpenAIExternalResearchProvider(
                api_key=api_key,
                transport=selected_transport,
                agent_id=environment_value("OPENAI_RESEARCH_AGENT_ID") or None,
                model=response_model,
                fallback_model=os.environ.get("OPENAI_RESEARCH_FALLBACK_MODEL") or None,
                fallback_transport=fallback_transport,
                timeout_seconds=float(os.environ.get("OPENAI_RESEARCH_TIMEOUT_SECONDS", "30")),
                max_output_tokens=min(
                    int(os.environ.get("OPENAI_RESEARCH_MAX_OUTPUT_TOKENS", "2500")),
                    configuration.max_tokens_per_match,
                ),
                max_search_calls=configuration.max_search_calls_per_match,
                max_cost_per_match=configuration.max_cost_per_match,
                responses_instructions=responses_instructions,
                web_search_context_size=os.environ.get("OPENAI_RESEARCH_WEB_SEARCH_CONTEXT_SIZE", "low"),
                input_cost_per_million=_decimal_env("OPENAI_RESEARCH_INPUT_COST_PER_MILLION")
                or (Decimal("0.20") if response_model == "gpt-5.6-luna" else None),
                output_cost_per_million=_decimal_env("OPENAI_RESEARCH_OUTPUT_COST_PER_MILLION")
                or (Decimal("1.20") if response_model == "gpt-5.6-luna" else None),
                web_search_cost_per_call=_decimal_env("OPENAI_RESEARCH_WEB_SEARCH_COST_PER_CALL")
                or Decimal("0.01"),
            )
    return ExternalResearchService(
        configuration, provider, repository=repository, metrics=metrics, clock=clock
    )


class ExternalResearchService:
    """Calls a research provider only for configured, missing fields.

    Results are normalized before agents see them; providers remain inaccessible to agents.
    """

    def __init__(self, settings, provider, *, repository, metrics, clock=None):
        self.settings = settings
        self.provider = provider
        self.repository = repository
        self.metrics = metrics
        self.clock = clock or (lambda: datetime.now(UTC))
        self._cache = {}

    def describe(self):
        missing = []
        if self.settings.enabled and self.provider is None:
            missing.append("OPENAI_API_KEY")
        if (
            self.settings.enabled
            and self.provider is not None
            and not getattr(self.provider, "agent_id", None)
        ):
            missing.append("OPENAI_RESEARCH_AGENT_ID")
        return {
            "enabled": self.settings.enabled,
            "provider": getattr(self.provider, "provider_id", self.settings.provider)
            if self.settings.enabled
            else None,
            "model": getattr(self.provider, "model", None),
            "transport": getattr(self.provider, "transport", None),
            "agent_id": getattr(self.provider, "agent_id", None),
            "phase": "QUALITATIVE_PRIMARY" if self.settings.research_primary_source else "OPTIONAL",
            "research_primary_source": self.settings.research_primary_source,
            "ready": not missing,
            "configuration_error": f"Configurazione mancante: {', '.join(missing)}" if missing else None,
            "fields": {
                name: {"enabled": policy.enabled, "ttl_seconds": policy.ttl_seconds}
                for name, policy in self.settings.fields.items()
            },
        }

    async def research_missing(self, match, *, as_of, missing_fields, source_state_version, cache_only=False):
        fields = tuple(
            field
            for field in dict.fromkeys(missing_fields)
            if (policy := self.settings.fields.get(field)) is not None and policy.enabled
        )
        now = self.clock()
        if not self.settings.enabled:
            return self._empty(match.match_id, as_of, fields, ResearchRunStatus.DISABLED, now)
        if self.provider is None:
            result = self._empty(
                match.match_id,
                as_of,
                fields,
                ResearchRunStatus.EXTERNAL_RESEARCH_ERROR,
                now,
                "OPENAI_API_KEY is not configured",
            )
            self.metrics.research_error(result.status.value)
            return result
        if not fields:
            return self._empty(match.match_id, as_of, (), ResearchRunStatus.NOT_REQUIRED, now)
        ttl = min(self.settings.fields[field].ttl_seconds for field in fields)
        bucket = int(as_of.timestamp()) // ttl
        key = (match.match_id, fields, bucket, source_state_version)
        cached = self._cache.get(key)
        if cached is not None:
            self.metrics.research_cache(True)
            return cached.model_copy(update={"cached": True})
        entity_id = f"{match.match_id}:{bucket}"
        prior = self.repository.list_records("research_results", last=1000, entity_id=entity_id)
        requests_by_id = {
            row["request_id"]: row
            for row in self.repository.list_records("research_requests", last=1000, entity_id=entity_id)
        }
        # Field subsets and TTL boundaries must not trigger a duplicate search.
        # Acquisition dates, source identity and the current saved agent still
        # have to match; the original point-in-time evidence is retained.
        compatible_requests = {r["request_id"]: r for r in self.repository.all_records(
            "research_requests", match_id=match.match_id)}
        compatible = next((row for row in self.repository.all_records("research_results", match_id=match.match_id)
            if row.get("status") in {ResearchRunStatus.OK.value, ResearchRunStatus.RESEARCH_NOT_FOUND.value}
            and (req := compatible_requests.get(row.get("request_id"))) is not None
            and set(fields) <= set(req.get("missing_fields", ()))
            and req.get("source_state_version") == source_state_version
            and row.get("agent_id") == getattr(self.provider, "agent_id", None)
            and datetime.fromisoformat(row["as_of"]) <= as_of
            and 0 <= (now - datetime.fromisoformat(row["completed_at"])).total_seconds() < ttl), None)
        if compatible:
            result = ResearchResult.model_validate(compatible)
            chosen = tuple(f for f in result.findings if f.field in fields)
            from better.domain.research import finding_is_resolved
            result = result.model_copy(update={"cached": True, "findings": chosen, "searched_fields": fields,
                "unresolved_fields": tuple(f.field for f in chosen if not finding_is_resolved(f.status)),
                "status": ResearchRunStatus.OK if any(finding_is_resolved(f.status) for f in chosen) else ResearchRunStatus.RESEARCH_NOT_FOUND})
            self._cache[key] = result
            self.metrics.research_cache(True)
            return result
        reusable = next(
            (
                row
                for row in prior
                if row.get("status")
                in {ResearchRunStatus.OK.value, ResearchRunStatus.RESEARCH_NOT_FOUND.value}
                and (request_row := requests_by_id.get(row.get("request_id"))) is not None
                and tuple(request_row.get("missing_fields", ())) == fields
                and request_row.get("source_state_version") == source_state_version
            ),
            None,
        )
        if reusable is not None:
            result = ResearchResult.model_validate(reusable).model_copy(update={"cached": True})
            self._cache[key] = result
            self.metrics.research_cache(True)
            return result
        if cache_only:
            return None
        requests = sum(_request_budget_spend(row) for row in prior)
        searches = sum(int(row.get("search_calls", 0)) for row in prior)
        tokens = sum(int(row.get("input_tokens", 0)) + int(row.get("output_tokens", 0)) for row in prior)
        costs = [
            Decimal(str(row["estimated_cost"])) for row in prior if row.get("estimated_cost") is not None
        ]
        minimum_next_cost = getattr(self.provider, "web_search_cost_per_call", None)
        if (
            requests >= self.settings.max_requests_per_match
            or searches >= self.settings.max_search_calls_per_match
            or tokens >= self.settings.max_tokens_per_match
            or sum(costs, Decimal(0)) >= self.settings.max_cost_per_match
            or (
                minimum_next_cost is not None
                and sum(costs, Decimal(0)) + minimum_next_cost > self.settings.max_cost_per_match
            )
        ):
            result = self._empty(
                match.match_id, as_of, fields, ResearchRunStatus.RESEARCH_BUDGET_EXCEEDED, now
            )
            self.metrics.research_error(result.status.value)
            return result
        request = ResearchRequest(
            request_id=str(
                uuid5(
                    NAMESPACE_URL,
                    f"better:research:{match.match_id}:{bucket}:{source_state_version}:{','.join(fields)}:{now.isoformat()}",
                )
            ),
            match_id=match.match_id,
            home_team=match.home_team,
            away_team=match.away_team,
            competition=match.competition,
            kickoff=match.kickoff,
            as_of=as_of,
            missing_fields=fields,
            source_state_version=source_state_version,
            requested_at=now,
        )
        self._persist("research_requests", request.request_id, entity_id, match.match_id, request)
        self.metrics.research_requested(fields)
        started = datetime.now(UTC)
        try:
            result = await self.provider.research(request)
            if self._budget_exceeded(result, prior):
                result = result.model_copy(
                    update={
                        "status": ResearchRunStatus.RESEARCH_BUDGET_EXCEEDED,
                        "error": "External research response exceeded the configured per-match budget",
                        "unresolved_fields": fields,
                    }
                )
                self.metrics.research_error(result.status.value)
        except ResearchProviderFailure as error:
            status = (
                ResearchRunStatus.EXTERNAL_RESEARCH_ERROR
                if error.status == ResearchRunStatus.RESEARCH_PROVIDER_ERROR
                else error.status
            )
            result = self._empty(match.match_id, as_of, fields, status, self.clock(), str(error))
            result = result.model_copy(
                update={"request_id": request.request_id, "request_count": error.attempts}
            )
            self.metrics.research_error(status.value)
        latency = (datetime.now(UTC) - started).total_seconds() * 1000
        self._persist_result(entity_id, result)
        self.metrics.research_completed(result, latency_ms=latency)
        self.metrics.research_cache(False)
        if result.status in {ResearchRunStatus.OK, ResearchRunStatus.RESEARCH_NOT_FOUND}:
            self._cache[key] = result
        return result

    def _budget_exceeded(self, result, prior):
        requests = sum(_request_budget_spend(row) for row in prior) + result.request_count
        searches = sum(int(row.get("search_calls", 0)) for row in prior) + result.search_calls
        tokens = (
            sum(int(row.get("input_tokens", 0)) + int(row.get("output_tokens", 0)) for row in prior)
            + result.input_tokens
            + result.output_tokens
        )
        known_cost = result.estimated_cost is not None and all(
            row.get("estimated_cost") is not None for row in prior
        )
        cost = sum(
            (Decimal(str(row["estimated_cost"])) for row in prior if row.get("estimated_cost") is not None),
            Decimal(0),
        ) + (result.estimated_cost or Decimal(0))
        return (
            requests > self.settings.max_requests_per_match
            or searches > self.settings.max_search_calls_per_match
            or tokens > self.settings.max_tokens_per_match
            or (known_cost and cost > self.settings.max_cost_per_match)
        )

    def _empty(self, match_id, as_of, fields, status, completed_at, error=None):
        return ResearchResult(
            request_id=f"none:{canonical_hash({'match_id': match_id, 'as_of': as_of.isoformat(), 'status': status})}",
            match_id=match_id,
            as_of=as_of,
            searched_fields=(),
            unresolved_fields=fields,
            provider=getattr(self.provider, "provider_id", self.settings.provider),
            model=getattr(self.provider, "model", "") or "",
            transport=getattr(self.provider, "transport", "unknown"),
            agent_id=getattr(self.provider, "agent_id", None),
            status=status,
            error=error,
            completed_at=completed_at,
        )

    def _persist(self, table, identifier, entity_id, match_id, value):
        payload = value.model_dump(mode="json")
        with self.repository.engine.begin() as connection:
            self.repository._append(
                connection,
                table,
                identifier=identifier,
                entity_id=entity_id,
                match_id=match_id,
                payload=payload,
            )

    def _persist_result(self, entity_id, result):
        self._persist("research_results", result.request_id, entity_id, result.match_id, result)
        for index, finding in enumerate(result.findings):
            finding_id = f"{result.request_id}:{index}:{sha256(finding.field.encode()).hexdigest()[:12]}"
            self._persist("research_findings", finding_id, result.request_id, result.match_id, finding)
            for source_index, source in enumerate(finding.sources):
                source_payload = {
                    **source.model_dump(mode="json"),
                    "request_id": result.request_id,
                    "finding_id": finding_id,
                    "field": finding.field,
                    "provider": result.provider,
                    "provenance": as_provenance(source, finding.field).model_dump(mode="json"),
                }
                with self.repository.engine.begin() as connection:
                    self.repository._append(
                        connection,
                        "research_sources",
                        identifier=f"{finding_id}:{source_index}:{canonical_hash(source_payload)}",
                        entity_id=finding_id,
                        match_id=result.match_id,
                        payload=source_payload,
                    )

    async def close(self):
        if self.provider is not None and hasattr(self.provider, "close"):
            await self.provider.close()


def research_coverage(result):
    from better.domain.research import finding_is_resolved

    found = sum(finding_is_resolved(finding.status) for finding in result.findings)
    return {
        "before": len(result.unresolved_fields) + found,
        "after": len(result.unresolved_fields),
        "found": found,
    }
