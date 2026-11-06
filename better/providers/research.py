"""External research providers. They find evidence; they never make betting decisions."""

import json
from datetime import UTC, datetime
from decimal import Decimal
from time import perf_counter
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit

import httpx
from pydantic import TypeAdapter, ValidationError

from better.domain.enums import ReliabilityTier
from better.domain.football_data_research import (
    FootballDataFinding,
    FootballDataFindingStatus,
    FootballDataResearchRequest,
    FootballDataResearchResult,
    FootballDataRunStatus,
    RawMatchObservation,
)
from better.domain.research import (
    ResearchFinding,
    ResearchFindingStatus,
    ResearchProviderFailure,
    ResearchRequest,
    ResearchResult,
    ResearchRunStatus,
    ResearchSource,
    finding_is_resolved,
)
from better.providers.research_sources import SOURCE_TYPES, source_category


def research_auth_message(response):
    """Classify safe provider codes without echoing bodies that may contain keys."""
    try:
        body = response.json()
        error = body.get("error", {}) if isinstance(body, dict) else {}
        code = error.get("code") if isinstance(error, dict) else None
    except ValueError:
        code = None
    if code == "expired_secret_key":
        return "OPENAI_API_KEY scaduta (OPENAI_KEY_EXPIRED; HTTP 401). Sostituire la chiave nello stesso progetto degli agenti."
    if response.status_code == 403:
        return "OpenAI: permessi o accesso al progetto insufficienti (OPENAI_PERMISSION_ERROR; HTTP 403)."
    return "OPENAI_API_KEY non valida o revocata (OPENAI_AUTH_ERROR; HTTP 401)."


class ExternalResearchProvider(Protocol):
    async def research(self, request: ResearchRequest) -> ResearchResult: ...


def canonical_source_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), "", ""))


def _normalize_source_timestamps(payload):
    """Canonicalize valid ISO dates/naive datetimes; malformed values still fail Pydantic."""
    for finding in payload.get("findings", ()):
        for source in finding.get("sources", ()):
            for field in ("published_at", "retrieved_at", "updated_at"):
                value = source.get(field)
                if not isinstance(value, str):
                    continue
                try:
                    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if parsed.tzinfo is None:
                    source[field] = parsed.replace(tzinfo=UTC).isoformat()
    return payload


def _trusted_tier(source: ResearchSource) -> ReliabilityTier:
    source_type = source_category(source)
    if source_type in {"OFFICIAL_CLUB", "OFFICIAL_COMPETITION", "FEDERATION", "INSTITUTIONAL"}:
        return source.reliability_tier
    if source_type in {"TRUSTED_MEDIA", "LOCAL_MEDIA", "BROADCASTER", "TEAM_REPORTER"}:
        return max(source.reliability_tier, ReliabilityTier.B, key=lambda tier: "ABCD".index(tier.value))
    if source_type in {"AGGREGATOR", "SECONDARY"}:
        return max(source.reliability_tier, ReliabilityTier.C, key=lambda tier: "ABCD".index(tier.value))
    return ReliabilityTier.D


