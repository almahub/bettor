import sqlite3
from pathlib import Path

from better.desktop import launcher


def test_initialization_preserves_personal_config_and_database(tmp_path, monkeypatch):
    resources = tmp_path / 'resources'
    config = resources / 'config/888'
    config.mkdir(parents=True)
    (config / 'app.yaml').write_text('database_url: sqlite:///data/888-real.db')
    (config / 'sources.yaml').write_text('providers: {}')
    monkeypatch.setattr(launcher, 'resource_root', lambda: resources)
    monkeypatch.setenv('OPENAI_API_KEY', 'inherited-test-value')
    home = tmp_path / 'user'
    launcher.initialize(home)
    custom = home / 'config/888/app.yaml'
    custom.write_text('personal-settings')
    (home / '.env').write_text('OPENAI_API_KEY=local-test-value\n')
    (home / 'data').mkdir()
    database = home / 'data/888-real.db'
    database.write_bytes(b'preserve-existing-data')
    launcher.initialize(home)
    assert custom.read_text() == 'personal-settings'
    assert database.read_bytes() == b'preserve-existing-data'
    assert launcher.os.environ['OPENAI_API_KEY'] == 'local-test-value'


def test_backup_captures_wal_and_keeps_original(tmp_path):
    data = tmp_path / 'data'
    data.mkdir()
    path = data / '888-real.db'
    with sqlite3.connect(path) as connection:
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('CREATE TABLE sample(value INTEGER)')
        connection.execute('INSERT INTO sample VALUES (42)')
        connection.commit()
        backup = launcher.backup_databases(tmp_path)
        with sqlite3.connect(backup / 'data/888-real.db') as saved:
            assert saved.execute('SELECT value FROM sample').fetchall() == [(42,)]
        assert connection.execute('SELECT value FROM sample').fetchall() == [(42,)]
    assert launcher.backup_databases(tmp_path) != backup


def test_bundle_uses_explicit_public_resources_only():
    spec = Path('packaging/Better.spec').read_text()
    assert "root / 'better/web'" in spec
    assert "root / 'config/888'" in spec
    assert "root / 'data'" not in spec
    assert '.env' not in spec
