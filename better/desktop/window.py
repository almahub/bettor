"""Desktop presentation; closing the window leaves the collector running."""

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def identity_at(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/desktop/identity", timeout=2) as response:
            return json.load(response).get("identity")
    except urllib.error.URLError:
        return None


def backend_command(home, port):
    executable = (
        [sys.executable]
        if getattr(sys, "frozen", False)
        else [sys.executable, "-m", "better.desktop.launcher"]
    )
    return executable + ["--data-dir", str(Path(home).resolve()), "--port", str(port), "--no-browser"]


def ensure_backend(home, port, identity):
    current = identity_at(port)
    if current is not None:
        if current != identity:
            raise RuntimeError("La porta è usata da un’altra installazione Bettor.")
        return
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    child = subprocess.Popen(
        backend_command(home, port),
        creationflags=flags,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(90):
        current = identity_at(port)
        if current == identity:
            return
        if current is not None:
            raise RuntimeError("La porta è usata da un’altra installazione Bettor.")
        if child.poll() not in (None, 0):
            break
        time.sleep(1)
    raise RuntimeError("Bettor non si è avviato. Controlla logs/desktop.log nella cartella dati.")


def show(url, home=None, port=None):
    import webview

    from better.desktop.launcher import resource_root

    webview.settings["ALLOW_DOWNLOADS"] = True
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
    from better.desktop.maintenance import DesktopActions

    actions = DesktopActions(home, port) if home is not None and port is not None else None
    window = webview.create_window("Bettor", url, width=1360, height=900, min_size=(960, 640), js_api=actions)
    if actions is not None:
        actions._window = window
    # Force the current renderer: never fall back to legacy Internet Explorer.
    webview.start(gui="edgechromium", debug=False, private_mode=True,
                  icon=str(resource_root() / "better/web/brand/bettor.ico"))
