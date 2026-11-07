# Conferma automatica delle multiple a T-15

Il collector esegue il ciclo ordinario ogni 1.800 secondi. Tra i cicli un timer
locale controlla soltanto le scadenze persistenti: nessuna ricerca o richiesta
provider viene avviata dal timer. Le scadenze note T-24 delle analisi Serie A e
T-15 delle multiple anticipano il ciclo ordinario, anche dopo un riavvio.

## Preparazione e conferma

Con automazione delle giocate e consenso validi, le candidate utilizzabili
vengono trasformate in multiple READY. La preparazione assegna il budget del
turno, senza addebitarlo alla Banca. Non modifica schedine preesistenti.

Ogni multipla conserva una scadenza propria: il minimo dei kickoff delle sue
selezioni meno quindici minuti. Per kickoff alle 15:00, 18:00 e 20:30, il
controllo finale parte alle 14:45. Due multiple disgiunte possono avere scadenze
diverse. Non si attende l'ultima partita della giornata.

Al controllo finale vengono verificati identità, calendario, analisi, quote,
consenso, versione della schedina e disponibilità della Banca. Le regole di
ammissione restano quelle esistenti. Quote cambiate o analisi superate bloccano
la conferma: una schedina già preparata non viene riscritta silenziosamente.

La conferma può iniziare soltanto nel minuto successivo alla scadenza T-15;
anche la verifica deve concludersi entro questa finestra. Un backend spento,
un ciclo troppo lungo o un errore non autorizzano recuperi tardivi: la schedina
rimane READY, con T15_MISSED e possibilità di controllo manuale. Nessun dato
post-kickoff diventa una feature pre-match.

## Consenso, budget e idempotenza

Approvazione Auto è il consenso esistente FUTURE_PROPOSALS, visibile nella
Dashboard e nell'anteprima. CURRENT_PROPOSAL vale soltanto per la composizione
approvata e si disattiva al termine delle conferme programmate. Spegnere
l'automazione interrompe nuove conferme, senza cancellare schedine o movimenti.
Una nuova attivazione non riutilizza implicitamente il consenso delle vecchie
schedine programmate: queste richiedono gestione manuale.

Il budget è cumulativo per il turno settimanale, non per ciclo. I record di
preparazione, firma della schedina e completamento sono persistenti. Il controllo
finale rilegge consenso e stato sotto la transazione che registra il movimento;
polling concorrenti e riavvii non producono addebiti duplicati. Le schedine
manuali, modificate, annullate o già confermate non vengono alterate.

Il programma deve restare in esecuzione e il PC non deve essere sospeso.
Il timer non è un servizio di sistema capace di risvegliare Windows.
Il promemoria Dashboard resta un ausilio separato dalla conferma automatica.

## Diagnostica

SalaBet mostra l'orario della conferma programmata e il primo kickoff.
Data Pipeline espone scheduled_confirmations, approvazione richiesta ed errori.
AWAITING_T15 indica attesa; T15_MISSED indica finestra perduta;
SCHEDULED_SLIP_CHANGED protegge modifiche manuali. Gli errori tecnici ripetuti
usano il circuito di arresto già esistente. Archivio e cache restano prioritari;
i massimali OpenAI non vengono aumentati.
