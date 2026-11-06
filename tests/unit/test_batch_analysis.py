from datetime import UTC, datetime

import pytest

from better.config import Settings
from better.domain.salabet import BetCandidateCreate
from better.domain.schemas import AnalysisRequest
from better.providers.fake import make_demo_bundle
from better.services.application import make_application
from better.services.batch_analysis import BatchAnalysisService
from better.services.salabet_service import SalaBetService
from better.storage.repository import Repository
from better.system_one import SystemOneSettings


def runtime(case="bet", *, now=None):
    instant = now or datetime.now(UTC)
    return make_application(
        settings=Settings(),
        providers=make_demo_bundle(case, as_of=instant),
        repository=Repository("sqlite:///:memory:"),
        system_one_settings=SystemOneSettings(),
    )


async def test_batch_identifies_local_archive_miss_without_starting_analysis(monkeypatch):
    from unittest.mock import AsyncMock

    from better.domain.enums import ErrorCode
    from better.domain.errors import BetterError

    engine = runtime()
    monkeypatch.setattr(engine.providers.stats, "get_stats", AsyncMock(side_effect=BetterError(
        ErrorCode.MISSING_DATA, "Archivio locale: dato assente", source="local_archive")))
    try:
        report = await BatchAnalysisService(engine).run(["demo-match"])
        assert report["analyzed"] == 0
        assert report["rows"][0]["exclusion"] == "ARCHIVE_DATA_UNAVAILABLE"
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_batch_screens_then_transfers_one_ranked_candidate_without_slip_or_bank_movement():
    engine = runtime()
    report = await BatchAnalysisService(engine).run(["demo-match"], filter_used={"competition": "all"})

    assert report["total"] == report["analyzed"] == report["completed"] == 1
    assert report["candidates"] == report["transferred"] == 1
    assert report["rows"][0]["status"] == "BET"
    assert report["rows"][0]["score_breakdown"]["components"]
    candidate = SalaBetService(engine.repository).list_candidates()[0]
    assert candidate.batch_id is not None
    assert candidate.ranking_position == 1
    assert candidate.ranking_score == report["rows"][0]["ranking_score"]
    assert candidate.ranking_reasons
    assert candidate.batch_data_quality == report["rows"][0]["data_quality"]
    assert SalaBetService(engine.repository).list_slips() == []
    assert BatchAnalysisService(engine).get(report["batch_id"])["batch_id"] == report["batch_id"]


@pytest.mark.asyncio
async def test_failed_screening_does_not_execute_expensive_pipeline_steps():
    engine = runtime("odds_offline")
    report = await BatchAnalysisService(engine).run(["demo-match"])

    assert report["analyzed"] == 0
    assert report["transferred"] == 0
    assert report["rows"][0]["exclusion"] == "API_PROVIDER_ERROR"
    assert engine.providers.fixtures.calls.get("news", 0) == 0
    assert engine.providers.fixtures.calls.get("lineups", 0) == 0


def test_ranking_is_deterministic_limited_and_keeps_one_candidate_per_match():
    rows = [
        {
            "match_id": f"match-{index // 2}",
            "market": "1X2",
            "selection": "HOME" if index % 2 == 0 else "DRAW",
            "ranking_score": 100 - index,
        }
        for index in range(16)
    ]
    first = BatchAnalysisService.select_top(rows, 6)
    second = BatchAnalysisService.select_top(list(reversed(rows)), 6)

    assert first == second
    assert len(first) == 6
    assert len({row["match_id"] for row in first}) == 6


@pytest.mark.asyncio
async def test_refresh_is_a_new_audited_batch_with_change_status():
    now = datetime.now(UTC)
    engine = runtime(now=now)
    clock_values = iter((now, now, now, now, now, now))
    service = BatchAnalysisService(engine, clock=lambda: next(clock_values))
    first = await service.run(["demo-match"])
    refreshed = await service.run(["demo-match"], parent_batch_id=first["batch_id"])

    assert refreshed["batch_id"] != first["batch_id"]
    assert refreshed["parent_batch_id"] == first["batch_id"]
    assert refreshed["rows"][0]["change"] in {"UNCHANGED", "IMPROVED", "WORSENED"}


