import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from better import __version__
from better.api.settings import mount_settings
from better.services import desktop_updates as updates


def release(version='999.0.0'):
    name = f'Bettor-Setup-{version}-x64.exe'
    return {'tag_name': 'v' + version, 'draft': False, 'prerelease': False, 'assets': [
        {'name': n, 'browser_download_url': f'https://github.com/almahub/bettor/releases/download/v{version}/{n}'}
        for n in (name, name + '.sha256')]}


def test_release_prefers_branded_verified_assets_and_rejects_other_origins():
    info = updates.release_info(release())
    assert info['status'] == 'AVAILABLE' and info['current'] == __version__
    assert updates.release_info(release(__version__))['status'] == 'CURRENT'
    raw = release()
    raw['assets'][0]['browser_download_url'] = 'https://evil.example/setup.exe'
    with pytest.raises(updates.UpdateError):
        updates.release_info(raw)
    raw = release()
    raw['assets'].append(raw['assets'][0])
    with pytest.raises(updates.UpdateError):
        updates.release_info(raw)
    for url in ['http://github.com/setup', 'https://localhost/a', 'https://github.com:8443/a', 'https://user@github.com/a']:
        assert not updates.allowed_download(url)


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid', [False, True])
async def test_download_is_verified_before_ready_and_never_installs(tmp_path, monkeypatch, invalid):
    payload = b'MZ-controlled-test-payload'
    info = updates.release_info(release())
    expected = ('0' * 64) if invalid else hashlib.sha256(payload).hexdigest()
    client_type = httpx.AsyncClient
    calls = []

    def handler(request):
        calls.append(str(request.url))
        content = (expected + '  ' + info['asset']).encode() if str(request.url).endswith('.sha256') else payload
        return httpx.Response(200, content=content)

    monkeypatch.setattr(updates.httpx, 'AsyncClient', lambda **kwargs: client_type(transport=httpx.MockTransport(handler), **kwargs))
    if invalid:
        with pytest.raises(updates.UpdateError, match='Integrità'):
            await updates.prepare_update(tmp_path, info)
        assert not (tmp_path / 'updates/ready.json').exists()
        assert not list((tmp_path / 'updates').glob('*.exe'))
    else:
        assert (await updates.prepare_update(tmp_path, info))['status'] == 'READY'
        ready = json.loads((tmp_path / 'updates/ready.json').read_text())
        assert ready['sha256'] == expected
        assert (tmp_path / 'updates' / ready['filename']).read_bytes() == payload
        assert updates.prepared_update(tmp_path)['version'] == '999.0.0'
    assert len(calls) == 2
    assert not list((tmp_path / 'updates').glob('*.partial'))


@pytest.mark.asyncio
async def test_redirect_to_untrusted_host_cannot_create_installer(tmp_path):
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(302, headers={'location': 'https://evil.example/setup.exe'})
    )) as client:
        with pytest.raises(updates.UpdateError, match='Redirect'):
            await updates.download_file(client, 'https://github.com/file', tmp_path / 'setup', 100)
    assert not (tmp_path / 'setup').exists()


def test_status_is_local_and_download_preflight_require_token_and_idle(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    app = FastAPI()
    runtime = SimpleNamespace(settings=SimpleNamespace(database_url='sqlite:///data/test.db'), _locks={})
    mount_settings(app, lambda: runtime)
    with TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 1)) as client:
        assert client.get('/system/settings/updates/status').json()['current'] == __version__
        assert client.post('/system/settings/updates/download').status_code == 403
        headers = {'X-Better-Settings-Token': client.get('/system/settings').json()['token']}
        pending = tmp_path / 'restore-pending.json'
        pending.write_text('{}')
        assert client.post('/system/settings/updates/download', headers=headers).status_code == 409
        assert client.get('/system/settings/updates/status').json()['pending_restore'] is True
        path = '/system/settings/backups/desktop-preflight'
        assert client.post(path, headers=headers).status_code == 409
        app.state.desktop_stop = lambda: None
        app.state.data_pipeline_collector = SimpleNamespace(running=True)
        assert client.post(path, headers=headers).status_code == 409
        app.state.data_pipeline_collector.running = False
        assert client.post(path, headers=headers).status_code == 200
        assert pending.exists(), 'preflight must never apply a restore'


def test_native_actions_do_not_spawn_without_trusted_window(tmp_path, monkeypatch):
    from better.desktop.maintenance import DesktopActions
    actions = DesktopActions(tmp_path, 8001)
    actions._window = SimpleNamespace(get_current_url=lambda: 'https://evil.example')
    spawned = []
    monkeypatch.setattr('better.desktop.maintenance.subprocess.Popen', lambda *a, **k: spawned.append(a))
    assert actions.restart()['status'] == 'ERROR'
    assert actions.install_update()['status'] == 'ERROR'
    assert not spawned


def test_native_restart_starts_one_hidden_helper_and_closes_only_its_window(tmp_path, monkeypatch):
    from io import BytesIO

    from better.desktop import maintenance
    monkeypatch.setattr(maintenance, 'sys', SimpleNamespace(platform='win32', frozen=True, executable='C:/Apps/Better.exe'))
    monkeypatch.setattr(maintenance.urllib.request, 'urlopen', lambda *a, **kw: BytesIO(b'{"token":"local-test-token"}'))
    calls, closed = [], []
    monkeypatch.setattr(maintenance.subprocess, 'Popen', lambda *a, **kw: calls.append((a, kw)))
    actions = maintenance.DesktopActions(tmp_path, 8001)
    actions._window = SimpleNamespace(get_current_url=lambda: 'http://127.0.0.1:8001/#system/backup',
                                     destroy=lambda: closed.append(True))
    assert actions.restart()['status'] == 'STARTING'
    assert actions.restart()['status'] == 'ERROR'
    assert len(calls) == 1 and closed == [True]
    command = calls[0][0][0]
    assert '-WindowProcessId' in command and str(tmp_path) in command
    assert calls[0][1]['creationflags'] == maintenance.subprocess.CREATE_NO_WINDOW
    assert list((tmp_path / 'updates').glob('control-*.ps1'))


def test_helper_waits_for_app_then_stops_backend_and_verifies_before_install():
    script = (Path(__file__).parents[2] / 'better/desktop/maintenance.ps1').read_text()
    assert script.index('Wait-Process') < script.index('--stop') < script.index('Get-FileHash') < script.index('$Install =')
    assert 'restore-pending.json' in script and 'ready.json' in script
    assert '-Wait -PassThru' in script and '/NORESTART' in script
    assert 'Stop-Process' not in script
    assert 'Start-Process -FilePath $AppExecutable -ArgumentList $Arguments -WindowStyle Normal' in script
