from copy import deepcopy
from datetime import timedelta

import pytest

from better.domain.enums import ReliabilityTier
from better.observability.metrics import Metrics
from better.providers.research import FakeFootballDataResearchProvider, normalize_finding
from better.services.football_archive import FootballArchive
from better.services.football_data_research import (
    FootballDataResearchConfiguration,
    FootballDataResearchService,
    merge_football_stats,
    missing_model_research_fields,
    normalize_football_data,
)
from better.storage.repository import Repository
from tests.unit.test_api_football import MATCH, NOW, fixture, make_provider
from tests.unit.test_external_research import finding, request, source
from tests.unit.test_football_data_research import NOW as RESEARCH_NOW
from tests.unit.test_football_data_research import football_result


@pytest.mark.parametrize("acquired_later", [False, True])
async def test_previous_season_local_only_preserves_season_and_known_at(acquired_later):
    provider, calls = make_provider()
    repo = Repository("sqlite:///:memory:")
    provider.archive = FootballArchive(repo, seasons=(2026, 2025), leagues=(5, 6))
    old = []
    for team in (1, 2):
        for i in range(8):
            row = fixture(1000 + team * 10 + i, team, 3, NOW - timedelta(days=130 + i))
            row["league"] = {"id": 6, "season": 2025}
            old.append(row)
    future = deepcopy(old[0])
    future["fixture"].update(id=9999, date=(NOW + timedelta(days=1)).isoformat())
    old.append(future)
    provider.archive.save("fixtures", {"league": 6, "season": 2025}, {
        "data": old, "retrieved_at": NOW + timedelta(seconds=1) if acquired_later else NOW,
        "url": "https://provider.test/old", "sha256": "old",
    }, 30 * 86400)
    result = await provider.get_stats(MATCH.match_id, NOW)
    assert result.home.sample_size == (3 if acquired_later else 10)
    assert result.home.segments["season"]["sample_size"] == 3
    assert all(r.kickoff < NOW for r in result.home.recent_matches)
    if not acquired_later:
        assert sum(r.competition_id == "api-football:league:6:season:2025" for r in result.home.recent_matches) == 7
        assert any(p.url == "https://provider.test/old" for p in result.provenance)
    assert all(r.url.params.get("season") != "2025" for r in calls)
    await provider.close()
    repo.close()


async def test_partial_stats_recovery_is_targeted_and_cache_survives_restart():
    from better.providers.fake import make_demo_bundle
    match = make_demo_bundle("bet", as_of=RESEARCH_NOW).fixtures.match
    repo = Repository("sqlite:///:memory:")
    requests = []

    def result(req):
        requests.append(req)
        return football_result(match).model_copy(update={"request_id": req.request_id})

    provider = FakeFootballDataResearchProvider(result=result)
    settings = FootballDataResearchConfiguration(enabled=True, supported_competitions=(match.competition,))
    service = FootballDataResearchService(settings, provider, repository=repo, metrics=Metrics(), clock=lambda: RESEARCH_NOW)
    recovered = await service.research(match, RESEARCH_NOW, requested_fields=("recent_form",))
    assert requests[0].requested_fields == ("recent_form",)
    restarted = FootballDataResearchService(settings, provider, repository=repo, metrics=Metrics(), clock=lambda: RESEARCH_NOW)
    assert (await restarted.research(match, RESEARCH_NOW, requested_fields=("recent_form",))).cached
    assert len(requests) == 1
    normalized = normalize_football_data(match, recovered)
    assert not missing_model_research_fields(normalized, 10)
    assert normalized.home.recent_matches[0].competition_id == "Serie A"
    original = normalized.model_copy(update={"home": normalized.home.model_copy(update={
        "sample_size": 8, "segments": {**normalized.home.segments, "season": {"sample_size": 8}},
        "xg_per_match": 1.3,
    })})
    assert missing_model_research_fields(original, 10) == ("recent_form",)
    merged = merge_football_stats(original, normalized)
    assert merged.home.sample_size == 10
    assert merged.home.xg_per_match == 1.3
    assert merged.home.segments["season"]["sample_size"] == 8
    assert merged.away == original.away
    assert merge_football_stats(original, None) is original
    repo.close()


@pytest.mark.parametrize("url,label,tier,expected", [
    ("https://www.udinese.it/news/x", "official club announcement", "A", "A"),
    ("https://www.udinese.it/news/x", "official club announcement", "C", "C"),
    ("https://udinese.it.attacker.test/news/x", "OFFICIAL_CLUB", "A", "D"),
    ("https://unknown.test/news/x", "OFFICIAL_CLUB", "A", "D"),
])
def test_source_alias_requires_reviewed_identity_and_never_upgrades_claimed_tier(url, label, tier, expected):
    result = normalize_finding(finding(sources=(source(url=url, source_type=label, reliability_tier=tier),)), request())
    assert result.sources[0].reliability_tier == ReliabilityTier(expected)
    if expected != "A":
        assert result.value is None


def test_cross_domain_canonical_and_missing_dates_cannot_confirm_evidence():
    for item in (
        source(url="https://unknown.test/news", canonical_url="https://www.udinese.it/news"),
        source(url="https://www.udinese.it/news", published_at=None),
    ):
        result = normalize_finding(finding(sources=(item,)), request())
        assert result.value is None
        assert result.status == "INSUFFICIENT_EVIDENCE"


async def test_analysis_enriches_partial_stats_but_skips_complete_stats():
    from dataclasses import replace
    from unittest.mock import AsyncMock

    from better.config import Settings
    from better.domain.schemas import AnalysisRequest
    from better.providers.fake import make_demo_bundle
    from better.services.application import make_application

    bundle = make_demo_bundle("bet", as_of=RESEARCH_NOW)
    match = bundle.fixtures.match
    original = await bundle.stats.get_stats(match.match_id, RESEARCH_NOW)
    partial = original.model_copy(update={"home": original.home.model_copy(update={"sample_size": 8})})
    for supplied, expected_calls in ((partial, 1), (original, 0)):
        repo = Repository("sqlite:///:memory:")
        requests = []

        def result(req, requests=requests):
            requests.append(req)
            return football_result(match).model_copy(update={"request_id": req.request_id, "as_of": req.as_of})

        research = FootballDataResearchService(
            FootballDataResearchConfiguration(enabled=True, supported_competitions=(match.competition,)),
            FakeFootballDataResearchProvider(result=result), repository=repo, metrics=Metrics(),
        )
        runtime = make_application(settings=Settings(),
            providers=replace(bundle, stats=AsyncMock(get_stats=AsyncMock(return_value=supplied))),
            repository=repo, football_data_research=research)
        run = await runtime.analyze(AnalysisRequest(match_id=match.match_id))
        assert len(requests) == expected_calls
        if requests:
            assert requests[0].requested_fields == ("recent_form",)
            assert run.snapshot.stats.home.sample_size == 10
            assert run.snapshot.stats.away == original.away
        assert not repo.all_records("validation_observations")
        await runtime.close()
