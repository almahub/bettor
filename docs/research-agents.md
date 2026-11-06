# Due saved agent per la ricerca

Il profilo 888 usa questo flusso:

```text
888 -> identità, kickoff, quote e mercati
better-football-data-research-v1 -> dati quantitativi correnti
better-external-research-v1 -> informazioni qualitative e contestuali
validazione -> normalizzazione -> provenance -> EnrichedSnapshot -> agenti Bettor
```

I due scope sono disgiunti da `research_routing.py`. Entrambi condividono il transport Agents API,
parsing SSE, citazioni, usage, timeout e calcolo del costo, ma hanno contratti, agent ID, cache,
budget e metriche separati. Usano la stessa `OPENAI_API_KEY`.

Football Data Research è limitato inizialmente a Serie A, Serie B e Serie C. Cerca classifica,
forma recente grezza, split casa/trasferta, tiri, tiri in porta, possesso, clean sheet, BTTS,
Over/Under 2.5 e statistiche giocatori correnti. Wikipedia è ammessa soltanto come fallback Tier C
per identità, rosa, ruoli o allenatore. Storia, palmarès e dati remoti nel tempo sono fuori scope.

Le ultime partite mantengono data, avversario, sede, punteggio e competizione. System 0 calcola
W/D/L, GF/GS, medie gol, BTTS e Over 2.5. xG, xGA, PPDA, opponent strength, rating, probabilità ed
expected points non vengono inventati. Il Model Validator continua a usare soltanto validazione
storica indipendente.

External Research gestisce indisponibili, dubbi, rientri, squalifiche, lineup, convocazioni,
conferenze, cambi allenatore, turnover, congestione, news, viaggio, meteo, terreno e contesto.

`EnrichedSnapshot` mantiene tre sezioni: `structured_market_data`, `football_data_research` ed
`external_research`. `football_data_analyst` riceve lo `StatsSnapshot` derivato; predictive model,
market selection, value bettor, risk manager e professional bettor lo ricevono attraverso il DAG.

Configurazione:

```powershell
$env:OPENAI_API_KEY = "..."
$env:OPENAI_RESEARCH_AGENT_ID = "agent_..."
$env:OPENAI_FOOTBALL_DATA_AGENT_ID = "agent_..."
$env:EXTERNAL_RESEARCH_ENABLED = "true"
$env:FOOTBALL_DATA_RESEARCH_ENABLED = "true"
```

Ogni agente ha un massimo di tre ricerche nella prima passata, context size low, budget token/costo
e TTL propri. Un errore di uno non impedisce l'esecuzione dell'altro né causa un crash globale.


## Contratto operativo e avvio locale

Nel contratto `recent_form`, `home` indica la squadra di casa della partita analizzata e `away`
indica la squadra ospite; non sono filtri per sede. Servono dieci risultati verificati per ciascuna
squadra. Quando la stagione corrente ne contiene meno, la ricerca prova a completare il campione
con la stagione precedente e mantiene competizione e data di ogni osservazione. Se non riesce,
il dato resta `INSUFFICIENT_EVIDENCE` e non viene trasformato in uno `StatsSnapshot`.

Per l'installazione locale, conservare chiave e due agent ID distinti nel file `.env`. Bettor rifiuta
l'avvio se gli agent ID coincidono. Le variabili definite nella sessione PowerShell hanno precedenza
sul file: prima dell'avvio si possono eliminare valori temporanei obsoleti con:

```powershell
Remove-Item Env:OPENAI_API_KEY,Env:OPENAI_RESEARCH_AGENT_ID,Env:OPENAI_FOOTBALL_DATA_AGENT_ID -ErrorAction SilentlyContinue
```

Il rinnovo delle quote 888 non invalida la cache qualitativa: prezzi e ricerca pre-match hanno
versioni e budget separati. Un errore di autenticazione avvenuto prima della creazione della sessione
OpenAI non blocca un nuovo tentativo dopo la correzione della configurazione.