def normalize_finding(finding: ResearchFinding, request: ResearchRequest) -> ResearchFinding:
    """Apply deterministic source, temporal and confirmation rules."""
    deduplicated = {}
    future = False
    for source in finding.sources:
        if source.published_at is not None and source.published_at > request.as_of:
            future = True
            continue
        canonical = canonical_source_url(source.canonical_url or source.url)
        normalized = source.model_copy(
            update={
                "canonical_url": canonical,
                "source_type": source_category(source),
                "reliability_tier": _trusted_tier(source),
                "retrieved_at": request.requested_at,
            }
        )
        current = deduplicated.get(canonical)
        if current is None or "ABCD".index(normalized.reliability_tier.value) < "ABCD".index(
            current.reliability_tier.value
        ):
            deduplicated[canonical] = normalized
    sources = tuple(deduplicated.values())
    status, value, notes = finding.status, finding.value, finding.notes
    if not sources and future:
        status, value = ResearchFindingStatus.STALE, None
    if status in {
        ResearchFindingStatus.FOUND,
        ResearchFindingStatus.CONFIRMED,
        ResearchFindingStatus.PROBABLE,
        ResearchFindingStatus.REPORTED,
    }:
        dated = [source for source in sources if source.published_at is not None]
        credible = [
            source for source in dated if source.reliability_tier in {ReliabilityTier.A, ReliabilityTier.B}
        ]
        if not credible:
            status, value = ResearchFindingStatus.INSUFFICIENT_EVIDENCE, None
            notes = notes or "No dated Tier A/B source available at the requested cutoff."
        if finding.field == "confirmed_lineups" and not any(
            source.reliability_tier == ReliabilityTier.A for source in sources
        ):
            status, value = ResearchFindingStatus.INSUFFICIENT_EVIDENCE, None
            notes = "A confirmed lineup requires an official Tier A source."
        elif value is not None:
            if finding.field == "confirmed_lineups":
                status = ResearchFindingStatus.CONFIRMED
            elif finding.field == "probable_lineups":
                status = ResearchFindingStatus.PROBABLE
            elif status == ResearchFindingStatus.FOUND:
                status = ResearchFindingStatus.REPORTED
            if status == ResearchFindingStatus.CONFIRMED and not any(
                source.reliability_tier == ReliabilityTier.A for source in sources
            ):
                status = ResearchFindingStatus.REPORTED
                notes = notes or "Confirmation was downgraded because no Tier A source was cited."
    return finding.model_copy(update={"sources": sources, "status": status, "value": value, "notes": notes})


