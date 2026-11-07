# Bettor

Applicazione Windows per analisi calcistiche, ricerca documentata, previsioni probabilistiche, validazione e gestione delle giocate in SalaBet.

## Installazione Windows — 0.2.9

Scarica **Bettor-Setup-0.2.9-x64.exe** e il relativo `.sha256` da [GitHub Releases](https://github.com/almahub/bettor/releases/latest). Richiede Windows x64 e Microsoft Edge WebView2 Runtime. Python non è richiesto sul PC utente.

Il collegamento **Bettor** apre una finestra desktop senza console e barra degli indirizzi. Il primo avvio propone:

- **Inizia da zero:** saldo zero, nessuna schedina o analisi precedente, credenziali da configurare, collector e automazioni spenti.
- **Ripristina un backup:** recupera database, storico, Banca, schedine e impostazioni.
- **Usa i dati esistenti**, quando presenti: nessuna cancellazione silenziosa.

Le chiavi si inseriscono in **Sistema → Impostazioni**, con campi password e protezione Windows DPAPI. Su un altro PC/account le chiavi non decifrabili vanno reinserite. La Dashboard segnala le configurazioni mancanti.

Per compatibilità, programma e dati restano in `%LOCALAPPDATA%/Programs/Better` e `%LOCALAPPDATA%/Better`. Il modulo Python si chiama `better`; il prodotto si chiama **Bettor**. Il setup non include dati o credenziali personali.

Chiudere la finestra lascia il servizio attivo. Sistema mostra la versione attuale e consente di scaricare e verificare l'aggiornamento, quindi installarlo con riavvio assistito. Per un ripristino programmato usare **Riavvia Bettor e applica ripristino**. L'arresto è bloccato durante analisi o checkpoint attivi.

## Funzioni

- Dashboard sintetica, Analisi e Journal con evidenze, fonti, timestamp e motivi delle limitazioni.
- 14 agenti tipizzati, modello Poisson e gate deterministici. OpenAI raccoglie informazioni, senza sostituire il modello statistico.
- Archivio/cache locali prima della rete; provider configurabili per calendario, quote, risultati, statistiche e ricerca mirata.
- Automazione Serie A **T−24** con dati pre-match e trasferimento idempotente delle candidate.
- SalaBet: singole e multiple, anteprima, budget e controlli prima della registrazione automatica opzionale.
- Banca come unica fonte dei movimenti economici; risultati verificati, settlement e archivio.
- Model Validator e diagnostica per mercato, probabilità, qualità dei dati e versione. Nessuna ricostruzione retroattiva LIVE_FORWARD.
- Backup completo verificato, ripristino offline e aggiornamenti volontari.

**Bettor non invia puntate ai bookmaker.** Confermare una giocata la registra in SalaBet e Banca: deve corrispondere alla giocata effettuata dall'utente. Le previsioni INDICATIVE restano tali; le automazioni non modificano soglie o ammissione del modello.

## Documentazione

- [Primo avvio desktop](docs/desktop-first-run.md)
- [Configurazione e credenziali](docs/settings.md)
- [Backup e aggiornamenti](docs/backup-and-releases.md)
- [Build Windows](docs/windows-distribution.md)
- [Architettura](docs/architecture.md), [agenti](docs/agents.md), [matematica](docs/math.md)
- [Automazione Serie A](docs/serie-a-automation.md), [Data Pipeline](docs/data-pipeline.md)
- [Conferma automatica T-15](docs/scheduled-confirmation.md)
- [SalaBet](docs/salabet.md), [sicurezza automazioni e diagnostica](docs/placement-safety-and-model-diagnostics.md)
- [API Control](docs/api-control.md), [ricerca AI](docs/research-agents.md)
- [Note 0.2.9](docs/releases/0.2.9.md)

## Sviluppo

Python 3.12+, Node.js per i test UI, dipendenze fissate in `uv.lock`:

```powershell
uv sync --locked --extra dev --extra desktop
New-Item -ItemType Directory -Path .cache -Force | Out-Null
$env:OPENAI_USAGE_LIVE_ENABLED = 'false'
.venv/Scripts/python.exe -m pytest -q
$uiTests = Get-ChildItem tests/ui -Filter '*.mjs' | ForEach-Object FullName
node --test $uiTests
```

Backend dai sorgenti:

```powershell
.venv/Scripts/better.exe --config config/888 --log-file data/logs/better.jsonl serve --port 8001
```

Questo comando usa i dati relativi alla directory di lavoro: non sostituisce l'avvio desktop con la propria directory utente. Non avviare due processi sulla stessa porta/database.

Per una demo separata: `better serve --demo bet`. I dati sintetici sono dichiarati e non rappresentano risultati reali. Non vengono usati come fallback operativo.

Le release stabili vengono pubblicate solo su richiesta esplicita. Dopo verifica degli asset si conservano al massimo tre release, senza cancellare tag o storico Git.
