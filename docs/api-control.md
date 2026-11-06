# API Control

Disponibile in **Agenti → API Control**, con sezioni separate per OpenAI, API-Football e football-data.org. Il riepilogo va letto insieme alla diagnostica: configurazione, accesso verificato e disponibilità dei dati sono condizioni distinte.

## OpenAI

Saved agent, chiamate registrate, token, costi, errori e stato del refresh contabile. Il totale contabile proviene dal Costs endpoint/export, convertito in euro; non si ricostruisce il credito residuo dal costo del periodo. Il portafoglio OpenAI non è disponibile tramite questa lettura: la UI rimanda alla pagina ufficiale del credito.

Le chiamate sono paginate a dieci righe; Attività per modello e Costi contabilizzati a cinque. Gli errori storici non dimostrano da soli un guasto corrente. Il refresh contabile richiede la chiave amministrativa distinta e l'abilitazione live; un errore può lasciare visibili gli export locali, con provenienza esplicita.

Le stime per chiamata restano distinte dal totale contabile. Se usage o costo non sono attribuibili, Bettor non li inventa. Le credenziali si configurano in Sistema → Impostazioni e non vengono mostrate nei report.

## Provider sportivi

API-Football espone configurazione/politiche di rete, piano e limiti quando verificati, season/coverage, associazioni, richieste/cache, rate limit ed errori. Una rete disattivata non viene riattivata salvando una chiave. Coverage positiva non garantisce che ogni campo di una singola partita sia popolato.

football-data.org espone stato della configurazione e dati delle verifiche disponibili. Le verifiche esplicite consumano richieste provider; le metriche non presenti restano non disponibili, senza piani o accessi presunti.

Dettagli di raccolta, checkpoint e settlement sono in Data Pipeline; la Dashboard conserva solo le attenzioni utili.
