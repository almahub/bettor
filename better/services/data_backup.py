"""Verified portable data archives; replacement is performed only at offline startup."""
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
import zipfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from uuid import uuid4

from better import __version__
from better.services.credential_store import (
    AGENT_NAMES,
    FLAG_NAMES,
    SECRET_NAMES,
    CredentialError,
    CredentialStore,
    WindowsProtector,
    atomic_write,
)

MAX_ARCHIVE = 1024 ** 3
MAX_EXPANDED = 4 * 1024 ** 3
ROOT_FILES = {'.env', '.env.dpapi', 'credentials.dpapi'}


class BackupError(ValueError):
    pass


def allowed(name):
    path = PurePosixPath(name)
    forbidden = {'CON', 'PRN', 'AUX', 'NUL', *{f'COM{i}' for i in range(1, 10)}, *{f'LPT{i}' for i in range(1, 10)}}
    return (not path.is_absolute() and '\\' not in name and ':' not in name
            and all(part not in {'..', '.'} for part in path.parts)
            and all(not part.endswith((' ', '.')) and part.split('.')[0].upper() not in forbidden for part in path.parts)
            and (name in ROOT_FILES or (len(path.parts) > 1 and path.parts[0] in {'data', 'config'}))
            and not name.endswith(('-wal', '-shm', '-journal')))


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def verify_database(path):
    try:
        with closing(sqlite3.connect(Path(path).as_uri() + '?mode=ro', uri=True)) as connection:
            if connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise BackupError('Database del backup non integro.')
    except sqlite3.Error:
        raise BackupError('Database del backup non leggibile.') from None


def create_backup(home):
    home = Path(home).resolve()
    folder = home / 'backups'
    folder.mkdir(parents=True, exist_ok=True)
    name = 'Better-Backup-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + uuid4().hex[:8] + '.zip'
    destination = folder / name
    with tempfile.TemporaryDirectory(prefix='better-backup-', dir=folder) as temporary:
        stage = Path(temporary)
        for root in ('data', 'config'):
            source = home / root
            if source.is_symlink() or source.is_junction():
                raise BackupError('Cartella dati collegata: backup non consentito.')
            for path in sorted(source.rglob('*')):
                if path.is_symlink() or path.is_junction():
                    raise BackupError('Il backup non include collegamenti simbolici.')
                if not path.is_file() or not allowed(path.relative_to(home).as_posix()):
                    continue
                target = stage / path.relative_to(home)
                target.parent.mkdir(parents=True, exist_ok=True)
                if path.suffix.lower() in {'.db', '.sqlite', '.sqlite3'}:
                    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as original:
                        with closing(sqlite3.connect(target)) as copied:
                            original.backup(copied)
                            copied.execute('PRAGMA journal_mode=DELETE')
                    verify_database(target)
                else:
                    shutil.copyfile(path, target)
        env = home / '.env'
        if env.exists():
            original = env.read_bytes()
            lines = original.decode('utf-8-sig').splitlines()
            safe = [line for line in lines if '=' not in line or line.split('=', 1)[0].strip() not in SECRET_NAMES]
            # Agent IDs and operational flags are portable; only keys stay DPAPI-bound.
            try:
                configured = CredentialStore(home).values()
            except CredentialError:
                configured = {}  # Preserve opaque keys; data backup must remain possible.
            safe.extend(f'{name}={configured[name]}' for name in sorted(AGENT_NAMES | FLAG_NAMES)
                        if configured.get(name) is not None)
            (stage / '.env').write_text('\n'.join(safe) + '\n', encoding='utf-8')
            (stage / '.env.dpapi').write_bytes(WindowsProtector().protect(original))
        vault = home / 'credentials.dpapi'
        if vault.exists():
            shutil.copyfile(vault, stage / vault.name)
        paths = [p for p in stage.rglob('*') if p.is_file()]
        manifest = {'format': 1, 'application': 'Better', 'version': __version__,
                    'created_at': datetime.now(timezone.utc).isoformat(),
                    'files': {p.relative_to(stage).as_posix(): {'sha256': digest(p), 'size': p.stat().st_size} for p in paths}}
        (stage / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
        temporary_zip = destination.with_suffix('.partial')
        try:
            with zipfile.ZipFile(temporary_zip, 'w', zipfile.ZIP_DEFLATED) as archive:
                for path in stage.rglob('*'):
                    if path.is_file():
                        archive.write(path, path.relative_to(stage).as_posix())
            if temporary_zip.stat().st_size > MAX_ARCHIVE:
                raise BackupError('Backup oltre il limite di 1 GB.')
            # Read every entry and verify hashes before publishing the archive.
            with tempfile.TemporaryDirectory(dir=folder) as validation:
                unpack_verified(temporary_zip, Path(validation))
            os.replace(temporary_zip, destination)
        finally:
            temporary_zip.unlink(missing_ok=True)
    return destination


def unpack_verified(archive_path, destination):
    try:
        if Path(archive_path).stat().st_size > MAX_ARCHIVE:
            raise BackupError('Archivio oltre il limite di 1 GB.')
        with zipfile.ZipFile(archive_path) as archive:
            entries = archive.infolist()
            names = [e.filename for e in entries]
            if len(entries) > 100000 or len(set(n.lower() for n in names)) != len(names) or sum(e.file_size for e in entries) > MAX_EXPANDED:
                raise BackupError('Archivio troppo grande o con nomi duplicati.')
            for entry in entries:
                if (entry.filename != 'manifest.json' and not allowed(entry.filename)) or stat.S_ISLNK(entry.external_attr >> 16) or entry.is_dir():
                    raise BackupError('Archivio con percorsi non consentiti.')
            info = archive.getinfo('manifest.json')
            if info.file_size > 16 * 1024 ** 2:
                raise BackupError('Indice del backup troppo grande.')
            manifest = json.loads(archive.read(info))
            if manifest.get('format') != 1 or manifest.get('application') != 'Better' or not isinstance(manifest.get('files'), dict):
                raise BackupError('Formato backup non riconosciuto.')
            if set(names) != {'manifest.json', *manifest['files']}:
                raise BackupError('Contenuto non corrispondente al backup.')
            if not any(n.startswith('data/') and n.endswith('.db') for n in names) or 'config/888/app.yaml' not in names:
                raise BackupError('Backup incompleto: database o configurazione mancanti.')
            for entry in entries:
                if entry.filename == 'manifest.json':
                    continue
                target = destination / entry.filename
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(entry) as source, target.open('xb') as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
                expected = manifest['files'][entry.filename]
                if target.stat().st_size != expected['size'] or digest(target) != expected['sha256']:
                    raise BackupError('Integrità del backup non verificata.')
                if target.suffix.lower() in {'.db', '.sqlite', '.sqlite3'}:
                    verify_database(target)
            return manifest
    except BackupError:
        raise
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile, RuntimeError):
        raise BackupError('Backup danneggiato o non valido.') from None


