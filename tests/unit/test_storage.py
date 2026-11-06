from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from better.storage.db import TABLES
from better.storage.repository import Repository


def test_journal_append_only_and_idempotent():
    repository = Repository("sqlite:///:memory:")
    as_of = datetime(2026, 1, 1, tzinfo=timezone.utc)
    arguments = dict(
        analysis_id="a",
        match_id="m",
        as_of=as_of,
        kickoff=as_of + timedelta(hours=1),
        payload={"status": "NO_BET"},
    )
    first = repository.append_pre_match(**arguments)
    assert repository.append_pre_match(**arguments) == first
    assert len(repository.list_records("journal_entries")) == 1
    with pytest.raises(ValueError):
        repository.append_pre_match(**{**arguments, "payload": {"status": "BET"}})
    with repository.engine.begin() as connection:
        with pytest.raises(IntegrityError):
            connection.execute(update(TABLES["journal_entries"]).values(payload={"tampered": True}))
    with repository.engine.connect() as connection:
        assert (
            connection.execute(select(TABLES["journal_entries"].c.payload)).scalar_one()["snapshot"]["status"]
            == "NO_BET"
        )


def test_journal_rejects_postkickoff_or_naive_time():
    repository = Repository("sqlite:///:memory:")
    now = datetime.now(timezone.utc)
    with pytest.raises(ValueError):
        repository.append_pre_match(analysis_id="a", match_id="m", as_of=now, kickoff=now, payload={})
    with pytest.raises(ValueError):
        repository.append_pre_match(
            analysis_id="a", match_id="m", as_of=now.replace(tzinfo=None), kickoff=now, payload={}
        )
