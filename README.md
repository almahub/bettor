# Better â€” sistema calcistico multi-agent

Per riprendere lo sviluppo in una nuova chat o con un nuovo programmatore, leggere
prima [il report tecnico e passaggio di consegne](docs/HANDOFF.md). Contiene lo
stato aggiornato al 2 ottobre 2026, struttura, avvio, provider, limiti verificati
e prioritÃ  successive.

Sistema con dashboard grafica, 14 agenti tipizzati, evidence/provenance, matematica deterministica, decision gates, journal append-only, API, CLI e replay offline. La specifica originale `BETTER_CODEX_SPEC_V1_START_RIZZO.md` Ã¨ preservata. Audit iniziale: la cartella conteneva esclusivamente quel documento.

Il workspace separato [SalaBet](docs/salabet.md) riceve i pronostici tramite BET,
costruisce singole e multiple da 2 a 6 selezioni e registra soltanto puntate
piazzate manualmente. Banca resta l'unica autorità economica.

Nuovo in 0.2.0: [Data Pipeline centrale](docs/data-pipeline.md), raccolta point-in-time
delle sole partite selezionate, settlement e validazione delle previsioni, controllo
usage/costi API in euro e filtro 888 vincolato agli ID nativi verificati. I due
[saved research agent](docs/research-agents.md) restano separati tra dati quantitativi
correnti e contesto qualitativo pre-match.

System One Ã¨ opzionale e **solo shadow**. Rizzo Flow non modifica agenti eseguiti, probabilitÃ , confidence, stake o decisione. Non esiste esecuzione automatica di scommesse.

## Avvio immediato su questo PC

Nuovo in 0.1.5: identity resolution persistente 888 â†’ Sportradar, metriche di copertura e raccolta storica point-in-time senza retrodatazione. [Identity resolution e storico](docs/identity-resolution-and-history.md). La **Banca** continua a gestire budget EUR e puntate.

