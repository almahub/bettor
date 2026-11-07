# Data Pipeline centrale

La pagina **Data pipeline** e `better pipeline-status` espongono un unico piano
di controllo per raccolta, storico, validazione e spazio occupato.

La raccolta avviata dall'utente resta limitata alle partite selezionate. Ogni analisi reale salva uno
snapshot point-in-time append-only e lo classifica in una finestra `EARLY`, `T24`,
`T6`, `T1` o `CLOSING`. La finestra descrive quando è avvenuta la cattura; non
certifica da sola che il prezzo sia il closing ufficiale. `LINEUP` sarà usato per
una cattura dedicata dopo la pubblicazione delle formazioni ufficiali.

La raccolta operativa manuale resta limitata alle partite selezionate. In parallelo,
la stessa Data Pipeline gestisce una **Validation Cohort** indipendente dalla UI:
iscrive automaticamente le partite delle sole competizioni con ID nativo verificato,
cattura gli stessi checkpoint e costruisce il campione `LIVE_FORWARD_VALIDATION`.
La coorte non crea BET e non modifica Decision Engine, soglie o Banca.

## Validation Cohort

Il checkpoint canonico predefinito è `T6`. Se T6 è stato perso, la pipeline può
usare `T24` soltanto quando la finestra T6 è ormai trascorsa e la prediction è
ancora strettamente pre-kickoff. Una prediction canonica congela match,
competizione, kickoff, as-of, modello/versione, feature schema, config hash,
distribuzione completa, fair odds, quote d'ingresso, snapshot sorgenti e versione
software. La chiave idempotente ammette una sola distribuzione per
match/modello/versione/mercato.

Gli stati principali sono `ENROLLED`, `SNAPSHOT_READY`,
`CANONICAL_PREDICTION_CREATED`, `KICKOFF_PASSED`, `RESULT_PENDING`,
`RESULT_VERIFIED`, `SETTLED` e `VALIDATION_INCLUDED`. Esclusioni, dati
insufficienti, conflitti tra risultati e fallimenti sono eventi append-only con
motivo esplicito. Riavvii e polling ripetuti non duplicano prediction, risultato,
settlement o osservazione.

Solo osservazioni `LIVE_FORWARD`, create prima del kickoff e liquidate dopo con un
risultato Tier A/B, alimentano lo sblocco operativo. `HISTORICAL_BACKTEST` resta
separato. Ogni partita conta una volta per mercato: l'1X2 conserva la distribuzione
multiclasse completa. Versione modello, feature schema e configurazione formano
campioni distinti. La soglia resta 200.

Il provider risultati è un protocollo componibile. L'adapter football-data.org usa
il punteggio dei tempi regolamentari e conserva separatamente supplementari; fonti
affidabili discordanti producono `RESULT_CONFLICT`. Per identità 888 prive di un
mapping verso un provider risultati configurato, lo stato resta `RESULT_PENDING`
e rimane disponibile il settlement manuale Tier A/B.

Il Football Data Research può essere chiamato dalla coorte soltanto nei checkpoint
canonico/fallback quando manca lo snapshot quantitativo. Budget per match, giorno e
settimana, cache hit, token, tool call e costo sono registrati separatamente. La
ricerca qualitativa esterna non viene eseguita dalla coorte.

## Livelli di maturità

- `DISCOVERED`: identità partita persistita;
- `SNAPSHOT_CAPTURED`: osservazione pre-match conservata;
- `ANALYZED`: pipeline agenti completata;
- `PREDICTED`: probabilità di un modello persistite;
- `SETTLED`: esito registrato dopo il kickoff;
- `CLOSING_PRICE_CAPTURED`: quota finale registrata con il settlement.

Una partita è `validation_ready` soltanto se possiede una previsione pre-match e
un esito successivo. Questo impedisce di trasformare semplici dati storici o
informazioni recuperate dopo la partita in validazione del modello.

## Risultati, closing e validazione

Le partite analizzate che hanno superato kickoff più 90 minuti compaiono nella
sezione **Risultati da registrare**. In assenza di un provider risultati
strutturato, l'utente inserisce punteggio e URL della fonte. Sono accettate solo
fonti dichiarate tier A/B; il record è append-only e un risultato incompatibile
non può sovrascrivere quello già acquisito.

Al momento del settlement Bettor:

1. sceglie per ogni mercato l'ultima quota 888 osservata prima del kickoff;
2. collega soltanto forecast creati prima del kickoff;
3. calcola gli esiti binari per 1X2, Over/Under 2.5 e BTTS;
4. conserva modello, versione, selezione, checkpoint, probabilità, quota di
   ingresso, closing, risultato e timestamp;
5. aggiorna Brier score, log loss ed errore di calibrazione per gruppo
   modello/mercato/checkpoint.

I gruppi rimangono `INSUFFICIENT_SAMPLE` finché non raggiungono la soglia
configurata. Le probabilità implicite de-viggated sono archiviate come baseline
`market_no_vig`; non diventano un modello indipendente e non sbloccano da sole un
BET. Più analisi della stessa partita nello stesso checkpoint non aumentano il
campione.

Una partita viene registrata per la raccolta quando viene selezionata e consultata
nel Centro analisi oppure quando viene avviata un'analisi reale. Il collector gira
ogni trenta minuti, con risveglio anticipato ai checkpoint T24/T15 e interroga soltanto dati strutturati e quote; non avvia ricerche
OpenAI in background. All'interno della stessa finestra temporale una partita viene
archiviata una sola volta. La pagina permette anche di eseguire manualmente i
checkpoint dovuti.

## Storage control

La policy predefinita è 5 GB complessivi, 30 giorni per i raw e 7 giorni per le
cache. I valori sono configurabili con:

```text
BETTER_STORAGE_MAX_GB=5
BETTER_RAW_RETENTION_DAYS=30
BETTER_CACHE_RETENTION_DAYS=7
BETTER_PIPELINE_AUTO_COLLECT=true
BETTER_PIPELINE_POLL_SECONDS=1800
```

Il report elenca solamente i candidati alla retention. Database, journal,
previsioni, provenance, esiti e metriche sono append-only e non vengono inclusi
tra i file eliminabili.

## Comandi

```powershell
.\.venv\Scripts\better.exe --config config/888 --log-file data/logs/pipeline.jsonl pipeline-status
```

La matrice di copertura riporta per ciascuno dei 14 agenti quante analisi lo hanno
eseguito, quanti output `SUCCESS` o `PARTIAL` sono leggibili e quante evidenze sono
state prodotte. La copertura non è un voto e non modifica i gate decisionali.
