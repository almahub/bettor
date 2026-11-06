# Impostazioni locali

Sistema → Impostazioni permette di configurare le chiavi OpenAI, football-data.org e API-Football e gli ID dei due saved agent. La chiave amministrativa OpenAI è opzionale e distinta da quella usata per la ricerca.

Le chiavi già configurate vengono importate nel file credentials.dpapi, cifrato mediante Windows DPAPI per l’account Windows corrente. Prima della rimozione delle righe gestite da .env viene conservata una copia completa cifrata in backups. I dati e i database non vengono modificati. Il file cifrato non è trasferibile liberamente su un altro account/PC: sul nuovo PC reinserire le chiavi. I vecchi backup precedenti alla migrazione possono ancora contenere .env e devono restare privati.

I campi password sono vuoti: il segnaposto Configurata non è la chiave. Mostra digitato rivela soltanto il nuovo testo inserito. Lasciare vuoto conserva il valore; Rimuovi lo annulla esplicitamente. Salva richiede poi un riavvio controllato di Bettor, quando non sono in corso analisi/checkpoint. Salvare una chiave non cambia le politiche dei provider né attiva automazioni.

Verifica credenziale salvata effettua una richiesta reale all’endpoint ufficiale: OpenAI /v1/models, football-data.org /v4/competitions/SA, API-Football /status. Non esegue generazioni o ricerche OpenAI. Un successo OpenAI verifica autenticazione/accesso ai modelli, non l’esecuzione dei saved agent. Le verifiche sportive consumano una richiesta provider. La chiave amministrativa è utilizzata dalla diagnostica OpenAI esistente e non dal controllo /models.

L’API di configurazione accetta soltanto peer e host locali; controlla origine e token per le modifiche, non restituisce segreti e disabilita la cache HTTP. Nessuna credenziale viene salvata nel browser.
