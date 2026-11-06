# Better per Windows

## Stato effettivo
Launcher, bundle PyInstaller, setup Inno Setup e workflow GitHub predisposti. Bundle autonomo compilato e avviato senza Python nel PATH, health e doppio avvio verificati; setup Inno Setup compilato (circa 19,6 MB). Installazione e reinstallazione di aggiornamento collaudate in .cache/installed-better, database e .env di prova invariati e backup creati. Sorgenti pubblicati nella repository almahub/bettor. Prima release v0.2.0 creata in bozza con setup e checksum: https://github.com/almahub/bettor/releases. Da collaudare su un secondo PC prima della pubblicazione.

## Percorsi
Programma: %LOCALAPPDATA%/Programs/Better.
Dati: %LOCALAPPDATA%/Better; database data/888-real.db; config/888; chiavi .env; log data/logs.
L'istanza D:/bettor resta separata e non viene migrata automaticamente.
Installazione nuova: database vuoto, nessuna chiave inclusa, collector disattivato. Configurare il nuovo .env; BETTER_PIPELINE_AUTO_COLLECT=true abilita il collector. Ogni PC mantiene un database distinto.

## Build Windows x64
Richiede Python 3.12 e Inno Setup 6.

1. python -m venv .cache/packaging-venv
2. .cache/packaging-venv/Scripts/python.exe -m pip install .[dev] pyinstaller
3. powershell -File scripts/build-windows.ps1 -Iscc "C:/Program Files (x86)/Inno Setup 6/ISCC.exe"

Output dist/installer/Better-Setup-VERSION-x64.exe e .sha256.
Bundle con soli codice, web e config/888; esclusi database, .env e archivi personali. Revisionare comunque i file config prima di pubblicare.

## GitHub e aggiornamenti
Workflow .github/workflows/windows-release.yml: test, build, artefatto; un tag vVERSION produce una release BOZZA. Tag, pyproject.toml e better/__init__.py devono coincidere. Prima di caricare i sorgenti revisionare i file: non caricare indiscriminatamente D:/bettor.

scripts/download-update.ps1 scarica l'ultima release pubblicata di almahub/bettor e verifica SHA-256. Non avvia il setup. SHA-256 non sostituisce una firma del produttore; firma del setup non ancora configurata.

Chiudere Better con Ctrl+C nella console dopo la conclusione del collector. Il setup interroga il vecchio launcher prima dell'aggiornamento: backend attivo o backup fallito bloccano l'installazione. Backup coerenti SQLite in backups/TIMESTAMP/data. Configurazioni e chiavi non sovrascritte; dati personali conservati alla disinstallazione.
Rollback manuale: conservare il setup precedente e ripristinare, da fermo, versione e backup pre-aggiornamento se necessario.

## Collaudo richiesto prima della pubblicazione
Su VM pulita senza Python verificare: installazione, health e pagina iniziale, doppio avvio, porta occupata, aggiornamento bloccato con backend attivo, aggiornamento da fermo con backup e dati invariati, disinstallazione con dati conservati. Non usare il database operativo.

Notifica integrata, arresto automatico coordinato con collector, migrazione guidata dell'archivio attuale e rollback automatico non implementati in questo primo blocco.

## Verifiche 6 novembre 2026

663 test Python, 25 test UI passati; Ruff passato. Smoke del bundle autonomo con PATH senza Python: health ok, pagina iniziale, identità e riapertura senza duplicare il server. Backup eseguito anche dal programma compilato. Primo aggiornamento temporaneamente bloccato su un file occupato: reinstallazione successiva completata, hash del database di prova invariato. Resta necessario un collaudo su VM/PC pulito: i test svolti qui non equivalgono a quella verifica.

La modalità /NOICONS è stata verificata sul setup finale: nessun collegamento creato. Disinstallazione di prova con dati personali conservati.

## Versione 0.2.1

Automazione Serie A solo T24, senza confronto T30. Dettagli in [Automazione Serie A](serie-a-automation.md).
