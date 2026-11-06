# Deterministic decision engine

`DecisionService` implements the Professional Football Bettor. It consumes typed upstream evidence, independent validation and the current portfolio. It never counts agent directions or averages agent confidences. Shared evidence ancestry is validated, duplicate IDs must be identical, and only reliable external evidence can support a BET. A resolution must reference available trusted evidence and an as-of timestamp.

The trace records quality, provenance, model validity, market eligibility, numeric consistency, price freshness, value, contrary concerns, risk, timing and confidence. Suspended models, stale critical data, fatal concerns and risk rejections cannot produce BET. Missing critical data produces INSUFFICIENT_DATA. Material uncertain lineups produce WAIT_FOR_LINEUPS; inadequate current price produces WAIT_FOR_BETTER_ODDS. WATCHLIST includes an observable condition. All non-BET decisions have exactly zero stake.

Edge means model probability minus proportionally de-vigged market probability (probability points). EV is probability times decimal odds minus one. They are separate values. Final confidence is the minimum of documented data, reliability, consistency, uncertainty, lineup, liquidity and contrary-severity components. It is an operational conservative indicator, not a calibrated chance of winning. Model probabilities remain unmodified.

V1 execution supports only full 1X2, O/U 2.5 and BTTS markets. Refund/quarter-line markets require settlement-aware pricing and independent validation; available enums and mathematical helpers do not authorize betting on them.

## Pronostico probabilistico indicativo

Ogni analisi espone separatamente `forecast`. Se esistono osservazioni per entrambe le squadre,
Bettor mostra le distribuzioni grezze del modello Poisson anche quando campione o validazione non
permettono di usarle nella decisione. Se sono disponibili quote complete, simultanee e dello stesso
bookmaker, mostra inoltre le probabilità implicite depurate proporzionalmente dal margine.

Queste stime restano esplicitamente `NON AMMESSE ALLA DECISIONE` quando i gate originali non passano.
Non generano edge, EV, Kelly o stake. Le probabilità da quote descrivono il prezzo del mercato e non
costituiscono una previsione indipendente. In assenza sia di osservazioni minime sia di un mercato quote
completo, Bettor non inventa percentuali.
# Partial-output policy

Critical scoped degradations veto BET even from optional agents. Noncritical warnings must state why missing checks are not required or are already reflected in data quality/uncertainty. Explicit confidence caps become individual components of the minimum-based operational confidence. A bare legacy PARTIAL is never automatically usable for a new decision. Required Brier/log loss/calibration metrics must be present; a NORMAL_WEIGHT label alone is insufficient. Details and rationale: [PARTIAL audit](partial-and-observability.md).
