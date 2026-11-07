"""Download only public release assets from the configured repository; never install in-process."""
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from better import __version__
from better.services.credential_store import atomic_write

RELEASE_URL = 'https://api.github.com/repos/almahub/bettor/releases/latest'
MAX_SETUP = 256 * 1024 * 1024


class UpdateError(ValueError):
    pass


def release_info(release):
    version = str(release.get('tag_name', '')).removeprefix('v')
    if not re.fullmatch(r'\d+\.\d+\.\d+', version) or release.get('draft') or release.get('prerelease'):
        raise UpdateError('Release stabile non valida.')
    assets = release.get('assets', [])
    for prefix in ('Bettor', 'Better'):
        name = f'{prefix}-Setup-{version}-x64.exe'
        found = {key: [a for a in assets if a.get('name') == key] for key in (name, name + '.sha256')}
        if all(len(items) == 1 for items in found.values()):
            urls = [found[key][0].get('browser_download_url', '') for key in found]
            base = f'https://github.com/almahub/bettor/releases/download/v{version}/'
            if any(url != base + key for url, key in zip(urls, found, strict=True)):
                raise UpdateError('Origine del setup non consentita.')
            available = tuple(map(int, version.split('.'))) > tuple(map(int, __version__.split('.')))
            return {'status': 'AVAILABLE' if available else 'CURRENT', 'current': __version__,
                    'latest': version, 'url': f'https://github.com/almahub/bettor/releases/tag/v{version}',
                    'asset': name, 'download_url': urls[0], 'checksum_url': urls[1]}
    raise UpdateError('Setup o checksum mancanti o ambigui.')


async def latest_release():
    async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
        result = await client.get(RELEASE_URL, headers={'Accept': 'application/vnd.github+json'})
    if result.status_code == 404:
        return {'status': 'NO_RELEASE', 'current': __version__}
    result.raise_for_status()
    return release_info(result.json())


def allowed_download(url):
    parsed = urlsplit(str(url))
    return (parsed.scheme == 'https' and not parsed.username and not parsed.password
            and parsed.port in (None, 443) and parsed.hostname in {
                'github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com'})


async def download_file(client, url, destination, limit):
    for _ in range(6):
        if not allowed_download(url):
            raise UpdateError('Redirect download non consentito.')
        async with client.stream('GET', url, follow_redirects=False) as result:
            if result.is_redirect:
                url = str(result.url.join(result.headers['location']))
                continue
            result.raise_for_status()
            count = 0
            with destination.open('wb') as stream:
                async for chunk in result.aiter_bytes():
                    count += len(chunk)
                    if count > limit:
                        raise UpdateError('Download oltre il limite consentito.')
                    stream.write(chunk)
            if not count:
                raise UpdateError('Download vuoto.')
            return
    raise UpdateError('Troppi redirect nel download.')


async def prepare_update(home, release):
    if release['status'] != 'AVAILABLE':
        raise UpdateError('Nessun aggiornamento più recente disponibile.')
    folder = Path(home).resolve() / 'updates'
    folder.mkdir(parents=True, exist_ok=True)
    setup = folder / (uuid4().hex + '.partial')
    checksum = folder / (uuid4().hex + '.sha256')
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(180, connect=15)) as client:
            await download_file(client, release['checksum_url'], checksum, 4096)
            text = checksum.read_text(encoding='ascii').strip()
            match = re.fullmatch(r'([a-fA-F0-9]{64})\s+\*?' + re.escape(release['asset']), text)
            if not match:
                raise UpdateError('Checksum non valido per questo setup.')
            await download_file(client, release['download_url'], setup, MAX_SETUP)
        with setup.open('rb') as stream:
            actual = hashlib.file_digest(stream, 'sha256').hexdigest()
        if actual != match[1].lower():
            raise UpdateError('Integrità setup non verificata: installazione bloccata.')
        target = folder / release['asset']
        setup.replace(target)
        ready = {'version': release['latest'], 'filename': target.name, 'sha256': actual}
        atomic_write(folder / 'ready.json', json.dumps(ready).encode())
        return {'status': 'READY', 'version': release['latest']}
    finally:
        setup.unlink(missing_ok=True)
        checksum.unlink(missing_ok=True)


def prepared_update(home):
    try:
        folder = Path(home).resolve() / 'updates'
        value = json.loads((folder / 'ready.json').read_text())
        if not re.fullmatch(r'\d+\.\d+\.\d+', value['version']):
            return None
        if value['filename'] not in {f'{p}-Setup-{value["version"]}-x64.exe' for p in ('Better', 'Bettor')}:
            return None
        if not re.fullmatch(r'[a-f0-9]{64}', value['sha256']) or not (folder / value['filename']).is_file():
            return None
        if tuple(map(int, value['version'].split('.'))) <= tuple(map(int, __version__.split('.'))):
            return None
        return {'status': 'READY', 'version': value['version']}
    except (OSError, ValueError, KeyError, TypeError):
        return None
