# Automazione Serie A — solo T24

Con Automazione Serie A attiva il collector scopre le partite presenti nel catalogo tramite la coorte, senza richiedere la selezione manuale. Sono ammesse solo le competizioni Serie A configurate per questa automazione.

- Analisi iniziale a kickoff meno 24 ore, entro la tolleranza esistente di dieci minuti e i massimali già configurati.
- Archivio e cache hanno priorità; nessuna ripetizione di ricerche con cache valida.
- Nessuna analisi automatica T30 e nessun confronto sperimentale fra checkpoint.
- Ogni analisi utilizzabile trasferisce la migliore candidata secondo il ranking esistente a SalaBet / Da valutare. Il requisito è 0 < kickoff - adesso <= 24 ore.
- Il trasferimento usa l'identità deterministica della candidata e un evento append-only: polling e restart non creano duplicati; un capture completato prima del restart può essere trasferito senza rifare l'analisi.
- Un pronostico senza valore positivo o con input non utilizzabili non viene forzato: motivo registrato nella diagnostica dell'automazione.
- Le candidate INDICATIVE restano tali, con tutti i blocchi operativi esistenti.
- La preparazione delle multiple resta manuale e seleziona fra le candidate ancora future nella finestra delle prossime 24 ore, al massimo sei partite. Tutti i kickoff del gruppo ricadono quindi in un intervallo massimo di 24 ore.
- Non vengono create/confermate schedine o registrati movimenti Banca dall'analisi automatica.

Un checkpoint T24 mancato non è ricreato retroattivamente; la UI lo segnala. La disponibilità dipende dal catalogo, dalle fonti e dal fatto che PC e Better siano accesi. L'interruttore conserva la scelta dell'utente durante l'aggiornamento.

I checkpoint passati T30 rimangono nello storico append-only; il lettore storico interno è conservato per compatibilità, senza essere usato dalla UI operativa. Il Validator e la raccolta/settlement della coorte non sono stati modificati. La chiave API storica timing_comparison resta compatibile: restituisce mode=T24_ONLY e nessuna metrica di confronto.

## Verifiche

665 test Python e 25 test UI superati; controlli Ruff superati. Test mirati per assenza di T30, scoperta di partite non selezionate, trasferimento idempotente e ripresa dopo capture, input non utilizzabili, no leakage, nessuna schedina/validazione/Banca generata dal checkpoint. Smoke test del bundle 0.2.1 con archivio separato e collector spento, poi controllo sull'installazione aggiornata.
