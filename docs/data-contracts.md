# Contracts

`better/domain` is the single contract source. Every result has analysis/agent/prompt versions, separate direction/strength/confidence, a discriminated typed report, errors, evidence, data quality and UTC generation time. External observations carry source, retrieval/publication time, reliability tier, raw reference and field. Evidence preserves `derived_from` links and original sources.

`DataQuality.score` is the minimum of completeness, freshness, source reliability and cross-source consistency: the weakest required component limits quality. It does not count positive agents. All four components remain visible. Unknown data remains null or an explicit missing status.

Decimal money and decimal odds are distinct types. Probabilities must be finite in [0,1], full mutually exclusive markets sum to one, odds exceed 1, and intervals enclose their point estimate. A confirmed lineup requires eleven distinct players per team and official tier-A provenance. Every WATCHLIST carries observable conditions; every non-BET has zero stake.

As-of availability is strict: observations must have been retrieved/published by the historical decision timestamp. Backtest records separate prediction cutoff, prediction time and settlement time. Post-lineup runs link to previous analysis IDs without modifying them.
# Explicit degradations (runtime 0.1.1)

AgentResult retains the master status enum and adds structured `degradations`: missing checks, criticality, affected gates, market scope, deterministic impact, optional confidence cap, reason and policy version. New PARTIAL execution requires an assessment; legacy records remain readable but unclassified partial input is blocked on reevaluation. Decision stores the applicable assessments with agent identity and scope, and enforces caps and critical vetoes. See [policy and audit](partial-and-observability.md).
