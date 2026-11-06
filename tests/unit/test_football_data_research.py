import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from better.config import Settings
from better.domain.football_data_research import (
    FootballDataFinding,
    FootballDataResearchRequest,
    FootballDataResearchResult,
    FootballDataRunStatus,
    FootballDataSource,
)
from better.domain.research import (
    ResearchFinding,
    ResearchProviderFailure,
    ResearchResult,
    ResearchRunStatus,
    ResearchSource,
)
from better.observability.metrics import Metrics
from better.providers.fake import make_demo_bundle
from better.providers.research import (
    FakeFootballDataResearchProvider,
    FakeResearchProvider,
    OpenAIExternalResearchProvider,
    OpenAIFootballDataResearchProvider,
    _validate_football_data_findings,
)
from better.services.application import make_application
from better.services.external_research import (
    ExternalResearchConfiguration,
    ExternalResearchService,
    ResearchFieldPolicy,
)
from better.services.football_data_research import (
    FootballDataResearchConfiguration,
    FootballDataResearchService,
    normalize_football_data,
)
from better.services.local_environment import environment_value
from better.services.research_enrichment import build_enriched_snapshot
from better.services.research_routing import (
    EXTERNAL_RESEARCH_FIELDS,
    FOOTBALL_DATA_RESEARCH_FIELDS,
    route_research_field,
)
from better.storage.repository import Repository

NOW = datetime.now(UTC)


def request(fields=("recent_form",)):
    return FootballDataResearchRequest(
        request_id="fd-1",
        match_id="match-1",
        home_team="Inter",
        away_team="Milan",
        competition="Serie A",
        season="2026/27",
        kickoff=NOW + timedelta(days=1),
        as_of=NOW,
        requested_fields=fields,
        requested_at=NOW,
    )


def source(url="https://www.legaseriea.it/statistiche"):
    return FootballDataSource(
        url=url,
        publisher="Lega Serie A",
        source_type="OFFICIAL_COMPETITION",
        source_tier="A",
        retrieved_at=NOW,
        updated_at=NOW - timedelta(minutes=10),
        competition="Serie A",
        season="2026/27",
        sample_size=10,
        source_family="lega-serie-a",
        lineage=(url,),
    )


def raw_matches(prefix, *, old=False):
    start = 500 if old else 1
    return [
        {
            "date": (NOW - timedelta(days=start + index * 7)).isoformat(),
            "opponent": f"{prefix} Opponent {index}",
            "venue": "HOME" if index % 2 == 0 else "AWAY",
            "goals_for": 2 if index % 3 else 0,
            "goals_against": 1 if index % 2 else 0,
            "competition": "Serie A",
        }
        for index in range(10)
    ]


def football_result(match, *, home=None, away=None):
    finding = FootballDataFinding(
        field="recent_form",
        status="FOUND",
        value={"home": home or raw_matches("Home"), "away": away or raw_matches("Away")},
        confidence=0.95,
        sources=(source(),),
    )
    return FootballDataResearchResult(
        request_id="fd-1",
        match_id=match.match_id,
        as_of=NOW,
        findings=(finding,),
        requested_fields=("recent_form",),
        provider="fake_football_data_research",
        status="OK",
        completed_at=NOW,
    )


def test_routing_is_explicit_and_scopes_never_overlap():
    assert not (EXTERNAL_RESEARCH_FIELDS & FOOTBALL_DATA_RESEARCH_FIELDS)
    assert route_research_field("injuries") == "external_research"
    assert route_research_field("weather") == "external_research"
    assert route_research_field("league_table") == "football_data_research"
    assert route_research_field("shots") == "football_data_research"
    assert route_research_field("xg") is None


def test_competition_scope_is_italian_and_does_not_match_brasileirao():
    service = FootballDataResearchService(
        FootballDataResearchConfiguration(), None, repository=None, metrics=Metrics()
    )
    assert service.supports("Serie A")
    assert service.supports("Serie C Girone A")
    assert not service.supports("Brasileirao Serie A")


def test_same_api_key_is_used_with_distinct_saved_agent_ids():
    external = OpenAIExternalResearchProvider(
        api_key="same-secret", agent_id="agent_external", client=object()
    )
    football = OpenAIFootballDataResearchProvider(
        api_key="same-secret", agent_id="agent_data", client=object()
    )
    assert external.agent_id != football.agent_id
    assert external._api_key == football._api_key


def test_application_rejects_same_saved_agent_for_two_research_roles():
    repository = Repository("sqlite:///:memory:")
    external_provider = FakeResearchProvider()
    football_provider = FakeFootballDataResearchProvider()
    external_provider.agent_id = "agent_same"
    football_provider.agent_id = "agent_same"
    external = ExternalResearchService(
        ExternalResearchConfiguration(enabled=True),
        external_provider,
        repository=repository,
        metrics=Metrics(),
    )
    football = FootballDataResearchService(
        FootballDataResearchConfiguration(enabled=True),
        football_provider,
        repository=repository,
        metrics=Metrics(),
    )
    with pytest.raises(ValueError, match="two distinct saved agents"):
        make_application(
            settings=Settings(),
            providers=make_demo_bundle("bet", as_of=NOW),
            repository=repository,
            external_research=external,
            football_data_research=football,
        )
    repository.close()


