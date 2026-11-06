from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from better.config import Settings
from better.domain.research import (
    ResearchFinding,
    ResearchResult,
    ResearchRunStatus,
    ResearchSource,
)
from better.domain.schemas import AnalysisRequest
from better.observability.metrics import Metrics
from better.providers.fake import make_demo_bundle
from better.providers.research import FakeResearchProvider
from better.services.application import UnconfiguredProvider, make_application
from better.services.external_research import (
    ExternalResearchConfiguration,
    ExternalResearchService,
    ResearchFieldPolicy,
)
from better.services.provider_factory import SourceConfiguration
from better.services.research_enrichment import (
    build_enriched_snapshot,
    normalized_qualitative_snapshots,
)
from better.storage.repository import Repository

NOW = datetime.now(UTC)


@pytest.fixture
def live_clock(monkeypatch):
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)

    monkeypatch.setattr("better.orchestration.orchestrator.datetime", FixedDateTime)


def source(tier="A", kind="OFFICIAL_CLUB"):
    return ResearchSource(
        url="https://club.example/team-news",
        publisher="Example FC",
        source_type=kind,
        reliability_tier=tier,
        published_at=NOW - timedelta(hours=1),
        retrieved_at=NOW,
    )


def result(match, findings):
    unresolved = tuple(
        finding.field
        for finding in findings
        if finding.status.value in {"NOT_FOUND", "CONFLICT", "INSUFFICIENT_EVIDENCE", "STALE"}
    )
    return ResearchResult(
        request_id="research-1",
        match_id=match.match_id,
        as_of=NOW,
        findings=tuple(findings),
        searched_fields=tuple(finding.field for finding in findings),
        unresolved_fields=unresolved,
        provider="fake_external_research",
        model="saved-agent-model",
        session_id="session-1",
        request_count=1,
        completed_at=NOW,
    )


@pytest.mark.parametrize(
    "status",
    ["NOT_FOUND", "INSUFFICIENT_EVIDENCE", "STALE"],
)
def test_missing_and_no_source_fields_remain_explicit(status):
    finding = ResearchFinding(field="injuries", status=status, confidence=0)
    assert finding.value is None and finding.sources == ()
    with pytest.raises(ValidationError):
        ResearchFinding(field="injuries", status="REPORTED", value=["Player"], confidence=0.5)


def test_conflict_requires_details_and_never_becomes_a_value():
    finding = ResearchFinding(
        field="injuries",
        status="CONFLICT",
        confidence=0.2,
        conflicts=("Club says available; broadcaster says injured",),
    )
    assert finding.value is None and finding.conflicts


def test_all_fields_keep_field_level_lineage():
    match = make_demo_bundle("bet", as_of=NOW).fixtures.match
    fields = ExternalResearchConfiguration().fields
    findings = [
        ResearchFinding(
            field=name,
            status="PROBABLE" if name == "probable_lineups" else "REPORTED",
            value={"summary": name},
            confidence=0.8,
            sources=(source(),),
        )
        for name in fields
    ]
    enriched = build_enriched_snapshot(match, result(match, findings))
    assert set(enriched.fields) == set(fields)
    assert all(field.source_url and field.publisher and field.lineage for field in enriched.fields.values())


def test_official_and_probable_lineups_normalize_separately():
    match = make_demo_bundle("bet", as_of=NOW).fixtures.match
    players = {
        "home": [f"Home {number}" for number in range(1, 12)],
        "away": [f"Away {number}" for number in range(1, 12)],
    }
    confirmed = ResearchFinding(
        field="confirmed_lineups",
        status="CONFIRMED",
        value=players,
        confidence=1,
        sources=(source(),),
    )
    snapshots = normalized_qualitative_snapshots(
        match, build_enriched_snapshot(match, result(match, [confirmed]))
    )
    assert snapshots["lineups"].status == "CONFIRMED_LINEUP"

    probable = ResearchFinding(
        field="probable_lineups",
        status="PROBABLE",
        value=players,
        confidence=0.8,
        sources=(source(),),
    )
    snapshots = normalized_qualitative_snapshots(
        match, build_enriched_snapshot(match, result(match, [probable]))
    )
    assert snapshots["lineups"].status == "PROBABLE_LINEUP"


