from datetime import timedelta

import pytest

from better.domain.enums import ErrorCode
from better.domain.errors import BetterError
from better.observability.diagnostics import failure_detail
from better.providers.result_identity import ResultIdentityStatus, normalize_team_name
from better.services.bank_service import BankService
from better.services.data_pipeline import DataPipelineCollector, DataPipelineService
from better.services.validation_cohort import ValidationCohortService
from tests.unit.test_result_identity import CandidateProvider, candidate, resolver, source_match
from tests.unit.test_validation_cohort import NOW, ResultProvider, runtime


def test_diagnostics_redact_secrets_and_preserve_actionable_context(monkeypatch):
    monkeypatch.setenv("API_FOOTBALL_API_KEY", "private-credential-value")
    error = BetterError(ErrorCode.MISSING_DATA,
                        'Missing data private-credential-value Authorization: Bearer abc123 '
                        'api_key="second-secret" https://provider.test/data?token=third-secret',
                        source="api_football")
    row = failure_detail(error, match_id="match-1", checkpoint="T6", stage="STATS")
    assert row["code"] == "MISSING_DATA"
    assert row["provider"] == "api_football"
    assert row["match_id"] == "match-1" and row["checkpoint"] == "T6"
    assert all(secret not in row["message"] for secret in ["private-credential-value", "abc123", "second-secret", "third-secret"])


async def test_complete_cohort_and_checkpoints_survive_more_than_1000_records():
    app, repo = runtime()
    collector = DataPipelineCollector(app, clock=lambda: NOW)
    await collector.collect_due()
    original = next(row for row in repo.all_records("validation_cohort") if row["lifecycle_status"] == "ENROLLED")
    with repo.engine.begin() as connection:
        for index in range(1005):
            match_id = f"other-{index}"
            payload = {**original, "match_id": match_id, "match": {**original["match"], "match_id": match_id}}
            repo._append(connection, "validation_cohort", identifier=match_id, entity_id=match_id,
                         match_id=match_id, payload=payload)
            repo._append(connection, "historical_observations", identifier=f"old-snapshot-{index}",
                         entity_id="demo-match", match_id="demo-match",
                         payload={"checkpoint": "EARLY", "collected_at": NOW.isoformat(), "snapshots": {}, "failures": {}})
    cohort = ValidationCohortService(app, clock=lambda: NOW)
    assert len(cohort.states()) == 1006
    assert cohort.report()["enrolled"] == 1006
    assert repo.has_historical_checkpoint("demo-match", "T6")
    assert DataPipelineService(repo, now=lambda: NOW).report()["checkpoints"]["total"] == 1006
    await app.close()


async def test_recovery_reuses_verified_result_completes_partial_markets_and_never_moves_bank():
    provider = ResultProvider()
    app, repo = runtime(result_provider=provider)
    await DataPipelineCollector(app, clock=lambda: NOW).collect_due()
    match = app.providers.fixtures.match
    now = match.kickoff + timedelta(hours=2)
    result = await provider.get_result(match, now)
    predictions = repo.all_records("canonical_predictions")
    # Store a real-contract verified result and one already settled canonical market.
    with repo.engine.begin() as connection:
        repo._append(connection, "match_results", identifier="verified", entity_id=match.match_id,
                     match_id=match.match_id, payload={**result.model_dump(mode="json"), "status": "FINAL",
                                                     "validation_eligible": True, "recorded_at": now.isoformat()})
        repo._append(connection, "validation_observations", identifier="already-settled", entity_id=match.match_id,
                     match_id=match.match_id, payload={**predictions[0], "inclusion_status": "VALIDATION_INCLUDED"})
        # Old canonical records must still be found behind 1,000 newer records.
        for index in range(1001):
            repo._append(connection, "canonical_predictions", identifier=f"unrelated-{index}", entity_id=f"other-{index}",
                         match_id=f"other-{index}", payload={**predictions[0], "match_id": f"other-{index}"})
    before = BankService(repo).status()
    provider.calls = 0
    cohort = ValidationCohortService(app, clock=lambda: now)
    report = await cohort.poll_results(now, match_ids={match.match_id})
    assert len(report["settled"]) == 2
    assert provider.calls == 0
    assert len(repo.all_records("validation_observations", entity_id=match.match_id)) == 3
    restarted = ValidationCohortService(app, clock=lambda: now + timedelta(minutes=5))
    assert not (await restarted.poll_results(match_ids={match.match_id}))["settled"]
    assert BankService(repo).status() == before
    await app.close()


async def test_missing_pre_match_prediction_is_excluded_without_backdating():
    app, repo = runtime(result_provider=ResultProvider())
    match = app.providers.fixtures.match
    cohort = ValidationCohortService(app, clock=lambda: NOW)
    cohort.enroll(match)
    now = match.kickoff + timedelta(hours=2)
    await cohort.poll_results(now)
    assert repo.all_records("canonical_predictions") == []
    assert repo.all_records("validation_observations") == []
    assert cohort.states()[0]["reason"] == "PREMATCH_PREDICTION_MISSING"
    await app.close()