async def test_valid_football_data_saved_agent_session():
    citation = source().url
    finding = FootballDataFinding(
        field="recent_form",
        status="FOUND",
        value={"home": raw_matches("Home"), "away": raw_matches("Away")},
        confidence=0.9,
        sources=(source(),),
    )
    text = json.dumps({"findings": [finding.model_dump(mode="json")]})
    events = [
        {
            "type": "agent.session.created",
            "session": {"id": "sess_fd", "agent": {"id": "agent_data", "model": "saved-model"}},
        },
        {
            "type": "agent.session.turn.item.done",
            "item": {"id": "search-1", "type": "web_search_call", "action": {"sources": [{"url": citation}]}},
        },
        {
            "type": "agent.session.turn.item.done",
            "item": {
                "id": "message-1",
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": text,
                        "annotations": [{"type": "url_citation", "url": citation}],
                    }
                ],
            },
        },
        {
            "type": "agent.session.turn.completed",
            "turn": {
                "id": "turn_fd",
                "agent_id": "agent_data",
                "usage": {"input_tokens": 100, "output_tokens": 50},
            },
        },
    ]
    body = "".join(f"data: {json.dumps(event)}\n\n" for event in events) + "data: [DONE]\n\n"
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, text=body)))
    provider = OpenAIFootballDataResearchProvider(
        api_key="same-secret", agent_id="agent_data", client=client, max_search_calls=3
    )
    result = await provider.research(request())
    assert result.status == FootballDataRunStatus.OK
    assert result.session_id == "sess_fd" and result.search_calls == 1
    assert result.input_tokens == 100 and result.output_tokens == 50
    await client.aclose()


def test_wikipedia_is_fallback_only_and_cannot_source_performance_data():
    finding = FootballDataFinding(
        field="recent_form",
        status="FOUND",
        value={"home": raw_matches("Home"), "away": raw_matches("Away")},
        confidence=0.5,
        sources=(source("https://it.wikipedia.org/wiki/Inter"),),
    )
    normalized = _validate_football_data_findings(
        json.dumps({"findings": [finding.model_dump(mode="json")]}),
        {"https://it.wikipedia.org/wiki/Inter"},
        request(),
    )
    assert normalized[0].status.value == "INSUFFICIENT_EVIDENCE"


def test_raw_last_ten_are_normalized_and_system_zero_metrics_are_derived():
    match = make_demo_bundle("bet", as_of=NOW).fixtures.match
    result = football_result(match)
    snapshot = normalize_football_data(match, result)
    assert snapshot.home.sample_size == 10
    assert snapshot.home.xg_per_match is None and snapshot.home.xga_per_match is None
    last_five = snapshot.home.segments["last_5"]
    assert last_five["matches"] == 5
    assert last_five["wins"] + last_five["draws"] + last_five["losses"] == 5
    assert 0 <= last_five["btts_rate"] <= 1
    assert 0 <= last_five["over_2_5_rate"] <= 1
    assert snapshot.provenance[0].url and "season=2026/27" in snapshot.provenance[0].notes


def test_old_matches_are_ignored_and_cannot_create_stats():
    match = make_demo_bundle("bet", as_of=NOW).fixtures.match
    assert normalize_football_data(match, football_result(match, home=raw_matches("Home", old=True))) is None


def test_recent_form_found_requires_ten_matches_for_each_requested_team():
    finding = FootballDataFinding(
        field="recent_form",
        status="FOUND",
        value={"home": raw_matches("Home"), "away": raw_matches("Away")[:3]},
        confidence=0.9,
        sources=(source(),),
    )
    actual = _validate_football_data_findings(
        json.dumps({"findings": [finding.model_dump(mode="json")]}),
        {source().url},
        request(),
    )
    assert actual[0].status.value == "INSUFFICIENT_EVIDENCE"
    assert actual[0].value is None


def test_enriched_snapshot_has_three_separate_sections():
    bundle = make_demo_bundle("bet", as_of=NOW)
    match = bundle.fixtures.match
    result = football_result(match)
    enriched = build_enriched_snapshot(match, None, football_result=result, odds=bundle.odds.odds)
    assert enriched.structured_market_data
    assert enriched.football_data_research["provider"] == "fake_football_data_research"
    assert enriched.external_research is None


def test_failure_statuses_are_namespaced():
    assert FootballDataRunStatus.FOOTBALL_DATA_RESEARCH_ERROR.value.startswith("FOOTBALL_DATA")
    error = ResearchProviderFailure(ResearchRunStatus.RESEARCH_TIMEOUT, "timeout")
    assert error.status == ResearchRunStatus.RESEARCH_TIMEOUT


