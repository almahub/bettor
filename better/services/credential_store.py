"""Per-user Windows DPAPI store. Secrets never appear in status responses."""
import ctypes
import json
import os
import threading
from pathlib import Path
from uuid import uuid4

SECRET_NAMES = frozenset({'OPENAI_API_KEY', 'OPENAI_ADMIN_KEY', 'API_FOOTBALL_API_KEY',
    'FOOTBALL_DATA_ORG_API_KEY', 'SPORTRADAR_API_KEY', 'BETTER_FEED_API_KEY'})
AGENT_NAMES = frozenset({'OPENAI_RESEARCH_AGENT_ID', 'OPENAI_FOOTBALL_DATA_AGENT_ID'})
FLAG_NAMES = frozenset({'EXTERNAL_RESEARCH_ENABLED', 'FOOTBALL_DATA_RESEARCH_ENABLED',
    'OPENAI_USAGE_LIVE_ENABLED', 'BETTER_PIPELINE_AUTO_COLLECT'})
MANAGED_NAMES = SECRET_NAMES | AGENT_NAMES | FLAG_NAMES
_LOCK = threading.RLock()
_ACTIVE = {}
MAGIC = b'BETTER-CREDENTIALS-v1\0'


class CredentialError(ValueError):
    pass


class WindowsProtector:
    def _crypt(self, data, decrypt=False):
        if os.name != 'nt':
            raise CredentialError('Archivio protetto disponibile soltanto su Windows.')

        class Blob(ctypes.Structure):
            _fields_ = [('size', ctypes.c_ulong), ('data', ctypes.POINTER(ctypes.c_ubyte))]

        buffer = ctypes.create_string_buffer(data)
        source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
        output = Blob()
        crypt = ctypes.WinDLL('crypt32', use_last_error=True)
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
        function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(Blob)]
        function.restype = ctypes.c_int
        if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
            raise CredentialError('Credenziali non accessibili con questo account Windows.')
        try:
            return ctypes.string_at(output.data, output.size)
        finally:
            kernel.LocalFree.argtypes = [ctypes.c_void_p]
            kernel.LocalFree(ctypes.cast(output.data, ctypes.c_void_p))

    def protect(self, data):
        return self._crypt(data)

    def unprotect(self, data):
        return self._crypt(data, decrypt=True)


def legacy_values(path):
    try:
        lines = Path(path).read_text(encoding='utf-8-sig').splitlines()
    except FileNotFoundError:
        return {}
    values = {}
    for line in lines:
        if line.strip() and not line.lstrip().startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            if key.strip() in MANAGED_NAMES:
                values[key.strip()] = value.strip().strip('"').strip("'") or None
    return values


def atomic_write(path, data):
    path = Path(path)
    if path.is_symlink():
        raise CredentialError('Percorso credenziali non consentito.')
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp-' + uuid4().hex)
    try:
        with temporary.open('xb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class CredentialStore:
    def __init__(self, root=None, *, protector=None):
        self.root = Path(root or Path.cwd()).resolve()
        self.path = self.root / 'credentials.dpapi'
        self.env_path = self.root / '.env'
        self.protector = protector or WindowsProtector()

    def read(self):
        if not self.path.exists():
            return {'revision': 'initial', 'values': {}}
        if self.path.is_symlink() or self.path.stat().st_size > 100_000:
            raise CredentialError('Archivio credenziali non valido.')
        try:
            data = self.path.read_bytes()
            if not data.startswith(MAGIC):
                raise CredentialError('Archivio credenziali non valido.')
            payload = json.loads(self.protector.unprotect(data[len(MAGIC):]))
            if not isinstance(payload.get('revision'), str) or not isinstance(payload.get('values'), dict):
                raise ValueError()
            if any(k not in MANAGED_NAMES or (v is not None and not isinstance(v, str))
                   for k, v in payload['values'].items()):
                raise ValueError()
            return payload
        except CredentialError:
            raise
        except Exception:
            raise CredentialError('Archivio credenziali non accessibile o danneggiato.') from None

    def values(self):
        return {**legacy_values(self.env_path), **self.read()['values']}

    def write(self, values, revision):
        with _LOCK:
            if not isinstance(values, dict) or any(k not in MANAGED_NAMES or
                    (v is not None and (not isinstance(v, str) or len(v) > 4096 or
                     any(c in v for c in '\r\n\x00'))) for k, v in values.items()):
                raise CredentialError('Configurazione credenziali non valida.')
            if self.read()['revision'] != revision:
                raise CredentialError('Configurazione cambiata: aggiorna la pagina prima di salvare.')
            payload = {'revision': uuid4().hex, 'values': values}
            encoded = MAGIC + self.protector.protect(json.dumps(payload).encode('utf-8'))
            # Verify the encrypted data before committing it.
            if json.loads(self.protector.unprotect(encoded[len(MAGIC):])) != payload:
                raise CredentialError('Verifica salvataggio protetto fallita.')
            atomic_write(self.path, encoded)
            return payload['revision']

    def import_legacy(self):
        with _LOCK:
            legacy = legacy_values(self.env_path)
            if not legacy:
                return False
            current = self.read()
            missing = {k:v for k,v in legacy.items() if v is not None and k not in current['values']}
            if missing or not self.path.exists():
                self.write({**missing, **current['values']}, current['revision'])
            # Preserve an encrypted complete copy before removing managed values.
            original = self.env_path.read_bytes()
            backup = self.root / 'backups' / ('credentials-import-' + uuid4().hex + '.dpapi')
            atomic_write(backup, MAGIC + self.protector.protect(original))
            remaining = [line for line in original.decode('utf-8-sig').splitlines()
                         if line.lstrip().startswith('#') or '=' not in line
                         or line.split('=',1)[0].strip() not in MANAGED_NAMES]
            atomic_write(self.env_path, ('\n'.join(remaining) + '\n').encode('utf-8'))
            return True


def active_values(root=None):
    root = Path(root or Path.cwd()).resolve()
    key = str(root)
    if key not in _ACTIVE:
        store = CredentialStore(root)
        if not store.path.exists():
            return {}
        try:
            _ACTIVE[key] = store.read()['values']
        except CredentialError:
            # Never revive stale plaintext or inherited credentials after decrypt failure.
            _ACTIVE[key] = dict.fromkeys(MANAGED_NAMES)
    return dict(_ACTIVE[key])


def activate_credentials(root=None):
    root = Path(root or Path.cwd()).resolve()
    values = CredentialStore(root).read()['values']
    _ACTIVE[str(root)] = dict(values)
    for name, value in values.items():
        os.environ[name] = value or ''
    return values
