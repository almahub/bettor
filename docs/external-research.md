# External Research Service

Versione 0.2.0. Nel profilo 888 il saved agent OpenAI è la fonte primaria dei dati
qualitativi pre-match. Non calcola probabilità, fair odds, EV, Kelly, stake o decisioni.

## Flusso

```text
888 identity + competition + kickoff + odds + markets
  -> ResearchRequest unico
  -> OpenAI saved Agent Session (massimo 2 Web Search nella prima passata)
  -> validazione Pydantic, citazioni, cutoff e source policy
  -> EnrichedSnapshot
  -> NewsSnapshot + LineupSnapshot
  -> agenti Bettor e downstream DAG
```

Gli agenti non hanno accesso al provider OpenAI. Ricevono l'`EnrichedSnapshot` nel loro
`AgentContext` e le viste normalizzate già supportate da `news_intelligence` e
`lineup_player_impact`. `tactical_analyst`, `devils_advocate` e `professional_bettor` ricevono
gli output tramite le dipendenze dichiarate nel DAG.

## Campi e stati

La richiesta primaria comprende injuries, suspensions, unavailable/returned players,
probable/confirmed lineups, squad callups, conferenze, turnover, manager change, fixture
congestion, relevant match news, meteo, condizioni del terreno/gioco e contesto. Ogni campo
conserva `value`, `status`, `confidence`, source URL, publisher, tier, `published_at`,
`retrieved_at`, `as_of`, lineage, conflitti e l'elenco completo delle fonti.

Gli stati operativi sono `CONFIRMED`, `PROBABLE`, `REPORTED`, `NOT_FOUND`, `CONFLICT`,
`INSUFFICIENT_EVIDENCE` e `STALE`. Le fonti C/D non possono produrre `CONFIRMED`; una formazione
confermata richiede Tier A ufficiale e undici giocatori distinti per squadra.

Priorità fonti: club ufficiali; competizioni/federazioni; broadcaster e media affidabili;
team reporter affidabili; aggregatori soltanto se necessari. Informazioni pubblicate dopo
`as_of` sono escluse. Il servizio non ricerca o inventa xG, xGA, PPDA, opponent strength,
probabilità o rating proprietari.

## Runtime, budget e storage

Il transport predefinito è `agents_api` con `OPENAI_RESEARCH_AGENT_ID`. Bettor invia soltanto
identità partita, competizione, kickoff, `as_of`, campi richiesti e budget. Modello, istruzioni,
reasoning effort, verbosity e Web Search appartengono alla definizione salvata. Il transport
Responses API resta disponibile come fallback esplicito.

```powershell
$env:EXTERNAL_RESEARCH_ENABLED = "true"
$env:RESEARCH_PRIMARY_SOURCE = "true"
$env:OPENAI_API_KEY = "..."
$env:OPENAI_RESEARCH_AGENT_ID = "agent_..."
$env:OPENAI_RESEARCH_TRANSPORT = "agents_api"
```

Cache, timeout, massimo tool call, token e costo per partita restano attivi. Request, result,
finding e source sono salvati append-only nelle rispettive tabelle. Timeout, rate limit, schema
invalido o servizio disabilitato degradano a uno stato `RESEARCH_*` esplicito senza cambiare
soglie, Decision Engine o risk policy.

I dati quantitativi correnti appartengono al distinto Football Data Research Agent; questo servizio
non ricerca statistiche di rendimento. Sportradar e API-Football non sono selezionati dal profilo 888. I loro adapter e test restano
disponibili per profili futuri. Il dataset storico resta una pipeline distinta e point-in-time.

## Verifica reale del 1 ottobre 2026

Il tentativo read-only di risolvere `better-external-research-v1` tramite `GET /v1/agents` ha
restituito `401 invalid_api_key`; `OPENAI_RESEARCH_AGENT_ID` non è presente nell'ambiente.
Nessuna sessione è stata creata e non ci sono tool call, token, costi, fonti o finding reali da
riportare. Il test va ripetuto con una chiave valida dello stesso progetto e l'agent ID.
