# Sources, provenance and provider boundaries

888 Sport Italia now has a dedicated public catalogue/odds adapter with a separate `config/888` profile. It uses native identifiers, bounded caching and selected-event quote retrieval. It does not provide statistics, official lineups or calibration history. Live verification and limitations: [provider-888.md](provider-888.md).

Agents consume the protocols in `better/providers/base.py`: `FixtureProvider`, `FootballDataProvider`, `OddsProvider`, `NewsProvider` and `LineupProvider`. `ProviderBundle` supplies those interfaces plus the independent validation history, a portfolio snapshot and an optional `SystemTwoProvider`. Source-specific imports do not appear in the specialist agents.

`make_demo_bundle(case, as_of)` is an **explicitly synthetic offline fixture**. Team names, competition, bookmaker, provenance notes, timestamps and raw references identify it as synthetic. Its 300 historical records per market exist only to exercise validation and gates. Supported fixture scenarios are declared in `DEMO_CASES`; they include a valid proposal, drawdown veto, missing data, expired odds, lineup wait, model suspension, source conflict, exposure veto, price wait, unavailable news/odds and watchlist uncertainty. They are not live football data or trained model evidence.

## Real HTTP ingestion

`HTTPFootballProvider` is a real HTTP client for a feed that explicitly implements Bettor's canonical JSON contract. It is **not** advertised as a mapping adapter for a named commercial provider. The source must return these Pydantic payloads:

| GET route | Validated response |
|---|---|
| `/matches?as_of=<ISO timestamp>` | Array of `Match` |
| `/matches/{id}` | `Match` |
| `/matches/{id}/stats?as_of=...` | `StatsSnapshot` |
| `/matches/{id}/odds?as_of=...` | `OddsSnapshot` |
| `/matches/{id}/news?as_of=...` | `NewsSnapshot` |
| `/matches/{id}/lineups?as_of=...` | `LineupSnapshot` |

The constructor accepts a configured base URL, provider identity, optional bearer token, timeout, retry count, rate and TTL. Secrets belong in environment/configuration supplied by the caller and are never put in provenance or error messages. Match IDs are URL encoded. Responses are validated without filling absent observations with invented values. Tests use `httpx.MockTransport`, so the default suite needs no credentials or network.

The dedicated `football_data_org` adapter, when configured, maps its documented native fixture endpoint; see `provider-football-data.md`. A fixture adapter does not by itself supply xG, bookmaker odds, independent historical calibration or a verified portfolio. Live analysis remains conservative when those critical inputs are unavailable.

## Freshness, temporal consistency and resilience

Every critical observation carries provenance with source ID/type, retrieval/publication timestamps, tier, field and raw reference. Source-backed conclusions preserve provenance and `derived_from` links. Stats validate match/team identities and cutoff. Quote selection uses only timestamps and provenance available at `as_of`; complete bookmaker markets must share a snapshot timestamp before proportional de-vigging. News are deduplicated by event and primary-source identity; contradictory events remain explicit. Confirmed lineups require official tier-A provenance and eleven distinct starters per team.

Collection freshness TTLs come from `Settings`: statistics 24 hours, odds 5 minutes, news 30 minutes, probable lineups 5 minutes, official lineups 2 minutes by default. Observation timestamps are never rewritten to make cached data appear fresh. `SnapshotCache` stores payload, actual cache fetch time, expiry and provider separately; canonical feed cache keys include the as-of query. Collection agents still check the underlying source timestamps after cache retrieval.

`ProviderExecutor` applies bounded timeouts, transient-only exponential retry, request pacing and a circuit breaker. HTTP 429 and 5xx are retriable; schema errors and other 4xx responses are not. Authentication failures never generate synthetic fallback data. The root snapshot collector applies the same resilience boundary to provider operations and preserves the collected snapshot for the whole analysis.

`IdentityRegistry` uses explicit aliases and refuses ambiguous remapping. It does not guess that similarly named clubs are identical. `numeric_source_conflict` preserves divergent values and the configured tolerance; it does not silently pick a preferred value.

`MultiSourceProvider(primary, secondary)` evaluates both configured interfaces concurrently. Statistics are compared by canonical team/field and tolerance; odds are compared only for complete, matching bookmaker/market/line snapshots within the configured timestamp tolerance. Material divergence raises `SCHEMA_ERROR` with explicit `SOURCE_CONFLICT` fields and values, which reaches the analysis error envelope. Complete source payloads and the selected action are also retained in `comparison_records` for an audit sink. Within tolerance the primary observation remains unchanged; the wrapper never invents an average observation. Unavailable/missing primary data can use secondary data with its original provenance. Schema errors and source conflicts cannot be bypassed by fallback. News snapshots combine source events for the dedicated agent to deduplicate and retain contradictions. Comparison records held in memory require a persistence sink for durable full-source replay.

## Qualitative reasoning sources

`SystemTwoProvider` handles optional deep qualitative interpretation. `ValidatedSystemTwoProvider` depends on an injected transport and validates an output containing narrative, evidence IDs, limitations and review state. The typed response intentionally has no fields for model probability, stake or final betting decision. Provider text is always data, separated from trusted instructions. Its call records retain model/prompt version, temperature, input hash, output and latency; unknown costs/tokens stay unknown.

`SystemOneProvider` and Rizzo Flow have a separate provider boundary and shadow-only rollout. They are not football data sources, a replacement for model probabilities or permission to bypass deterministic gates. See the System One architecture/configuration documentation for that integration.
