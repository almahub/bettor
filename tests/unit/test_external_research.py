import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from pydantic import ValidationError

from better.domain.enums import ReliabilityTier
from better.domain.research import (
    ResearchFinding,
    ResearchFindingStatus,
    ResearchProviderFailure,
    ResearchRequest,
    ResearchResult,
    ResearchRunStatus,
    ResearchSource,
)
from better.domain.schemas import AnalysisRequest
from better.observability.metrics import Metrics
from better.providers.research import FakeResearchProvider, OpenAIResearchProvider, normalize_finding
from better.services.application import make_application
from better.services.external_research import (
    ExternalResearchConfiguration,
    ExternalResearchService,
    build_external_research,
    qualitative_source_state,
)
from better.storage.repository import Repository

NOW = datetime(2026, 10, 1, 12, tzinfo=UTC)


def test_qualitative_source_state_depends_only_on_match_identity():
    class Match:
        def model_dump(self, mode):
            assert mode == "json"
            return {
                "match_id": "match-1",
                "home_team": "Home",
                "away_team": "Away",
                "kickoff": "2026-10-02T12:00:00Z",
            }

    first = qualitative_source_state(Match())
    # Live quote refreshes are deliberately absent from this state version.
    assert qualitative_source_state(Match()) == first


@pytest.fixture(autouse=True)
def verified_test_source(monkeypatch):
    from better.providers.research_sources import OFFICIAL_DOMAINS
    monkeypatch.setitem(OFFICIAL_DOMAINS, "club.example", "OFFICIAL_CLUB")


def request(**changes):
    values = {
        "request_id": "request-1",
        "match_id": "match-1",
        "home_team": "Home FC",
        "away_team": "Away FC",
        "competition": "League",
        "kickoff": NOW + timedelta(hours=5),
        "as_of": NOW,
        "missing_fields": ("news",),
        "source_state_version": "abc123",
        "requested_at": NOW,
    }
    values.update(changes)
    return ResearchRequest(**values)


def source(**changes):
    values = {
        "url": "https://club.example/news/team-update?tracking=1",
        "publisher": "Home FC",
        "source_type": "OFFICIAL_CLUB",
        "reliability_tier": "A",
        "published_at": NOW - timedelta(hours=1),
        "retrieved_at": NOW,
        "title": "Team update",
    }
    values.update(changes)
    return ResearchSource(**values)


def finding(**changes):
    values = {
        "field": "news",
        "status": "FOUND",
        "value": {"summary": "One player returned to training"},
        "confidence": 0.91,
        "sources": (source(),),
    }
    values.update(changes)
    return ResearchFinding(**values)


def result_for(req):
    return ResearchResult(
        request_id=req.request_id,
        match_id=req.match_id,
        as_of=req.as_of,
        findings=(finding(),),
        searched_fields=("news",),
        unresolved_fields=(),
        provider="fake_external_research",
        model="fake",
        response_id="response-1",
        search_calls=1,
        request_count=1,
        input_tokens=100,
        output_tokens=50,
        estimated_cost=Decimal("0.011"),
        completed_at=NOW,
    )


def agent_stream(*, output=None, completed=True, include_citations=True):
    output = output or {"findings": [finding().model_dump(mode="json")]}
    events = [
        {
            "type": "agent.session.created",
            "session": {
                "id": "sess_123",
                "agent": {
                    "id": "agent_saved_123",
                    "name": "better-external-research-v1",
                    "model": "saved-agent-model",
                },
            },
        },
        {
            "type": "agent.session.turn.item.done",
            "session_id": "sess_123",
            "turn_id": "turn_123",
            "item": {
                "id": "search_1",
                "type": "web_search_call",
                "action": {
                    "sources": (
                        [{"url": "https://club.example/news/team-update"}] if include_citations else []
                    )
                },
            },
        },
        {
            "type": "agent.session.turn.item.done",
            "session_id": "sess_123",
            "turn_id": "turn_123",
            "item": {
                "id": "msg_1",
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": json.dumps(output),
                        "annotations": [
                            {
                                "type": "url_citation",
                                "url": "https://club.example/news/team-update",
                            }
                        ],
                    }
                ],
            },
        },
    ]
    if completed:
        events.append(
            {
                "type": "agent.session.turn.completed",
                "session_id": "sess_123",
                "turn": {
                    "id": "turn_123",
                    "agent_id": "agent_saved_123",
                    "usage": {"input_tokens": 321, "output_tokens": 123},
                },
            }
        )
    return "".join(f"data: {json.dumps(event)}\n\n" for event in events) + "data: [DONE]\n\n"