async def test_current_outcome_can_recur_after_failure_without_duplicating_unchanged_state():
    app, repo = runtime()
    cohort = ValidationCohortService(app, clock=lambda: NOW)
    match = app.providers.fixtures.match
    cohort.enroll(match)
    cohort._event(match, "KICKOFF_PASSED", "REGULATION_TIME_ELAPSED")
    cohort._event(match, "RESULT_PENDING", "RESULT_IDENTITY_AMBIGUOUS")
    cohort._event(match, "SETTLEMENT_FAILED", "TIMEOUT")
    cohort._event(match, "KICKOFF_PASSED", "REGULATION_TIME_ELAPSED")
    cohort._event(match, "RESULT_PENDING", "RESULT_IDENTITY_AMBIGUOUS")
    assert cohort.states()[0]["lifecycle_status"] == "RESULT_PENDING"
    count = len(repo.all_records("validation_cohort"))
    cohort._event(match, "KICKOFF_PASSED", "REGULATION_TIME_ELAPSED")
    cohort._event(match, "RESULT_PENDING", "RESULT_IDENTITY_AMBIGUOUS")
    assert len(repo.all_records("validation_cohort")) == count
    await app.close()


async def test_missing_canonical_checkpoint_reports_waiting_instead_of_stale_failure():
    app, repo = runtime()
    cohort = ValidationCohortService(app, clock=lambda: NOW)
    match = app.providers.fixtures.match
    cohort.enroll(match)
    cohort._event(match, "PREDICTION_FAILED", "BetterError")
    assert cohort.create_canonical_predictions(match, now=NOW) == []
    state = cohort.states()[0]
    assert state["lifecycle_status"] == "PREDICTION_PENDING"
    assert state["reason"] == "WAITING_CANONICAL_CHECKPOINT"
    assert repo.all_records("canonical_predictions") == []
    await app.close()


async def test_provider_identity_mismatch_remains_explicit_and_cannot_settle():
    class ChangedIdentity(ResultProvider):
        async def get_result(self, match, as_of):
            result = await super().get_result(match, as_of)
            return result.model_copy(update={"provider_status": "IDENTITY_MISMATCH"})

    app, repo = runtime(result_provider=ChangedIdentity(status="UNVERIFIED"))
    await DataPipelineCollector(app, clock=lambda: NOW).collect_due()
    match = app.providers.fixtures.match
    now = match.kickoff + timedelta(hours=2)
    cohort = ValidationCohortService(app, clock=lambda: now)
    summary = await cohort.poll_results(now)
    assert summary["conflicts"] == [match.match_id]
    assert not summary["settled"]
    assert repo.all_records("validation_observations") == []
    assert cohort.states()[0]["reason"] == "RESULT_IDENTITY_MISMATCH"
    assert cohort.states()[0]["details"]["diagnostic"]["provider"] == "official-test"
    await app.close()


async def test_collector_reports_errors_staleness_and_restart_truthfully(monkeypatch):
    monkeypatch.setattr("better.services.data_pipeline.time_ns", lambda: 123)
    monkeypatch.setattr("better.services.data_pipeline.monotonic_ns", lambda: 123)
    app, repo = runtime()
    now = [NOW]
    collector = DataPipelineCollector(app, clock=lambda: now[0])
    assert not collector.status()["healthy"]
    collector.loop_active = True
    await collector.collect_due()
    assert collector.status()["healthy"]
    now[0] += timedelta(seconds=601)
    assert collector.status()["status"] == "STALE"

    async def broken():
        raise BetterError(ErrorCode.TIMEOUT, "Collection deadline", source="api_football")

    monkeypatch.setattr(collector, "_collect_due", broken)
    with pytest.raises(BetterError):
        await collector.collect_due()
    assert collector.status()["status"] == "DEGRADED"
    assert collector.status()["errors"][0]["provider"] == "api_football"
    assert repo.list_records("annotations", last=1, entity_id="collector:lifecycle")[0]["event"] == "FAILED"
    assert not DataPipelineCollector(app).status()["healthy"]
    await app.close()


async def test_pipeline_storage_report_tolerates_disappearing_cache_files(tmp_path, monkeypatch):
    app, repo = runtime()
    service = DataPipelineService(repo, root=tmp_path, now=lambda: NOW)
    monkeypatch.setattr(service, "_files", lambda _directory: [tmp_path / "gone.db-journal"])
    assert service._storage_report(NOW)["bytes"] == 0
    await app.close()


async def test_mapping_normalizes_legal_names_but_rejects_collisions_and_changed_orientation():
    assert normalize_team_name("SSD Citta di Example 1945") == normalize_team_name("Example FC")
    assert normalize_team_name("ACD Example") == normalize_team_name("Example")
    assert normalize_team_name("Example U23") != normalize_team_name("Example")
    first = candidate()
    service, repo = resolver(CandidateProvider([first, first.model_copy(update={"provider_match_id": "other"})]))
    assert (await service.resolve(source_match(), NOW)).status == ResultIdentityStatus.AMBIGUOUS
    assert repo.all_records("identity_mappings") == []
    service, _ = resolver(CandidateProvider([first]))
    await service.resolve(source_match(), NOW)
    reversed_match = source_match().model_copy(update={"home_team_id": "different-team"})
    assert (await service.resolve(reversed_match, NOW)).status == ResultIdentityStatus.CONFLICT
