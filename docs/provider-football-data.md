# football-data.org fixture adapter

`FootballDataOrgFixtureProvider` maps the official v4 API, using `X-Auth-Token` from `FOOTBALL_DATA_ORG_API_KEY`. It supplies scheduled fixtures and separately typed result records. It does not invent xG, odds, injuries, lineups or training history.

Configure `fixture.primary: football_data_org` in `config/sources.yaml`. Canonical IDs use `football-data:match:<id>`, `football-data:team:<id>` and stable provider competition IDs. Unknown opponents are explicitly skipped; postponed, cancelled and finished matches cannot be returned as eligible pre-match matches.

The date filter uses a UTC day with an exclusive next-day `dateTo`, as documented in the [v4 API overview](https://docs.football-data.org/general/v4/index.html) and [match resource](https://docs.football-data.org/general/v4/match.html). Mapping tests use explicit synthetic native responses and an HTTP mock transport, not live accounts.

The adapter preserves actual retrieval time, provider update time, original JSON hash, status and provenance. Live results cannot be backdated into historical inputs. Cache entries retain their original retrieval timestamps. Timeout, bounded retries, rate limiting and cooldown apply; secrets never enter run manifests or health output.

Additional environment variables: `FOOTBALL_DATA_ORG_BASE_URL`, `FOOTBALL_DATA_ORG_TIMEOUT_SECONDS`, `FOOTBALL_DATA_ORG_MAX_RETRIES`, `FOOTBALL_DATA_ORG_REQUESTS_PER_MINUTE`, `FOOTBALL_DATA_ORG_CACHE_TTL_SECONDS`. Default budget is ten requests per minute. A running, authenticated endpoint has not been validated in this workspace because no account credentials were provided.
