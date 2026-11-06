# SalaBet

SalaBet è il workspace operativo manuale di Bettor, disponibile su `/salabet`.
Bettor continua a produrre analisi, probabilità, fair odds, edge, EV e stato di
validazione. Il pulsante `BET` crea soltanto un `BetCandidate` append-only. Non
imposta uno stake, non muove denaro e non comunica con un bookmaker.

## Flusso

1. Il browser invia `analysis_id`, `prediction_id`, mercato e selezione.
2. Il server rilegge l'`AnalysisRun` persistito e ricostruisce tutti i dati
   autorevoli. Probability, fair odds, modello e validazione non sono campi client.
3. SalaBet conserva il candidate come `VALIDATO`, `INDICATIVO` o `NON OPERATIVO`.
4. Una selezione crea una `SINGLE`; da due a sei creano una `MULTIPLE` semplice.
5. L'utente inserisce bookmaker, quote realmente accettate, stake e note, quindi
   prepara la schedina. La quota combinata è il prodotto delle quote piazzate.
6. `CONFERMA PUNTATA` registra che la giocata è già stata effettuata manualmente.
   Banca scrive un solo movimento per l'intera schedina.
7. Il collector consuma i risultati finali verificati tier A/B già registrati da
   Bettor e chiude selezioni, schedina e movimento Banca in modo idempotente.

Non esistono login, click automatici, API ordine, live betting o cash out.

## Regole multiple e settlement

- Sono vietati duplicati e risultati mutuamente esclusivi dello stesso mercato.
- Mercati diversi della stessa partita sono ammessi con
  `CORRELATED_SELECTIONS_WARNING`. SalaBet non calcola una probabilità congiunta.
- Finché esistono leg irrisolte, la multipla resta `OPEN`.
- Una leg `LOST` rende la multipla `LOST`.
- Tutte le leg `WON` rendono la multipla `WON`.
- Le leg `VOID` assumono quota effettiva `1.00`; con altre leg vinte lo stato è
  `PARTIAL_VOID`, con tutte le leg void lo stato è `VOID`.

## Autorità economica

Banca resta l'unica fonte per bankroll, disponibilità, stake registrati, ritorni,
profit/loss ed esposizione. SalaBet conserva i dati analitici e operativi della
schedina e legge i valori economici dal ledger Banca per archivio e statistiche.

## API

- `POST/GET /salabet/candidates`
- `POST /salabet/candidates/{id}/remove`
- `POST/GET /salabet/slips`
- `GET/PATCH /salabet/slips/{id}`
- `POST /salabet/slips/{id}/confirm`
- `POST /salabet/slips/{id}/place`
- `POST /salabet/slips/{id}/cancel`
- `POST /salabet/slips/{id}/selections/{candidate_id}/void`
- `POST /salabet/settle`
- `GET /salabet/archive`
- `GET /salabet/stats`

Candidate, schedine, snapshot delle selezioni, eventi e note usano le tabelle
append-only introdotte dalla migration Alembic `0003`.