def test_contracts_reject_unproven_found_and_post_kickoff_request():
    with pytest.raises(ValidationError, match="FOUND requires"):
        finding(sources=())
    with pytest.raises(ValidationError, match="before kickoff"):
        request(as_of=NOW + timedelta(hours=6))


def test_temporal_filter_tiering_and_source_deduplication():
    derived = source(
        url="https://club.example/news/team-update?copy=2",
        source_type="AGGREGATOR",
        reliability_tier="A",
    )
    normalized = normalize_finding(finding(sources=(source(), derived)), request())
    assert len(normalized.sources) == 1
    assert normalized.sources[0].reliability_tier == ReliabilityTier.A
    future = normalize_finding(
        finding(
            sources=(
                source(
                    published_at=NOW + timedelta(minutes=1),
                    retrieved_at=NOW + timedelta(minutes=2),
                ),
            )
        ),
        request(),
    )
    assert future.status == ResearchFindingStatus.STALE
    assert future.value is None
    weak = normalize_finding(
        finding(sources=(source(source_type="AGGREGATOR", reliability_tier="A"),)), request()
    )
    assert weak.status == ResearchFindingStatus.INSUFFICIENT_EVIDENCE


async def test_openai_responses_web_search_mapping_and_cost():
    citation = "https://club.example/news/team-update"
    output = {
        "findings": [finding().model_dump(mode="json")],
    }
    raw = {
        "id": "resp_123",
        "model": "gpt-5.6-luna-2026-09-01",
        "usage": {"input_tokens": 1000, "output_tokens": 200},
        "output": [
            {"type": "web_search_call", "action": {"sources": [{"url": citation}]}},
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": json.dumps(output),
                        "annotations": [{"type": "url_citation", "url": citation}],
                    }
                ],
            },
        ],
    }

    async def handle(http_request):
        assert http_request.url.path == "/v1/responses"
        assert http_request.headers["authorization"] == "Bearer secret"
        sent = json.loads(http_request.content)
        assert sent["tools"] == [{"type": "web_search", "search_context_size": "low"}]
        assert sent["text"]["format"]["strict"] is True
        assert sent["tool_choice"] == "required"
        assert sent["max_tool_calls"] == 2
        return httpx.Response(200, json=raw)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    provider = OpenAIResearchProvider(
        api_key="secret",
        transport="responses_api",
        model="gpt-5.6-luna",
        client=client,
        input_cost_per_million=Decimal("0.20"),
        output_cost_per_million=Decimal("1.20"),
        web_search_cost_per_call=Decimal("0.01"),
    )
    actual = await provider.research(request())
    assert actual.response_id == "resp_123"
    assert actual.search_calls == 1
    assert actual.estimated_cost == Decimal("0.01044")
    assert actual.findings[0].sources[0].canonical_url == citation
    await client.aclose()


async def test_saved_agent_session_uses_agent_without_configuration_overrides():
    async def handle(http_request):
        assert http_request.url.path == "/v1/agents/sessions"
        assert http_request.headers["openai-beta"] == "agents=v1"
        sent = json.loads(http_request.content)
        assert set(sent) == {"agent_id", "environment", "input", "stream"}
        assert sent["agent_id"] == "agent_saved_123"
        assert "agent" not in sent
        task = json.loads(sent["input"])
        assert set(task) == {
            "match_identity",
            "competition",
            "kickoff",
            "as_of",
            "missing_fields",
            "max_search_budget",
            "source_contract",
        }
        assert task["missing_fields"] == ["news"]
        return httpx.Response(200, text=agent_stream())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    provider = OpenAIResearchProvider(
        api_key="secret",
        transport="agents_api",
        agent_id="agent_saved_123",
        client=client,
        max_search_calls=2,
        max_output_tokens=2000,
        max_cost_per_match=Decimal("0.25"),
        input_cost_per_million=Decimal("1"),
        output_cost_per_million=Decimal("2"),
        web_search_cost_per_call=Decimal("0.01"),
    )
    actual = await provider.research(request())
    assert actual.transport == "agents_api"
    assert actual.session_id == "sess_123"
    assert actual.response_id == "turn_123"
    assert actual.agent_id == "agent_saved_123"
    assert actual.model == "saved-agent-model"
    assert actual.search_calls == 1 and actual.tool_calls == 1
    assert actual.input_tokens == 321 and actual.output_tokens == 123
    assert actual.estimated_cost == Decimal("0.010567")
    assert actual.findings[0].sources[0].url == "https://club.example/news/team-update?tracking=1"
    assert actual.duration_ms is not None
    await client.aclose()