class OpenAIResearchProvider:
    provider_id = "openai_web_search"

    def __init__(
        self,
        *,
        api_key: str,
        transport: str = "agents_api",
        agent_id: str | None = None,
        model: str | None = None,
        fallback_model: str | None = None,
        fallback_transport: str | None = None,
        timeout_seconds: float = 30,
        max_output_tokens: int = 2500,
        max_search_calls: int = 2,
        max_cost_per_match: Decimal | None = None,
        responses_instructions: str | None = None,
        web_search_context_size: str = "low",
        input_cost_per_million: Decimal | None = None,
        output_cost_per_million: Decimal | None = None,
        web_search_cost_per_call: Decimal | None = None,
        client=None,
    ):
        if not api_key.strip():
            raise ValueError("OPENAI_API_KEY is not configured")
        if transport not in {"agents_api", "responses_api"}:
            raise ValueError("OPENAI_RESEARCH_TRANSPORT must be agents_api or responses_api")
        if fallback_transport not in {None, "responses_api"}:
            raise ValueError("Only responses_api is supported as a fallback transport")
        self._api_key = api_key
        self.transport = transport
        self.agent_id = agent_id
        self.model = model
        self.fallback_model = fallback_model
        self.fallback_transport = fallback_transport
        self.max_output_tokens = max_output_tokens
        self.max_search_calls = max_search_calls
        self.max_cost_per_match = max_cost_per_match
        self.responses_instructions = responses_instructions
        self.web_search_context_size = web_search_context_size
        self.input_cost_per_million = input_cost_per_million
        self.output_cost_per_million = output_cost_per_million
        self.web_search_cost_per_call = web_search_cost_per_call
        self.client = client or httpx.AsyncClient(timeout=timeout_seconds)
        self._owns_client = client is None

    async def close(self):
        if self._owns_client:
            await self.client.aclose()

    async def research(self, request: ResearchRequest) -> ResearchResult:
        if self.transport == "agents_api":
            try:
                return await self._call_agents(request)
            except ResearchProviderFailure:
                if self.fallback_transport != "responses_api":
                    raise
                return await self._research_responses(request, initial_attempt=1)
        return await self._research_responses(request)

    async def _research_responses(self, request: ResearchRequest, *, initial_attempt=0) -> ResearchResult:
        failures = []
        models = tuple(model for model in (self.model, self.fallback_model) if model)
        if not models:
            raise ResearchProviderFailure(
                ResearchRunStatus.RESEARCH_PROVIDER_ERROR,
                "OPENAI_RESEARCH_MODEL is required for the Responses API transport",
            )
        for attempt, model in enumerate(models, 1):
            try:
                return await self._call_responses(request, model, initial_attempt + attempt)
            except ResearchProviderFailure as error:
                failures.append(error)
                if error.status in {
                    ResearchRunStatus.RESEARCH_RATE_LIMIT,
                    ResearchRunStatus.RESEARCH_TIMEOUT,
                }:
                    break
        error = failures[-1]
        raise ResearchProviderFailure(
            error.status, str(error), attempts=initial_attempt + len(failures)
        ) from error

    async def _call_responses(self, request: ResearchRequest, model: str, attempt: int) -> ResearchResult:
        started = perf_counter()
        payload = {
            "model": model,
            "tools": [{"type": "web_search", "search_context_size": self.web_search_context_size}],
            "tool_choice": "required",
            "include": ["web_search_call.action.sources"],
            "max_output_tokens": self.max_output_tokens,
            "max_tool_calls": self.max_search_calls,
            "input": [
                {
                    "role": "system",
                    "content": self.responses_instructions
                    or "Return cited football research as the configured Better JSON schema.",
                },
                {"role": "user", "content": json.dumps(request.model_dump(mode="json"), ensure_ascii=False)},
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "better_external_research",
                    "strict": True,
                    "schema": _response_schema(),
                }
            },
        }
        try:
            response = await self.client.post(
                "https://api.openai.com/v1/responses",
                headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
                json=payload,
            )
        except httpx.TimeoutException as error:
            raise ResearchProviderFailure(
                ResearchRunStatus.RESEARCH_TIMEOUT, "OpenAI research timed out"
            ) from error
        except httpx.HTTPError as error:
            raise ResearchProviderFailure(
                ResearchRunStatus.RESEARCH_PROVIDER_ERROR, "OpenAI research transport failed"
            ) from error
        if response.status_code == 429:
            raise ResearchProviderFailure(ResearchRunStatus.RESEARCH_RATE_LIMIT, "OpenAI rate limit reached")
        if response.status_code in (401, 403):
            raise ResearchProviderFailure(
                ResearchRunStatus.RESEARCH_PROVIDER_ERROR,
                research_auth_message(response),
            )
        if response.status_code >= 400:
            raise ResearchProviderFailure(
                ResearchRunStatus.RESEARCH_PROVIDER_ERROR,
                f"OpenAI Responses API returned HTTP {response.status_code}",
            )
        try:
            raw = response.json()
            text, cited_urls, search_calls = _extract_response(raw)
            findings = _validate_findings(text, cited_urls, request, "Responses API")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, ValidationError) as error:
            raise ResearchProviderFailure(
                ResearchRunStatus.RESEARCH_SCHEMA_ERROR, "OpenAI returned an invalid research schema"
            ) from error
        normalized = findings
        searched = tuple(finding.field for finding in normalized)
        usage = raw.get("usage") or {}
        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        cost = self._cost(input_tokens, output_tokens, search_calls)
        unresolved = tuple(finding.field for finding in normalized if not finding_is_resolved(finding.status))
        return ResearchResult(
            request_id=request.request_id,
            match_id=request.match_id,
            as_of=request.as_of,
            findings=tuple(normalized),
            searched_fields=searched,
            unresolved_fields=unresolved,
            provider=self.provider_id,
            model=raw.get("model") or model,
            transport="responses_api",
            response_id=raw.get("id"),
            search_calls=search_calls,
            tool_calls=search_calls,
            request_count=attempt,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost=cost,
            status=ResearchRunStatus.RESEARCH_NOT_FOUND
            if len(unresolved) == len(normalized)
            else ResearchRunStatus.OK,
            duration_ms=(perf_counter() - started) * 1000,
            completed_at=datetime.now(UTC),
        )

    async def _call_agents(self, request: ResearchRequest) -> ResearchResult:
        if not self.agent_id:
            raise ResearchProviderFailure(
                ResearchRunStatus.RESEARCH_PROVIDER_ERROR,
                "OPENAI_RESEARCH_AGENT_ID is required for the Agents API transport",
            )
        started = perf_counter()
        task = {
            "match_identity": {
                "match_id": request.match_id,
                "home_team": request.home_team,
                "away_team": request.away_team,
            },
            "competition": request.competition,
            "kickoff": request.kickoff.isoformat(),
            "as_of": request.as_of.isoformat(),
            "missing_fields": list(request.missing_fields),
            "source_contract": {
                "source_type_values": list(SOURCE_TYPES),
                "rule": "Use exactly one listed source_type. Identify the publisher from the actual source domain; never infer official status from a title. Supply a real publication timestamp or null. Never invent dates.",
            },
            "max_search_budget": {
                "max_search_calls": self.max_search_calls,
                "max_tokens": self.max_output_tokens,
                "max_estimated_cost": str(self.max_cost_per_match)
                if self.max_cost_per_match is not None
                else None,
            },
        }
        agent_output = await self._create_agent_session(task)
        try:
            findings = _validate_findings(
                agent_output["text"],
                agent_output["cited_urls"],
                request,
                "Agents API",
                structured_citations_allowed=agent_output["search_calls"] > 0,
            )
        except ResearchProviderFailure:
            raise
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, ValidationError) as error:
            raise ResearchProviderFailure(
                ResearchRunStatus.RESEARCH_SCHEMA_ERROR,
                "OpenAI Agent Session returned an invalid research schema",
            ) from error
        usage = agent_output["usage"]
        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        search_calls = agent_output["search_calls"]
        unresolved = tuple(finding.field for finding in findings if not finding_is_resolved(finding.status))
        return ResearchResult(
            request_id=request.request_id,
            match_id=request.match_id,
            as_of=request.as_of,
            findings=findings,
            searched_fields=tuple(finding.field for finding in findings),
            unresolved_fields=unresolved,
            provider=self.provider_id,
            model=agent_output["model"],
            transport="agents_api",
            response_id=agent_output["turn_id"],
            session_id=agent_output["session_id"],
            agent_id=agent_output["agent_id"] or self.agent_id,
            search_calls=search_calls,
            tool_calls=agent_output["tool_calls"],
            request_count=1,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost=self._cost(input_tokens, output_tokens, search_calls),
            status=ResearchRunStatus.RESEARCH_NOT_FOUND
            if len(unresolved) == len(findings)
            else ResearchRunStatus.OK,
            duration_ms=(perf_counter() - started) * 1000,
            completed_at=datetime.now(UTC),
        )

    async def _create_agent_session(self, task):
        try:
            response = await self.client.post(
                "https://api.openai.com/v1/agents/sessions",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                    "Accept": "text/event-stream",
                    "OpenAI-Beta": "agents=v1",
                },
                json={
                    "agent_id": self.agent_id,
                    "environment": {"type": "none"},
                    "input": json.dumps(task, ensure_ascii=False),
                    "stream": True,
                },
            )
        except httpx.TimeoutException as error:
            raise ResearchProviderFailure(
                ResearchRunStatus.RESEARCH_TIMEOUT, "OpenAI Agent Session timed out"
            ) from error
        except httpx.HTTPError as error:
            raise ResearchProviderFailure(
                ResearchRunStatus.RESEARCH_PROVIDER_ERROR,
                "OpenAI Agent Session transport failed",
            ) from error
        if response.status_code == 429:
            raise ResearchProviderFailure(
                ResearchRunStatus.RESEARCH_RATE_LIMIT, "OpenAI Agents API rate limit reached"
            )
        if response.status_code in (401, 403):
            raise ResearchProviderFailure(
                ResearchRunStatus.RESEARCH_PROVIDER_ERROR,
                research_auth_message(response),
            )
        if response.status_code >= 400:
            raise ResearchProviderFailure(
                ResearchRunStatus.RESEARCH_PROVIDER_ERROR,
                f"OpenAI Agents API returned HTTP {response.status_code}",
            )
        return _extract_agent_stream(response.text)

    def _cost(self, input_tokens, output_tokens, search_calls):
        rates = (self.input_cost_per_million, self.output_cost_per_million, self.web_search_cost_per_call)
        if any(rate is None for rate in rates):
            return None
        return (
            Decimal(input_tokens) * rates[0] / Decimal(1_000_000)
            + Decimal(output_tokens) * rates[1] / Decimal(1_000_000)
            + Decimal(search_calls) * rates[2]
        )


