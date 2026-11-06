from datetime import datetime, timedelta, timezone

from better.services.data_pipeline import DataPipelineService, checkpoint_for
from better.storage.repository import Repository


def test_checkpoint_boundaries_are_deterministic():
    kickoff = datetime(2026, 10, 2, 20, tzinfo=timezone.utc)
    assert checkpoint_for(kickoff, kickoff - timedelta(days=2)) == "EARLY"
    assert checkpoint_for(kickoff, kickoff - timedelta(hours=24)) == "T24"
    assert checkpoint_for(kickoff, kickoff - timedelta(hours=6)) == "T6"
    assert checkpoint_for(kickoff, kickoff - timedelta(hours=1)) == "T1"
    assert checkpoint_for(kickoff, kickoff - timedelta(minutes=10)) == "CLOSING"


def test_pipeline_report_is_empty_but_operational(tmp_path):
    repository = Repository("sqlite:///:memory:")
    report = DataPipelineService(repository, root=tmp_path).report()
    assert report["status"] == "READY"
    assert report["policy"]["selection"] == "SELECTED_MATCHES_ONLY"
    assert report["policy"]["database_records_are_immutable"] is True
    assert report["maturity"]["matches"] == 0
    assert report["agent_coverage"]["analysis_runs"] == 0
    assert len(report["agent_coverage"]["agents"]) == 14
    assert report["storage"]["within_limit"] is True


def test_storage_retention_only_identifies_raw_and_cache(tmp_path):
    old = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for relative in ("data/raw/old.json", ".cache/old.tmp", "data/keep.db"):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")
        timestamp = old.timestamp()
        import os

        os.utime(path, (timestamp, timestamp))
    service = DataPipelineService(
        Repository("sqlite:///:memory:"),
        root=tmp_path,
        now=lambda: datetime(2026, 10, 2, tzinfo=timezone.utc),
    )
    candidates = service.report()["storage"]["retention_candidates"]["items"]
    assert {item["path"] for item in candidates} == {"data/raw/old.json", ".cache/old.tmp"}


async def test_selected_match_collector_captures_each_checkpoint_once():
    from better.config import Settings
    from better.services.application import make_application
    from better.services.data_pipeline import DataPipelineCollector
    from better.system_one import SystemOneSettings

    now = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
    repository = Repository("sqlite:///:memory:")
    runtime = make_application(
        settings=Settings(),
        repository=repository,
        demo="bet",
        as_of=now,
        system_one_settings=SystemOneSettings(enabled=False),
    )
    match = await runtime.providers.fixtures.get_match("demo-match")
    repository.select_for_collection(match=match, selected_at=now)
    collector = DataPipelineCollector(runtime, clock=lambda: now)
    first = await collector.collect_due()
    second = await collector.collect_due()
    assert first["captured"]
    assert second["captured"] == []
    assert second["skipped"] == first["captured"]
    assert len(repository.list_records("historical_observations")) == 1


async def test_automation_targets_include_unselected_cohort_matches(monkeypatch):
    from better.config import Settings
    from better.services.application import make_application
    from better.services.data_pipeline import DataPipelineCollector
    from better.services.timing_comparison import TimingComparisonService
    from better.services.validation_cohort import ValidationCohortService

    now = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)
    repo = Repository('sqlite:///:memory:')
    runtime = make_application(settings=Settings(), repository=repo, demo='bet', as_of=now)
    match = await runtime.providers.fixtures.get_match('demo-match')
    captured_targets = []

    async def enroll(self, at):
        return {'enrolled': 0}

    async def timing(self, targets):
        captured_targets.extend(targets)
        return {'captured': [], 'failed': []}

    monkeypatch.setattr(ValidationCohortService, 'auto_enroll', enroll)
    monkeypatch.setattr(ValidationCohortService, 'active_pre_match',
        lambda self, at: [{'match_id': match.match_id, 'match': match.model_dump(mode='json')}])
    monkeypatch.setattr(TimingComparisonService, 'collect_due', timing)
    assert not repo.active_collection_selections(now)
    try:
        await DataPipelineCollector(runtime, clock=lambda: now).collect_due()
        assert any(row['match_id'] == match.match_id for row in captured_targets)
    finally:
        await runtime.close()
        repo.close()