async def test_agents_api_accepts_structured_sources_when_web_search_has_no_annotations():
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, text=agent_stream(include_citations=False))
        )
    )
    provider = OpenAIResearchProvider(
        api_key="secret", transport="agents_api", agent_id="agent_saved_123", client=client
    )
    actual = await provider.research(request())
    assert actual.status == ResearchRunStatus.OK
    assert actual.search_calls == 1
    assert actual.findings[0].sources[0].url.startswith("https://club.example/")
    await client.aclose()


async def test_agents_api_normalizes_iso_source_dates_without_timezone():
    raw_finding = finding().model_dump(mode="json")
    raw_finding["sources"][0]["published_at"] = "2026-10-01"
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, text=agent_stream(output={"findings": [raw_finding]}))
        )
    )
    provider = OpenAIResearchProvider(
        api_key="secret", transport="agents_api", agent_id="agent_saved_123", client=client
    )
    actual = await provider.research(request())
    assert actual.findings[0].sources[0].published_at == datetime(2026, 10, 1, tzinfo=UTC)
    await client.aclose()


async def test_agents_api_requires_agent_id_without_network_call():
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: pytest.fail("transport must not be called"))
    )
    provider = OpenAIResearchProvider(api_key="secret", transport="agents_api", client=client)
    with pytest.raises(ResearchProviderFailure, match="OPENAI_RESEARCH_AGENT_ID") as caught:
        await provider.research(request())
    assert caught.value.status == ResearchRunStatus.RESEARCH_PROVIDER_ERROR
    await client.aclose()


@pytest.mark.parametrize(
    "status_code,expected", [(500, "RESEARCH_PROVIDER_ERROR"), (429, "RESEARCH_RATE_LIMIT")]
)
async def test_agents_api_session_creation_failure(status_code, expected):
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(status_code, text="failure"))
    )
    provider = OpenAIResearchProvider(
        api_key="secret", transport="agents_api", agent_id="agent_saved_123", client=client
    )
    with pytest.raises(ResearchProviderFailure) as caught:
        await provider.research(request())
    assert caught.value.status.value == expected
    await client.aclose()


async def test_invalid_api_key_has_an_actionable_error_for_both_transports():
    for transport in ("agents_api", "responses_api"):
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(401, json={"error": {}}))
        )
        provider = OpenAIResearchProvider(
            api_key="invalid",
            transport=transport,
            agent_id="agent_saved_123",
            model="fallback-model",
            client=client,
        )
        with pytest.raises(ResearchProviderFailure, match="OPENAI_API_KEY non valida o revocata"):
            await provider.research(request())
        await client.aclose()


@pytest.mark.parametrize('transport',['agents_api','responses_api'])
@pytest.mark.parametrize(('status','code','expected'),[(401,'expired_secret_key','OPENAI_KEY_EXPIRED'),(403,'insufficient_permissions','OPENAI_PERMISSION_ERROR')])
async def test_auth_causes_are_specific_and_never_echo_secret_response(transport,status,code,expected):
    secret='sk-proj-must-not-leak-in-errors'
    client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _:httpx.Response(status,json={
        'error':{'code':code,'message':'Rejected key '+secret}})))
    provider=OpenAIResearchProvider(api_key=secret,transport=transport,agent_id='agent_test',model='test-model',client=client)
    with pytest.raises(ResearchProviderFailure) as caught:
        await provider.research(request())
    assert expected in str(caught.value)
    assert secret not in str(caught.value)
    await client.aclose()


