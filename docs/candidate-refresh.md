# Recupero automatico delle candidate scadute

Quando l’automazione delle giocate è attiva e approvata, il controllo finale
continua a bloccare `ANALYSIS_STALE` con il TTL statistico già configurato.
Il Collector avvia allora un aggiornamento delle sole partite selezionate
nell’anteprima, ancora future entro 24 ore e nelle competizioni Serie A configurate.

- Verifica identità, orientamento e kickoff prima del lavoro.
- Riusa un’analisi locale più recente e valida quando disponibile.
- Altrimenti esegue la pipeline esistente, con `parent_analysis_id` e timestamp
  attuali. Archivio e cache mantengono la precedenza; i saved agent passano dal
  guard di budget esistente. Senza consenso alla ricerca a pagamento, solo cache.
- I limiti per partita/giorno/settimana e le riserve USD/EUR restano invariati.
  Una riserva di ricerca non viene ripetuta nei retry della stessa analisi scaduta.
- Ripete il ranking esistente. Crea una nuova candidata solo se utilizzabile;
  nasconde le vecchie candidate tramite eventi, senza cancellarle o modificare
  le selezioni copiate nelle schedine esistenti.
- Non registra un nuovo T24 e non ricostruisce checkpoint retroattivi. Le due
  analisi rimangono consultabili nel Journal.
- Durante l’aggiornamento non crea schedine, non conferma giocate e non muove
  denaro. Al ciclo successivo ripete tutti i controlli finali esistenti.
- Un’approvazione CURRENT_PROPOSAL va riconfermata quando cambia l’anteprima;
  FUTURE_PROPOSALS conserva il consenso già espresso entro i limiti approvati.

I claim persistenti impediscono doppie analisi dopo polling, restart e chiamate
concorrenti. Al massimo tre tentativi per analisi scaduta, distanziati almeno
15 minuti. Il timeout del singolo tentativo resta inferiore alla durata del claim.
Gli errori tecnici continuano ad alimentare l’arresto dopo tre errori consecutivi;
attendere il retry non azzera il contatore. Partite iniziate, identità cambiate e
informazioni successive all’as_of non vengono accettate.
Se l’utente disattiva l’automazione durante un aggiornamento, non vengono
avviati i lavori successivi e non viene confermata alcuna giocata dal refresh.

In Data Pipeline, “Giocate e preparazione automatiche” mostra il motivo e gli
ultimi sei eventi di aggiornamento. I fallimenti salvano soltanto reason code
controllati, senza eccezioni contenenti dettagli del provider o segreti.

Regression test: refresh senza movimenti, idempotenza/restart, concorrenza,
riapprovazione, retry limitati, informazioni future, riuso locale, budget zero,
conservazione delle schedine e assenza di acquisizioni T24 artificiali.

Suite conclusiva: 733 test Python e 35 test UI superati. Setup locale compilato
e verificato tramite SHA256; nessuna release GitHub pubblicata da questo intervento.
