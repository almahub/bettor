# System 0: formule, modelli e limiti

Il codice in `better/system_zero/` e `better/models/` è deterministico e indipendente
da LLM, System One e provider. Ogni fixture dei test è sintetica. Le soglie iniziali
sono configurabili, non risultati di calibrazione empirica sul calcio.

## Quote e probabilità

`better.system_zero.math` espone le funzioni seguenti. Le quote sono decimali,
finite e strettamente maggiori di 1; le probabilità sono finite in [0, 1]. Booleani,
NaN e infinito vengono rifiutati. Non si costruiscono valori inventati per riempire
campi assenti. `fair_odds(0)` e `minimum_odds(0)` sollevano `ValueError`, perché non
esiste una quota finita da serializzare.

| Funzione | Formula / significato |
|---|---|
| `implied_probability(o)` | `1 / o` |
| `bookmaker_margin(odds)` | `sum(1/o_i) - 1` |
| `devig(odds)` | metodo proporzionale: `(1/o_i) / sum(1/o_j)` |
| `fair_odds(p)` | `1 / p` |
| `edge(p, q)` | `p - q`, dove `q` è la probabilità senza vig |
| `expected_value(p, o)` | `p*o - 1`, profitto atteso netto per unità |
| `minimum_odds(p, min_ev=0)` | `(1 + min_ev) / p` |
| `kelly(p, o)` | `max(0, min(1, (p*o - 1)/(o - 1)))` |
| `fractional_kelly(p, o, fraction=.25)` | `fraction * kelly(p, o)` |
| `stake_amount(bankroll, fraction)` | importo `Decimal`, arrotondato verso il basso al centesimo |

La specifica riporta un esempio di edge prossimo all'EV senza definirne la formula.
Qui **edge significa punti di probabilità**; non è né EV né percentuale relativa.
La soglia di edge e il vincolo EV positivo sono controlli distinti. La quota minima
assicura l'EV richiesto; non può da sola garantire l'edge sulla distribuzione senza
vig, che dipende da tutte le quote dello stesso mercato. Al prezzo di pareggio
esatto, EV è zero: il gate `EV > 0` resta necessario.

Il de-vig richiede un insieme completo di esiti mutuamente esclusivi, riferiti allo
stesso mercato/bookmaker/istante. `validate_complete_market()` controlla gli esiti;
gli adapter devono controllare timestamp e sorgente. Un overround negativo è
ammesso come osservazione matematica, senza dichiarare automaticamente arbitraggio.

## Mercati con rimborso e matched betting

Il percorso standard supporta 1X2, Over/Under 2.5 e BTTS. DNB e handicap interi
richiedono un esito di rimborso: `push_expected_value(win, loss, odds)` usa
`win*(odds-1)-loss`, mentre `push_fair_odds(win, loss)` usa `1+loss/win`.
Non usare la probabilità incondizionata di vittoria dentro EV o Kelly binari per
questi mercati. Gli Asian Handicap a quarti richiedono due mezze puntate e restano
esclusi dalla selezione automatica finché non è collegato un settlement dedicato.

`matched_back_lay()` è un calcolo separato dal giudizio sportivo. Con puntata back
`B`, quota back `b`, quota lay `l`, commissione `c`, lay stake è `B*b/(l-c)`;
per free bet senza restituzione della puntata è `B*(b-1)/(l-c)`. Liability è
`lay_stake*(l-1)`. Gli importi sono `Decimal`. La funzione restituisce anche i due
profitti per verificare la copertura. Non valuta automaticamente eleggibilità,
restrizioni della promozione o rollover, e non esegue scommesse.

## Modelli riproducibili

- `poisson.score_distribution(home_rate, away_rate, max_goals=12)` usa Poisson
  indipendenti. Restituisce `{(gol_casa, gol_trasferta): p}`. Espone anche
  `truncated_mass()`: se il cutoff perde oltre lo 0.1% della massa, rifiuta il
  calcolo chiedendo un cutoff maggiore. Il residuo viene normalizzato; la
  limitazione dell'indipendenza dei gol resta esplicita.
- `poisson.market_probabilities(distribution)` aggrega 1X2 (`HOME/DRAW/AWAY`),
  `OVER_UNDER_2_5` (`OVER_2_5/UNDER_2_5`) e BTTS (`YES/NO`). Richiede una
  distribuzione normalizzata e non negativa.
- `dixon_coles.score_distribution(..., rho=-.1)` applica tau: 0–0
  `1-lambda*mu*rho`, 0–1 `1+lambda*rho`, 1–0 `1+mu*rho`, 1–1 `1-rho`.
  Rifiuta combinazioni che generano massa negativa. Il rho iniziale non è stimato
  da partite reali e il modello necessita validazione prima dell'uso operativo.
