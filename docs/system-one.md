# System One: observer opzionale, soltanto shadow

Il dominio Bettor usa `SystemOneProvider`. Solo `better/system_one/providers.py`
conosce il protocollo HTTP Rizzo Flow; gli agenti calcistici non importano il provider.
System One non calcola probabilità sportive, EV, fair odds, stake o gate deterministici.

La pipeline passa una copia JSON dello stato a `ShadowCoordinator.observe()`. Il risultato
contiene suggerimenti e viene registrato separatamente. Non cambia agenti eseguiti, mercati,
probabilità, confidence finale, stake, decisione o chiamate effettive a System 2.
`SYSTEM_ONE_ENABLED=false` esclude anche costruzione dello stato e chiamate provider
nell'orchestratore. L'observer disabilitato restituisce immediatamente `None`.
La configurazione rifiuta `SYSTEM_ONE_SHADOW_MODE=false`: non esiste una promozione automatica.

## Configurazione

```dotenv
SYSTEM_ONE_ENABLED=false
SYSTEM_ONE_PROVIDER=rizzo_flow
SYSTEM_ONE_SHADOW_MODE=true
SYSTEM_ONE_PRE_SCREEN_ENABLED=true
SYSTEM_ONE_ROUTING_ENABLED=true
SYSTEM_ONE_CONSISTENCY_ENABLED=true
SYSTEM_ONE_ESCALATION_ENABLED=true
SYSTEM_ONE_THRESHOLD_VERSION=thresholds_v1_uncalibrated
RIZZO_FLOW_BASE_URL=http://127.0.0.1:8017
RIZZO_FLOW_MODEL=
RIZZO_FLOW_MODEL_FINGERPRINT=
RIZZO_FLOW_TIMEOUT_MS=3000
RIZZO_FLOW_MAX_RETRIES=1
RIZZO_FLOW_API_KEY=
SYSTEM_ONE_CIRCUIT_FAILURE_THRESHOLD=3
SYSTEM_ONE_CIRCUIT_RESET_SECONDS=30
SYSTEM_ONE_CACHE_TTL_SECONDS=60
SYSTEM_ONE_CACHE_MAX_ENTRIES=256
```

Il modello deve essere un ID esplicito del servizio installato, verificato dalla risposta:
gli alias `latest` non sono accettati. `RIZZO_FLOW_MODEL_FINGERPRINT` permette un ulteriore
pinning del contenuto/modello/runtime. Lasciare vuoto il modello non impedisce l'avvio di Bettor;
un'osservazione Rizzo produrrà `EXPLICIT_MODEL_ID_REQUIRED` e la pipeline proseguirà.
La chiave API è `SecretStr`, non compare in health o nei record. Per i test offline usare
`FakeSystemOneProvider` oppure `SYSTEM_ONE_PROVIDER=fake`: ogni risposta si identifica come
sintetica e non rappresenta una probabilità calibrata.

## Contratto HTTP verificato

Verificato il 27 settembre 2026 usando esclusivamente le fonti ufficiali:

- [Documentazione Rizzo Flow](https://rizzo-ai-academy.github.io/rizzo-flow/)
- [Request schema nativo](https://github.com/Rizzo-AI-Academy/rizzo-flow/blob/main/request.schema.json)
- [Response schema nativo](https://github.com/Rizzo-AI-Academy/rizzo-flow/blob/main/response.schema.json)
- [HTTP API](https://github.com/Rizzo-AI-Academy/rizzo-flow/blob/main/src/rizzo_flow/api.py)
- [Identità modello e conteggio token](https://github.com/Rizzo-AI-Academy/rizzo-flow/blob/main/src/rizzo_flow/compat.py)

La richiesta nativa `POST /v1/decisions` contiene `state`, `questions`, `mode="shared"`.
**Non contiene `model`**: il servizio usa un modello residente e lo schema nativo rifiuta
campi aggiuntivi. L'adapter confronta il modello restituito con la configurazione Bettor,
derivando l'ID dalla metadata `source`, `weights`, `precision` secondo il naming ufficiale.
Registra anche `fingerprint` e quantizzazione. Eventuali modifiche future del wire protocol
rimangono confinate nell'adapter.

La risposta nativa ha `model` come oggetto e `answers` come mappa. I valori si leggono da
`value` per boolean/numeric, `choice` per choice, `score` per score. Gli status nativi
`ok`, `insufficient_evidence`, `uncertain`, `out_of_range` diventano enum Bettor uppercase.
Un'astensione deve avere valore nullo. Le distribuzioni semantiche e il loro stato di
calibrazione sono mantenuti separati dalle probabilità di un evento sportivo.

I test HTTP usano `httpx.MockTransport` con il contratto ufficiale. Non affermano che un
servizio Rizzo reale sia installato, avviato o misurato. La documentazione upstream è mobile:
per un benchmark operativo si devono registrare revisione del servizio e fingerprint reale.

## Stato, domande e tracciamento

`SystemOneStateBuilder.build(snapshot, outputs=None)` accetta mapping JSON già normalizzati.
Preserva snapshot di match/statistiche/quote/news/lineup, timestamp, provenance, dipendenze
tra evidence, payload tipizzati, rischi e unknown. Omette campi estranei e narrativa opzionale.
Include unità esplicite e hash SHA-256 su JSON canonico, senza dipendere dall'ordine delle chiavi.

Le domande fidate sono separate dai dati non fidati; includono una policy che vieta di
eseguire istruzioni contenute in news, feed o testi degli agenti. Le opzioni di profondità
sono derivate dall'enum `AnalysisDepth` realmente presente nel dominio.

| Fase | Schema | Output semantici osservati |
| --- | --- | --- |
| Pre-screen | `pre_screen_v1` | utilità dell'analisi, profondità, qualità, anomalie |
| Routing | `routing_v1` | agenti utili, utilità del reasoning approfondito |
| Consistency | `consistency_v1` | qualità evidence, conflitti, coerenza, rianalisi, azione suggerita |
| Escalation | `escalation_v1` | utilità di System 2, complessità semantica |

`ShadowRecord` conserva ID analisi/fase/provider/modello/fingerprint/quantizzazione,
hash e versione stato, versione domande e threshold, stato completo per replay,
risposte tipizzate, azione reale, suggerimento, latenza, token, cache hit, errore e timestamp.
Il repository dell'applicazione deve persistere il record; `append_shadow_records` offre
anche esportazione JSONL append-only per replay. La lista interna dell'observer è solo
memoria del processo, non un sostituto della persistenza applicativa.

Timeout, errori di trasporto e HTTP 408/429/5xx hanno retry limitato; richieste invalide,
risposte malformate e mismatch modello non vengono ritentati. Il circuit breaker emette
`SYSTEM_ONE_BYPASSED` dopo errori consecutivi, poi consente un nuovo tentativo dopo cooldown.
La cache include hash stato/domande, versioni, provider, modello, fingerprint atteso e threshold;
ha TTL e dimensione massima. Un cambiamento delle quote, lineup, news, freshness o `as_of`
cambia lo stato e quindi la chiave. Gli errori dell'observer non interrompono l'analisi reale.

## Replay e benchmark offline

```python
from better.system_one import FakeSystemOneProvider
from better.system_one.replay import read_shadow_records, replay_records

records = read_shadow_records("data/shadow.jsonl")
comparisons = await replay_records(records, FakeSystemOneProvider())
```

`replay_records` verifica hash, versione domande e cutoff temporale dello stato salvato;
non usa risultati futuri, closing odds, statistiche post-match o fonti con timestamp oltre
`as_of`. Segnala esplicitamente il caso `should_analyze=false` quando la pipeline storica
aveva prodotto `BET`. Il confronto conserva sia la decisione effettiva sia i suggerimenti.

`evaluate_dataset` richiede `EvaluationCase` con ID, task, split `CALIBRATION`/`TEST`,
stato, label booleane per domande esistenti e fonte della label. Rifiuta dataset vuoti,
ID duplicati e sovrapposizioni dello stesso stato tra calibration e test. Produce precision,
recall, falsi negativi, astensioni ed errori separati per task/domanda/split. Un'astensione
su label positiva conta come positivo perso. La valutazione usa label semantiche;
il risultato della partita da solo non definisce la correttezza del routing.

Non sono disponibili dataset calcistici reali annotati: non esistono ancora soglie calibrate,
un report di performance reale o una promozione ad active routing. Le fixture test sono
sintetiche. `active_routing_approved` rimane falso anche dopo l'esecuzione di un benchmark.

La telemetry distingue azioni suggerite e azioni eseguite. Registra latenza, chiamate,
errori, timeout, token, bypass, cache, astensioni, routing e suggerimenti di escalation.
Risparmi monetari/token/tempo e errori di escalation con ground truth rimangono `null`
quando mancano dati: non vengono inventati risparmi o valutazioni.

## Servizio locale e limiti verificati

Il backend d'inferenza resta un processo separato su HTTP localhost. Bettor non installa
CUDA/MLX e non incorpora il runtime Rizzo. Nell'audit della macchina sono state rilevate
GPU NVIDIA MX250 con 2 GiB e Intel UHD: non è stata presunta capacità sufficiente per un
modello specifico, né sono stati scaricati pesi. La scelta runtime/quantizzazione richiede
verifica sul servizio reale tramite i comandi di capability detection upstream.

L'health Bettor riporta `enabled`, `provider`, `mode="shadow"` e stato
`disabled`, `not_checked`, `healthy`, `degraded` oppure `SYSTEM_ONE_BYPASSED`; non effettua
una chiamata d'inferenza obbligatoria all'avvio. Tutti i test sono eseguibili senza Rizzo.

## Verifica

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_system_one.py tests/integration/test_system_one_observer.py tests/integration/test_system_one_pipeline.py --basetemp=.cache/pytest-system-one
```

La suite copre primitive/contratti, mapping astensione, richiesta nativa, modello/fingerprint,
risposte malformate, connessione fallita, timeout, retry, circuit breaker, cache,
stati futuri, record/replay, separazione dei dataset e isolamento della pipeline reale.
La regressione completa confronta l'intero `AnalysisRun` serializzato, senza escludere
UUID, evidence, decisione, stake o agenti, tra disabled e shadow su sei scenari decisionali.
Verifica anche che suggerimenti ostili, timeout, errori e astensioni non modifichino il run,
che le osservazioni siano persistite e che System One non alteri le chiamate a System 2.