async def test_agents_api_invalid_schema_and_timeout():
    invalid = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text=agent_stream(output={"wrong": []})))
    )
    provider = OpenAIResearchProvider(
        api_key="secret", transport="agents_api", agent_id="agent_saved_123", client=invalid
    )
    with pytest.raises(ResearchProviderFailure) as caught:
        await provider.research(request())
    assert caught.value.status == ResearchRunStatus.RESEARCH_SCHEMA_ERROR
    await invalid.aclose()

    def timeout_handler(http_request):
        raise httpx.ReadTimeout("slow", request=http_request)

    timed_out = httpx.AsyncClient(transport=httpx.MockTransport(timeout_handler))
    provider = OpenAIResearchProvider(
        api_key="secret", transport="agents_api", agent_id="agent_saved_123", client=timed_out
    )
    with pytest.raises(ResearchProviderFailure) as caught:
        await provider.research(request())
    assert caught.value.status == ResearchRunStatus.RESEARCH_TIMEOUT
    await timed_out.aclose()


async def test_agents_api_can_fall_back_to_responses_api():
    response_raw = {
        "id": "resp_fallback",
        "model": "response-model",
        "usage": {"input_tokens": 20, "output_tokens": 10},
        "output": [
            {
                "type": "web_search_call",
                "action": {"sources": [{"url": "https://club.example/news/team-update"}]},
            },
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": json.dumps({"findings": [finding().model_dump(mode="json")]}),
                        "annotations": [
                            {
                                "type": "url_citation",
                                "url": "https://club.example/news/team-update",
                            }
                        ],
                    }
                ],
            },
        ],
    }
    paths = []

    def handle(http_request):
        paths.append(http_request.url.path)
        if http_request.url.path == "/v1/agents/sessions":
            return httpx.Response(500)
        return httpx.Response(200, json=response_raw)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    provider = OpenAIResearchProvider(
        api_key="secret",
        transport="agents_api",
        agent_id="agent_saved_123",
        model="response-model",
        fallback_transport="responses_api",
        client=client,
    )
    actual = await provider.research(request())
    assert paths == ["/v1/agents/sessions", "/v1/responses"]
    assert actual.transport == "responses_api"
    assert actual.request_count == 2
    await client.aclose()


async def test_openai_rejects_uncited_source_and_maps_rate_limit():
    uncited = {
        "id": "resp_bad",
        "model": "test",
        "output": [
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": json.dumps({"findings": [finding().model_dump(mode="json")]}),
                        "annotations": [],
                    }
                ],
            }
        ],
    }
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=uncited)))
    provider = OpenAIResearchProvider(
        api_key="secret", transport="responses_api", model="test", client=client
    )
    with pytest.raises(ResearchProviderFailure) as caught:
        await provider.research(request())
    assert caught.value.status == ResearchRunStatus.RESEARCH_SCHEMA_ERROR
    await client.aclose()

    limited = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(429)))
    provider = OpenAIResearchProvider(
        api_key="secret", transport="responses_api", model="test", client=limited
    )
    with pytest.raises(ResearchProviderFailure) as caught:
        await provider.research(request())
    assert caught.value.status == ResearchRunStatus.RESEARCH_RATE_LIMIT
    await limited.aclose()

    def timeout_handler(http_request):
        raise httpx.ReadTimeout("slow", request=http_request)

    timed_out = httpx.AsyncClient(transport=httpx.MockTransport(timeout_handler))
    provider = OpenAIResearchProvider(
        api_key="secret", transport="responses_api", model="test", client=timed_out
    )
    with pytest.raises(ResearchProviderFailure) as caught:
        await provider.research(request())
    assert caught.value.status == ResearchRunStatus.RESEARCH_TIMEOUT
    await timed_out.aclose()


async def test_openai_fallback_model_is_used_after_schema_error():
    citation = "https://club.example/news/team-update"
    valid = {
        "id": "resp_fallback",
        "model": "fallback-model",
        "usage": {"input_tokens": 10, "output_tokens": 5},
        "output": [
            {"type": "web_search_call", "action": {"sources": [{"url": citation}]}},
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": json.dumps({"findings": [finding().model_dump(mode="json")]}),
                        "annotations": [{"type": "url_citation", "url": citation}],
                    }
                ],
            },
        ],
    }
    calls = []

    def handle(http_request):
        sent = json.loads(http_request.content)
        calls.append(sent["model"])
        return httpx.Response(200, json={"output": []} if len(calls) == 1 else valid)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    provider = OpenAIResearchProvider(
        api_key="secret",
        transport="responses_api",
        model="cheap-model",
        fallback_model="fallback-model",
        client=client,
    )
    actual = await provider.research(request())
    assert calls == ["cheap-model", "fallback-model"]
    assert actual.request_count == 2 and actual.model == "fallback-model"
    await client.aclose()


