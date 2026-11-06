# Agent roles and executable boundaries

`better.agents.registry.create_registry(providers, settings, journal=None)` constructs all fourteen roles. The sports workflow runs thirteen; matched betting has a separate typed input and is disabled by default. Every role is a `BaseAgent` with a versioned dedicated prompt copied from the master specification, declared dependencies, authorized/forbidden tools, timeout, typed result, errors and provenance. The prompts also forbid treating provider text as instructions.

The agents are executable Python components, not fourteen LLM personas. Quantitative operations call deterministic System 0 functions. `SystemTwoProvider` is optional qualitative reasoning for tactical interpretation; it cannot supply probabilities, prices, risk limits or stakes. Tool declarations are programmatically checkable through `require_tool`; dependencies and access to histories/portfolio are enforced at the `run` boundary. Python code itself is trusted; these declarations are not an operating-system sandbox.

| Agent ID | Implemented responsibility | Required inputs and limits |
|---|---|---|
| `football_data_analyst` | Validate match/team identity; preserve source-supplied statistics, segments and sample warnings | Statistics provider; missing opponent/segment coverage reduces completeness; no odds access |
| `football_odds_analyst` | Latest as-of quotes, opening-to-current movement and source timestamps | Odds provider; no value judgement; unknown cause remains unknown |
| `news_intelligence` | Source/event deduplication, verification tiers, explicit conflicts and material unverified news | News provider; copied articles sharing primary source do not create independent evidence |
| `lineup_player_impact` | Distinguish probable/confirmed formation and source-supplied sensitivity | Lineup provider; no invented numerical player delta; confirmed lineup contract requires 11 distinct starters and official provenance |
| `tactical_analyst` | Compare documented opposing traits, preserve limitations; optionally request validated System 2 interpretation | Data, lineup and news; no fitted causal adjustment is claimed |
| `predictive_model` | Deterministic Poisson score distribution, model metadata and uncertainty sensitivity envelope | Data and independent validation; only allowed markets enter operational ensemble |
| `value_bettor` | Implied/no-vig probabilities, fair odds, edge, EV and conservative minimum price | Simultaneous complete bookmaker market, model, eligibility and validation; cannot modify model probability |
| `market_selection` | Filter markets by configured capabilities, sample, validation, coverage and known liquidity | Data, odds, prediction and validator; may report `NO_SUITABLE_MARKET` |
| `matched_betting` | Equal-profit back/lay arithmetic with commission and explicit promotion assumptions | Separate `MatchedBettingInput`; no football evidence or final decision inputs; rollover is not assumed fulfilled |
| `model_validator` | Settled out-of-sample Brier, log loss, calibration, yield, CLV and volatility | Historical records only, filtered by competition, market, model/version and `as_of`; bankroll ROI/drawdown omitted without historical initial bankroll; never current forecast input |
| `risk_manager` | Conservative fractional Kelly and simultaneous exposure/loss/drawdown limits | Value, validation, portfolio; all positions on the match count as correlated exposure |
| `devils_advocate` | Audit preliminary proposal for stale/future data, missing opponent adjustment, small samples, uncertainty and contradictory news | Runs after value; concerns have severity, evidence IDs and observable resolution conditions |
| `professional_bettor` | Invoke deterministic `DecisionService`; return one of the six allowed statuses and gate trace | Specialist reports and portfolio; no provider access and no counting positive agents |
| `betting_journal` | Append immutable pre-match decision through repository | Final decision only; without repository explicitly returns `recorded=false`, never pretends persistence |

The Model Validator runs alongside collection **before** the ensemble, rather than after value as in the illustrative original graph. This necessary ordering prevents suspended models from influencing operational probabilities. Validation history is independent of the current prediction. The Devil's Advocate still runs after a preliminary value proposal and before the final decision.

## Current quantitative scope

Operational prediction and value cover 1X2, over/under 2.5 and BTTS. The Poisson rates average each team's observed attack with its opponent's defense, preferring supplied xG/xGA over goals. This is a documented baseline, not a claimed calibrated production model. The interval uses `1.96 * sqrt(p*(1-p)/team_sample)` as a conservative sensitivity envelope; it is explicitly **not** a fitted or empirically calibrated confidence interval. Value uses the lower envelope to set minimum acceptable odds, and final confidence accounts for its width.

Additional model implementations exist in `better/models`, but they are not silently mixed into the operational ensemble without validated fitted inputs. DNB, Asian handicap and team totals remain unsupported in the operational pipeline until native push/line settlement contracts and market-specific validation are provided. No player/corner/card props are implied.

Validation currently reports competition/market/model/version groups. Historical records preserve season and bookmaker for later segment reports; the agent does not claim a complete odds-bucket/season/bookmaker dashboard. Bankroll ROI and drawdown require historical starting capital, which the current agent context does not supply: those metrics are omitted and explicitly marked unknown, rather than computed using a fabricated bankroll. The deterministic performance service supports them when the caller supplies verified starting capital. Validation thresholds are configurable initial engineering defaults, not empirically certified betting targets. Synthetic fixture history demonstrates mechanics only and is never evidence that a real model is profitable.

## System 2 and failure behavior

`ValidatedSystemTwoProvider` passes trusted role/task/schema separately from untrusted evidence. It validates the complete output and permits only a bounded schema repair. The tactical agent checks every returned evidence ID against actual supplied parent evidence. An optional narrative cannot change probability or stake. Timeout/unavailable provider leaves a partial deterministic tactical report; missing configured provider is an explicit unknown. Invalid structured output or invented citations produces `VALIDATION_ERROR`.

The provider exposes call records containing model, temperature, prompt version, input hash, attempt, raw output, latency and validity, plus structured log metadata. The orchestrator can persist those records; in-memory records alone are not durable storage. Unknown vendor token/cost counts are not fabricated. `FakeSystemTwoProvider` is explicitly synthetic.

The registry supports configured feature flags. The orchestrator handles critical versus optional roles and scopes each agent's context to its declared dependencies. Source failure does not turn into a forecast. Missing critical data prevents `BET`; optional news/tactical failure remains visible and can be promoted to a critical policy through configuration. No real wagers or bookmaker execution is implemented.

## Verification

`tests/contracts/test_agent_contract.py` checks all fourteen identities, dedicated prompts, authorization declarations, context isolation and serialization. `tests/unit/test_agents_pipeline.py` runs the real offline agent graph and verifies valid synthetic BET, vetoes, missing/stale data, future-history exclusion, suspended ensemble filtering, optional role disablement and independent matched arithmetic. Provider tests cover mapping, malformed data, TTL, bounded retries, circuit breaking and untrusted System 2 text.
