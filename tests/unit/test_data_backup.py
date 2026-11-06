import importlib.util
import sqlite3
import zipfile
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from better.api.settings import mount_settings
from better.services.credential_store import CredentialError, CredentialStore, WindowsProtector
from better.services.data_backup import (
    BackupError,
    create_backup,
    restore_pending,
    stage_restore,
    unpack_verified,
)


def home_at(path, value=42):
    (path / 'data').mkdir(parents=True)
    (path / 'config/888').mkdir(parents=True)
    (path / 'config/888/app.yaml').write_text('database_url: sqlite:///data/888-real.db\n')
    (path / '.env').write_text('OPENAI_API_KEY=private-test-key\nSOME_OPTION=keep\n')
    with closing(sqlite3.connect(path / 'data/888-real.db')) as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('CREATE TABLE financial(value INTEGER)')
        db.execute('INSERT INTO financial VALUES (?)', (value,))
        db.commit()
    (path / 'data/history.json').write_text('{"persist":true}')
    return path


def amount(home):
    with closing(sqlite3.connect(home / 'data/888-real.db')) as db:
        return db.execute('SELECT value FROM financial').fetchone()[0]


def test_full_backup_restore_and_safety_copy(tmp_path):
    home = home_at(tmp_path / 'home')
    archive = create_backup(home)
    with zipfile.ZipFile(archive) as file:
        assert 'private-test-key' not in file.read('.env').decode()
        assert not any(n.endswith('-wal') or n.startswith('backups/') for n in file.namelist())
        assert 'data/history.json' in file.namelist()
    with closing(sqlite3.connect(home / 'data/888-real.db')) as db:
        db.execute('UPDATE financial SET value=99')
        db.commit()
    stage_restore(home, archive)
    assert amount(home) == 99
    result = restore_pending(home)
    assert amount(home) == 42
    assert result['status'] == 'RESTORED'
    assert (home / 'backups' / result['safety_backup']).is_file()
    assert not (home / 'restore-pending.json').exists()
    assert restore_pending(home) is None
    assert 'BETTER_PIPELINE_AUTO_COLLECT=false' in (home / '.env').read_text()


def test_restore_other_pc_keeps_data_without_credentials(tmp_path, monkeypatch):
    original = home_at(tmp_path / 'original')
    with (original / '.env').open('a') as stream:
        stream.write('OPENAI_RESEARCH_AGENT_ID=agent_external\nOPENAI_FOOTBALL_DATA_AGENT_ID=agent_football\n')
    CredentialStore(original).import_legacy()
    archive = create_backup(original)
    target = tmp_path / 'new-pc'
    target.mkdir()
    stage_restore(target, archive)
    def unavailable(self, data):
        raise CredentialError('Altro account Windows')
    monkeypatch.setattr(WindowsProtector, 'unprotect', unavailable)
    result = restore_pending(target)
    assert amount(target) == 42
    assert not result['credentials_restored']
    assert not (target / 'credentials.dpapi').exists()
    assert 'private-test-key' not in (target / '.env').read_text()
    assert 'OPENAI_RESEARCH_AGENT_ID=agent_external' in (target / '.env').read_text()
    assert 'OPENAI_FOOTBALL_DATA_AGENT_ID=agent_football' in (target / '.env').read_text()


@pytest.mark.parametrize('entry', ['../escape', 'data/../../escape', 'data/CON', 'data/a:secret', 'data/a\\b'])
def test_malicious_archive_paths_rejected(tmp_path, entry):
    archive = tmp_path / 'bad.zip'
    with zipfile.ZipFile(archive, 'w') as file:
        file.writestr('manifest.json', '{}')
        file.writestr(entry, 'malicious')
    with pytest.raises(BackupError):
        unpack_verified(archive, tmp_path / 'stage')
    assert not (tmp_path / 'escape').exists()


def test_tamper_and_restore_rollback(tmp_path, monkeypatch):
    home = home_at(tmp_path / 'home')
    archive = create_backup(home)
    with zipfile.ZipFile(archive, 'a') as file:
        file.writestr('data/history.json', 'changed')
    with pytest.raises(BackupError):
        stage_restore(home, archive)
    assert not (home / 'restore-pending.json').exists()
    assert amount(home) == 42


def test_api_backup_maintenance_and_cancel(tmp_path, monkeypatch):
    home = home_at(tmp_path / 'home')
    monkeypatch.chdir(home)
    app = FastAPI()
    runtime = SimpleNamespace(settings=SimpleNamespace(database_url='sqlite:///data/888-real.db'), _locks={})
    mount_settings(app, lambda: runtime)
    @app.post('/mutate')
    async def mutate():
        return {'ok': True}
    with TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 123)) as client:
        token = client.get('/system/settings').json()['token']
        headers = {'x-better-settings-token': token}
        assert client.post('/system/settings/backups').status_code == 403
        backup = client.post('/system/settings/backups', headers=headers)
        assert backup.status_code == 200
        name = backup.json()['name']
        assert client.get('/system/settings/backups/download/' + name).content.startswith(b'PK')
        assert client.post('/system/settings/backups/restore/' + name, headers=headers).status_code == 200
        assert client.post('/mutate').status_code == 409
        assert client.post('/system/settings/backups/cancel', headers=headers).status_code == 200
        assert client.post('/mutate').status_code == 200
        app.state.data_pipeline_collector = SimpleNamespace(running=True)
        assert client.post('/system/settings/backups', headers=headers).status_code == 409


def test_retention_keeps_current_and_latest_two():
    spec = importlib.util.spec_from_file_location('release_tool', Path(__file__).parents[2] / 'scripts/publish-release.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    releases = [{'tag_name': 'v0.2.' + str(n), 'created_at': str(n), 'published_at': None} for n in range(6)]
    assert set(module.obsolete_releases(releases, 'v0.2.5')) == {'v0.2.0', 'v0.2.1', 'v0.2.2'}


def test_backup_ui_contract():
    ui = (Path(__file__).parents[2] / 'better/web/backup-settings.mjs').read_text(encoding='utf-8-sig')
    assert 'Crea backup completo' in ui and 'Importa backup' in ui
    assert 'Controlla aggiornamenti' in ui and 'confirm(' in ui

def test_failed_replacement_restores_previous_files(tmp_path, monkeypatch):
    import better.services.data_backup as module
    home = home_at(tmp_path / 'home')
    archive = create_backup(home)
    with closing(sqlite3.connect(home / 'data/888-real.db')) as db:
        db.execute('UPDATE financial SET value=99')
        db.commit()
    stage_restore(home, archive)
    original_replace = module.os.replace
    failed = False
    def fail_once(source, target):
        nonlocal failed
        if not failed and Path(target) == home / 'config' and 'restore-stage-' in str(source):
            failed = True
            raise OSError('test failure')
        return original_replace(source, target)
    monkeypatch.setattr(module.os, 'replace', fail_once)
    with pytest.raises(BackupError):
        restore_pending(home)
    assert failed and amount(home) == 99
    assert (home / 'config/888/app.yaml').is_file()
    assert (home / 'restore-pending.json').exists()
