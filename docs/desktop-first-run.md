# Bettor: finestra desktop e primo avvio

Da 0.2.6 il collegamento Bettor apre una finestra Windows con WebView2, senza console e barra indirizzi. Il server continua a usare solo il loopback locale. Chiudere la finestra lascia il servizio attivo per collector e automazioni; non attiva automazioni disattivate.

## Installazione nuova

Il primo avvio propone **Inizia da zero** oppure **Ripristina un backup**. In presenza di dati precedenti propone anche **Usa i dati esistenti**. La chiusura del dialogo annulla l'avvio.

**Inizia da zero** non importa soldi, schedine, analisi, ricerche, chiavi o impostazioni personali. Banca non inizializzata, saldo zero, nessuna giocata, nessuna osservazione Validator precedente, collector e automazioni spenti. I file pubblici di configurazione sono valori predefiniti del programma, non configurazioni personali.

Se esistono dati, prima del nuovo archivio viene creato un backup completo verificato; i file precedenti vengono conservati in `backups/first-run-previous-*`. In caso di errore durante la creazione del nuovo archivio vengono ripristinati i file precedenti. Questa operazione non cancella le copie di sicurezza precedenti.

**Ripristina un backup** usa il ripristino verificato esistente, a database fermo, con copia di sicurezza preventiva quando ci sono dati. Recupera storico, Banca, schedine e impostazioni del backup. Le chiavi DPAPI sono utilizzabili solo se Windows può decifrarle; su un altro account/PC devono essere reinserite. Il collector resta disattivato dopo il ripristino.

## Configurazione mancante

Sistema → Impostazioni mostra chiavi/ID mancanti, indisponibilità delle rispettive ricerche, provider risultati non configurato, archivio storico vuoto e Banca senza fondi. La Dashboard rimanda al riepilogo se incompleto. Una chiave configurata non dimostra accesso/autenticazione: usare API Control per verificarli. Nessun fondo viene generato automaticamente.

## Servizio, riavvio e disinstallazione

- Chiudere la finestra, usare **Arresta servizio Bettor** dal menu Start, quindi riaprire Bettor per applicare nuove credenziali o un ripristino programmato.
- **Configurazione iniziale Bettor** nel menu Start riapre le scelte, solo a servizio fermo.
- Aggiornare normalmente conserva i dati e la scelta iniziale.
- La disinstallazione arresta il servizio se inattivo e rimuove soltanto il marcatore della scelta iniziale: conserva dati e backup. Una reinstallazione chiede nuovamente cosa fare, senza cancellare silenziosamente nulla.
- Il servizio in background non inizializza un archivio prima della scelta dell'utente.

Richiesto Microsoft Edge WebView2 Runtime. Il setup ne controlla la presenza e, se manca, indica il download ufficiale senza modificare i dati. Riferimento: [distribuzione WebView2](https://learn.microsoft.com/en-us/microsoft-edge/webview2/concepts/distribution).

I dati restano nella directory utente, normalmente `%LOCALAPPDATA%/Better`; il setup non include database o credenziali personali. I log del launcher senza console sono in `logs/desktop.log`; quelli operativi restano in `data/logs/better.jsonl`.
