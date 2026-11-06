import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
from fastapi.testclient import TestClient

from better.api.app import create_app
from better.config import Settings
from better.observability.metrics import Metrics
from better.providers.fake import make_demo_bundle
from better.providers.research import OpenAIResearchProvider
from better.services.application import UnconfiguredProvider, make_application
from better.services.external_research import (
    ExternalResearchConfiguration,
    ExternalResearchService,
    ResearchFieldPolicy,
)
from better.storage.repository import Repository


def test_selected_match_news_research_through_api_with_mock_openai_transport(monkeypatch):
    from better.providers.research_sources import OFFICIAL_DOMAINS
    monkeypatch.setitem(OFFICIAL_DOMAINS, "club.example", "OFFICIAL_CLUB")
    now = datetime.now(UTC)
    citation = "https://club.example/official-team-news"

    def handle(request):
        sent = json.loads(request.content)
        research_request = json.loads(sent["input"][1]["content"])
        published = datetime.fromisoformat(research_request["as_of"]) - timedelta(hours=1)
        finding = {
            "field": "news",
            "status": "FOUND",
            "value": {"summary": "Official squad availability update"},
            "confidence": 0.95,
            "sources": [
                {
                    "url": citation,
                    "publisher": research_request["home_team"],
                    "source_type": "OFFICIAL_CLUB",
                    "reliability_tier": "A",
                    "published_at": published.isoformat(),
                    "retrieved_at": research_request["requested_at"],
                    "title": "Official team news",
                    "canonical_url": citation,
                    "derived_from": [],
                }
            ],
            "conflicts": [],
            "notes": "Official source available before the cutoff",
        }
        return httpx.Response(
            200,
            json={
                "id": "resp_integration",
                "model": "gpt-test",
                "usage": {"input_tokens": 120, "output_tokens": 40},
                "output": [
                    {"type": "web_search_call", "action": {"sources": [{"url": citation}]}},
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps({"findings": [finding]}),
                                "annotations": [{"type": "url_citation", "url": citation}],
                            }
                        ],
                    },
                ],
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    provider = OpenAIResearchProvider(
        api_key="test-secret", transport="responses_api", model="gpt-test", client=client
    )
    repository = Repository("sqlite:///:memory:")
    metrics = Metrics()
    external = ExternalResearchService(
        ExternalResearchConfiguration(enabled=True, fields={"news": ResearchFieldPolicy(enabled=True)}),
        provider,
        repository=repository,
        metrics=metrics,
    )
    providers = replace(make_demo_bundle("bet", as_of=now), news=UnconfiguredProvider())
    runtime = make_application(
        settings=Settings(),
        providers=providers,
        repository=repository,
        external_research=external,
    )

    with TestClient(create_app(runtime)) as api:
        response = api.get("/matches/demo-match/research")
        assert response.status_code == 200
        payload = response.json()
        assert payload["external_research"]["status"] == "OK"
        assert payload["external_research"]["coverage"] == {
            "before": 1,
            "after": 0,
            "found": 1,
        }
        assert payload["external_research"]["findings"][0]["sources"][0]["url"] == citation
        assert payload["analysis_started"] is False

    assert len(repository.list_records("research_requests")) == 1
    assert len(repository.list_records("research_results")) == 1
    assert len(repository.list_records("research_findings")) == 1
    stored_source = repository.list_records("research_sources")[0]
    assert stored_source["provider"] == "openai_web_search"
    assert stored_source["provenance"]["url"] == citation
    assert repository.list_records("analysis_runs") == []
    asyncio.run(runtime.close())
    asyncio.run(client.aclose())