@pytest.mark.parametrize("status", ["NOT_FOUND", "CONFLICT", "INSUFFICIENT_EVIDENCE"])
def test_unresolved_finding_statuses_are_explicit(status):
    conflicts = ("Official and local report disagree",) if status == "CONFLICT" else ()
    actual = ResearchFinding(field="news", status=status, value=None, confidence=0.2, conflicts=conflicts)
    assert actual.status.value == status
    assert actual.value is None


async def test_service_cache_persistence_telemetry_and_budget():
    repository = Repository("sqlite:///:memory:")
    metrics = Metrics()
    provider = FakeResearchProvider(result=result_for)
    settings = ExternalResearchConfiguration(enabled=True)
    service = ExternalResearchService(
        settings, provider, repository=repository, metrics=metrics, clock=lambda: NOW
    )

    class Match:
        match_id = "match-1"
        home_team = "Home FC"
        away_team = "Away FC"
        competition = "League"
        kickoff = NOW + timedelta(hours=5)

    first = await service.research_missing(
        Match, as_of=NOW, missing_fields=("news",), source_state_version="v1"
    )
    second = await service.research_missing(
        Match, as_of=NOW, missing_fields=("news",), source_state_version="v1"
    )
    assert first.cached is False and second.cached is True
    assert len(provider.calls) == 1
    assert len(repository.list_records("research_requests")) == 1
    assert len(repository.list_records("research_results")) == 1
    assert len(repository.list_records("research_findings")) == 1
    assert len(repository.list_records("research_sources")) == 1
    telemetry = metrics.snapshot()["external_research"]
    assert telemetry["requests"] == 1 and telemetry["fields_found"] == 1
    assert telemetry["cache_hit_rate"] == 0.5

    exhausted = await service.research_missing(
        Match, as_of=NOW, missing_fields=("news",), source_state_version="v2"
    )
    assert exhausted.status == ResearchRunStatus.RESEARCH_BUDGET_EXCEEDED
    assert len(provider.calls) == 1
    repository.close()


async def test_successful_research_cache_is_reused_after_service_restart():
    repository = Repository("sqlite:///:memory:")
    provider = FakeResearchProvider(result=result_for)
    settings = ExternalResearchConfiguration(enabled=True)

    class Match:
        match_id = "match-1"
        home_team = "Home FC"
        away_team = "Away FC"
        competition = "League"
        kickoff = NOW + timedelta(hours=5)

    first = ExternalResearchService(
        settings, provider, repository=repository, metrics=Metrics(), clock=lambda: NOW
    )
    await first.research_missing(Match, as_of=NOW, missing_fields=("news",), source_state_version="v1")
    restarted = ExternalResearchService(
        settings, provider, repository=repository, metrics=Metrics(), clock=lambda: NOW
    )
    cached = await restarted.research_missing(
        Match, as_of=NOW, missing_fields=("news",), source_state_version="v1"
    )

    assert cached.cached is True
    assert len(provider.calls) == 1
    repository.close()


async def test_disabled_or_unconfigured_service_never_calls_provider():
    repository = Repository("sqlite:///:memory:")
    provider = FakeResearchProvider(result=result_for)
    service = ExternalResearchService(
        ExternalResearchConfiguration(enabled=False), provider, repository=repository, metrics=Metrics()
    )

    class Match:
        match_id = "match-1"
        home_team = "Home FC"
        away_team = "Away FC"
        competition = "League"
        kickoff = NOW + timedelta(hours=5)

    actual = await service.research_missing(
        Match, as_of=NOW, missing_fields=("news",), source_state_version="v1"
    )
    assert actual.status == ResearchRunStatus.DISABLED
    assert provider.calls == []
    repository.close()


