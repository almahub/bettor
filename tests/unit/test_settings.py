from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from better.api.settings import mount_settings
from better.services.credential_store import CredentialError, CredentialStore, WindowsProtector


def test_dpapi_roundtrip():
    protector = WindowsProtector()
    encrypted = protector.protect(b'test-secret')
    assert b'test-secret' not in encrypted
    assert protector.unprotect(encrypted) == b'test-secret'


def test_import_preserves_and_encrypts(tmp_path):
    (tmp_path / '.env').write_text('OPENAI_API_KEY=test-secret\nOTHER=keep\n')
    store = CredentialStore(tmp_path)
    assert store.import_legacy()
    assert store.values()['OPENAI_API_KEY'] == 'test-secret'
    assert (tmp_path / '.env').read_text() == 'OTHER=keep\n'
    assert b'test-secret' not in store.path.read_bytes()
    assert list((tmp_path / 'backups').glob('*.dpapi'))
    assert not store.import_legacy()
    with pytest.raises(CredentialError):
        store.write({}, 'stale')


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    app = FastAPI()
    mount_settings(app, lambda: SimpleNamespace(settings=SimpleNamespace(database_url='sqlite:///data/test.db')))
    with TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 123)) as client:
        yield client


def test_secret_redaction_save_remove_revision(client):
    before = client.get('/system/settings').json()
    headers = {'x-better-settings-token': before['token']}
    saved = client.post('/system/settings', headers=headers, json={'revision': before['revision'], 'updates': {'OPENAI_API_KEY': 'private-test-value'}})
    assert saved.status_code == 200
    assert 'private-test-value' not in saved.text
    assert saved.json()['fields']['OPENAI_API_KEY'] == {'configured': True}
    assert saved.json()['restart_required']
    assert client.post('/system/settings', headers=headers, json={'revision': before['revision'], 'updates': {}}).status_code == 409
    removed = client.post('/system/settings', headers=headers, json={'revision': saved.json()['revision'], 'remove': ['OPENAI_API_KEY']})
    assert not removed.json()['fields']['OPENAI_API_KEY']['configured']


def test_security_and_invalid_input_do_not_echo_secrets(client):
    assert client.post('/system/settings', json={}).status_code == 403
    assert client.get('/system/settings', headers={'origin': 'https://evil.test'}).status_code == 403
    assert client.get('/system/settings', headers={'host': 'evil.test'}).status_code == 403
    metadata = client.get('/system/settings').json()
    response = client.post('/system/settings', headers={'x-better-settings-token': metadata['token']}, json={'revision': metadata['revision'], 'updates': {'OPENAI_API_KEY': 'secret\ninvalid'}})
    assert response.status_code == 422
    assert 'secret' not in response.text


def test_verify_does_not_echo_provider_body(client, monkeypatch):
    data = client.get('/system/settings').json()
    headers = {'x-better-settings-token': data['token']}
    client.post('/system/settings', headers=headers, json={'revision': data['revision'], 'updates': {'OPENAI_API_KEY': 'private-value'}})
    async def get(self, url, **kwargs):
        assert kwargs['headers']['Authorization'] == 'Bearer private-value'
        return httpx.Response(401, json={'error': 'private-value'})
    monkeypatch.setattr(httpx.AsyncClient, 'get', get)
    response = client.post('/system/settings/verify/openai', headers=headers)
    assert response.json()['status'] == 'AUTH_ERROR'
    assert 'private-value' not in response.text


def test_settings_ui_contract():
    source = Path(__file__).parents[2] / 'better/web'
    assert 'Impostazioni' in (source / 'dashboard.mjs').read_text(encoding='utf-8')
    ui = (source / 'settings.mjs').read_text(encoding='utf-8-sig')
    assert "'password'" in ui and 'Mostra digitato' in ui
    assert 'localStorage' not in ui and 'sessionStorage' not in ui
