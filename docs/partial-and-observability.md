# Audit degli appunti sui log — 28 settembre 2026

Il comando `better analyze --match demo-match --demo bet` esegue una dimostrazione sintetica esplicita. Non verifica capacità predittiva sul calcio reale. La prova ripetuta dopo le modifiche restituisce ancora BET, probabilità 67,44%, quota 2,10, EV 0,4162, stake teorico 10 unità e confidenza operativa 79,4624%.

## Riscontri nel codice

| Osservazione | Riscontro | Intervento |
|---|---|---|
| Validator PARTIAL ma MODEL_VALIDITY PASS | Mancano bankroll iniziale storico e metriche economiche che lo richiedono; campione, Brier, log loss e calibrazione sono invece calcolati sullo storico out-of-sample della competizione | Dichiarazione strutturata `HISTORICAL_BANKROLL_UNAVAILABLE`, controlli mancanti, gate interessato e motivazione del passaggio |
| Tactical PARTIAL ma confidence 79,5% | Il confronto tattico è qualitativo; non modifica probabilità o stake. La confidenza finale è il minimo di componenti quantitative, non la media della confidence degli agenti | Dichiarazione `TACTICAL_EFFECT_UNVALIDATED`, ruolo escluso dagli aggiustamenti quantitativi e scomposizione completa della confidenza |
| Due `agent_completed` per agente | Un log interno misura l'esecuzione; il secondo registra il risultato ricevuto dall'orchestratore | Interno `agent_execution_finished` a DEBUG; un solo `orchestrator_agent_result_received` a INFO |
| Agenti troppo rapidi per retrieval reale | SnapshotCollector raccoglie i dati prima di avviare gli agenti. Questi leggono copie locali, anche con provider remoti | Nuovi eventi `source_collection_completed` con tipo di fonte, classe provider, stato e durata. I tempi degli agenti non vengono presentati come tempi di rete |

La quota minima non è un buffer arbitrario sopra la quota fair: è `(1 + minimum_ev) / conservative_probability`. In questa fixture: `1,01 / 0,571690… ≈ 1,767`. La quota fair usa invece la stima puntuale: `1 / 0,674378… ≈ 1,483`. Il report mostra gli input effettivi salvati.

## Contratto delle limitazioni

Gli stati della specifica master (`OK`, `PARTIAL`, `INSUFFICIENT_DATA`, ecc.) restano invariati. AgentResult aggiunge `degradations`, con oggetti `Degradation`:

```json
{
  "code": "HISTORICAL_BANKROLL_UNAVAILABLE",
  "missing_checks": ["bankroll_roi", "bankroll_drawdown"],
  "critical": false,
  "affected_gates": ["MODEL_VALIDITY"],
  "market_scope": [],
  "impact": "WARNING_ONLY",
  "confidence_cap": null,
  "reason": "Historical bankroll metrics are not used for model admission.",
  "policy_version": "explicit_degradations_v1"
}
```

- `WARNING_ONLY`: omissione esplicita non bloccante; nessuna penalità numerica aggiuntiva. Se qualità/copertura già riducono la confidenza, la motivazione lo dichiara per evitare un doppio conteggio.
- `CONFIDENCE_CAP`: limite operativo deterministico, registrato come componente della confidenza. Il valore finale resta il minimo delle componenti, senza votazione.
- `BLOCK`: mancanza critica; il gate interessato fallisce, la confidenza operativa è azzerata e BET è impossibile. Anche un agente normalmente opzionale può segnalare una limitazione critica.

Una limitazione può essere circoscritta a mercati. DecisionDegradation conserva l'agente e `applies_to_decision`, rendendo verificabile perché una limitazione su BTTS non blocca un candidato 1X2. I gate mantengono PASS/FAIL/WAIT; un PASS con avvertimento esplicito non viene confuso con un passaggio privo di limitazioni.

BaseAgent rifiuta nuovi output PARTIAL privi di questa descrizione. Gli snapshot vecchi rimangono leggibili, ma un PARTIAL non classificato sottoposto a una nuova valutazione genera `UNCLASSIFIED_PARTIAL` bloccante. MODEL_VALIDITY richiede anche la presenza effettiva di Brier, log loss e calibrazione, oltre a stato, campione e soglie.

Quando System 2 è esplicitamente richiesto ma manca, fallisce o richiede revisione, la confidenza è limitata a 0,5, ossia l'indicatore qualitativo già adottato dall'agente tattico. È una politica conservativa dichiarata, non una probabilità calibrata. Le normali limitazioni dichiarate da un'interpretazione completata senza richiesta di revisione restano avvertimenti; le probabilità del modello non cambiano. System One resta indipendente, disattivabile e solo shadow.

## Report e log

Lo stesso comando dell'utente ora mostra log sintetici su stderr, report su stdout e JSON strutturato in `data/logs/better.jsonl`. Il file usa rotazione a 5 MB con tre copie precedenti. Esempi:

```powershell
.\.venv\Scripts\better.exe analyze --match demo-match --demo bet
.\.venv\Scripts\better.exe --log-format json analyze --match demo-match --demo bet --json
.\.venv\Scripts\better.exe --log-file .cache/session.jsonl --verbose analyze --match demo-match --demo bet
```

I flag globali precedono il sottocomando. I report indicano controlli mancanti, criticità, gate, ambito, motivazione e impatto. La dashboard espone le stesse informazioni e la scomposizione della confidenza. `RISK: LOW` resta un indicatore operativo legato ai controlli e al dimensionamento, non una probabilità empirica di perdita.

Il bus eventi mantiene `agent_completed` per compatibilità degli ascoltatori; questo non aggiunge una seconda riga INFO di esecuzione. DEBUG distingue l'evento interno, senza farlo sembrare un nuovo avvio.

## Versione e verifica

Runtime e pacchetto passano a **0.1.1**, il contratto comune degli agenti a **1.1.0**; prompt e formule del modello rimangono invariati. La versione software entra nell'hash degli input per evitare il riuso di una vecchia analisi dopo il cambiamento di policy. Gli snapshot precedenti non vengono riscritti. Il replay esatto richiede la versione software originale e segnala esplicitamente un'incompatibilità.

Verifica: 259 test Python e 5 JavaScript, più esecuzione diretta del comando demo e controlli Ruff. I nuovi test coprono PARTIAL opzionali/critici, metrica obbligatoria mancante nonostante NORMAL_WEIGHT, scope per mercato, legacy, limiti di confidenza, JSON round-trip, console/file log e raccolta distinta dall'esecuzione. I test di System One e replay rimangono verdi.

L'uso di una partita reale richiede provider, copertura dati e storico out-of-sample effettivi. Le fonti non configurate continuano a produrre errori/dati insufficienti senza fallback implicito ai fake. Le latenze, da sole, non provano né smentiscono la provenienza reale di un dato.
