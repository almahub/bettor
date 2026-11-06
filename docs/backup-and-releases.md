# Backup, ripristino e rilascio Bettor

## Comando di rilascio

«Rilascia aggiornamento» autorizza una release stabile su almahub/bettor, con setup e SHA256 verificati. Non è una semplice bozza. Il workflow associato al tag di una nuova versione esegue suite, build e pubblicazione; dopo la pubblicazione riuscita conserva al massimo tre release, comprese le vecchie bozze, senza cancellare tag o sorgenti. Nessuna release viene pubblicata soltanto perché una modifica è stata implementata.

I programmi installati possono usare Controlla aggiornamenti nel menu Start oppure Sistema → Aggiornamenti → Controlla aggiornamenti. L’installazione resta volontaria: aprire la release, scaricare setup e checksum; il comando del menu Start scarica e verifica SHA256 automaticamente. Arrestare Bettor prima di installare. Nessun aggiornamento silenzioso mentre il database è aperto.

## Backup

In Sistema → Backup: Crea backup completo, poi Scarica. Copia lo ZIP anche su un disco esterno. Include data (database SQLite coerenti, storico, ricerche/cache, log, schedine, Banca), config e ambiente. Non include la cartella dei backup, gli eseguibili o l’ambiente Python. Le credenziali restano cifrate con Windows DPAPI; nel file .env portabile non sono incluse le chiavi gestite. Anche prima di un aggiornamento il setup crea un backup completo.

Ogni archivio contiene versione, data, elenco file e SHA256. La creazione rilegge l’archivio e controlla i database con quick_check. Sono accettati al massimo 1 GB compresso, 4 GB estratto e 100.000 file. Il backup viene rifiutato se sono in corso analisi/checkpoint. L’elenco UI mostra gli ultimi sei backup; le copie precedenti restano sul disco. Conservare gli ZIP privatamente: lo storico e i dati economici sono leggibili, le chiavi invece restano cifrate.

## Ripristino / nuova installazione

Scegli Ripristina su un backup locale oppure Importa backup per uno ZIP salvato. Importare solo backup fidati: la configurazione dei provider fa parte del backup. Dopo conferma l’archivio viene validato e viene programmata la sostituzione. Il database corrente non cambia ancora. Le scritture e il collector vengono sospesi. Annulla ripristino programmato riattiva il normale uso senza sostituzione.

Per applicare, arrestare davvero il backend Bettor (chiudere solo la scheda del browser non basta), poi avviare Better. Il launcher possiede il blocco di istanza e ripristina prima di aprire database e pipeline. Prima salva uno ZIP di sicurezza dello stato attuale; in caso di errore durante la sostituzione rimette i file precedenti. Non si possono ripristinare archivi prodotti da una versione più recente senza prima aggiornare Better.

Il ripristino sostituisce l’intero archivio: non fonde i database. Storico, Banca, schedine e impostazioni vengono riportati alla data del backup. Il collector resta fermo dopo il ripristino: controllare il risultato e le chiavi, poi riattivarlo in Impostazioni e riavviare. Su un nuovo PC/account Windows le chiavi DPAPI non decifrabili vengono omesse: reinserirle in Impostazioni. Gli ID degli agenti e le altre impostazioni non segrete restano disponibili.

Percorso installato: %LOCALAPPDATA%\Better\backups. I backup completi non vengono cancellati dalla regola delle tre release GitHub.
