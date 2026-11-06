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
import webbrowser
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
        env.write_text("# Better: inserisci qui le tue chiavi personali. Non condividerle.\n"
                       "OPENAI_API_KEY=\nOPENAI_RESEARCH_AGENT_ID=\n"
                       "OPENAI_FOOTBALL_DATA_AGENT_ID=\nFOOTBALL_DATA_ORG_API_KEY=\n"
                       "BETTER_PIPELINE_AUTO_COLLECT=false\n", encoding="utf-8")
    for line in env.read_text(encoding="utf-8-sig").splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ[key.strip()] = value.strip().strip("\"'")
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, default=Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Better")
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--backup-only", action="store_true")
    args = parser.parse_args()
    mutex = None
    try:
        home = initialize(args.data_dir)
        identity = hashlib.sha256(str(home).lower().encode()).hexdigest()[:24]
        if sys.platform == "win32":
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.CreateMutexW.restype = ctypes.c_void_p
            mutex = kernel.CreateMutexW(None, False, "Local\\Better-" + identity)
            if not mutex:
                raise OSError("Impossibile acquisire il blocco di avvio")
            if ctypes.get_last_error() == 183:
                if args.backup_only:
                    raise RuntimeError("Better è attivo: arrestarlo prima di aggiornare")
                with urllib.request.urlopen(f"http://127.0.0.1:{args.port}/desktop/identity", timeout=3) as response:
                    if json.load(response).get("identity") != identity:
                        raise RuntimeError("Istanza non corrispondente: controllare la porta")
                if not args.no_browser:
                    webbrowser.open(f"http://127.0.0.1:{args.port}/")
                return
        if args.backup_only:
            backup_databases(home)
            return
        import socket
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", args.port))
            except OSError as exc:
                raise RuntimeError("Porta occupata da un altro processo. Chiudere l'altra istanza oppure scegliere un'altra porta.") from exc
        os.chdir(home)
        # Launch through existing services; no decision or financial logic changes.
        import better.api.app as api_module
        from better.cli.main import main as cli_main
        original = api_module.create_app

        def desktop_app(*a, **kw):
            app = original(*a, **kw)

            @app.get("/desktop/identity", include_in_schema=False)
            async def desktop_identity():
                return {"identity": identity}

            return app

        api_module.create_app = desktop_app
        if not args.no_browser:
            import threading
            def open_when_ready():
                import time
                for _ in range(90):
                    try:
                        with urllib.request.urlopen(f"http://127.0.0.1:{args.port}/health", timeout=1) as response:
                            if json.load(response).get("status") == "ok":
                                webbrowser.open(f"http://127.0.0.1:{args.port}/")
                                return
                    except Exception:
                        time.sleep(1)
            threading.Thread(target=open_when_ready, daemon=True).start()
        sys.argv = ["Better", "--config", "config/888", "--log-file", "data/logs/better.jsonl", "serve", "--port", str(args.port)]
        cli_main()
    except Exception as exc:
        message = f"Avvio Better non riuscito: {exc}"
        if sys.platform == "win32":
            ctypes.windll.user32.MessageBoxW(None, message, "Better", 16)
        else:
            print(message, file=sys.stderr)
        raise
    finally:
        if mutex:
            kernel.CloseHandle(ctypes.c_void_p(mutex))


if __name__ == "__main__":
    main()
