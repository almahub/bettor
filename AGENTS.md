# Bettor — repository instructions

Root operativo sorgenti: D:\bettor. I dati dell’installazione sono nella directory utente effettiva, normalmente %LOCALAPPDATA%\Better; non usare automaticamente il vecchio database nella root sorgenti.

## Comando dell’utente: Rilascia aggiornamento

Quando l’utente scrive «Rilascia aggiornamento», autorizza la pubblicazione di una nuova release stabile su GitHub almahub/bettor, visibile al controllo aggiornamenti dei programmi installati.

Prima completare test, build e verifica del setup e checksum, verificare che la versione sia nuova e coerente, includere esclusivamente sorgenti pubblici e note di rilascio. Non caricare dati, credenziali, database, backup o log privati. Pubblicare setup x64 e checksum. La pubblicazione non deve restare solo in bozza.

Dopo una pubblicazione riuscita conservare al massimo tre release, comprese le eventuali vecchie bozze. Eliminare le release più vecchie e i relativi asset, senza cancellare tag o storico Git. Non eliminare release prima che il nuovo setup sia pubblicato e verificato.

L’implementazione di una funzionalità da sola non equivale al comando di pubblicazione: non pubblicare questo aggiornamento finché non richiesto.

I backup devono conservare Banca, storico, schedine e impostazioni. Ripristinare solo da backend fermo, con verifica integrità e copia di sicurezza preventiva. Su altri PC richiedere nuove immissioni delle chiavi legate a Windows DPAPI.

## Nome del prodotto
Il nome corretto e definitivo indicato dall’utente è Bettor. Usarlo in UI, finestre, messaggi, setup e comunicazioni. I percorsi legacy Better, il modulo Python better, i mutex, gli header e i formati backup esistenti restano compatibili: non rinominarli causando nuovi archivi o perdita dei dati.