class OpenAIExternalResearchProvider(OpenAIResearchProvider):
    """Named qualitative provider; transport behavior is inherited and shared."""


class OpenAIFootballDataResearchProvider(OpenAIResearchProvider):
    """Saved-agent provider restricted to current quantitative football data."""

    provider_id = "openai_football_data_research"

    async def research(self, request: FootballDataResearchRequest) -> FootballDataResearchResult:
        if not self.agent_id:
            raise ResearchProviderFailure(
                ResearchRunStatus.RESEARCH_PROVIDER_ERROR,
                "OPENAI_FOOTBALL_DATA_AGENT_ID is required",
            )
        started = perf_counter()
        task = {
            "match_identity": {
                "match_id": request.match_id,
                "home_team": request.home_team,
                "away_team": request.away_team,
            },
            "competition": request.competition,
            "season": request.season,
            "kickoff": request.kickoff.isoformat(),
            "as_of": request.as_of.isoformat(),
            "requested_fields": list(request.requested_fields),
            "max_search_budget": {
                "max_search_calls": self.max_search_calls,
                "max_tokens": self.max_output_tokens,
                "max_estimated_cost": str(self.max_cost_per_match)
                if self.max_cost_per_match is not None
                else None,
            },
        }
        output = await self._create_agent_session(task)
        try:
            findings = _validate_football_data_findings(
                output["text"],
                output["cited_urls"],
                request,
                structured_citations_allowed=output["search_calls"] > 0,
            )
        except ResearchProviderFailure:
            raise
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, ValidationError) as error:
            raise ResearchProviderFailure(
                ResearchRunStatus.RESEARCH_SCHEMA_ERROR,
                "Football Data Agent returned an invalid schema",
            ) from error
        usage = output["usage"]
        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        unresolved = tuple(
            finding.field for finding in findings if finding.status != FootballDataFindingStatus.FOUND
        )
        cost = self._cost(input_tokens, output_tokens, output["search_calls"])
        return FootballDataResearchResult(
            request_id=request.request_id,
            match_id=request.match_id,
            as_of=request.as_of,
            findings=findings,
            requested_fields=request.requested_fields,
            unresolved_fields=unresolved,
            model=output["model"],
            agent_id=output["agent_id"] or self.agent_id,
            session_id=output["session_id"],
            response_id=output["turn_id"],
            search_calls=output["search_calls"],
            tool_calls=output["tool_calls"],
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost=float(cost) if cost is not None else None,
            duration_ms=(perf_counter() - started) * 1000,
            status=FootballDataRunStatus.OK,
            completed_at=datetime.now(UTC),
        )


