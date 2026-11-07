"""Explicit native actions; the backend never restores or installs while running."""
import json
import os
import shutil
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4


class DesktopActions:
    def __init__(self, home, port):
        # pywebview recursively exposes public attributes, not just methods.
        # Never expose Path objects or the native window/COM object graph.
        self._home, self._port = Path(home).resolve(), port
        self._window = None
        self._lock = threading.Lock()

    def restart(self):
        return self._start('restart')

    def install_update(self):
        return self._start('update')

    def _start(self, mode):
        if not self._lock.acquire(blocking=False):
            return {'status': 'ERROR', 'message': 'Operazione già in corso.'}
        started = False
        try:
            current = urlsplit(self._window.get_current_url())
            if current.scheme != 'http' or current.hostname != '127.0.0.1' or current.port != self._port:
                raise ValueError('Origine non consentita.')
            if not getattr(sys, 'frozen', False) or sys.platform != 'win32':
                return {'status': 'ERROR', 'message': 'Riavvio integrato disponibile nell’applicazione Windows installata.'}
            base = f'http://127.0.0.1:{self._port}/system/settings'
            with urllib.request.urlopen(base, timeout=5) as response:
                token = json.load(response)['token']
            request = urllib.request.Request(base + '/backups/desktop-preflight', data=b'', method='POST',
                                             headers={'X-Better-Settings-Token': token})
            with urllib.request.urlopen(request, timeout=5):
                pass
            if mode == 'update':
                with urllib.request.urlopen(base + '/updates/status', timeout=5) as response:
                    status = json.load(response)
                if not status['prepared'] or status['pending_restore']:
                    return {'status': 'ERROR', 'message': 'Verifica il download e completa o annulla prima il ripristino pendente.'}
            from better.desktop.launcher import resource_root
            folder = self._home / 'updates'
            folder.mkdir(parents=True, exist_ok=True)
            helper = folder / ('control-' + uuid4().hex + '.ps1')
            shutil.copyfile(resource_root() / 'better/desktop/maintenance.ps1', helper)
            powershell = Path(os.environ['SYSTEMROOT']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
            subprocess.Popen([str(powershell), '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                              '-File', str(helper), '-Mode', mode, '-HomeDirectory', str(self._home),
                              '-AppExecutable', sys.executable, '-Port', str(self._port),
                              '-WindowProcessId', str(os.getpid())], creationflags=subprocess.CREATE_NO_WINDOW)
            started = True
            self._window.destroy()
            return {'status': 'STARTING'}
        except Exception:
            return {'status': 'ERROR', 'message': 'Operazione non avviata. Attendi la conclusione di analisi/checkpoint e riprova. Nessun dato sostituito.'}
        finally:
            if not started:
                self._lock.release()
