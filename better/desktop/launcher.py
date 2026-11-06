"""Independent installed runtime; mutable files never live in the program directory."""

import argparse
import ctypes
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


def resource_root():
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))


def initialize(home):
    home = Path(home).resolve()
    home.mkdir(parents=True, exist_ok=True)
    target = home / "config" / "888"
    target.mkdir(parents=True, exist_ok=True)
    for name in ("app.yaml", "sources.yaml"):
        dest = target / name
        if not dest.exists():
            shutil.copyfile(resource_root() / "config" / "888" / name, dest)
    env = home / ".env"
    if not env.exists():
        env.write_text(
            "# Bettor: inserisci qui le tue chiavi personali. Non condividerle.\n"
            "OPENAI_API_KEY=\nOPENAI_ADMIN_KEY=\nAPI_FOOTBALL_API_KEY=\nSPORTRADAR_API_KEY=\nBETTER_FEED_API_KEY=\nOPENAI_RESEARCH_AGENT_ID=\n"
            "OPENAI_FOOTBALL_DATA_AGENT_ID=\nFOOTBALL_DATA_ORG_API_KEY=\n"
            "OPENAI_USAGE_LIVE_ENABLED=false\nBETTER_PIPELINE_AUTO_COLLECT=false\n",
            encoding="utf-8",
        )
    for line in env.read_text(encoding="utf-8-sig").splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ[key.strip()] = value.strip().strip("\"'")
    from better.services.credential_store import CredentialStore, activate_credentials

    CredentialStore(home).import_legacy()
    activate_credentials(home)
    return home


