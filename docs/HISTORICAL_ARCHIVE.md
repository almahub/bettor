# Archivio storico locale Serie A, B e C

Database operativo: `%LOCALAPPDATA%/Better/data/888-real.db`. Configurazione: `config/888/sources.yaml`, sezione `api_football`.

- Stagioni iniziali: 2026, 2025, 2024. Competizioni: Serie A (135), Serie B (136), Serie C Girone A (138), Girone B (942), Girone C (943).
- `archive_leagues` configura le competizioni. La rotazione tra leghe e stagioni riprende dal cursore persistito; nessuna lega monopolizza i lotti di recupero. Il budget di 100 tentativi/giorno e il limite di 4 per ciclo sono condivisi, non moltiplicati per lega.
- I calendari delle stagioni correnti mancanti/scaduti hanno precedenza sul recupero storico quando la coverage corrente e gia nota. Il limite giornaliero resta vincolante: esaurito il budget, il recupero riprende nei giorni successivi; un dato scaduto non viene presentato come aggiornato.
- Il collector esegue il recupero dopo checkpoint, risultati e settlement: massimo 4 richieste per ciclo, 100 tentativi al giorno UTC, conteggiati anche dopo un riavvio.
- Recupera coverage, calendario con ID e risultati, classifiche, poi dettagli delle sole partite FT con punteggio completo. Ogni richiesta di dettagli contiene al massimo 20 ID.
- Eventi, formazioni effettive, statistiche squadra e statistiche giocatore sono conservati quando restituiti dal provider. Un campo vuoto non viene inventato e non provoca un ciclo continuo di download.
- Prima della rete il provider consulta l'archivio persistente. Il calendario completo risponde anche alle richieste per squadra, data e stato; i documenti partita rispondono agli endpoint di dettaglio.
- Calendario corrente: aggiornamento dopo 1 ora; classifiche correnti: 6 ore. Calendari/classifiche delle stagioni precedenti: 30 giorni. Dettagli FT recenti: verifica dopo 6 ore, precedenti: 7 giorni. Le verifiche conservano una nuova copia del contenuto solo se cambia.
- Tabelle append-only: `api_football_archive` (contenuto deduplicato), `api_football_archive_checks` (verifiche e scadenza), `api_football_archive_attempts` (budget persistente).
- Ogni contenuto mantiene fonte, hash, prima acquisizione; ogni verifica mantiene data e URL. Le correzioni creano versioni nuove. `as_of` esclude dati acquisiti successivamente alla data richiesta.
- Nessun backfill crea snapshot pre-match, prediction, settlement o osservazioni LIVE_FORWARD. Snapshot e ricerche già esistenti restano nelle rispettive tabelle, senza essere copiati in questo archivio. Il recupero di dati precedenti non cambia i gate del modello né aggiunge automaticamente stagioni al suo campione.

Consultazione locale senza chiamate al provider:

- `GET /archive/serie-a`: progresso per stagione, dettagli acquisiti, campi popolati, coverage e ultimo ciclo.
- `GET /archive/football`: progresso e coverage di tutte le leghe configurate.
- `GET /archive/football/fixtures/{fixture_id}?as_of=...`: lettura locale di una partita di qualsiasi lega archiviata, con lo stesso controllo della data di acquisizione.
- `GET /archive/serie-a/fixtures/{fixture_id}?as_of=...`: documento disponibile alla data indicata (fuso orario obbligatorio). Senza `as_of` restituisce l'ultima versione; ciò non autorizza il suo impiego retroattivo nei backtest.

Un errore di archivio non interrompe il collector operativo. Il recupero riparte dai dati persistiti al ciclo successivo; il budget locale non sostituisce i limiti reali dell'abbonamento.

## Separazione analisi e acquisizione

In `config/888`, `analysis_archive_only: true` rende esclusivamente locali le letture statistiche e formazioni delle analisi per tutte le leghe e stagioni configurate nell'archivio. Anche il mapping delle analisi usa il calendario locale. Un dato assente o scaduto produce un errore esplicito dell'archivio, senza ripiegare su una chiamata API-Football o sulla sola cache di processo.

Il collector continua ad alimentare l'archivio; polling dei risultati e verifiche esplicite del provider restano abilitati. Dal 15 ottobre anche Serie B e i tre gironi di Serie C seguono questo percorso. Una coverage positiva non garantisce che ogni partita abbia tutti i campi popolati: le disponibilita effettive sono riportate separatamente.

Le analisi normali conservano le statistiche strutturate quando presenti. Football Data Research interviene soltanto se lo snapshot statistico manca; External Research continua a raccogliere il contesto qualitativo con la cache e le fonti esistenti. Quote/calendario restano forniti da 888; modello, gate e settlement restano interni a Bettor. Nessun nuovo endpoint o nuova credenziale OpenAI.

## Verifica storica Serie B/C del 15 ottobre 2026 (non garantisce la copertura attuale del piano)

- 48 richieste dedicate: calendari e classifiche per 136/138/942/943, stagioni 2026/2025/2024; 4.557 fixture complessive e primi 20 dettagli FT per ogni coppia lega/stagione (240 partite).
- Serie B: statistiche squadra e giocatore dichiarate coperte e realmente popolate nel primo lotto delle tre stagioni.
- Serie C: coverage `statistics_fixtures=false` e `statistics_players=false` nelle tre stagioni; i relativi array sono vuoti. Risultati, classifiche, eventi e formazioni sono disponibili, senza garanzia di completezza su ogni partita (19/20 formazioni nel primo lotto Girone B 2026).
- Le statistiche di forma ricavate dai punteggi restano disponibili per il modello; non equivalgono a statistiche dettagliate come tiri o prestazioni individuali.
- Nessuna promozione dello storico acquisito a osservazione LIVE_FORWARD. Nessuna modifica ai gate.