API-Football: il profilo `config/888` legge `API_FOOTBALL_API_KEY` dal file locale `.env` (o dall'ambiente). In Analisi, **Verifica accesso** mostra piano e copertura. Il piano gratuito collaudato esclude il 2026: non basta per le partite attuali. Comandi per diagnostica e download dello storico in [docs/provider-api-football.md](docs/provider-api-football.md).

Dalla root `D:\bettor`, l'ambiente `.venv` Ã¨ giÃ  installato:

```powershell
.\.venv\Scripts\better.exe health
.\.venv\Scripts\better.exe analyze --match demo-match --demo bet
.\.venv\Scripts\better.exe analyze --match demo-match --demo no_bet
.\.venv\Scripts\better.exe analyze --match demo-match --demo wait_lineups
.\.venv\Scripts\better.exe analyze --match demo-match --demo bet --market 1X2 --json
.\.venv\Scripts\better.exe journal --last 20
```

Le demo sono **dati sintetici dichiarati**, inclusa la storia di validazione. Non rappresentano partite o prestazioni reali. Altri scenari: `wait_odds`, `watchlist`, `suspended_model`, `risk_limit`, `source_conflict`, `stale_odds`, `missing_data`, `news_offline`, `odds_offline`.

```powershell
.\.venv\Scripts\better.exe serve --demo bet
```

**Dashboard: [http://127.0.0.1:8000](http://127.0.0.1:8000)**. API: `http://127.0.0.1:8000/docs`. Arresto: Ctrl+C. Senza `--demo`, le fonti devono essere configurate esplicitamente; il sistema non sostituisce dati mancanti con fixture.

La dashboard include contesto della partita, probabilitÃ , quote, evidenze con fonti, risultati degli agenti e controlli decisionali. Le sezioni Journal, Portafoglio, Modelli e Sistema permettono di riaprire gli snapshot e verificarne il contesto. In demo puoi selezionare scenari diversi e premere **Avvia analisi**. Non occorre avviare un secondo server o installare dipendenze frontend. Dettagli: [docs/dashboard.md](docs/dashboard.md).

## Installazione riproducibile

Per il catalogo e le quote reali **888 Sport**, usare il profilo separato:

```powershell
.\.venv\Scripts\better.exe --config config/888 --log-file data/logs/888.jsonl serve --port 8001
```

Lo stato di raccolta, maturità, copertura agenti e spazio è disponibile nella
pagina **Data pipeline** oppure con `better pipeline-status`; dettagli in
[docs/data-pipeline.md](docs/data-pipeline.md).

Dashboard: `http://127.0.0.1:8001`. In **Analisi**, il catalogo 888 mostra le
partite dei prossimi sette giorni appartenenti agli ID nativi italiani verificati.
In 0.2.0 sono verificati i tre gironi di Serie C; Serie A/B saranno aggiunte quando
appariranno nel feed e i rispettivi ID saranno verificati. Il caricamento non avvia
analisi. Dettagli: [provider 888](docs/provider-888.md).

Python 3.12+, dipendenze fissate in `uv.lock`:

```powershell
uv sync --locked --extra dev
```

Su questo workspace, per usare runtime/cache giÃ  presenti:

```powershell
$env:UV_CACHE_DIR = "$PWD\.cache\uv"
$env:UV_PYTHON_INSTALL_DIR = "$PWD\.python"
uv sync --locked --extra dev
```

Alternativa con un Python 3.12 funzionante: `python -m pip install -e ".[dev]"`.

## Verifica

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check better tests
.\.venv\Scripts\python.exe -m ruff format --check better tests
node --test tests/ui/test_ui.mjs tests/ui/test_dashboard.mjs
```

La suite usa esclusivamente fixture e trasporti HTTP finti: non richiede account, connessione a Rizzo o chiavi API. I temporanei pytest sono confinati a `.cache/pytest`.

Il comando `analyze` mostra una riga per agente, tempi di raccolta separati e limitazioni esplicite. Il report scompone la confidenza e spiega perchÃ© un agente `PARTIAL` puÃ² o non puÃ² contribuire alla decisione. Log JSON completi: `data/logs/better.jsonl` (rotazione automatica). Per JSON anche in console usa `--log-format json` prima del sottocomando; `--log-file <percorso>` cambia destinazione. Dettagli dell'audit e della policy: [PARTIAL e osservabilitÃ ](docs/partial-and-observability.md).

## Decisioni e mercati

Stati: `BET`, `NO_BET`, `WATCHLIST`, `WAIT_FOR_LINEUPS`, `WAIT_FOR_BETTER_ODDS`, `INSUFFICIENT_DATA`. Gli agenti non votano. Ogni BET supera controlli su identitÃ /as-of, qualitÃ , lineage/provenance, validazione, mercato, formule/quote, freshness, value, contrarian, rischio, timing e confidence. Ogni altro stato ha stake zero.

La pipeline operativa V1 ammette 1X2, Over/Under 2.5 e BTTS. DNB, Asian Handicap e Team Totals sono modellati come categorie ma bloccati operativamente fino a contratti di settlement e validazione dedicati. Poisson alimenta la pipeline; Dixonâ€“Coles, Elo, logistica, Monte Carlo e ensemble sono implementati e testati come baseline quantitative, senza attribuire loro calibrazione inesistente.

## Fonti e configurazione

Soglie e feature flag: `config/*.yaml`; override delle impostazioni applicative tramite `BETTER_<NOME_CAMPO>`. Le variabili documentate in `.env.example` vanno impostate nell'ambiente: il file `.env` non viene caricato automaticamente.

- `config/sources.yaml`: provider primari/fallback. Default: non configurati.
- `http_feed`: adapter HTTP per un feed conforme ai contratti Better. Richiede `BETTER_FEED_BASE_URL`; token opzionale `BETTER_FEED_API_KEY`.
- `football_data_org`: adapter effettivo v4 per fixture, con `FOOTBALL_DATA_ORG_API_KEY`. Non fornisce automaticamente statistiche avanzate, news o quote.
- `BETTER_HISTORY_FILE`: JSON con `HistoricalPrediction[]` out-of-sample verificabili.
- `BETTER_PORTFOLIO_FILE`: JSON conforme a `Portfolio`, con importi monetari stringhe decimali.

Senza storia sufficiente il Validator non ammette il modello. I conflitti materiali tra fonti rimangono visibili e bloccano l'utilizzo del dato.

## System One e System 2

Default `SYSTEM_ONE_ENABLED=false`. Per Rizzo locale:

```powershell
$env:SYSTEM_ONE_ENABLED = "true"
$env:SYSTEM_ONE_SHADOW_MODE = "true"
$env:SYSTEM_ONE_PROVIDER = "rizzo_flow"
$env:RIZZO_FLOW_BASE_URL = "http://127.0.0.1:8017"
$env:RIZZO_FLOW_MODEL = "<identificatore-esatto-del-modello-servito>"
```

Un servizio assente o incompatibile degrada l'osservazione shadow e non cambia la pipeline. La modalitÃ  active viene rifiutata. Per una prova interamente offline scegliere `SYSTEM_ONE_PROVIDER=fake`.

System 2 ha interfaccia vendor-neutral e output qualitativo validato, repair limitato, citazioni controllate e registrazione delle chiamate. L'integrazione si inietta nella composition root; nessun modello LLM commerciale viene selezionato o autenticato implicitamente.

## Persistenza e riproducibilitÃ 

SQLite locale: `data/better.db`; SQLAlchemy e migrazione Alembic inclusi. Decisioni, prezzi e journal sono append-only anche tramite trigger DB. Risultati post-match e annotazioni sono eventi separati. Il manifest conserva gli input e le versioni; esclude credenziali/URL del database.

```powershell
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\better.exe replay-pipeline --analysis-id ID
.\.venv\Scripts\better.exe replay --analysis-id ID
```

`replay-pipeline` riesegue gli agenti offline e confronta decisione/output. `replay` rivaluta gli stati shadow con il SystemOneProvider configurato. Per PostgreSQL installare l'extra `postgres` e impostare `BETTER_DATABASE_URL`; la migrazione SQLite Ã¨ stata eseguita, mentre PostgreSQL richiede una verifica nell'ambiente di destinazione.

## Stato e limiti verificati

Il backend V1 e la milestone System One shadow sono implementati e coperti dalla suite offline. Restano attivitÃ  che richiedono input esterni: credenziali e feed reali, un servizio Rizzo con modello fissato, un provider System 2 reale e dataset storici rappresentativi per calibrazione/benchmark. Il routing attivo rimane intenzionalmente disabilitato. Non sono dichiarati rendimenti, calibrazione calcistica o validazioni live non misurati.

Documentazione: [audit](docs/current-state.md), [architettura](docs/architecture.md), [agenti](docs/agents.md), [contratti](docs/data-contracts.md), [matematica](docs/math.md), [decision engine](docs/decision-engine.md), [fonti](docs/sources.md), [System One](docs/system-one.md), [replay](docs/replay.md), [test](docs/testing.md), [stato delle fasi](docs/implementation-status.md).


## Windows Setup

Build e aggiornamento manuale: [Distribuzione Windows](docs/windows-distribution.md).
Il setup conserva configurazione e database nella cartella dati personale, separata dai file del programma.