def backup_databases(home):
    """Consistent SQLite snapshots, including any WAL contents."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backups = Path(home) / "backups" / stamp
    databases = sorted((Path(home) / "data").rglob("*.db"))
    for database in databases:
        target = backups / database.relative_to(home)
        target.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as source:
            with sqlite3.connect(target) as destination:
                source.backup(destination)
                if destination.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise RuntimeError("Verifica backup database fallita")
    return backups if databases else None


class InstanceLock:
    def __init__(self, identity, kind="backend"):
        self.handle = None
        self.acquired = True
        if sys.platform == "win32":
            self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            self.kernel.CreateMutexW.restype = ctypes.c_void_p
            self.handle = self.kernel.CreateMutexW(
                None, False, "Local\\Better-" + ("window-" if kind == "window" else "") + identity
            )
            if not self.handle:
                raise OSError("Impossibile acquisire il blocco di avvio")
            self.acquired = ctypes.get_last_error() != 183

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(ctypes.c_void_p(self.handle))
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def redirect_windowless_logs(home):
    # PyInstaller --windowed provides None for stdout/stderr. Uvicorn needs streams.
    if sys.stdout is None or sys.stderr is None:
        folder = Path(home) / "logs"
        folder.mkdir(parents=True, exist_ok=True)
        stream = (folder / "desktop.log").open("a", encoding="utf-8", buffering=1)
        if sys.stdout is None:
            sys.stdout = stream
        if sys.stderr is None:
            sys.stderr = stream


def stop_backend(port, identity):
    from better.desktop.window import identity_at

    current = identity_at(port)
    if current is None:
        return
    if current != identity:
        raise RuntimeError("Istanza non corrispondente: arresto annullato.")
    base = f"http://127.0.0.1:{port}"
    with urllib.request.urlopen(base + "/system/settings", timeout=5) as response:
        token = json.load(response)["token"]
    request = urllib.request.Request(
        base + "/system/settings/backups/desktop-stop",
        data=b"",
        headers={"X-Better-Settings-Token": token},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5):
        pass
    import time

    for _ in range(60):
        if identity_at(port) is None:
            return
        time.sleep(0.5)
    raise RuntimeError("Arresto ancora in corso. Attendi prima di aggiornare.")


def own_runtime(app, runtime):
    """Close async providers on the server loop, after collector shutdown."""
    from contextlib import asynccontextmanager

    original = app.router.lifespan_context

    @asynccontextmanager
    async def desktop_lifespan(application):
        try:
            async with original(application):
                yield
        finally:
            await runtime.close()

    app.router.lifespan_context = desktop_lifespan


def serve(home, port, identity):

    import uvicorn

    from better.api.app import create_app
    from better.config import Settings
    from better.observability.logging import configure_logging
    from better.services.application import make_application

    configure_logging(log_file=home / "data/logs/better.jsonl")
    runtime = make_application(settings=Settings.load(home / "config/888"))
    app = create_app(runtime)
    own_runtime(app, runtime)

    @app.get("/desktop/identity", include_in_schema=False)
    async def desktop_identity():
        return {"identity": identity}

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, use_colors=False))

    def shutdown():
        server.should_exit = True

    app.state.desktop_stop = shutdown
    server.run()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--data-dir", type=Path, default=Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Better"
    )
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--no-browser", action="store_true", help="Servizio in background senza finestra")
    parser.add_argument("--backup-only", action="store_true")
    parser.add_argument(
        "--setup", action="store_true", help="Riapri la configurazione iniziale a servizio fermo"
    )
    parser.add_argument("--stop", action="store_true", help="Arresto controllato del servizio desktop")
    args = parser.parse_args()
    home = args.data_dir.resolve()
    identity = hashlib.sha256(str(home).lower().encode()).hexdigest()[:24]
    try:
        redirect_windowless_logs(home)
        from better.desktop.onboarding import choose, prepare, ready
        from better.services.data_backup import create_backup, restore_pending

        if args.stop:
            stop_backend(args.port, identity)
            return
        if args.no_browser or args.backup_only:
            with InstanceLock(identity) as lock:
                if not lock.acquired:
                    if args.backup_only:
                        raise RuntimeError("Bettor è attivo: arrestarlo prima di aggiornare")
                    return
                if args.backup_only:
                    if (home / "data").exists():
                        create_backup(home)
                    return
                if not ready(home):
                    print("Prima configurazione necessaria: apri Bettor dal collegamento desktop.")
                    return
                restore_pending(home)
                initialize(home)
                os.chdir(home)
                serve(home, args.port, identity)
            return
        with InstanceLock(identity, "window") as window_lock:
            if not window_lock.acquired:
                if sys.platform == "win32":
                    handle = ctypes.windll.user32.FindWindowW(None, "Bettor")
                    if handle:
                        ctypes.windll.user32.ShowWindow(handle, 9)
                        ctypes.windll.user32.SetForegroundWindow(handle)
                return
            mode = None
            if args.setup or not ready(home):
                with InstanceLock(identity) as lock:
                    if not lock.acquired:
                        raise RuntimeError("Per la configurazione iniziale arresta prima il servizio Bettor.")
                    restore_pending(home)
                    choice = choose(home)
                    if choice is None:
                        return
                    mode, archive = choice
                    prepare(home, mode, initialize, archive)
            from better.desktop.window import ensure_backend, show

            ensure_backend(home, args.port, identity)
            suffix = "#system/settings" if mode in {"new", "restore"} else ""
            show(f"http://127.0.0.1:{args.port}/" + suffix)
    except Exception as exc:
        # Do not include exception text that could contain configuration secrets.
        import traceback

        if sys.stderr is not None:
            traceback.print_tb(exc.__traceback__, file=sys.stderr)
            print(type(exc).__name__, file=sys.stderr)
        message = (
            "Arresto non completato: verifica che non siano in corso analisi o checkpoint. Attendi e riprova. Nessun dato è stato cancellato."
            if args.stop
            else "Avvio Bettor non riuscito. Per la prima configurazione arresta il servizio Bettor. Verifica anche Microsoft Edge WebView2 Runtime. Dettagli in logs/desktop.log."
        )
        if sys.platform == "win32":
            ctypes.windll.user32.MessageBoxW(None, message, "Bettor", 16)
        raise


if __name__ == "__main__":
    main()
