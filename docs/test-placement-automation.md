# Automazione delle giocate in SalaBet

La conferma registra una giocata e addebita la Banca; Bettor non contatta bookmaker.
L'interruttore Automazione Piazza Scommessa resta distinto dall'analisi Serie A.
Richiede anteprima e consenso: proposta corrente oppure Approvazione Auto
(FUTURE_PROPOSALS), entro il budget autorizzato. Spegnerlo ferma nuove conferme.

Dal rilascio 0.2.9 prepara multiple READY senza addebito. La verifica finale e la
conferma avvengono a T-15 dal primo kickoff di ciascuna multipla. Il collector
ordinario opera ogni 30 minuti e anticipa le scadenze note.
Vedi [tempi e protezioni](scheduled-confirmation.md).

Il budget del turno parte da 50 EUR ed e' modificabile in Da valutare. Il turno
e' la settimana da lunedi' nel fuso Europe/Rome. Preparare assegna budget,
confermare addebita la Banca. Nessun nuovo budget a ogni polling; nessun aumento
automatico del consenso. I massimali OpenAI restano separati.

Ranking e compositore esistenti selezionano fino a sei partite Serie A entro
24 ore, 3-6 partite distinte per schedina, massimo tre multiple. Non si forzano
alternative mancanti. Mercati diversi della stessa partita restano correlati.
Il residuo si divide in centesimi tra le multiple composte.

Quote, dati, kickoff, consenso, integrita' della schedina e saldo sono verificati
prima della conferma. Quote cambiate, analisi superate o saldo insufficiente
bloccano l'addebito. Le schedine modificate, annullate o gia' confermate non
vengono riscritte. Addebito e completamento sono atomici e idempotenti.

Il pulsante manuale di preparazione resta senza addebito e non programma una
conferma automatica. Il circuito di arresto dopo tre errori tecnici resta attivo.
Le previsioni INDICATIVE mantengono stato e limitazioni del modello.