async def test_pre_session_configuration_failure_does_not_lock_match_budget():
    repository = Repository("sqlite:///:memory:")
    failed = FakeResearchProvider(
        error=ResearchProviderFailure(ResearchRunStatus.EXTERNAL_RESEARCH_ERROR, "invalid API key")
    )
    service = ExternalResearchService(
        ExternalResearchConfiguration(enabled=True),
        failed,
        repository=repository,
        metrics=Metrics(),
        clock=lambda: NOW,
    )

    class Match:
        match_id = "match-auth-retry"
        home_team = "Home FC"
        away_team = "Away FC"
        competition = "League"
        kickoff = NOW + timedelta(hours=5)

    first = await service.research_missing(
        Match, as_of=NOW, missing_fields=("news",), source_state_version="bad-config"
    )
    assert first.status == ResearchRunStatus.EXTERNAL_RESEARCH_ERROR

    recovered = FakeResearchProvider(result=result_for)
    service.provider = recovered
    second = await service.research_missing(
        Match, as_of=NOW, missing_fields=("news",), source_state_version="fixed-config"
    )
    assert second.status == ResearchRunStatus.OK
    assert len(recovered.calls) == 1
    repository.close()


async def test_missing_api_key_degrades_without_startup_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("EXTERNAL_RESEARCH_ENABLED", "true")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    repository = Repository("sqlite:///:memory:")
    service = build_external_research(
        ExternalResearchConfiguration(), repository, Metrics(), clock=lambda: NOW
    )

    class Match:
        match_id = "match-1"
        home_team = "Home FC"
        away_team = "Away FC"
        competition = "League"
        kickoff = NOW + timedelta(hours=5)

    actual = await service.research_missing(
        Match, as_of=NOW, missing_fields=("news",), source_state_version="v1"
    )
    assert actual.status == ResearchRunStatus.EXTERNAL_RESEARCH_ERROR
    assert actual.error == "OPENAI_API_KEY is not configured"
    assert repository.list_records("research_requests") == []
    repository.close()


def test_builder_defaults_to_saved_agent_without_model_override(monkeypatch):
    monkeypatch.setenv("EXTERNAL_RESEARCH_ENABLED", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "test-secret")
    monkeypatch.setenv("OPENAI_RESEARCH_AGENT_ID", "agent_saved_123")
    monkeypatch.delenv("OPENAI_RESEARCH_TRANSPORT", raising=False)
    monkeypatch.delenv("OPENAI_RESEARCH_USE_AGENTS_API", raising=False)
    monkeypatch.delenv("OPENAI_RESEARCH_MODEL", raising=False)
    repository = Repository("sqlite:///:memory:")
    service = build_external_research(ExternalResearchConfiguration(), repository, Metrics())
    assert service.provider.transport == "agents_api"
    assert service.provider.agent_id == "agent_saved_123"
    assert service.provider.model is None
    asyncio.run(service.close())
    repository.close()


def test_use_agents_api_false_selects_responses_transport(monkeypatch):
    monkeypatch.setenv("EXTERNAL_RESEARCH_ENABLED", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "test-secret")
    monkeypatch.setenv("OPENAI_RESEARCH_USE_AGENTS_API", "false")
    monkeypatch.delenv("OPENAI_RESEARCH_TRANSPORT", raising=False)
    repository = Repository("sqlite:///:memory:")
    service = build_external_research(ExternalResearchConfiguration(), repository, Metrics())
    assert service.provider.transport == "responses_api"
    assert service.provider.model == "gpt-5.6-luna"
    asyncio.run(service.close())
    repository.close()


async def test_external_research_does_not_change_analysis_when_disabled_or_enabled_shadow():
    disabled = make_application(demo="bet", as_of=NOW, repository=Repository("sqlite:///:memory:"))
    shadow_repository = Repository("sqlite:///:memory:")
    shadow_service = ExternalResearchService(
        ExternalResearchConfiguration(enabled=True),
        FakeResearchProvider(result=result_for),
        repository=shadow_repository,
        metrics=Metrics(),
        clock=lambda: NOW,
    )
    enabled_shadow = make_application(
        demo="bet",
        as_of=NOW,
        repository=shadow_repository,
        external_research=shadow_service,
    )
    analysis_request = AnalysisRequest(match_id="demo-match", as_of=NOW)
    baseline = await disabled.analyze(analysis_request)
    observed = await enabled_shadow.analyze(analysis_request)
    assert observed.model_dump(mode="json") == baseline.model_dump(mode="json")
    assert shadow_service.provider.calls == []
    await disabled.close()
    await enabled_shadow.close()