- `elo.probabilities()` usa rating, vantaggio casa e scala configurabili; la
  probabilità di pareggio è un parametro separato. `elo.update()` conserva la
  somma dei rating. Non presenta queste assunzioni iniziali come parametri fitted.
- `monte_carlo.simulate(distribution, samples=10000, seed=42)` campiona con un RNG
  locale e ordinamento stabile degli esiti. Il seed va conservato nel run.
- `ensemble.combine(predictions, weights, suspended=...)` combina distribuzioni
  compatibili con pesi espliciti. Modelli sospesi o con peso zero non contribuiscono.
  Nessun peso viene inventato da un LLM.
- `logistic.fit()` è una baseline binaria con gradient descent deterministico e
  regolarizzazione L2. Richiede disponibilità temporale per ogni riga e cutoff;
  i timestamp devono rappresentare la disponibilità congiunta di feature e label.
  Non normalizza feature automaticamente: la trasformazione deve essere fitted
  sul solo training e versionata dal chiamante.

## Validazione e backtest

`calibration.brier_score()` calcola media di `(p-y)^2`; `log_loss()` usa clipping
esplicito a epsilon `1e-15`; `reliability_curve()` aggrega bucket uniformi, lasciando
i bucket vuoti con medie nulle; `expected_calibration_error()` pesa gli scarti dei
bucket per il campione.

`validation.validate_model(predictions, outcomes, ...)` è indipendente dalla
costruzione delle predizioni. Campione insufficiente assegna peso operativo zero;
Brier o calibration error oltre soglia sospendono il modello; deterioramento
intermedio riduce il peso. Le soglie predefinite non sostituiscono benchmark
out-of-sample e vanno configurate per segmento.

`closing_line_value(entry, closing) = entry/closing - 1` misura miglioramento di
prezzo e non profitto. `performance()` distingue ROI (`profitto/bankroll iniziale`)
da yield (`profitto/turnover`); drawdown è la massima perdita relativa dal picco
della curva del capitale; volatilità è la deviazione standard della resa per bet.

`HistoricalPrediction` conserva cutoff, versioni modello/feature, competizione,
mercato, fonte quote e ipotesi esecutiva. Richiede previsione pre-kickoff, training
e feature disponibili entro la previsione e risultato non precedente al kickoff.
`temporal_split()` separa TRAIN/VALIDATION/TEST/LIVE usando disponibilità del
risultato, vietando eventi duplicati fra split. `evaluate(..., as_of=...)` rifiuta
risultati non ancora disponibili al cutoff. I timestamp sono responsabilità della
provenance upstream: il modulo non può certificare timestamp inventati.

## Freshness e rischio

`freshness(observed_at, as_of, max_age)` rifiuta timestamp naive/futuri; la soglia
esatta è fresca, oltre soglia è stale. La freshness score è 1/0, per non attribuire
un punteggio apparentemente utile a dati già scaduti. `require_as_of()` controlla
retrieval, pubblicazione e osservazione, inclusa la disponibilità effettiva al tempo
della decisione storica.

`risk.assess_stake()` applica fractional Kelly, fattori espliciti di confidence e
reliability, cap per singola puntata e budget residuo per match, portafoglio,
giorno/settimana UTC, squadra, campionato e gruppo correlato. Non compensa perdite
e non applica sconti di esposizione presumendo indipendenza. `RiskLimits` contiene
limiti configurabili; `OpenPosition` conserva importo Decimal e timestamp. Il
drawdown massimo è un veto. Importi vengono sempre arrotondati verso il basso.

L'elenco posizioni deve includere tutte le passività aperte e gli importi necessari
al controllo dei periodi. Le posizioni con `settled_at <= as_of` liberano
l'esposizione del portafoglio, ma continuano a consumare il turnover giornaliero e
settimanale. Una posizione senza settlement è considerata aperta. Home Win e Home
−0.5 sono equivalenti mediante
`equivalent_selection()`; tutte le giocate sullo stesso match consumano comunque
il medesimo cap. `capped_stake()` serve soltanto il caso singolo: non sostituisce
il controllo del portafoglio.

## Verifica

`python -m pytest tests/unit/test_quant_math.py tests/unit/test_quant_models.py -q`
verifica formule, casi limite, rimborsi, coperture, Decimal, freshness, cap,
correlazioni, distribuzioni, seed, ensemble, apprendimento logistico, sospensione
modelli, calibrazione e assenza di leakage temporale.
