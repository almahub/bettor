# Sicurezza delle giocate registrate e diagnostica del modello (0.2.5)

Le schedine confermate con addebito Banca sono giocate registrate, non vengono chiamate test nell’interfaccia. Bettor non trasmette puntate al bookmaker e l’addebito interno non prova un invio esterno. I vecchi nomi tecnici degli eventi e UUID sono conservati per compatibilità e idempotenza, senza riscrivere lo storico.

## Anteprima e autorizzazione

Dashboard → Automazione Piazza Scommessa → Attiva apre un’anteprima in sola lettura: ogni multipla, selezioni, quote, importo, totale, saldo corrente e previsto. Nessun addebito nell’apertura. Conferma soltanto queste multiple autorizza la precisa composizione mostrata: se cambia serve una nuova approvazione, e dopo la registrazione l’autorizzazione termina. La casella Autorizzo anche composizioni future, non preselezionata, abilita invece composizioni successive entro il budget di ogni turno. Anche senza tre partite attuali è possibile autorizzare esplicitamente le proposte future; il sistema attende. Le vecchie automazioni abilitate senza anteprima richiedono una nuova approvazione e non possono addebitare.

L’identificativo dell’anteprima include quote, selezioni, composizione, importi e piano. Un cambiamento durante la verifica blocca l’operazione. Un aumento oltre il budget approvato richiede una nuova autorizzazione, anche per le proposte future. Il budget resta quello del turno settimanale Europe/Rome e non viene riutilizzato a ogni polling. Le schedine manuali preesistenti non vengono alterate.

## Verifica finale automatica

Prima degli addebiti automatici, vengono consultati soltanto i provider già configurati per calendario e quote, riusando il loro comportamento cache/TTL; nessuna nuova ricerca OpenAI. Si verificano identità, casa/trasferta, kickoff futuro, quote disponibili con timestamp/provenienza ammissibili e freschezza secondo odds_ttl_seconds. Si confrontano le quote con quelle approvate: se cambiano non vengono sostituite silenziosamente. L’analisi non deve essere più vecchia dello stats_ttl_seconds esistente; eventuali analisi più recenti richiedono revisione. Un’ulteriore verifica dopo le attese controlla scadenza e aggiornamenti concorrenti. Saldo e idempotenza sono ricontrollati nella transazione che registra tutte le multiple e i movimenti Banca.

Non viene aggiunta una policy nuova per l’età dei dati e non vengono cambiati Validator, gate o stake rules. Le formazioni conservano la policy delle fonti ufficiali già esistente. La conferma manuale SalaBet conserva il flusso precedente.

## Sospensione

Dopo tre errori tecnici consecutivi di provider o registrazione, l’automazione si sospende. Lo stato è persistente dopo restart, ed è visibile in Dashboard e Data Pipeline. Assenza di partite/budget residuo, saldo insufficiente, quote scadute/cambiate e analisi aggiornata sono attese o blocchi espliciti, non guasti tecnici. Cambiare il budget non rimuove la sospensione. Per ripartire occorre rivedere l’anteprima e autorizzare nuovamente. Le giocate già registrate e il loro settlement restano invariati.

## Dove sbaglia il modello

Modelli e validazione legge le validation_observations incluse e concluse, senza aggiungere osservazioni o modificare il Validator. Una previsione per partita/modello/versione/mercato; comparire in più multiple non ne moltiplica il conteggio. Le sorgenti LIVE_FORWARD, HISTORICAL_BACKTEST e sconosciute restano separate. Sono esclusi record non conclusi, probabilità non valide e leakage temporale. Per il backtest la creazione successiva alla partita è ammessa solo nella categoria storica esplicita, mai come LIVE_FORWARD.

Raggruppamenti: mercato, fascia della probabilità dell’esito più probabile (10 punti percentuali), qualità dati quando registrata oppure completezza input esplicitamente distinta (25 punti percentuali), modello/versione. Mostra N, probabilità media, frequenza corretta, scarto osservato meno stimato e Brier medio per classe. Il Brier va confrontato a parità di mercato. Le fasce sono descrittive, non soglie decisionali. Lo storico dei risultati senza previsioni non produce artificialmente metriche. Con pochi campioni non si dichiara un modello migliore.

Le tabelle si sfogliano a cinque righe. Profitto delle schedine e calibrazione del modello sono misure separate. I mercati della stessa partita possono essere correlati.