@pytest.mark.asyncio
async def test_indicative_candidate_is_kept_with_validator_penalty():
    now = datetime.now(UTC)
    engine = make_application(
        settings=Settings(min_sample=10_000),
        providers=make_demo_bundle("bet", as_of=now),
        repository=Repository("sqlite:///:memory:"),
        system_one_settings=SystemOneSettings(),
    )
    report = await BatchAnalysisService(engine).run(["demo-match"])

    assert report["transferred"] == 1
    assert report["rows"][0]["status"] == "INDICATIVE"
    assert report["rows"][0]["score_breakdown"]["penalties"]["validator_insufficient"] > 0


@pytest.mark.asyncio
async def test_reduced_training_sample_remains_indicative_and_is_penalized():
    now = datetime.now(UTC)
    engine = make_application(
        settings=Settings(min_training_sample=100, min_sample=10_000),
        providers=make_demo_bundle("bet", as_of=now),
        repository=Repository("sqlite:///:memory:"),
        system_one_settings=SystemOneSettings(),
    )
    report = await BatchAnalysisService(engine).run(["demo-match"])

    assert report["transferred"] == 1
    row = report["rows"][0]
    assert row["status"] == "INDICATIVE"
    assert row["score_breakdown"]["penalties"]["small_sample"] > 0
    assert "Campione ridotto 80/100" in row["reasons"]
    assert any(reason.startswith("Indicativa: Validator ") for reason in row["reasons"])


@pytest.mark.asyncio
async def test_known_critical_reason_excludes_candidate():
    engine = runtime()
    run = await engine.analyze(AnalysisRequest(match_id="demo-match"))
    blocked = run.model_copy(
        update={
            "decision": run.decision.model_copy(
                update={"reason_codes": ("IDENTITY_MAPPING_MISSING",)}
            )
        }
    )

    candidate, reason = BatchAnalysisService(engine)._best_candidate(blocked)

    assert candidate is None
    assert reason == "IDENTITY_MAPPING_MISSING"


@pytest.mark.asyncio
async def test_same_candidate_payload_is_deduplicated():
    engine = runtime()
    report = await BatchAnalysisService(engine).run(["demo-match"])
    row = report["rows"][0]
    desk = SalaBetService(engine.repository)
    request = BetCandidateCreate(
        analysis_id=row["analysis_id"],
        prediction_id=row["prediction_id"],
        market=row["market"],
        selection=row["selection"],
    )
    existing = desk.create_batch_candidate(
        request,
        {
            "batch_id": report["batch_id"],
            "ranking_position": 1,
            "ranking_score": row["ranking_score"],
            "ranking_reasons": row["reasons"],
            "batch_status": row["status"],
        },
    )

    assert existing.id == desk.list_candidates()[0].id
    assert len(engine.repository.list_records("bet_candidates")) == 1


@pytest.mark.asyncio
async def test_batch_analyzes_every_selected_match_and_reports_screening_exclusions(monkeypatch):
    engine = runtime()
    provider = engine.providers.fixtures
    original_get = provider._get

    async def selected_match_data(name, match_id):
        if match_id == "missing-match":
            raise ValueError("Unavailable fixture")
        value = await original_get(name, "demo-match")
        return value.model_copy(update={"match_id": match_id}) if "match_id" in type(value).model_fields else value

    monkeypatch.setattr(provider, "_get", selected_match_data)
    try:
        report = await BatchAnalysisService(engine).run(
            ["selected-one", "missing-match", "selected-two"], filter_used={"mode": "SELECTION"}
        )
        assert report["total"] == 3
        assert report["analyzed"] == report["completed"] == 2
        assert report["failed"] == 0
        rows = {row["match_id"]: row for row in report["rows"]}
        assert set(rows) == {"selected-one", "missing-match", "selected-two"}
        assert rows["missing-match"]["outcome"] == "EXCLUDED"
        assert rows["selected-one"]["analysis_id"] != rows["selected-two"]["analysis_id"]
    finally:
        await engine.close()