class FakeFootballDataResearchProvider:
    provider_id = "fake_football_data_research"

    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    async def research(self, request):
        self.calls.append(request)
        if self.error:
            raise self.error
        return self.result(request) if callable(self.result) else self.result


class FakeResearchProvider:
    provider_id = "fake_external_research"

    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    async def research(self, request):
        self.calls.append(request)
        if self.error:
            raise self.error
        if callable(self.result):
            return self.result(request)
        return self.result


def _validate_findings(text, cited_urls, request, transport_name, *, structured_citations_allowed=False):
    payload = _normalize_source_timestamps(json.loads(text))
    findings = TypeAdapter(tuple[ResearchFinding, ...]).validate_python(payload["findings"])
    allowed = {canonical_source_url(url) for url in cited_urls}
    if not allowed and structured_citations_allowed:
        allowed = {canonical_source_url(source.url) for finding in findings for source in finding.sources}
    normalized = []
    for finding in findings:
        if any(canonical_source_url(source.url) not in allowed for source in finding.sources):
            raise ResearchProviderFailure(
                ResearchRunStatus.RESEARCH_SCHEMA_ERROR,
                f"A research source was not present in {transport_name} citations",
            )
        normalized.append(normalize_finding(finding, request))
    searched = tuple(dict.fromkeys(finding.field for finding in normalized))
    if set(searched) != set(request.missing_fields) or len(searched) != len(normalized):
        raise ResearchProviderFailure(
            ResearchRunStatus.RESEARCH_SCHEMA_ERROR,
            "Research output does not cover every requested field exactly once",
        )
    return tuple(normalized)


