# Bettor per Windows

Versione corrente **0.2.8**: finestra desktop pywebview/WebView2, servizio locale separato e primo avvio guidato. Vedi [primo avvio](desktop-first-run.md) e [backup/rilascio](backup-and-releases.md).

## Percorsi

- Programma: `%LOCALAPPDATA%/Programs/Better/Better.exe`.
- Dati: `%LOCALAPPDATA%/Better`; database `data/888-real.db`, configurazione `config/888`.
- Credenziali: `credentials.dpapi`, protette dall'account Windows, gestite dalla UI.
- Log: `data/logs/better.jsonl` e `logs/desktop.log`.

I percorsi legacy restano invariati per riutilizzare i dati. Il setup non importa automaticamente database da cartelle sorgenti o altri PC.

## Build Windows x64

Richiede Python 3.12, Node.js e Inno Setup 6:

```powershell
python -m venv .cache/packaging-venv
.cache/packaging-venv/Scripts/python.exe -m pip install '.[dev,desktop]' pyinstaller
powershell -File scripts/build-windows.ps1 -Iscc 'C:/Program Files (x86)/Inno Setup 6/ISCC.exe'
```

Output: `dist/installer/Bettor-Setup-VERSION-x64.exe` e `.sha256`. L'alias identico `Better-Setup-VERSION-x64.exe` serve ai client precedenti. Il bundle include solo codice, web e configurazione pubblica: nessun database, backup, log o chiave personale.

Il setup verifica WebView2 e, se assente, indica il download Microsoft senza cancellare dati. Il setup non è firmato digitalmente: SHA256 verifica l'integrità rispetto al checksum pubblicato, non sostituisce una firma del produttore.

## Aggiornamento e disinstallazione

Sistema → Aggiornamenti mostra la versione in esecuzione e consente download verificato e installazione dopo conferma. Il componente nativo chiude la finestra, arresta il backend e avvia il setup esterno; quindi riapre Bettor con gli stessi dati e porta. L'installazione crea un backup completo verificato e si interrompe se il backend è attivo o il backup fallisce. Nelle versioni precedenti chiudere la finestra e usare “Arresta servizio Bettor” dal menu Start prima di aggiornare. Vedi [manutenzione desktop](desktop-maintenance.md).

La disinstallazione richiede la finestra chiusa, tenta l'arresto controllato del servizio e conserva dati/backup. Rimuove il marcatore di primo avvio: la reinstallazione chiede se mantenere i dati, iniziare da zero o ripristinare.

## Pubblicazione

Il workflow `windows-release.yml` esegue suite Python/UI, build e pubblicazione stabile per un tag `vVERSION`. Tag, `pyproject.toml` e `better/__init__.py` devono coincidere. Gli asset vengono riscaricati e confrontati prima della pubblicazione; solo dopo la verifica della release si eliminano quelle oltre le tre conservate, senza eliminare tag o commit.

## Verifiche e limiti

Collaudati localmente: bundle autonomo, finestra desktop, archivio vuoto senza fondi/giocate, avvisi credenziali, backup/ripristino isolato con confronto database, aggiornamento conservativo e arresto sullo stesso event loop dei provider.

Rimangono da effettuare il collaudo su un secondo PC/VM pulita e l'ispezione visiva automatizzata completa. I test svolti non equivalgono a queste verifiche.