def test_openai_configuration_can_be_read_from_local_env(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_FOOTBALL_DATA_AGENT_ID", raising=False)
    path = tmp_path / ".env"
    path.write_text("OPENAI_FOOTBALL_DATA_AGENT_ID=agent_from_file\n", encoding="utf-8")
    assert environment_value("OPENAI_FOOTBALL_DATA_AGENT_ID", path) == "agent_from_file"


async def test_one_research_agent_failure_does_not_stop_the_other():
    match = make_demo_bundle("bet", as_of=NOW).fixtures.match
    repository = Repository("sqlite:///:memory:")
    metrics = Metrics()
    football = FootballDataResearchService(
        FootballDataResearchConfiguration(enabled=True, supported_competitions=(match.competition,)),
        FakeFootballDataResearchProvider(
            error=ResearchProviderFailure(ResearchRunStatus.RESEARCH_TIMEOUT, "timeout")
        ),
        repository=repository,
        metrics=metrics,
        clock=lambda: NOW,
    )

    def external_result(research_request):
        cited = ResearchSource(
            url="https://club.example/news",
            publisher="Club",
            source_type="OFFICIAL_CLUB",
            reliability_tier="A",
            published_at=NOW - timedelta(hours=1),
            retrieved_at=NOW,
        )
        return ResearchResult(
            request_id=research_request.request_id,
            match_id=match.match_id,
            as_of=research_request.as_of,
            findings=(
                ResearchFinding(
                    field="injuries",
                    status="REPORTED",
                    value={"summary": "No new update"},
                    confidence=0.8,
                    sources=(cited,),
                ),
            ),
            searched_fields=("injuries",),
            provider="fake_external",
            model="fake",
            status="OK",
            completed_at=NOW,
        )

    external = ExternalResearchService(
        ExternalResearchConfiguration(enabled=True, fields={"injuries": ResearchFieldPolicy(enabled=True)}),
        FakeResearchProvider(result=external_result),
        repository=repository,
        metrics=metrics,
        clock=lambda: NOW,
    )
    failed = await football.research(match, NOW)
    succeeded = await external.research_missing(
        match, as_of=NOW, missing_fields=("injuries",), source_state_version="v1"
    )
    assert failed.status == FootballDataRunStatus.FOOTBALL_DATA_RESEARCH_TIMEOUT
    assert succeeded.status.value == "OK" and succeeded.findings
    repository.close()


async def test_football_data_cache_is_reused_after_service_restart():
    match = make_demo_bundle("bet", as_of=NOW).fixtures.match
    repository = Repository("sqlite:///:memory:")
    provider = FakeFootballDataResearchProvider(
        result=lambda req: football_result(match).model_copy(
            update={"request_id": req.request_id, "as_of": req.as_of}
        )
    )
    settings = FootballDataResearchConfiguration(
        enabled=True, supported_competitions=(match.competition,)
    )
    first = FootballDataResearchService(
        settings, provider, repository=repository, metrics=Metrics(), clock=lambda: NOW
    )
    await first.research(match, NOW)
    restarted = FootballDataResearchService(
        settings, provider, repository=repository, metrics=Metrics(), clock=lambda: NOW
    )
    cached = await restarted.research(match, NOW)

    assert cached.cached is True
    assert len(provider.calls) == 1
    repository.close()


@pytest.mark.parametrize("missing_stats", [False, True])
@pytest.mark.parametrize("lookup_only", [False, True])
async def test_normal_analysis_researches_statistics_only_when_missing(missing_stats, lookup_only, monkeypatch):
    from unittest.mock import AsyncMock

    from better.domain.enums import ErrorCode
    from better.domain.errors import BetterError
    from better.domain.schemas import AnalysisRequest

    now = datetime.now(UTC)
    bundle = make_demo_bundle("bet", as_of=now)
    match = bundle.fixtures.match
    if missing_stats:
        monkeypatch.setattr(bundle.stats, "get_stats", AsyncMock(side_effect=BetterError(ErrorCode.MISSING_DATA, "missing")))
    repository = Repository("sqlite:///:memory:")
    provider = FakeFootballDataResearchProvider(error=ResearchProviderFailure(ResearchRunStatus.RESEARCH_TIMEOUT, "timeout"))
    football = FootballDataResearchService(
        FootballDataResearchConfiguration(enabled=True, supported_competitions=(match.competition,)),
        provider, repository=repository, metrics=Metrics())
    external = ExternalResearchService(ExternalResearchConfiguration(enabled=False), None,
                                      repository=repository, metrics=Metrics())
    app = make_application(settings=Settings(), providers=bundle, repository=repository,
                           football_data_research=football, external_research=external)
    try:
        if lookup_only:
            from better.services.match_research import MatchResearchService
            await MatchResearchService(app).inspect(match.match_id)
        else:
            await app.analyze(AnalysisRequest(match_id=match.match_id))
        assert len(provider.calls) == int(missing_stats)
    finally:
        await app.close()