def _validate_football_data_findings(text, cited_urls, request, *, structured_citations_allowed=False):
    payload = _normalize_source_timestamps(json.loads(text))
    findings = TypeAdapter(tuple[FootballDataFinding, ...]).validate_python(payload["findings"])
    allowed_urls = {canonical_source_url(url) for url in cited_urls}
    if not allowed_urls and structured_citations_allowed:
        allowed_urls = {
            canonical_source_url(source.url) for finding in findings for source in finding.sources
        }
    forbidden_fields = {
        "xg",
        "xga",
        "ppda",
        "opponent_strength",
        "power_rating",
        "probabilities",
        "expected_points",
    }
    normalized = []
    for finding in findings:
        if finding.field.lower() in forbidden_fields:
            raise ValueError("advanced or inferred metric is outside the football-data scope")
        if any(canonical_source_url(source.url) not in allowed_urls for source in finding.sources):
            raise ResearchProviderFailure(
                ResearchRunStatus.RESEARCH_SCHEMA_ERROR,
                "A football-data source was not present in Agents API citations",
            )
        sources = []
        for source in finding.sources:
            if source.retrieved_at != request.requested_at:
                source = source.model_copy(update={"retrieved_at": request.requested_at})
            if source.published_at and source.published_at > request.as_of:
                continue
            is_wikipedia = "wikipedia.org" in urlsplit(source.url).netloc.lower()
            if is_wikipedia and finding.field not in {
                "team_identity",
                "squad_roles",
                "manager_identity",
            }:
                continue
            if is_wikipedia:
                source = source.model_copy(update={"source_tier": ReliabilityTier.C})
            else:
                source_type = source.source_type.upper()
                official = source_type in {
                    "OFFICIAL_CLUB",
                    "OFFICIAL_COMPETITION",
                    "FEDERATION",
                    "OFFICIAL_MATCH_CENTRE",
                }
                if not official and source.source_tier == ReliabilityTier.A:
                    source = source.model_copy(update={"source_tier": ReliabilityTier.B})
            sources.append(source)
        status, value = finding.status, finding.value
        if finding.field == "recent_form" and status == FootballDataFindingStatus.FOUND:
            if not isinstance(value, dict) or not all(
                isinstance(value.get(side), list) for side in ("home", "away")
            ):
                raise ValueError("recent_form requires home and away raw match lists")
            for side in ("home", "away"):
                for observation in value[side]:
                    parsed = RawMatchObservation.model_validate(observation)
                    if parsed.date >= request.as_of:
                        raise ValueError("recent_form contains a future match")
            if any(len(value[side]) < 10 for side in ("home", "away")):
                status, value = FootballDataFindingStatus.INSUFFICIENT_EVIDENCE, None
        if status == FootballDataFindingStatus.FOUND and not sources:
            status, value = FootballDataFindingStatus.INSUFFICIENT_EVIDENCE, None
        normalized.append(
            finding.model_copy(update={"status": status, "value": value, "sources": tuple(sources)})
        )
    if {finding.field for finding in normalized} != set(request.requested_fields):
        raise ValueError("football-data output must cover each routed field exactly once")
    return tuple(normalized)


def _sse_events(body):
    data = []
    for line in body.splitlines() + [""]:
        if line.startswith("data:"):
            data.append(line.removeprefix("data:").lstrip())
        elif not line and data:
            raw = "\n".join(data)
            data = []
            if raw != "[DONE]":
                yield json.loads(raw)


def _citation_urls(content):
    urls = set()
    for part in content or ():
        for annotation in part.get("annotations") or ():
            if annotation.get("type") == "url_citation":
                url = annotation.get("url") or (annotation.get("url_citation") or {}).get("url")
                if url:
                    urls.add(url)
    return urls


def _source_urls(item):
    urls = set()
    for source in (item.get("action") or {}).get("sources") or ():
        if source.get("url"):
            urls.add(source["url"])
    return urls


