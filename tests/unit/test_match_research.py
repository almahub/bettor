from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from better.config import Settings
from better.domain.errors import BetterError
from better.domain.research import ResearchFinding, ResearchResult
from better.providers.fake import make_demo_bundle
from better.providers.research import FakeResearchProvider
from better.services.application import UnconfiguredProvider, make_application
from better.services.external_research import (
    ExternalResearchConfiguration,
    ExternalResearchService,
    ResearchFieldPolicy,
)
from better.services.match_research import MatchResearchService
from better.storage.repository import Repository
from better.system_one import SystemOneSettings

NOW = datetime.now(UTC)


def runtime(missing_stats=False):
    bundle = make_demo_bundle("bet", as_of=NOW)
    if missing_stats:
        bundle = replace(bundle, stats=UnconfiguredProvider())
    return make_application(
        settings=Settings(),
        providers=bundle,
        repository=Repository("sqlite:///:memory:"),
        system_one_settings=SystemOneSettings(enabled=False),
    )


async def test_selected_lookup_archives_data_without_agents_or_decision():
    engine = runtime()
    service = MatchResearchService(engine, clock=lambda: NOW)
    report = await service.inspect("demo-match")
    assert report["analysis_started"] is False
    assert engine.repository.list_records("analysis_runs") == []
    assert engine.repository.list_records("journal_entries") == []
    stored = engine.repository.list_records("annotations", entity_id="match-research:demo-match")
    assert len(stored) == 1
    assert "stats" in stored[0]["snapshots"]
    assert len(report["sections"]) == 4
    assert all(row["consistency"] == "NOT_VERIFIED" for row in report["sections"])
    assert report["history"]["settled_predictions"] > 0
    again = await service.inspect("demo-match")
    assert again["cached"] is True
    assert again["collected_at"] == report["collected_at"]
    assert len(engine.repository.list_records("annotations", entity_id="match-research:demo-match")) == 1
    await engine.close()


async def test_missing_source_is_explicit_not_complete_or_fresh():
    engine = runtime(missing_stats=True)
    report = await MatchResearchService(engine, clock=lambda: NOW).inspect("demo-match")
    stats = next(row for row in report["sections"] if row["kind"] == "stats")
    assert stats["available"] is False and stats["complete"] is False
    assert stats["fresh"] is None and stats["reliability_tiers"] == []
    assert stats["error"]["code"] == "MISSING_DATA"
    assert stats["consistency"] == "NOT_VERIFIED"
    await engine.close()


async def test_external_news_research_is_targeted_and_shadow_only():
    bundle = replace(make_demo_bundle("bet", as_of=NOW), news=UnconfiguredProvider())
    engine = make_application(
        settings=Settings(),
        providers=bundle,
        repository=Repository("sqlite:///:memory:"),
        system_one_settings=SystemOneSettings(enabled=False),
    )

    def external_result(request):
        return ResearchResult(
            request_id=request.request_id,
            match_id=request.match_id,
            as_of=request.as_of,
            findings=(
                ResearchFinding(
                    field="news",
                    status="NOT_FOUND",
                    confidence=0,
                    notes="No sufficiently reliable news found",
                ),
            ),
            searched_fields=("news",),
            unresolved_fields=("news",),
            provider="fake_external_research",
            model="fake",
            search_calls=1,
            request_count=1,
            completed_at=NOW,
        )

    provider = FakeResearchProvider(result=external_result)
    engine.external_research = ExternalResearchService(
        ExternalResearchConfiguration(
            enabled=True,
            research_primary_source=False,
            fields={"news": ResearchFieldPolicy(enabled=True)},
        ),
        provider,
        repository=engine.repository,
        metrics=engine.metrics,
        clock=lambda: NOW,
    )
    report = await MatchResearchService(engine, clock=lambda: NOW).inspect("demo-match")
    assert report["external_research"]["searched_fields"] == ["news"]
    assert report["external_research"]["coverage"] == {"before": 1, "after": 1, "found": 0}
    assert len(provider.calls) == 1
    assert report["analysis_started"] is False
    # Phase 1 does not merge web findings into the effective SnapshotCollector input.
    assert engine.repository.list_records("analysis_runs") == []
    await engine.close()


