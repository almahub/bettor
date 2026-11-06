# API-Football: integrazione e copertura reale

Runtime 0.1.2. Il profilo `config/888` usa 888 per catalogo/quote e API-Football per statistiche/formazioni. News non configurate. L'adapter non effettua scraping, non genera dati sintetici e non cambia piano.

## Credenziale

`API_FOOTBALL_API_KEY` viene letta prima dall'ambiente e poi dal file `.env` nella directory di avvio. Non vengono eseguite interpolazioni o comandi. `.env` e gli archivi locali sono esclusi da Git. La chiave non entra nelle risposte dashboard, nei manifest di analisi o nei messaggi di errore. Non inserire la chiave in `sources.yaml`.

## Operazioni disponibili

```powershell
.\.venv\Scripts\better.exe --config config/888 sources-check --league 5 --season 2026
.\.venv\Scripts\better.exe --config config/888 fetch-history --league 5 --season 2024 --output data/raw/api-football/nations-league-2024.json
.\.venv\Scripts\better.exe --config config/888 serve --port 8001
```

La pagina Analisi mostra la sezione **Copertura dati / API-Football**. **Verifica accesso** controlla account e stagione della prima competizione configurata: non avvia analisi. Le risposte sono memorizzate per un'ora, incluse le esclusioni di stagione; ricaricare la pagina non interroga automaticamente l'API. Il riepilogo non certifica tutti i campionati.

`fetch-history` salva risultati nativi, URL, istante di acquisizione e SHA-256. Questo archivio NON equivale a previsioni out-of-sample e non alimenta automaticamente il Model Validator. I timestamp non vengono retrodatati.

## Risultato del collaudo reale, 28 settembre 2026

- Chiave valida, piano Free attivo, limite remoto di 100 richieste/giorno.
- Stagione 2026 rifiutata dal provider; il messaggio nativo indica accesso alle stagioni 2022–2024.
- Nations League 2024: 188 fixture scaricate in `data/raw/api-football/nations-league-2024.json`.
- Italia API ID 768 e Turchia API ID 777 verificati nell'archivio; mapping esplicito verso gli ID 888 già osservati. Competizione 888 `2000113672` associata a API league 5 / season 2026. L'associazione della singola fixture resta subordinata a ID squadre, competizione, stagione e kickoff identici.
- Pipeline live Turchia–Italia: analisi `7d705938-2482-5600-b227-ab7d1b6c425e`, quote OK, statistiche/formazioni MISSING_DATA con motivazione esplicita di stagione non consentita; INSUFFICIENT_DATA e stake zero. Consultabile nel Journal locale.
- Verifica finale: 298 test Python e 5 test JavaScript superati; nessuna rete nei test. Include cache della negazione di piano, segreti esclusi, identità, timestamp, campioni invalidi, provenienza, factory, endpoint diagnostico e pulsante UI senza richieste automatiche.

## Contratti e limiti

- Goal rate deterministici dagli ultimi N incontri FT della stessa competizione/stagione; minimo due risultati per squadra, esclusione eventi futuri, supplementari e risultati incompleti. Finestra conservativa di tre ore dopo kickoff per ammettere un risultato; nessuna pretesa di timestamp ufficiale di fine gara.
- Nessuna xG inventata. Snapshot con provenienza API/B, hash e timestamp originali della cache. Le richieste storiche as-of non vengono ricostruite da risposte attuali.
- Formazioni API aggregate restano PROBABLE_LINEUP senza conferma indipendente da fonte ufficiale A; entrambe devono contenere undici nomi distinti.
- Pacing di una richiesta ogni 6,1 secondi, nessun retry automatico, cache in memoria limitata a 128 risposte, massimo locale di 90 tentativi/giorno per istanza. Questo contatore si azzera al riavvio e non coordina altri processi: il limite remoto rimane autoritativo. Usare una sola istanza per non consumare quota duplicata.
- Risposte paginate non vengono trattate come campioni completi. Errori HTTP, errori applicativi dentro HTTP 200 e mancanza di mapping sono espliciti. Nessun fallback silenzioso a stagioni precedenti.
- Altre squadre e competizioni richiedono mapping verificati, senza fuzzy matching dei nomi.

L'integrazione software è operativa; un'analisi completa delle partite 2026 rimane bloccata dalla copertura gratuita. Servono inoltre notizie, storia validata out-of-sample, dati di portafoglio e gli altri requisiti dei gate. Non si abbassano le soglie per produrre BET.

Riferimenti: https://www.api-football.com/documentation-v3 e https://www.api-football.com/pricing.
