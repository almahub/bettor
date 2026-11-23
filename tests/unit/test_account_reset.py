import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest

from better.services.account_reset import reset_account
from better.storage.repository import Repository


def account(tmp_path):
    home = tmp_path / 'home'
    (home / 'config/888').mkdir(parents=True)
    (home / 'config/888/app.yaml').write_text('database_url: sqlite:///data/888-real.db\n')
    repo = Repository('sqlite:///' + (home / 'data/888-real.db').as_posix())
    with repo.engine.begin() as conn:
        for table in ('bet_candidates', 'bet_slips', 'bet_events', 'canonical_predictions', 'match_results'):
            repo._append(conn, table, identifier=table, entity_id='test', match_id='m', payload={'test': table})
        repo._append(conn, 'annotations', identifier='bank', entity_id='bank:EUR:v1', match_id=None, payload={'kind':'INITIAL'})
        repo._append(conn, 'annotations', identifier='cost', entity_id='automation:research-spend:v1', match_id=None, payload={'kind':'RESERVED'})
    repo.close()
    lock = SimpleNamespace(acquired=True, kind='backend',
        account_identity=hashlib.sha256(str(home.resolve()).lower().encode()).hexdigest()[:24])
    return home, lock


def test_reset_preserves_evidence_cost_reservations_and_verified_safety_copy(tmp_path):
    home, lock = account(tmp_path)
    result = reset_account(home, backend_lock=lock, safety_directory=tmp_path / 'protected')
    assert result['removed']['bet_slips'] == 1
    assert (tmp_path / 'protected').joinpath(result['safety_backup'].split('\\')[-1]).exists()
    with sqlite3.connect(home / 'data/888-real.db') as conn:
        assert conn.execute('SELECT count(*) FROM bet_slips').fetchone()[0] == 0
        assert conn.execute('SELECT count(*) FROM canonical_predictions').fetchone()[0] == 1
        assert conn.execute('SELECT count(*) FROM match_results').fetchone()[0] == 1
        assert json.loads(conn.execute('SELECT payload FROM annotations').fetchone()[0])['kind'] == 'RESERVED'


def test_reset_rejects_wrong_or_window_lock_before_backup(tmp_path):
    home, lock = account(tmp_path)
    lock.kind = 'window'
    with pytest.raises(ValueError, match='blocco backend'):
        reset_account(home, backend_lock=lock, safety_directory=tmp_path / 'protected')
    assert not (home / 'backups').exists()


def test_append_only_guards_are_restored_after_reset(tmp_path):
    home, lock = account(tmp_path)
    reset_account(home, backend_lock=lock, safety_directory=tmp_path / 'protected')
    with sqlite3.connect(home / 'data/888-real.db') as conn:
        before = conn.execute("SELECT count(*) FROM sqlite_master WHERE type='trigger'").fetchone()[0]
        assert before > 0
        with pytest.raises(sqlite3.IntegrityError, match='append-only'):
            conn.execute('DELETE FROM annotations')