async def test_external_research_is_not_called_when_structured_news_is_complete():
    engine = runtime()
    provider = FakeResearchProvider()
    engine.external_research = ExternalResearchService(
        ExternalResearchConfiguration(
            enabled=True,
            research_primary_source=False,
            fields={"news": ResearchFieldPolicy(enabled=True)},
        ),
        provider,
        repository=engine.repository,
        metrics=engine.metrics,
        clock=lambda: NOW,
    )
    report = await MatchResearchService(engine, clock=lambda: NOW).inspect("demo-match")
    assert report["external_research"]["status"] == "NOT_REQUIRED"
    assert provider.calls == []
    await engine.close()


async def test_cache_does_not_refresh_original_timestamps():
    engine = runtime()
    service = MatchResearchService(engine, clock=lambda: NOW)
    before = await service.inspect("demo-match")
    service.clock = lambda: NOW + timedelta(seconds=10)
    after = await service.inspect("demo-match")
    assert before["collected_at"] == after["collected_at"]
    for first, second in zip(before["sections"], after["sections"], strict=True):
        if first["age_seconds"] is not None:
            assert second["age_seconds"] == first["age_seconds"] + 10
    await engine.close()


async def test_expired_match_not_collected():
    engine = runtime()
    with pytest.raises(BetterError, match="kickoff"):
        await MatchResearchService(engine, clock=lambda: NOW + timedelta(days=3)).inspect("demo-match")
    assert engine.repository.list_records("annotations") == []
    await engine.close()


def test_statsbomb_removed_and_research_api_does_not_place_bets():
    from fastapi.testclient import TestClient

    from better.api.app import create_app

    engine = runtime()
    app = create_app(engine)
    app.state.match_research = MatchResearchService(engine, clock=lambda: NOW)
    with TestClient(app) as client:
        assert client.get("/research/statsbomb/competitions").status_code == 404
        assert client.post("/research/statsbomb/import", json={}).status_code == 404
        result = client.get("/matches/demo-match/research")
        assert result.status_code == 200
        assert result.json()["analysis_started"] is False
        assert client.get("/bank").json()["event_count"] == 0


@pytest.mark.parametrize(
    "same_origin,conflict,expected",
    [(False, False, "CONSISTENT"), (True, False, "NOT_VERIFIED"), (False, True, "CONFLICT")],
)
async def test_consistency_requires_independent_comparison(same_origin, conflict, expected):
    from unittest.mock import AsyncMock

    from better.orchestration.snapshot import SnapshotCollector
    from better.providers.multi_source import MultiSourceProvider

    engine = runtime()
    original = await engine.providers.stats.get_stats("demo-match", NOW)

    def snapshot(label, root):
        return original.model_copy(
            update={
                "provenance": tuple(
                    p.model_copy(update={"source_id": label, "primary_source_id": root})
                    for p in original.provenance
                )
            }
        )

    first, second = (
        snapshot("first", "origin-a"),
        snapshot("second", "origin-a" if same_origin else "origin-b"),
    )
    if conflict:
        second = second.model_copy(
            update={
                "home": second.home.model_copy(
                    update={"goals_for_per_match": second.home.goals_for_per_match + 1}
                )
            }
        )
    source = MultiSourceProvider(
        AsyncMock(get_stats=AsyncMock(return_value=first)),
        AsyncMock(get_stats=AsyncMock(return_value=second)),
    )
    engine.providers = replace(engine.providers, stats=source)
    engine.collector = SnapshotCollector(engine.providers, engine.settings)
    report = await MatchResearchService(engine, clock=lambda: NOW).inspect("demo-match")
    stats = next(s for s in report["sections"] if s["kind"] == "stats")
    assert stats["consistency"] == expected
    assert engine.repository.list_records("analysis_runs") == []
