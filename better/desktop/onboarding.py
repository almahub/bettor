"""Offline first-run choices. Existing data are never silently discarded."""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from better.services.credential_store import atomic_write
from better.services.data_backup import create_backup, restore_pending, stage_restore

MARKER = "desktop-onboarding.json"
DATA_ITEMS = ("data", "config", ".env", ".env.dpapi", "credentials.dpapi", "restore-result.json")


def existing_data(home):
    home = Path(home)
    return any((home / name).exists() for name in DATA_ITEMS)


def ready(home):
    try:
        value = json.loads((Path(home) / MARKER).read_text(encoding="utf-8"))
        return value.get("format") == 1 and value.get("mode") in {"existing", "new", "restore"}
    except (OSError, ValueError, AttributeError):
        return False


def prepare(home, mode, initialize, archive=None):
    """Caller owns the backend mutex. All operations run before opening the DB."""
    home = Path(home).resolve()
    if mode not in {"existing", "new", "restore"}:
        raise ValueError("Scelta iniziale non valida.")
    if (home / "restore-pending.json").exists():
        raise ValueError("Completa o annulla prima il ripristino già programmato.")
    safety = None
    if mode == "restore":
        if not archive:
            raise ValueError("Seleziona un backup Bettor.")
        stage_restore(home, archive)
        result = restore_pending(home)
        safety = result.get("safety_backup")
        initialize(home)
    elif mode == "new":
        had_data = existing_data(home)
        if had_data:
            safety = create_backup(home).name
        folder = home / "backups" / ("first-run-previous-" + uuid4().hex)
        moved = []
        originals = {name for name in DATA_ITEMS if (home / name).exists()}
        try:
            for name in DATA_ITEMS:
                source = home / name
                if source.exists():
                    folder.mkdir(parents=True, exist_ok=True)
                    os.replace(source, folder / name)
                    moved.append(name)
            initialize(home)
        except Exception:
            # Move new partial files aside; restore the exact previous files.
            import shutil

            for name in DATA_ITEMS:
                if name in originals and name not in moved:
                    continue
                current = home / name
                if current.is_dir():
                    shutil.rmtree(current)
                else:
                    current.unlink(missing_ok=True)
                if name in moved:
                    os.replace(folder / name, current)
            raise
    else:
        initialize(home)
    atomic_write(
        home / MARKER,
        json.dumps(
            {
                "format": 1,
                "mode": mode,
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "safety_backup": safety,
            }
        ).encode(),
    )
    return {"mode": mode, "safety_backup": safety}


def choose(home):
    """Native local dialog: no HTTP server, browser or secrets in the bridge."""
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    root = tk.Tk()
    root.title("Bettor — Configurazione iniziale")
    root.geometry("660x390")
    root.resizable(False, False)
    answer = None
    body = ttk.Frame(root, padding=26)
    body.pack(fill="both", expand=True)
    ttk.Label(body, text="Benvenuto in Bettor", font=("Segoe UI", 21, "bold")).pack(anchor="w")
    present = existing_data(home)
    text = (
        "Sono presenti dati di una precedente installazione. Scegli come procedere."
        if present
        else "Scegli se iniziare con un archivio vuoto o recuperare un backup."
    )
    ttk.Label(body, text=text, wraplength=600).pack(anchor="w", pady=(12, 16))

    def select(mode):
        nonlocal answer
        archive = None
        if mode == "new" and present:
            if not messagebox.askyesno(
                "Nuovo archivio",
                "Iniziare da zero? I dati attuali saranno prima salvati in un backup verificato e conservati separatamente.",
                parent=root,
            ):
                return
        if mode == "restore":
            archive = filedialog.askopenfilename(
                parent=root, title="Seleziona backup Bettor", filetypes=[("Backup Bettor", "*.zip")]
            )
            if not archive:
                return
            if present and not messagebox.askyesno(
                "Ripristino",
                "Il backup sostituirà i dati attuali. Sarà creata prima una copia di sicurezza. Continuare?",
                parent=root,
            ):
                return
        answer = (mode, archive)
        root.destroy()

    if present:
        ttk.Button(body, text="Usa i dati esistenti", command=lambda: select("existing")).pack(
            fill="x", pady=5
        )
    ttk.Button(body, text="Inizia da zero", command=lambda: select("new")).pack(fill="x", pady=5)
    ttk.Button(body, text="Ripristina un backup", command=lambda: select("restore")).pack(fill="x", pady=5)
    ttk.Label(
        body,
        text="Le chiavi API vanno reinserite se il backup proviene da un altro account Windows.\nChiudere questa finestra annulla l’avvio.",
        wraplength=590,
    ).pack(anchor="w", pady=18)
    root.mainloop()
    return answer
