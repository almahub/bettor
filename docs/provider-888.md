# Provider 888

Il profilo `config/888` legge calendario e quote native 888 tramite gli adapter esistenti. La configurazione distribuita abilita **Serie A e Serie B** mediante ID competizione e pattern verificati. Il collector e l'automazione analisi restano spenti in una nuova installazione fino alla configurazione esplicita; l'automazione Serie A usa T−24.

L'identità della competizione non viene dedotta soltanto dal nome visualizzato. Il catalogo usa ID nativi, casa/trasferta e kickoff; mapping verso provider statistici e risultati rimangono espliciti e verificati. I casi non risolti o ambigui non vengono promossi automaticamente.

Catalogo e quote sono aggiornati secondo le TTL configurate. Il numero di partite disponibili dipende dal feed al momento della richiesta, non è costante. Nessun catalogo sintetico sostituisce un errore del feed.

1X2, Over/Under 2,5 e BTTS sono i mercati operativi supportati. Quote mancanti, stale, sospese o incoerenti rimangono cause esplicite di blocco. Nessuna esecuzione di puntate o accesso a conti bookmaker è implementato.

La suite offline verifica identità, cache, timestamp, scala delle quote, mercati ammessi, sospensioni ed errori HTTP tramite trasporti di test. Questa verifica non equivale a un impegno di disponibilità del feed futuro.