def _extract_agent_stream(body):
    session_id = None
    agent_id = None
    model = ""
    turn_id = None
    usage = {}
    text = None
    cited_urls = set()
    tool_ids = set()
    search_ids = set()
    completed = False
    for event in _sse_events(body):
        event_type = event.get("type", "")
        session = event.get("session") or {}
        turn = event.get("turn") or {}
        item = event.get("item") or {}
        session_id = event.get("session_id") or session.get("id") or session_id
        session_agent = session.get("agent") or {}
        agent_id = turn.get("agent_id") or session_agent.get("id") or agent_id
        model = session_agent.get("model") or event.get("model") or model
        turn_id = turn.get("id") or event.get("turn_id") or turn_id
        if event_type in {"agent.session.turn.failed", "agent.session.failed"}:
            detail = turn.get("error") or event.get("error") or "Agent Session failed"
            raise ResearchProviderFailure(
                ResearchRunStatus.RESEARCH_PROVIDER_ERROR, f"OpenAI Agent Session failed: {detail}"
            )
        if event_type == "agent.session.turn.cancelled":
            raise ResearchProviderFailure(
                ResearchRunStatus.RESEARCH_PROVIDER_ERROR, "OpenAI Agent Session was cancelled"
            )
        if event_type == "agent.session.turn.output_text.done" and event.get("text") is not None:
            text = event["text"]
            cited_urls.update(_citation_urls([event]))
        if event_type == "agent.session.turn.item.done":
            item_id = item.get("id") or f"item:{len(tool_ids)}"
            item_type = item.get("type", "")
            if item_type.endswith("_call") or item_type in {"function_call", "mcp_call"}:
                tool_ids.add(item_id)
            if item_type in {"web_search_call", "web_search"}:
                search_ids.add(item_id)
                cited_urls.update(_source_urls(item))
            for part in item.get("content") or ():
                if part.get("type") == "output_text" and part.get("text") is not None:
                    text = part["text"]
            cited_urls.update(_citation_urls(item.get("content")))
        if event_type == "agent.session.turn.completed":
            completed = True
            usage = turn.get("usage") or event.get("usage") or usage
    if not completed:
        raise ResearchProviderFailure(
            ResearchRunStatus.RESEARCH_PROVIDER_ERROR,
            "OpenAI Agent Session stream ended without a completed turn",
        )
    if text is None:
        raise ValueError("missing agent output_text")
    if not session_id or not turn_id:
        raise ValueError("missing session or turn identifier")
    return {
        "session_id": session_id,
        "turn_id": turn_id,
        "agent_id": agent_id,
        "model": model,
        "usage": usage,
        "text": text,
        "cited_urls": cited_urls,
        "tool_calls": len(tool_ids),
        "search_calls": len(search_ids),
    }


def _extract_response(raw):
    text = None
    urls = set()
    search_calls = 0
    for item in raw.get("output", ()):
        if item.get("type") == "web_search_call":
            search_calls += 1
            for source in (item.get("action") or {}).get("sources", ()):
                if source.get("url"):
                    urls.add(source["url"])
        if item.get("type") == "message":
            for content in item.get("content", ()):
                if content.get("type") != "output_text":
                    continue
                text = content.get("text")
                for annotation in content.get("annotations", ()):
                    if annotation.get("type") == "url_citation" and annotation.get("url"):
                        urls.add(annotation["url"])
    if text is None:
        raise ValueError("missing output_text")
    return text, urls, search_calls


def _response_schema():
    source = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "url",
            "publisher",
            "source_type",
            "reliability_tier",
            "published_at",
            "retrieved_at",
            "title",
            "canonical_url",
            "derived_from",
        ],
        "properties": {
            "url": {"type": "string"},
            "publisher": {"type": ["string", "null"]},
            "source_type": {"type": "string", "enum": list(SOURCE_TYPES)},
            "reliability_tier": {"type": "string", "enum": ["A", "B", "C", "D"]},
            "published_at": {"type": ["string", "null"]},
            "retrieved_at": {"type": "string"},
            "title": {"type": ["string", "null"]},
            "canonical_url": {"type": ["string", "null"]},
            "derived_from": {"type": "array", "items": {"type": "string"}},
        },
    }
    finding = {
        "type": "object",
        "additionalProperties": False,
        "required": ["field", "status", "value", "confidence", "sources", "conflicts", "notes"],
        "properties": {
            "field": {"type": "string"},
            "status": {"type": "string", "enum": [status.value for status in ResearchFindingStatus]},
            "value": {"type": ["object", "array", "string", "number", "boolean", "null"]},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "sources": {"type": "array", "items": source},
            "conflicts": {"type": "array", "items": {"type": "string"}},
            "notes": {"type": ["string", "null"]},
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["findings"],
        "properties": {"findings": {"type": "array", "items": finding}},
    }