def stage_restore(home, archive):
    home = Path(home).resolve()
    folder = home / 'backups'
    folder.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=folder) as temporary:
        manifest = unpack_verified(archive, Path(temporary))
    target = folder / ('restore-' + uuid4().hex + '.zip')
    shutil.copyfile(archive, target)
    atomic_write(home / 'restore-pending.json', json.dumps({'archive': target.name, 'sha256': digest(target)}).encode())
    return manifest


def restore_pending(home):
    """Caller must own the desktop instance mutex; no database connection may be open."""
    home = Path(home).resolve()
    marker = home / 'restore-pending.json'
    if not marker.exists():
        return None
    try:
        pending = json.loads(marker.read_text())
        name = pending['archive']
        if Path(name).name != name or not name.startswith('restore-') or not name.endswith('.zip'):
            raise BackupError('Richiesta di ripristino non valida.')
        archive = home / 'backups' / name
        if digest(archive) != pending['sha256']:
            raise BackupError('Il backup selezionato è cambiato.')
        with tempfile.TemporaryDirectory(prefix='restore-stage-', dir=home / 'backups') as temporary:
            stage = Path(temporary)
            manifest = unpack_verified(archive, stage)
            def version_tuple(version):
                return tuple(int(part) for part in str(version).split('.'))
            if version_tuple(manifest['version']) > version_tuple(__version__):
                raise BackupError('Installa una versione Better compatibile prima del ripristino.')
            credentials_restored = False
            vault = stage / 'credentials.dpapi'
            if vault.exists():
                try:
                    CredentialStore(stage).read()
                    credentials_restored = True
                except CredentialError:
                    vault.unlink()
            encrypted_env = stage / '.env.dpapi'
            if encrypted_env.exists():
                try:
                    (stage / '.env').write_bytes(WindowsProtector().unprotect(encrypted_env.read_bytes()))
                    credentials_restored = True
                except CredentialError:
                    pass  # On another Windows account keep non-secret configuration only.
                encrypted_env.unlink()
            # Never import automatic activity on a new installation without review.
            env = stage / '.env'
            with env.open('a', encoding='utf-8') as stream:
                stream.write('\nBETTER_PIPELINE_AUTO_COLLECT=false\n')
            if credentials_restored:
                store = CredentialStore(stage)
                content = store.read()
                store.write({**content['values'], 'BETTER_PIPELINE_AUTO_COLLECT': 'false'}, content['revision'])
            # Reject old absolute SQLite paths rather than opening the source PC database.
            import yaml
            configuration = yaml.safe_load((stage / 'config/888/app.yaml').read_text(encoding='utf-8'))
            database_url = configuration.get('database_url', '')
            if not database_url.startswith('sqlite:///data/') or '..' in database_url.split('/'):
                raise BackupError('Il backup deve usare un database locale relativo a data/.')
            rollback = create_backup(home) if (home / 'data').exists() else None
            moved = []
            old = home / 'backups' / ('restore-rollback-' + uuid4().hex)
            old.mkdir()
            try:
                for name in ('data', 'config', '.env', 'credentials.dpapi'):
                    current, replacement = home / name, stage / name
                    if current.exists():
                        os.replace(current, old / name)
                    moved.append(name)
                    if replacement.exists():
                        os.replace(replacement, current)
                marker.unlink()
                result = {'status': 'RESTORED', 'credentials_restored': credentials_restored,
                          'safety_backup': rollback.name if rollback else None,
                          'message': 'Dati ripristinati. Verifica credenziali e riattiva il collector quando pronto.'}
                atomic_write(home / 'restore-result.json', json.dumps(result).encode())
                return result
            except Exception:
                for name in reversed(moved):
                    target = home / name
                    if target.is_dir():
                        shutil.rmtree(target)
                    else:
                        target.unlink(missing_ok=True)
                    if (old / name).exists():
                        os.replace(old / name, target)
                raise
    except BackupError:
        raise
    except Exception:
        raise BackupError('Ripristino non riuscito; conservare il backup e controllare lo spazio disponibile.') from None
