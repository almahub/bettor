"""Local backup endpoints, sharing settings origin and CSRF protection."""
import asyncio
import json
import tempfile
from pathlib import Path

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from better import __version__
from better.services.data_backup import MAX_ARCHIVE, BackupError, create_backup, stage_restore


def mount_backups(app, runtime, guard, response):
    active = 0
    update_lock = asyncio.Lock()

    @app.middleware('http')
    async def restore_maintenance(request, call_next):
        nonlocal active
        maintenance = Path('restore-pending.json').exists()
        if maintenance and request.method not in {'GET', 'HEAD'} and not request.url.path.startswith('/system/settings/backups'):
            return JSONResponse({'detail': 'Ripristino programmato: riavvia Bettor oppure annulla il ripristino.'}, status_code=409)
        relevant = request.method not in {'GET', 'HEAD'} and not request.url.path.startswith('/system/settings')
        if relevant:
            active += 1
        try:
            return await call_next(request)
        finally:
            if relevant:
                active -= 1

    def idle():
        collector = getattr(app.state, 'data_pipeline_collector', None)
        if active or (collector and collector.running) or any(lock.locked() for lock in getattr(runtime(), '_locks', {}).values()):
            raise HTTPException(409, 'Analisi o checkpoint in corso: attendi la conclusione e riprova.')

    @app.post('/system/settings/backups/desktop-stop', include_in_schema=False)
    async def desktop_stop(request: Request):
        guard(request, True)
        idle()
        stop = getattr(app.state, 'desktop_stop', None)
        if stop is None:
            raise HTTPException(409, 'Arresto desktop non disponibile in questa modalità.')
        stop()
        return response({'status': 'STOPPING'})

    @app.post('/system/settings/backups/desktop-preflight', include_in_schema=False)
    async def desktop_preflight(request: Request):
        guard(request, True)
        idle()
        if getattr(app.state, 'desktop_stop', None) is None:
            raise HTTPException(409, 'Riavvio integrato disponibile soltanto nell’applicazione desktop.')
        return response({'status': 'READY'})

    def archive_path(name):
        if Path(name).name != name or not name.startswith('Better-Backup-') or not name.endswith('.zip'):
            raise HTTPException(404, 'Backup non trovato.')
        path = Path.cwd() / 'backups' / name
        if not path.is_file() or path.is_symlink():
            raise HTTPException(404, 'Backup non trovato.')
        return path

    @app.get('/system/settings/backups')
    async def listing(request: Request):
        guard(request)
        files = sorted((Path.cwd() / 'backups').glob('Better-Backup-*.zip'), reverse=True)
        result = None
        try:
            result = json.loads(Path('restore-result.json').read_text())
        except (FileNotFoundError, ValueError):
            pass
        return response({'backups': [{'name': p.name, 'size': p.stat().st_size} for p in files if p.is_file() and not p.is_symlink()],
                         'pending_restore': Path('restore-pending.json').exists(), 'last_restore': result,
                         'data_directory': str(Path.cwd()), 'current': __version__})

    @app.post('/system/settings/backups')
    async def backup(request: Request):
        guard(request, True)
        idle()
        try:
            path = create_backup(Path.cwd())
            return response({'name': path.name, 'size': path.stat().st_size})
        except (BackupError, OSError):
            raise HTTPException(409, 'Backup non creato: verifica integrità, spazio e permessi della cartella dati.') from None

    @app.get('/system/settings/backups/download/{name}')
    async def download(name: str, request: Request):
        guard(request)
        return FileResponse(archive_path(name), media_type='application/zip', filename=name, headers={'Cache-Control': 'no-store'})

    @app.post('/system/settings/backups/restore/{name}')
    async def restore(name: str, request: Request):
        guard(request, True)
        idle()
        try:
            stage_restore(Path.cwd(), archive_path(name))
            return response({'status': 'PENDING_RESTART', 'message': 'Ripristino verificato e programmato. Arresta il backend e riavvia Bettor; nessun dato è stato ancora sostituito.'})
        except BackupError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.post('/system/settings/backups/import')
    async def import_backup(request: Request):
        guard(request, True)
        idle()
        folder = Path.cwd() / 'backups'
        folder.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=folder) as temporary:
            path = Path(temporary) / 'import.zip'
            size = 0
            with path.open('wb') as stream:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > MAX_ARCHIVE:
                        raise HTTPException(413, 'Backup troppo grande: massimo 1 GB.')
                    stream.write(chunk)
            idle()
            try:
                stage_restore(Path.cwd(), path)
            except BackupError as exc:
                raise HTTPException(422, str(exc)) from None
        return response({'status': 'PENDING_RESTART', 'message': 'Import verificato. Arresta il backend e riavvia Bettor per applicarlo. Su un altro PC reinserisci le chiavi API.'})

    @app.post('/system/settings/backups/cancel')
    async def cancel(request: Request):
        guard(request, True)
        Path('restore-pending.json').unlink(missing_ok=True)
        return response({'status': 'CANCELLED'})

    @app.get('/system/settings/updates/status')
    async def update_status(request: Request):
        guard(request)
        from better.services.desktop_updates import prepared_update
        return response({'current': __version__, 'prepared': prepared_update(Path.cwd()),
                         'desktop': getattr(app.state, 'desktop_stop', None) is not None,
                         'pending_restore': Path('restore-pending.json').exists()})

    @app.get('/system/settings/updates')
    async def updates(request: Request):
        guard(request)
        from better.services.desktop_updates import UpdateError, latest_release
        try:
            result = await latest_release()
            return response({k: v for k, v in result.items() if k not in {'download_url', 'checksum_url'}})
        except (httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError, UpdateError):
            return response({'status': 'UNAVAILABLE', 'current': __version__})

    @app.post('/system/settings/updates/download')
    async def download_update(request: Request):
        guard(request, True)
        from better.services.desktop_updates import UpdateError, latest_release, prepare_update
        if Path('restore-pending.json').exists():
            raise HTTPException(409, 'Completa o annulla il ripristino pendente prima dell’aggiornamento.')
        if update_lock.locked():
            raise HTTPException(409, 'Download già in corso: attendi la conclusione.')
        async with update_lock:
            try:
                release = await latest_release()
                return response(await prepare_update(Path.cwd(), release))
            except UpdateError as exc:
                raise HTTPException(409, str(exc)) from None
            except (httpx.HTTPError, OSError, ValueError, KeyError, TypeError, AttributeError):
                raise HTTPException(409, 'Aggiornamento non scaricato o non verificato. Nessuna installazione eseguita; riprova.') from None
