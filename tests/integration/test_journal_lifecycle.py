from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from better.config import Settings
from better.domain.schemas import AnalysisRequest
from better.orchestration.orchestrator import Orchestrator
from better.providers.fake import make_demo_bundle
from better.storage.repository import Repository
from better.system_one import ShadowCoordinator, SystemOneSettings


async def test_settlement_and_annotation_never_rewrite_prematch():
    as_of = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)
    repository = Repository("sqlite:///:memory:")
    runtime = Orchestrator(
        make_demo_bundle(as_of=as_of),
        Settings(),
        repository,
        shadow=ShadowCoordinator(SystemOneSettings(enabled=False)),
    )
    run = await runtime.analyze(AnalysisRequest(match_id="demo-match", as_of=as_of))
    original_entry = repository.list_records("journal_entries")[0]
    result = repository.settle(
        run.analysis_id,
        outcome=0,
        closing_odds=1.9,
        settled_at=run.snapshot.match.kickoff + timedelta(hours=2),
    )
    assert Decimal(result["theoretical_profit"]) == -run.decision.stake.amount
    assert result["review_tag"] == "UNEXPLAINED_VARIANCE"
    repository.annotate(
        run.analysis_id,
        author="human",
        reason="Review without altering original rationale",
        created_at=run.snapshot.match.kickoff + timedelta(hours=3),
    )
    assert repository.get_run(run.analysis_id) == run
    assert original_entry in repository.list_records("journal_entries")
    assert len(repository.list_records("annotations")) == 1
    with pytest.raises(ValueError, match="overwrite"):
        repository.settle(
            run.analysis_id,
            outcome=1,
            closing_odds=1.9,
            settled_at=run.snapshot.match.kickoff + timedelta(hours=2),
        )


async def test_manifest_never_persists_database_credentials():
    as_of = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)
    settings = Settings(database_url="postgresql://username:private_password@localhost/database")
    runtime = Orchestrator(
        make_demo_bundle(as_of=as_of),
        settings,
        Repository("sqlite:///:memory:"),
        shadow=ShadowCoordinator(SystemOneSettings(enabled=False)),
    )
    run = await runtime.analyze(AnalysisRequest(match_id="demo-match", as_of=as_of))
    assert "private_password" not in run.model_dump_json()
    assert "database_url" not in run.input_manifest["resolved_config"]
    assert settings.version == Settings().version
