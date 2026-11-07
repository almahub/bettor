# Bettor — aggiornamenti e ripristino guidati

## Correzione blocco finestra e tema SalaBet

Il bridge pywebview deve esporre soltanto `restart` e `install_update`.
Tutto lo stato interno di `DesktopActions`, in particolare riferimenti alla
finestra nativa e oggetti Path, usa attributi privati. La scoperta dell’API
pywebview esplora ricorsivamente gli attributi pubblici: esporre la finestra
provocava accessi COM fuori dal thread UI e blocchi della finestra Windows,
con backend ancora sano. Due regression test impediscono questa regressione.
Collaudo aggiuntivo con WebView2 reale, finestra nascosta isolata: solo le due
azioni esposte, bridge caricato e risposta JavaScript verificata per 60 secondi.
Questo collaudo non garantisce l’assenza di ogni possibile blocco a lungo termine.
Verifica suite di questa correzione: 724 test Python e 35 test UI superati.
Il processo di manutenzione rimane nascosto, ma riapre l’applicazione con
`WindowStyle Normal`, così il riavvio non lascia la finestra nascosta.

SalaBet usa superfici verde campo scuro, mantenendo l’accento ambra. Il contrasto
di testo normale, secondario, errori e AI è verificato sulle superfici verdi.

## Versione e aggiornamento

Sistema → Aggiornamenti mostra subito la versione del backend in esecuzione.
Il controllo GitHub resta un’azione esplicita. Se è disponibile una versione
più recente, il flusso è:

1. **Scarica e verifica aggiornamento**: setup e checksum SHA256 dalla release
   stabile di `almahub/bettor`; nessuna installazione durante il download.
2. **Installa aggiornamento e riavvia Bettor**: dopo conferma, il componente
   nativo chiude la propria finestra, attende la sua uscita e richiede l’arresto
   controllato del backend della stessa installazione.
3. Il processo esterno verifica nuovamente il checksum, esegue Inno Setup e
   riapre Bettor con la medesima directory dati e porta. Il setup conserva la
   verifica WebView2, il controllo backend fermo e il backup prima dell’upgrade.

Il componente esterno è necessario perché l’EXE in uso non può aggiornare sé
stesso. Il flusso non usa `Stop-Process` e non interrompe forzatamente checkpoint
o analisi. L’installazione si arresta se i controlli non passano. Nessun downgrade,
release draft o prerelease viene proposto; sono accettati gli asset Bettor e gli
alias legacy Better. Il checksum non sostituisce una firma Authenticode.

Un ripristino pendente blocca il download/installazione per evitare di unire
due interventi diversi. Prima applicarlo oppure annullarlo. Nelle versioni
precedenti senza bridge nativo il pulsante spiega la procedura manuale.

## Backup ancora visibili dopo una nuova installazione

La directory del programma e quella dei dati sono separate. Disinstallare o
scegliere “Inizia da zero” non elimina i backup precedenti in
`%LOCALAPPDATA%\Better\backups`. La pagina mostra quella directory esplicitamente
e presenta al massimo sei backup: non effettua una ricerca su tutto il PC e non
importa automaticamente un vecchio backup.

## Ripristino

Selezionare/importare un backup lo verifica e lo programma: non sostituisce il
database a backend attivo. La pagina mostra quindi il comando **Riavvia Bettor e
applica ripristino**. Il riavvio nativo usa gli stessi controlli di inattività e
identità del backend, quindi il launcher applica il backup sotto il mutex, prima
di aprire il database. Integrità, copia preventiva, rollback e DPAPI restano
quelli del servizio di backup esistente. Il collector non viene riattivato dal
ripristino.

Nelle installazioni precedenti:

1. Chiudere la finestra Bettor.
2. Dal menu Start scegliere **Arresta servizio Bettor**. Se un checkpoint o
   un’analisi è in corso, attendere e riprovare.
3. Riaprire Bettor. Il ripristino pendente viene applicato all’avvio del backend.
4. Verificare il messaggio dell’ultimo ripristino in Backup e le credenziali.

Chiudere e riaprire soltanto la finestra non riavvia il backend: il collector
continua intenzionalmente a funzionare senza la finestra.

## Collaudo

Test automatici su directory temporanee: download valido e checksum errato,
redirect non consentiti, release ambigue, autorizzazione locale/CSRF, checkpoint
occupato, ripristino pendente, avvio singolo dell’helper, versione immediata,
conferme UI e istruzioni per versioni precedenti. Nessun backup operativo è
stato applicato e nessun programma installato è stato sostituito dal collaudo.
L’installazione silenziosa completa con chiusura/riapertura delle finestre va
collaudata su una VM o un’installazione dedicata prima di pubblicare una release.

Suite completa: 722 test Python e 35 test UI superati. Verifica reale GitHub: release stabile 0.2.8 riconosciuta con setup Bettor e checksum. Il collaudo non ha installato aggiornamenti ne applicato backup operativi.
