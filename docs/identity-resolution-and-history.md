# Identity resolution 888 → Sportradar e raccolta storica

Il resolver incrocia esclusivamente la partita 888 selezionata con il programma
Sportradar del giorno. L'ordine di autorità è: mapping persistito verificato,
mapping iniziale in `config/888/sources.yaml`, quindi risoluzione automatica ad
alta confidenza. Il confronto automatico usa orientamento casa/trasferta,
normalizzazione dei nomi, competizione e distanza dal kickoff.

La risoluzione è fail-closed. Un candidato viene accettato automaticamente solo
se supera la soglia complessiva, le due soglie sui nomi, il limite temporale, il
controllo della competizione e il margine sul secondo candidato. I motivi di
fallimento sono strutturati: `NO_CANDIDATE`, `LOW_CONFIDENCE`,
`AMBIGUOUS_CANDIDATES`, `AMBIGUOUS_VERIFIED`, `COMPETITION_MISMATCH` e
`KICKOFF_OUTSIDE_AUTO_WINDOW`.

Le corrispondenze accettate salvano in modo append-only gli ID di partita,
squadre e competizione nella tabella `identity_mappings`, insieme a punteggio,
metodo, orario, hash del programma Sportradar e hash della fixture 888. Due ID
Sportradar verificati per la stessa identità 888 producono un errore bloccante:
non viene scelto silenziosamente il record più recente. Ogni tentativo viene
salvato in `identity_resolution_attempts` con candidati, punteggi e motivo.

Comandi operativi:

```powershell
.\.venv\Scripts\better.exe --config config/888 identity-coverage
.\.venv\Scripts\better.exe --config config/888 history-status
```

Le stesse informazioni sono disponibili da `GET
/sources/sportradar/identity-coverage` e `GET /history/collection`. Il riepilogo
del workspace include inoltre `sportradar.identity_coverage`.

Ogni consultazione reale di una partita 888 selezionata e ogni analisi live
archiviano un'osservazione point-in-time in `historical_observations`. La riga
contiene partita, cutoff, istante di acquisizione, snapshot disponibili ed
errori delle fonti. La scrittura è ammessa soltanto prima del kickoff e con
timestamp consapevoli del fuso orario.

Queste righe hanno sempre `eligible_for_model_validation: false`. Non vengono
caricate in `validation_history`: diventano materiale di validazione soltanto
dopo che una predizione realmente emessa entro il cutoff viene collegata a un
esito registrato successivamente. Demo, replay e richieste con `as_of` esplicito
non avviano la raccolta, così nessuna osservazione acquisita oggi viene
retrodatata.
