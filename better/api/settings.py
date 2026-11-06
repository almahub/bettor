"""Local-only credential administration. Stored secrets are never serialized."""
import hmac
import json
import re
import secrets
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from better.services.credential_store import (
    AGENT_NAMES,
    FLAG_NAMES,
    SECRET_NAMES,
    CredentialError,
    CredentialStore,
)


def mount_settings(app, runtime):
    token = secrets.token_urlsafe(32)
    initial = None

    def guard(request, write=False):
        host = urlsplit(str(request.base_url)).hostname
        if not request.client or request.client.host not in {'127.0.0.1', '::1'} or host not in {'127.0.0.1', 'localhost', '::1'}:
            raise HTTPException(403, 'Impostazioni disponibili soltanto sul PC locale.')
        origin = request.headers.get('origin')
        if request.headers.get('sec-fetch-site') == 'cross-site' or (origin and origin.rstrip('/') != str(request.base_url).rstrip('/')):
            raise HTTPException(403, 'Origine non consentita.')
        if write and not hmac.compare_digest(request.headers.get('x-better-settings-token', ''), token):
            raise HTTPException(403, 'Aggiorna Impostazioni e riprova.')

    def report():
        nonlocal initial
        store = CredentialStore()
        values = store.values()
        if initial is None:
            initial = dict(values)
        from better.services.installation_status import installation_status
        return {'revision': store.read()['revision'], 'token': token,
            'installation': installation_status(runtime(), values),
            'fields': {name: {'configured': bool(values.get(name)), **({'value': values.get(name) or ''} if name not in SECRET_NAMES else {})}
                       for name in sorted(SECRET_NAMES | AGENT_NAMES | FLAG_NAMES)},
            'restart_required': values != initial,
            'data_directory': str(Path.cwd()),
            'database': str(runtime().settings.database_url),
            'protection': 'Windows DPAPI: account Windows corrente',
            'ready': bool(values.get('OPENAI_API_KEY') and all(values.get(k) for k in AGENT_NAMES))}

    def response(value):
        return JSONResponse(value, headers={'Cache-Control': 'no-store', 'Pragma': 'no-cache'})

    @app.get('/system/settings')
    async def settings(request: Request):
        guard(request)
        try:
            return response(report())
        except CredentialError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post('/system/settings')
    async def save(request: Request):
        guard(request, True)
        raw = await request.body()
        if len(raw) > 16000:
            raise HTTPException(413, 'Configurazione troppo grande.')
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict) or set(payload) - {'revision', 'updates', 'remove'}:
                raise ValueError()
            updates, remove = payload.get('updates', {}), payload.get('remove', [])
            if not isinstance(updates, dict) or not isinstance(remove, list):
                raise ValueError()
            allowed = SECRET_NAMES | AGENT_NAMES | FLAG_NAMES
            if any(not isinstance(k, str) or k not in allowed for k in [*updates, *remove]):
                raise ValueError()
            store = CredentialStore()
            values = store.values()
            for name, value in updates.items():
                if not isinstance(value, str) or len(value) > 4096 or any(c in value for c in '\r\n\x00'):
                    raise ValueError()
                value = value.strip()
                if name in AGENT_NAMES and value and not re.fullmatch(r'agent_[A-Za-z0-9_-]+', value):
                    raise ValueError()
                if name in FLAG_NAMES and value not in {'true', 'false'}:
                    raise ValueError()
                if value:
                    values[name] = value
            for name in remove:
                values[name] = None
            if values.get('OPENAI_RESEARCH_AGENT_ID') and values.get('OPENAI_RESEARCH_AGENT_ID') == values.get('OPENAI_FOOTBALL_DATA_AGENT_ID'):
                raise HTTPException(422, 'I due agenti devono avere ID distinti.')
            store.write(values, payload.get('revision'))
            store.import_legacy()
            return response(report())
        except CredentialError as exc:
            raise HTTPException(409, str(exc)) from None
        except (ValueError, TypeError):
            raise HTTPException(422, 'Configurazione non valida: controlla campi e ID degli agenti.') from None

    checks = {}

    @app.post('/system/settings/verify/{provider}')
    async def verify(provider: str, request: Request):
        guard(request, True)
        from time import monotonic
        if monotonic() - checks.get(provider, -100) < 10:
            raise HTTPException(429, 'Attendi dieci secondi prima di ripetere la verifica.')
        checks[provider] = monotonic()
        endpoints = {
            'openai': ('OPENAI_API_KEY', 'https://api.openai.com/v1/models', 'Authorization'),
            'football-data': ('FOOTBALL_DATA_ORG_API_KEY', 'https://api.football-data.org/v4/competitions/SA', 'X-Auth-Token'),
            'api-football': ('API_FOOTBALL_API_KEY', 'https://v3.football.api-sports.io/status', 'x-apisports-key')}
        if provider not in endpoints:
            raise HTTPException(404, 'Verifica non disponibile.')
        name, url, header = endpoints[provider]
        try:
            key = CredentialStore().values().get(name)
            if not key:
                return response({'status': 'NOT_CONFIGURED', 'message': 'Chiave non configurata.'})
            async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
                result = await client.get(url, headers={header: ('Bearer ' if header == 'Authorization' else '') + key})
            code = result.status_code
            status = {401: 'AUTH_ERROR', 403: 'ACCESS_DENIED', 429: 'RATE_LIMIT'}.get(code, 'OK' if code == 200 else 'PROVIDER_ERROR')
            if status == 'OK':
                try:
                    body = result.json()
                    valid = isinstance(body, dict) and (isinstance(body.get('data'), list) if provider == 'openai' else body.get('id') is not None if provider == 'football-data' else isinstance(body.get('response'), dict) and not body.get('errors'))
                    if not valid:
                        status = 'PROVIDER_ERROR'
                except ValueError:
                    status = 'PROVIDER_ERROR'
            return response({'status': status, 'http_status': code, 'message': 'Autenticazione verificata; non certifica le chiamate ai saved agent.' if status == 'OK' and provider == 'openai' else 'Accesso verificato.' if status == 'OK' else 'Verifica non riuscita: controlla credenziali, permessi e piano del provider.'})
        except CredentialError as exc:
            raise HTTPException(409, str(exc)) from None
        except httpx.HTTPError:
            return response({'status': 'NETWORK_ERROR', 'message': 'Provider non raggiungibile o timeout.'})

    from better.api.backups import mount_backups
    mount_backups(app, runtime, guard, response)