async def test_enriched_snapshot_feeds_agents_and_keeps_888_odds(live_clock):
    bundle = make_demo_bundle("bet", as_of=NOW)
    bundle = replace(bundle, news=UnconfiguredProvider(), lineups=UnconfiguredProvider())
    match = bundle.fixtures.match
    players = {
        "home": [f"Home {number}" for number in range(1, 12)],
        "away": [f"Away {number}" for number in range(1, 12)],
    }

    def fake_output(request):
        findings = (
            ResearchFinding(
                field="relevant_match_news",
                status="REPORTED",
                value="Manager described the match preparation",
                confidence=0.8,
                sources=(source(),),
            ),
            ResearchFinding(
                field="confirmed_lineups",
                status="CONFIRMED",
                value=players,
                confidence=1,
                sources=(source(),),
            ),
        )
        return result(match, findings).model_copy(
            update={"request_id": request.request_id, "as_of": request.as_of}
        )

    repository = Repository("sqlite:///:memory:")
    external = ExternalResearchService(
        ExternalResearchConfiguration(
            enabled=True,
            fields={
                "relevant_match_news": ResearchFieldPolicy(enabled=True),
                "confirmed_lineups": ResearchFieldPolicy(enabled=True),
            },
        ),
        FakeResearchProvider(result=fake_output),
        repository=repository,
        metrics=Metrics(),
    )
    runtime = make_application(
        settings=Settings(), providers=bundle, repository=repository, external_research=external
    )
    run = await runtime.analyze(AnalysisRequest(match_id=match.match_id))
    assert run.snapshot.odds is not None and run.snapshot.odds.quotes
    assert run.snapshot.enriched is not None
    assert run.results["news_intelligence"].status.value != "INSUFFICIENT_DATA"
    assert run.results["lineup_player_impact"].status.value != "INSUFFICIENT_DATA"
    assert run.results["tactical_analyst"].status.value != "INSUFFICIENT_DATA"
    assert run.results["devils_advocate"].status.value != "INSUFFICIENT_DATA"
    assert run.results["professional_bettor"].payload is not None
    await runtime.close()


async def test_configured_openai_auth_failure_replaces_unconfigured_news_error(live_clock):
    bundle = make_demo_bundle("bet", as_of=NOW)
    bundle = replace(bundle, news=UnconfiguredProvider())
    match = bundle.fixtures.match

    def auth_failure(request):
        return result(match, []).model_copy(
            update={
                "request_id": request.request_id,
                "as_of": request.as_of,
                "status": ResearchRunStatus.EXTERNAL_RESEARCH_ERROR,
                "error": "OPENAI_API_KEY non valida o revocata",
                "unresolved_fields": request.missing_fields,
            }
        )

    repository = Repository("sqlite:///:memory:")
    external = ExternalResearchService(
        ExternalResearchConfiguration(
            enabled=True,
            fields={"relevant_match_news": ResearchFieldPolicy(enabled=True)},
        ),
        FakeResearchProvider(result=auth_failure),
        repository=repository,
        metrics=Metrics(),
    )
    runtime = make_application(
        settings=Settings(), providers=bundle, repository=repository, external_research=external
    )
    run = await runtime.analyze(AnalysisRequest(match_id=match.match_id))
    news_error = run.input_manifest["failures"]["news"]
    assert news_error["source"] == "openai_research"
    assert news_error["message"] == "OpenAI non disponibile: autenticazione fallita"
    assert "OPENAI_AUTH_ERROR" in run.decision.reason_codes
    assert "RESEARCH_FAILED" in run.decision.reason_codes
    assert "NEWS_PROVIDER_NOT_CONFIGURED" not in run.decision.reason_codes
    await runtime.close()


def test_888_profile_keeps_local_sources_and_limits_manual_api_football_access():
    config = SourceConfiguration.load("config/888/sources.yaml")
    assert config.providers["fixture"].primary == "sport_888"
    assert config.providers["odds"].primary == "sport_888"
    assert config.providers["football_stats"].primary == "free_football"
    assert config.providers["lineups"].primary == "free_football"
    assert config.api_football.network_enabled is True
    assert config.api_football.daily_request_budget == 90
    assert config.api_football.results_enabled is False
    assert set(config.sport_888.allowed_competitions) == {1000095001, 1000095002}
    assert config.external_research.enabled is True
    assert config.external_research.research_primary_source is False
