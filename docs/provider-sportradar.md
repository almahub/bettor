# Sportradar Soccer v4

La risoluzione delle identità, la persistenza dei mapping, le metriche di
copertura e la raccolta point-in-time sono descritte in
[Identity resolution e storico](identity-resolution-and-history.md).

`SportradarSoccerProvider` è un adapter opzionale per statistiche e formazioni.
Dal runtime 0.1.8 non appartiene al percorso predefinito del workspace 888: 888
fornisce identità e quote, mentre External Research fornisce i dati qualitativi.
Adapter, identity resolver, mapping e test restano disponibili per profili futuri.

## Credenziale e trial

La chiave si legge esclusivamente da `SPORTRADAR_API_KEY` nell'ambiente o nel
file `.env`, che non è versionato. Non entra in URL, log, errori, snapshot o
manifest. La configurazione corrente usa `trial`, Soccer v4 in inglese e un
budget locale conservativo di 900 chiamate per processo. Il provider limita le
richieste a una ogni due secondi, sotto il limite contrattuale del trial.

Il 29 settembre 2026 l'endpoint `competitions.json` ha risposto HTTP 200 con
1.281 competizioni. Questo dimostra l'accesso al catalogo, non la completezza di
ogni competizione o incontro.

## Identità e copertura

La risoluzione usa la partita 888 selezionata e il Daily Summaries del giorno.
Accetta una sola corrispondenza casa/trasferta. Le associazioni native configurate
(`sr:competition` e `sr:competitor`) hanno precedenza; con tutti e tre gli ID
mappati è tollerato uno scarto di orario fino a 15 minuti, utile per correzioni
del bookmaker. Senza ID espliciti sono richiesti nomi normalizzati e orario
identico. Nessun fuzzy matching viene applicato.

Prima di chiedere una formazione, il provider richiede `lineups=true` e
`lineups_availability=pre` sullo sport event. Undici titolari distinti per lato
sono obbligatori. Una conferma dichiarata dall'aggregatore rimane
`PROBABLE_LINEUP`: non viene trasformata in fonte ufficiale.

## Statistiche

Lo storico proviene da Competitor Summaries, fino a 10 incontri per squadra.
I gol nei 90 minuti usano i campi normaltime quando presenti oppure la somma
esatta dei due `regular_period`; supplementari e rigori non entrano nei modelli.
Partite abbandonate o payload storici senza due tempi regolamentari vengono
esclusi. Sono prodotti i segmenti osservabili `last_5`, `last_10`, `season`,
`home` e `away`. `opponent_strength` e xG non vengono inventati: la pipeline
segnala quindi copertura parziale finché non sono disponibili da dati adatti.

La prova reale del 30 settembre 2026 ha verificato la corrispondenza tra 888 e
Sportradar per UEFA Champions League Women e ha mostrato che disponibilità delle
formazioni e profondità dei dati variano per singolo incontro. HTTP 429 è
classificato come rate limit e non viene mascherato.

## Dashboard

`POST /sources/sportradar/check` verifica esplicitamente l'accesso; il semplice
caricamento della pagina non consuma quota. `/workspace` espone soltanto stato,
livello di accesso, conteggio locale e numero di mapping, mai la credenziale.
