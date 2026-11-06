# Banca e StatsBomb Open Data — 0.1.3

**Aggiornamento 0.1.4:** la Banca descritta qui rimane attiva. StatsBomb è stato rimosso da interfaccia, endpoint e provider su richiesta dell'utente; la relativa sezione sotto è storica. File acquisiti conservati. Il flusso corrente è descritto in [Consultazione automatica](automatic-research.md).

## Banca personale

Aprire la sezione **Banca**, inserire il budget iniziale in euro e premere **Crea Banca**. Nessun importo viene assegnato automaticamente. La Banca è un registro dichiarato dall'utente: non è un conto bancario, non verifica il saldo presso 888 e non esegue scommesse.

Il budget iniziale è registrato una sola volta. Successivamente si possono registrare puntate già effettuate, indicando partita pre-match dal catalogo, mercato supportato, importo e quota accettata. La registrazione è ammessa se il denaro disponibile basta; non equivale all'approvazione del Decision Engine e può documentare scelte fatte autonomamente dall'utente. Le liquidazioni sono Vinta, Persa o Rimborsata, con ritorno lordo rispettivamente importo × quota, zero, importo. Arrotondamento monetario HALF_UP al centesimo. Una liquidazione non può essere duplicata; gli esiti vinta/persa non sono ammessi prima del kickoff. L'utente è responsabile della correttezza dell'esito registrato.

- **Disponibile**: denaro libero da puntate aperte.
- **Impegnato**: somma degli importi delle puntate aperte.
- **Capitale totale**: disponibile + impegnato, valorizzato al costo delle puntate, non al possibile premio.
- **Risultato netto**: vincite/perdite realizzate; drawdown rispetto al massimo capitale osservato.

Il Risk Manager usa questo capitale nelle nuove analisi quando non è configurato un portafoglio esterno. Le posizioni conservano importo esatto e istante di liquidazione: quelle chiuse non occupano esposizione corrente ma continuano a consumare i limiti di turnover giornaliero/settimanale. Bankroll zero produce veto, senza divisioni per zero. Nessuna modifica retroattiva alle analisi archiviate: il portafoglio usato rimane nel manifest.

Architettura: `BankService` riutilizza la tabella append-only `annotations`, namespace `bank:EUR:v1`; non esiste un secondo database. Scritture transazionali serializzate con BEGIN IMMEDIATE su SQLite, advisory transaction lock su PostgreSQL. UUID di richiesta per idempotenza; riuso dello stesso UUID con contenuti diversi rifiutato. Test concorrente impedisce overspending. Le scritture nel workspace demo o con portafoglio esterno sono rifiutate. Nessuna credenziale bancaria richiesta.

Limiti attuali: una sola Banca EUR per database; nessun deposito/prelievo successivo, rettifica o cash-out parziale. Non cancellare il registro per modificare un saldo: eventuali future rettifiche dovranno essere nuovi movimenti tracciati. Tenere il server locale: non è un servizio autenticato per esposizione pubblica. La pagina conserva anche il portafoglio della vecchia analisi selezionata, distinto dalla Banca corrente.

API: GET `/bank`; POST `/bank/initialize`, `/bank/bets`, `/bank/bets/{id}/settle`. I POST richiedono un UUID `request_id`; budget e importi usano decimali, non calcoli LLM. Nessun endpoint invia puntate al bookmaker.

## StatsBomb storico

In **Modelli**, sezione **Archivio di ricerca · StatsBomb Open Data**:

1. Carica catalogo.
2. Seleziona competizione/stagione e Mostra partite.
3. Scegli una partita e Importa dati della partita.

Solo la partita scelta scarica eventi e rose. Le importazioni diventano consultabili con tiri, gol nei tempi regolamentari, xG StatsBomb, passaggi e numero di giocatori nella rosa. La rosa del file lineups non viene spacciata per undici titolari. Gli xG mancanti restano null; supplementari e rigori della serie finale sono esclusi. Gli autogol vengono attribuiti alla squadra avversaria senza doppio conteggio.

L'archivio integrale mantiene URL, timestamp di acquisizione e SHA-256 sotto `data/raw/statsbomb`. I riepiloghi sono persistiti nel namespace `statsbomb:imports` della tabella annotations. Nessun token o registrazione necessari. Il catalogo e gli eventi sono letti dal repository ufficiale https://github.com/hudl/open-data; i file sono identificati dal contenuto, non si assume che il ramo master sia immutabile. Cache in memoria limitata; le nuove versioni restano archivi distinti.

StatsBomb è una fonte **di ricerca storica**, non un fallback silenzioso per una partita 2026. Non alimenta automaticamente StatsSnapshot o HistoricalPrediction: un dataset acquisito oggi non dimostra quali informazioni erano disponibili prima delle partite. La validazione cronologica e i mapping verso altre fonti restano passaggi separati. Non vengono inseriti pronostici retrodatati nel Model Validator.

Collaudo reale: catalogo di 80 combinazioni competizione/stagione; importata Croazia–Italia, Euro 2024, 24 giugno, match 3930178. Archiviati eventi e rose; riepilogo 1–1 nei tempi regolamentari, 6/13 tiri e xG circa 1,48/0,99. Dati con attribuzione StatsBomb Open Data; rispettare la licenza e le richieste di attribuzione del repository nelle eventuali pubblicazioni.

L'aggiunta di Banca e archivio non elimina il blocco API-Football sulla stagione 2026 o le altre fonti mancanti. Non sono modificati i gate per far comparire BET.

## Verifiche

322 test Python e 5 test JavaScript superati, Ruff pulito. Test Banca su budget invalidi, idempotenza, saldo/riserva/capitale, tre esiti, rifiuto di risultati anticipati, bankroll esaurito, concorrenza e snapshot as-of nella pipeline. Test StatsBomb su xG mancanti, autogol, esclusione rigori finali, eventi duplicati/incompleti, provenienza e importazione selettiva. Il controller UI verifica creazione budget senza perdita dei campi, selezione persistente di stagione/partita e assenza di download automatici. Collaudo HTTP reale: runtime 0.1.3, Banca non inizializzata (zero movimenti), importazione Croazia–Italia consultabile tramite API. Verifica visuale nel browser ancora non eseguita.
